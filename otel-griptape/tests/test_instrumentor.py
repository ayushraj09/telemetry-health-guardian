"""Unit tests for GriptapeSemconvObservabilityDriver. No network calls --
uses fake `Observable.Call` objects and an in-memory span exporter, so
these run offline and don't need OPENAI_API_KEY or a live SigNoz endpoint.
"""

from __future__ import annotations

import pytest
from griptape.common import Observable
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from otel_griptape.instrumentor import (
    GriptapeSemconvObservabilityDriver,
    _patch_openai_finish_reason_capture,
    _unpatch_openai_finish_reason_capture,
)
from otel_griptape.semconv import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    PAYLOAD_CAPTURED_BYTES,
    PAYLOAD_RAW_BYTES,
)


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(self, input_tokens: int = 12, output_tokens: int = 34) -> None:
        self.usage = _FakeUsage(input_tokens, output_tokens)
        self.value = "a fake completion"


@pytest.fixture()
def exporter_and_driver():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    driver = GriptapeSemconvObservabilityDriver(tracer_provider=provider)
    return exporter, driver


def _make_call(func, instance, args=(), kwargs=None, tags=None) -> Observable.Call:
    return Observable.Call(
        func=func,
        instance=instance,
        args=args,
        kwargs=kwargs or {},
        decorator_args=(),
        decorator_kwargs={"tags": tags} if tags is not None else {},
    )


class _FakeOpenAiChatPromptDriver:
    """Deliberately named to match griptape's real class so
    gen_ai_system_for_driver's known-driver mapping ("openai") is exercised,
    without needing to construct the real driver (which requires a live
    tokenizer/client setup)."""

    __name__ = "OpenAiChatPromptDriver"

    def __init__(self, model: str) -> None:
        self.model = model


_FakeOpenAiChatPromptDriver.__name__ = "OpenAiChatPromptDriver"
_FakeOpenAiChatPromptDriver.__qualname__ = "OpenAiChatPromptDriver"


def test_prompt_driver_run_produces_gen_ai_span(exporter_and_driver):
    exporter, driver = exporter_and_driver
    fake_driver = _FakeOpenAiChatPromptDriver(model="gpt-4o-mini")
    fake_driver.__class__.__name__ = "OpenAiChatPromptDriver"

    def fn(self):
        return _FakeMessage(input_tokens=100, output_tokens=25)

    call = _make_call(fn, fake_driver, tags=["PromptDriver.run()"])
    result = driver.observe(call)

    assert isinstance(result, _FakeMessage)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "chat gpt-4o-mini"
    attrs = span.attributes
    assert attrs[GEN_AI_OPERATION_NAME] == "chat"
    assert attrs[GEN_AI_SYSTEM] == "openai"
    assert attrs[GEN_AI_REQUEST_MODEL] == "gpt-4o-mini"
    assert attrs[GEN_AI_USAGE_INPUT_TOKENS] == 100
    assert attrs[GEN_AI_USAGE_OUTPUT_TOKENS] == 25
    # No finish_reason patch applied in this test -> falls back to "unknown"
    # rather than silently omitting the required R1 field.
    assert attrs[GEN_AI_RESPONSE_FINISH_REASONS] == ("unknown",)


def test_tool_run_produces_tool_span_and_r6_attributes(exporter_and_driver):
    exporter, driver = exporter_and_driver

    class _FakeAction:
        name = "WebSearchTool"
        path = "search"

    class _FakeTool:
        pass

    def fn(self, activity, subtask, action, value):
        return _FakeResultArtifact("x" * 500)

    class _FakeResultArtifact:
        def __init__(self, value: str) -> None:
            self.value = value

    call = _make_call(
        fn,
        _FakeTool(),
        args=(lambda **_: None, object(), _FakeAction(), {}),
        tags=["Tool.run()"],
    )
    driver.observe(call)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "execute_tool WebSearchTool"
    assert span.attributes[GEN_AI_TOOL_NAME] == "WebSearchTool"
    assert span.attributes[PAYLOAD_RAW_BYTES] == 500
    assert span.attributes[PAYLOAD_CAPTURED_BYTES] == 500


def test_generic_call_produces_named_span(exporter_and_driver):
    exporter, driver = exporter_and_driver

    class _FakeAgent:
        pass

    def try_run(self):
        return self

    instance = _FakeAgent()
    call = _make_call(try_run, instance, tags=None)
    driver.observe(call)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "_FakeAgent.try_run()"


def test_exception_is_recorded_and_reraised(exporter_and_driver):
    exporter, driver = exporter_and_driver

    def fn(self):
        raise ValueError("boom")

    call = _make_call(fn, object(), tags=["PromptDriver.run()"])
    with pytest.raises(ValueError, match="boom"):
        driver.observe(call)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code.name == "ERROR"
    assert len(spans[0].events) == 1  # the recorded exception


def test_content_not_captured_by_default(exporter_and_driver):
    exporter, driver = exporter_and_driver
    fake_driver = _FakeOpenAiChatPromptDriver(model="gpt-4o-mini")
    fake_driver.__class__.__name__ = "OpenAiChatPromptDriver"

    def fn(self, prompt_input):
        return _FakeMessage(input_tokens=10, output_tokens=5)

    call = _make_call(fn, fake_driver, args=("what is 6*7?",), tags=["PromptDriver.run()"])
    driver.observe(call)

    span = exporter.get_finished_spans()[0]
    assert span.events == ()
    assert not any("content" in key or "prompt" in key for key in span.attributes)


def test_capture_content_writes_event_not_indexed_attribute():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    driver = GriptapeSemconvObservabilityDriver(tracer_provider=provider, capture_content=True)

    fake_driver = _FakeOpenAiChatPromptDriver(model="gpt-4o-mini")
    fake_driver.__class__.__name__ = "OpenAiChatPromptDriver"

    def fn(self, prompt_input):
        return _FakeMessage(input_tokens=10, output_tokens=5)

    call = _make_call(fn, fake_driver, args=("what is 6*7?",), tags=["PromptDriver.run()"])
    driver.observe(call)

    span = exporter.get_finished_spans()[0]
    assert len(span.events) == 1
    assert span.events[0].name == "gen_ai.content"
    assert span.events[0].attributes["gen_ai.prompt"] == "what is 6*7?"
    assert span.events[0].attributes["gen_ai.completion"] == "a fake completion"
    # Must never leak into the indexed (queryable) span attribute set --
    # that's exactly what R2 exists to flag.
    assert not any("content" in key or "prompt" in key or "completion" in key for key in span.attributes)


def test_finish_reason_patch_captures_and_restores(exporter_and_driver):
    from griptape.drivers.prompt.openai_chat_prompt_driver import (
        OpenAiChatPromptDriver,
    )

    exporter, driver = exporter_and_driver
    _patch_openai_finish_reason_capture()
    try:
        # Build a fake raw OpenAI completion result shaped like what
        # OpenAiChatPromptDriver._to_message actually receives.
        class _Choice:
            finish_reason = "length"
            message = type("M", (), {"content": "hi", "tool_calls": None, "audio": None})()

        class _Result:
            choices = [_Choice()]
            usage = None

        instance = object.__new__(OpenAiChatPromptDriver)
        message = OpenAiChatPromptDriver._to_message(instance, _Result())

        from otel_griptape.instrumentor import _finish_reason_var

        assert _finish_reason_var.get() == "length"
        assert message is not None
    finally:
        _unpatch_openai_finish_reason_capture()

    # After unpatching, _to_message is back to the original (no capture,
    # but must not raise either).
    assert (
        OpenAiChatPromptDriver._to_message.__name__ != "_to_message_capturing_finish_reason"
    )
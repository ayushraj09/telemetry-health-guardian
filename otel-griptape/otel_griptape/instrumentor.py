"""The core of otel-griptape: a griptape `BaseObservabilityDriver` that
turns griptape's own `@observable`-decorated calls into GenAI-semconv-
compliant OTel spans.

Design note (see project history for the full discussion): griptape 1.11+
ships its own `OpenTelemetryObservabilityDriver`, but it only emits generic
`ClassName.method()` spans with no GenAI semantic-convention attributes, no
per-tool-call spans, and no payload-size tracking. This driver replaces it
with one that adds all of that, while still hooking in through griptape's
own supported extension point (`Observability` + `BaseObservabilityDriver`)
rather than monkey-patching `Agent.run`/`Structure.run`/`PromptDriver.run`
directly -- those are already `@observable`, so there's no need to patch
them ourselves.

One exception, narrowly scoped: `gen_ai.response.finish_reasons` cannot be
recovered from the driver hook at all, because griptape's own `Message`
object discards `finish_reason` before the hook ever sees it (verified by
reading griptape 1.11.0's source directly -- it's not on `Message`, not on
`Message.usage`, and not published via griptape's `FinishPromptEvent`
either). Capturing it requires one small, explicitly-isolated patch at the
one point where griptape converts a raw provider response into a `Message`.
That patch lives in `_finish_reason_capture` below and nowhere else.
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING, Any

from griptape.drivers.observability import BaseObservabilityDriver
from opentelemetry import trace

from otel_griptape.payload_tracking import byte_length, record_payload_sizes
from otel_griptape.semconv import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_SYSTEM,
    GEN_AI_TOOL_NAME,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    OPERATION_CHAT,
    OPERATION_EXECUTE_TOOL,
    gen_ai_system_for_driver,
)

if TYPE_CHECKING:
    from griptape.common import Observable

_logger = logging.getLogger(__name__)

_TRACER_NAME = "otel_griptape"

# Tag strings griptape itself uses on the two calls we give special
# treatment. See base_prompt_driver.py (`@observable(tags=["PromptDriver.run()"])`)
# and tools/base_tool.py (`@observable(tags=["Tool.run()"])`) in griptape's source.
_PROMPT_DRIVER_RUN_TAG = "PromptDriver.run()"
_TOOL_RUN_TAG = "Tool.run()"


class GriptapeSemconvObservabilityDriver(BaseObservabilityDriver):
    """Griptape `BaseObservabilityDriver` implementation -- registered with
    griptape's own `Observability` extension point, per Section 4.2's
    requirements (see module docstring for the design rationale).
    """

    def __init__(
        self,
        tracer_provider: trace.TracerProvider | None = None,
        capture_content: bool = False,
    ) -> None:
        """Args:
        tracer_provider: as elsewhere in this module.
        capture_content: opt-in (default False) for recording raw
            prompt/completion text and raw tool-result content. Per
            Section 4.2, this is cardinality-safe *by construction* even
            when enabled: content is always written as a span *event*
            (`gen_ai.content`, `gen_ai.tool.content`), never as an indexed
            span attribute. Indexed attributes are what R2 (cardinality
            risk) exists to catch, so this library must never write raw
            content that way itself, opt-in or not.
        """
        self._tracer = trace.get_tracer(_TRACER_NAME, tracer_provider=tracer_provider)
        self._capture_content = capture_content

    # -- BaseObservabilityDriver interface -----------------------------------

    def __enter__(self) -> None:
        pass

    def __exit__(self, exc_type: Any, exc_value: Any, exc_traceback: Any) -> bool:
        return False

    def observe(self, call: Observable.Call) -> Any:
        tags = tuple(call.tags or ())

        if tags == (_PROMPT_DRIVER_RUN_TAG,):
            return self._observe_prompt_driver_run(call)
        if tags == (_TOOL_RUN_TAG,):
            return self._observe_tool_run(call)
        return self._observe_generic(call)

    def get_span_id(self) -> str | None:
        span = trace.get_current_span()
        if span is trace.INVALID_SPAN:
            return None
        return trace.format_span_id(span.get_span_context().span_id)

    # -- span builders --------------------------------------------------

    def _observe_prompt_driver_run(self, call: Observable.Call) -> Any:
        driver = call.instance
        model = getattr(driver, "model", "unknown")
        system = gen_ai_system_for_driver(driver)
        span_name = f"{OPERATION_CHAT} {model}"

        with self._tracer.start_as_current_span(
            span_name, record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute(GEN_AI_OPERATION_NAME, OPERATION_CHAT)
            span.set_attribute(GEN_AI_SYSTEM, system)
            span.set_attribute(GEN_AI_REQUEST_MODEL, model)

            # Clear any stale value before the call so a driver we haven't
            # patched (finish-reason capture is currently OpenAI-only, see
            # module docstring) reports "unknown" instead of a leftover
            # value from an earlier, unrelated call on this thread.
            _finish_reason_var.set(None)

            result = self._run(call, span)

            usage = getattr(result, "usage", None)
            if usage is not None:
                span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens or 0)
                span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens or 0)

            finish_reason = _finish_reason_var.get()
            span.set_attribute(GEN_AI_RESPONSE_FINISH_REASONS, [finish_reason or "unknown"])

            if self._capture_content:
                self._record_content_event(
                    span,
                    event_name="gen_ai.content",
                    fields={
                        "gen_ai.prompt": _stringify_prompt_input(call.args[0] if call.args else None),
                        "gen_ai.completion": _stringify(getattr(result, "value", None)),
                    },
                )

            return result

    def _observe_tool_run(self, call: Observable.Call) -> Any:
        # BaseTool.try_run(self, activity, subtask, action, value)
        action = call.args[2] if len(call.args) > 2 else None
        tool_name = getattr(action, "name", None) or type(call.instance).__name__
        activity_name = getattr(action, "path", None)
        span_name = f"{OPERATION_EXECUTE_TOOL} {tool_name}"

        with self._tracer.start_as_current_span(
            span_name, record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute(GEN_AI_OPERATION_NAME, OPERATION_EXECUTE_TOOL)
            span.set_attribute(GEN_AI_TOOL_NAME, tool_name)
            if activity_name:
                span.set_attribute("gen_ai.tool.call.activity", activity_name)

            result = self._run(call, span)

            # Auto R6 tracking: griptape doesn't truncate tool output on its
            # own, so raw == captured here by default. A chaos-injected
            # truncation, or a tool that does its own truncation, should
            # override this by calling payload_tracking.record_payload_sizes
            # again with the real captured size after this span is current.
            raw_bytes = _artifact_byte_length(result)
            record_payload_sizes(raw_bytes, raw_bytes, span=span)

            if self._capture_content:
                self._record_content_event(
                    span,
                    event_name="gen_ai.tool.content",
                    fields={"gen_ai.tool.result": _stringify(getattr(result, "value", result))},
                )

            return result

    def _observe_generic(self, call: Observable.Call) -> Any:
        class_name = f"{type(call.instance).__name__}." if call.instance is not None else ""
        span_name = f"{class_name}{call.func.__name__}()"

        with self._tracer.start_as_current_span(
            span_name, record_exception=False, set_status_on_exception=False
        ) as span:
            if call.tags:
                span.set_attribute("tags", list(call.tags))
            return self._run(call, span)

    @staticmethod
    def _record_content_event(span: trace.Span, event_name: str, fields: dict[str, str]) -> None:
        """Write raw content as a span *event*, never as an indexed span
        attribute -- event attributes aren't part of a span's indexed
        attribute set the way `span.set_attribute(...)` values are, so this
        can't feed R2's cardinality-risk detection the way an indexed
        attribute would. Only called when `capture_content=True`.
        """
        span.add_event(event_name, attributes=fields)

    @staticmethod
    def _run(call: Observable.Call, span: trace.Span) -> Any:
        try:
            # `call()` (== call.func(*call.args, **call.kwargs)) is correct
            # when call.func is already bound to call.instance -- which is
            # what griptape's real `@observable` (wrapt-based) decorator
            # produces. If call.func is an *unbound* plain function (no
            # `__self__`) and an instance is present, we must bind it
            # ourselves by passing instance as the first positional arg.
            if call.instance is not None and not hasattr(call.func, "__self__"):
                result = call.func(call.instance, *call.args, **call.kwargs)
            else:
                result = call()
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise
        else:
            span.set_status(trace.Status(trace.StatusCode.OK))
            return result


def _stringify(value: Any) -> str:
    """Best-effort str() that never raises -- telemetry must never break
    the real call, same rule as `_artifact_byte_length` below."""
    try:
        return "" if value is None else str(value)
    except Exception:  # noqa: BLE001
        return ""


def _stringify_prompt_input(prompt_input: Any) -> str:
    """`PromptDriver.run(prompt_input)` receives a griptape `PromptStack`
    or `BaseArtifact` (see base_prompt_driver.py), not a plain string.
    Prefer PromptStack's own string rendering when available so captured
    content is actually readable instead of a repr.
    """
    if prompt_input is None:
        return ""
    to_string = getattr(prompt_input, "to_string", None)
    if callable(to_string):
        return _stringify(to_string())
    return _stringify(getattr(prompt_input, "value", prompt_input))


def _artifact_byte_length(artifact: Any) -> int:
    try:
        value = getattr(artifact, "value", artifact)
        return byte_length(str(value))
    except Exception:  # noqa: BLE001 -- telemetry must never break the real call
        return 0


# --- The one narrow patch: finish_reason capture -----------------------
#
# See module docstring for why this exists. Scope is intentionally limited
# to OpenAiChatPromptDriver, the only driver this project's demo app uses.
# Extending to another provider means adding one function here that reads
# that provider's raw response and calling it from _patch_finish_reason_capture
# -- never anything to instrumentor.observe()'s core structure above.

_finish_reason_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "otel_griptape_finish_reason", default=None
)

_finish_reason_patch_applied = False
_original_openai_to_message: Any = None


def _patch_openai_finish_reason_capture() -> None:
    global _finish_reason_patch_applied, _original_openai_to_message  # noqa: PLW0603
    if _finish_reason_patch_applied:
        return
    try:
        from griptape.drivers.prompt.openai_chat_prompt_driver import (
            OpenAiChatPromptDriver,
        )
    except ImportError:
        _logger.debug("openai_chat_prompt_driver not available; skipping finish_reason patch")
        return

    _original_openai_to_message = OpenAiChatPromptDriver._to_message

    def _to_message_capturing_finish_reason(self: Any, result: Any) -> Any:
        try:
            choices = getattr(result, "choices", None)
            if choices:
                finish_reason = choices[0].finish_reason
                if finish_reason is not None:
                    _finish_reason_var.set(finish_reason)
        except Exception:  # noqa: BLE001 -- telemetry must never break the real call
            _logger.debug("finish_reason capture failed", exc_info=True)
        return _original_openai_to_message(self, result)

    OpenAiChatPromptDriver._to_message = _to_message_capturing_finish_reason
    _finish_reason_patch_applied = True


def _unpatch_openai_finish_reason_capture() -> None:
    global _finish_reason_patch_applied  # noqa: PLW0603
    if not _finish_reason_patch_applied or _original_openai_to_message is None:
        return
    from griptape.drivers.prompt.openai_chat_prompt_driver import OpenAiChatPromptDriver

    OpenAiChatPromptDriver._to_message = _original_openai_to_message
    _finish_reason_patch_applied = False
"""otel-griptape: GenAI-semconv-compliant OpenTelemetry auto-instrumentation
for Griptape (https://github.com/griptape-ai/griptape).

Usage:

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource

    import otel_griptape

    provider = TracerProvider(resource=Resource.create({"service.name": "my-app"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=...)))

    otel_griptape.instrument(tracer_provider=provider)

    # ... use griptape normally; Agent.run(), Structure.run(), PromptDriver.run(),
    # and BaseTool.try_run() are all now emitting GenAI-semconv spans.

`otel_griptape` installs standalone -- it has no dependency on the Guardian
service and works against any OTel-compatible backend, not just SigNoz.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider

__all__ = [
    "instrument",
    "uninstrument",
    "inject_traceparent_header",
    "record_payload_sizes",
]

_active_driver: Any | None = None


def __getattr__(name: str) -> Any:
    if name == "inject_traceparent_header":
        from otel_griptape.context_propagation import inject_traceparent_header as value
    elif name == "record_payload_sizes":
        from otel_griptape.payload_tracking import record_payload_sizes as value
    elif name == "GriptapeSemconvObservabilityDriver":
        from otel_griptape.instrumentor import GriptapeSemconvObservabilityDriver as value
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = value
    return value


def instrument(tracer_provider: TracerProvider | None = None) -> Any:
    """Install otel-griptape process-wide. Idempotent -- calling this more
    than once just returns the already-installed driver.

    Does three things:
      1. Registers a GriptapeSemconvObservabilityDriver as griptape's global
         Observability driver, so every @observable-decorated griptape call
         (Structure.run, Agent.run, PromptDriver.run, BaseTool.try_run)
         becomes a span.
      2. Patches ThreadPoolExecutor.submit for correct per-task context
         propagation (needed for R3's concurrent-tool-call case).
      3. Applies the one narrow finish_reason-capture patch, scoped to
         OpenAiChatPromptDriver (see instrumentor.py's module docstring).

    Call once, early in your app, after your TracerProvider is configured
    -- or pass tracer_provider directly and this will use it instead of
    whatever the global one is.
    """
    global _active_driver  # noqa: PLW0603

    from otel_griptape.context_propagation import instrument_context_propagation
    from otel_griptape.instrumentor import (
        GriptapeSemconvObservabilityDriver,
        _patch_openai_finish_reason_capture,
    )

    from griptape.observability import Observability

    if _active_driver is not None:
        return _active_driver

    driver = GriptapeSemconvObservabilityDriver(tracer_provider=tracer_provider)
    Observability.set_global_driver(driver)

    instrument_context_propagation()
    _patch_openai_finish_reason_capture()

    _active_driver = driver
    return driver


def uninstrument() -> None:
    """Reverse everything `instrument()` did. Mainly useful for tests."""
    global _active_driver  # noqa: PLW0603

    from otel_griptape.context_propagation import uninstrument_context_propagation
    from otel_griptape.instrumentor import _unpatch_openai_finish_reason_capture

    from griptape.observability import Observability

    Observability.set_global_driver(None)
    uninstrument_context_propagation()
    _unpatch_openai_finish_reason_capture()
    _active_driver = None

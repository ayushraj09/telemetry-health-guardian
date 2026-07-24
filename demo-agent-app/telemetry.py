"""
Telemetry bootstrap for the demo pipeline.

Sets up a bare OpenTelemetry SDK TracerProvider exporting to SigNoz Cloud's
OTLP endpoint. This module itself does NOT emit GenAI-semconv attributes or
handle cross-thread context propagation -- that correctness is otel-griptape's
job (Stage 2). `init_telemetry()` returns the TracerProvider so callers can
pass it straight into `otel_griptape.instrument(tracer_provider=...)`, which
registers the GenAI-semconv-compliant driver as griptape's global
Observability driver and patches ThreadPoolExecutor for correct context
propagation across the concurrent Fact-Check & Cite stage.
"""

import atexit
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from griptape.configs.defaults_config import Defaults
from griptape.configs.drivers.openai_drivers_config import OpenAiDriversConfig
from griptape.drivers.prompt.openai_chat_prompt_driver import OpenAiChatPromptDriver


def _parse_otlp_headers(raw: str) -> dict:
    headers = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        headers[key.strip()] = value.strip()
    return headers


class _NanoOpenAiDriversConfig(OpenAiDriversConfig):
    @property
    def prompt_driver(self) -> OpenAiChatPromptDriver:
        return OpenAiChatPromptDriver(model="gpt-4.1-nano")


def _configure_griptape_defaults() -> None:
    Defaults._drivers_config = _NanoOpenAiDriversConfig()


def init_telemetry(service_name: str | None = None) -> TracerProvider:
    """Set up the global TracerProvider once and return it.

    Safe to call more than once (e.g. from tests) -- only the first call
    actually installs a provider; subsequent calls just return whatever
    provider is already installed.

    Returns the `TracerProvider` itself (not a bound `Tracer`) so the
    caller can pass it directly to `otel_griptape.instrument(tracer_provider=...)`.
    Modules that just want a `Tracer` for their own hand-placed spans
    (e.g. `planner.py`) should call `trace.get_tracer(__name__)` themselves
    once this has run and installed the global provider.
    """
    name = service_name or os.getenv("OTEL_SERVICE_NAME", "document-research-pipeline")

    _configure_griptape_defaults()

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
        if not endpoint:
            raise RuntimeError(
                "OTEL_EXPORTER_OTLP_ENDPOINT is not set — copy .env.example to .env "
                "and fill in your SigNoz Cloud ingestion endpoint first."
            )
        traces_endpoint = endpoint if endpoint.endswith("/v1/traces") else f"{endpoint}/v1/traces"
        headers = _parse_otlp_headers(os.getenv("OTEL_EXPORTER_OTLP_HEADERS", ""))

        resource = Resource.create({"service.name": name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=traces_endpoint, headers=headers)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # BatchSpanProcessor exports asynchronously in the background — without
        # a flush at process exit, a short-lived CLI run can end before its
        # spans are sent, and "no trace in SigNoz" would look like a bug when
        # it's really just an unflushed batch.
        atexit.register(lambda: provider.shutdown())

    return trace.get_tracer_provider()  # type: ignore[return-value]
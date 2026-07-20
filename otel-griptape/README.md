# otel-griptape

GenAI-semantic-convention-compliant OpenTelemetry auto-instrumentation for
[Griptape](https://github.com/griptape-ai/griptape). Griptape has no
maintained OTel instrumentation package as of this writing (no
`openinference-instrumentation-griptape` exists, and griptape's own
built-in `OpenTelemetryObservabilityDriver` emits generic spans with no
`gen_ai.*` attributes) -- this library fills that gap.

Standalone and framework-generic in the way described in the parent
project's build spec, Section 9: point it at any Griptape app and you get
GenAI-semconv spans, correct trace-context propagation across sync and
thread-pool-based concurrency, and W3C `traceparent` forwarding for
outbound HTTP -- no dependency on the Telemetry Health Guardian service.

## Install

```bash
pip install -e .
```

## Usage

```python
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

import otel_griptape

provider = TracerProvider(resource=Resource.create({"service.name": "my-app"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="...")))

otel_griptape.instrument(tracer_provider=provider)

# Use griptape normally from here on.
```

## What it does

- **`Structure.run()` / `Agent.run()` / `PromptDriver.run()`**: already
  `@observable` in griptape 1.11+; this library's driver turns the
  `PromptDriver.run()` call specifically into a span with `gen_ai.system`,
  `gen_ai.operation.name`, `gen_ai.request.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and
  `gen_ai.response.finish_reasons`, named `"<operation> <model>"`.
- **Tool-call dispatch**: `BaseTool.try_run()` is also already
  `@observable`; this library turns it into a span named
  `"execute_tool <tool_name>"` with `gen_ai.tool.name`, and auto-records R6
  payload-size attributes (`payload.raw_bytes` / `payload.captured_bytes`).
- **Correct context propagation**: patches
  `concurrent.futures.ThreadPoolExecutor.submit` to snapshot and re-attach
  OTel context per submitted task (not per worker thread) -- see
  `context_propagation.py` for why the naive
  `opentelemetry-instrumentation-threading` approach is wrong for
  persistent thread pools.
- **W3C `traceparent` forwarding**: `inject_traceparent_header()` for
  outbound calls to another service.
- **Manual payload tracking**: `record_payload_sizes()` for app code that
  does byte-relevant work outside griptape's Tool abstraction (e.g. reading
  a PDF directly rather than through a griptape Tool).

## The one patch

Everything above hooks into griptape's own `Observability` extension
point -- no monkey-patching of `Agent`/`Structure`/`PromptDriver` classes.
One narrow exception: `gen_ai.response.finish_reasons` can't be recovered
from the observability hook at all, because griptape's own `Message`
object discards `finish_reason` before the hook ever sees it. Capturing it
requires a small, explicitly-isolated patch of
`OpenAiChatPromptDriver._to_message` -- see `instrumentor.py`'s docstring
and the `_patch_openai_finish_reason_capture` function for the full
rationale and scope. It only touches that one conversion point; nothing
else in the library is patched.

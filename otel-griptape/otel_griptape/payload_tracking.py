"""Byte-size tracking for R6 (silent truncation detection).

R6 compares `payload.raw_bytes` against `payload.captured_bytes` on
tool-call spans (Section 4.3.1). Two ways that pair of attributes gets set:

1. Automatically, for anything dispatched through a griptape `BaseTool` --
   `instrumentor.py` calls `record_payload_sizes` itself after a tool-call
   span's underlying call() returns, using the returned artifact's size for
   both values (griptape doesn't truncate tool output on its own, so
   raw == captured unless something downstream, e.g. chaos.py, changes
   that).

2. Manually, for app code that does its own byte-relevant work *outside*
   griptape's Tool abstraction -- e.g. this project's `fetch_and_read.py`,
   which extracts PDF text directly via `pypdf` rather than through a
   griptape Tool. That code should call `record_payload_sizes` itself, on
   its own current span, with the true extracted size and the size actually
   placed into context.
"""

from __future__ import annotations

from opentelemetry import trace

from otel_griptape.semconv import PAYLOAD_CAPTURED_BYTES, PAYLOAD_RAW_BYTES


def record_payload_sizes(
    raw_bytes: int,
    captured_bytes: int,
    span: trace.Span | None = None,
) -> None:
    """Record R6's two payload-tracking attributes on a span.

    Args:
        raw_bytes: the true size, in bytes, of the full content produced
            (e.g. the full extracted text of a PDF).
        captured_bytes: the size, in bytes, of what was actually placed
            into the LLM's context / the span. Equal to raw_bytes when
            nothing was truncated.
        span: the span to annotate. Defaults to the current active span.
    """
    target = span if span is not None else trace.get_current_span()
    target.set_attribute(PAYLOAD_RAW_BYTES, raw_bytes)
    target.set_attribute(PAYLOAD_CAPTURED_BYTES, captured_bytes)


def byte_length(value: str | bytes) -> int:
    """Small convenience so callers don't have to remember to .encode()."""
    if isinstance(value, bytes):
        return len(value)
    return len(value.encode("utf-8"))

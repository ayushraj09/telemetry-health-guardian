"""
Stage 2 of the pipeline (Section 4.1): Fetch & Read.

Reads each source PDF and extracts its text. By default this stage places
the FULL extracted text into context — no truncation. otel-griptape
(Stage 2) records the true extracted byte-size alongside the byte-size
actually placed into context, so R6 has something concrete to compare
once Stage 4 builds its truncation trigger.

This module does its own PDF-extraction work outside griptape's `Tool`
abstraction (it calls `pypdf` directly), so per otel_griptape.payload_tracking's
own contract, it must call `record_payload_sizes()` itself rather than
relying on otel-griptape's automatic per-Tool-call tracking. This is what
puts `payload.raw_bytes` / `payload.captured_bytes` on the span — the exact
attribute names Section 4.3.1 says the R6 rule engine queries.

`chaos.tag_r2_raw_content()` is called unconditionally right after
extraction -- it's a no-op unless CHAOS_MODE=1, in which case it may tag
this span with the raw extracted text as an indexed attribute, simulating
R2's canonical failure mode (Section 4.1).
"""

from pathlib import Path

from opentelemetry import trace
from otel_griptape.payload_tracking import byte_length, record_payload_sizes
from pypdf import PdfReader

import chaos

tracer = trace.get_tracer(__name__)


def fetch_and_read_sources(pdf_paths: list[str]) -> dict[str, str]:
    """Return {filename: extracted_text} for every given PDF path."""
    sources: dict[str, str] = {}
    for path in pdf_paths:
        with tracer.start_as_current_span("fetch_and_read.read_pdf") as span:
            filename = Path(path).name
            span.set_attribute("source.path", path)
            span.set_attribute("source.filename", filename)

            text = _extract_pdf_text(path)

            span.set_attribute("source.extracted_chars", len(text))
            chaos.tag_r2_raw_content(text, span=span)

            # R6 contract (Section 4.3.1): raw == captured here because
            # nothing truncates yet. Stage 4's chaos.py extension should
            # override captured_bytes with the real, smaller value it
            # actually places into context, on this same current span.
            raw_bytes = byte_length(text)
            record_payload_sizes(raw_bytes, raw_bytes, span=span)

            sources[filename] = text
    return sources


def _extract_pdf_text(path: str) -> str:
    reader = PdfReader(path)
    pages_text = []
    for page in reader.pages:
        pages_text.append(page.extract_text() or "")
    return "\n".join(pages_text)
"""
Stage 2 of the pipeline (Section 4.1): Fetch & Read.

Reads each source PDF and extracts its text. This stage places the FULL
extracted text into context -- no truncation, ever, regardless of
CHAOS_MODE. R6's truncation mechanism now lives downstream (see chaos.py's
module docstring and ollama_r6.py): the claim-check call chaos.py selects
is routed through a REAL local Ollama model with a small `num_ctx`,
instead of a fixed character slice being applied here. This stage's job
stays exactly what Stage 1/2 already verified it to be -- extract, don't
touch.

otel-griptape (Stage 2) still records the true extracted byte-size
alongside the byte-size actually placed into context on this span
(`payload.raw_bytes` / `payload.captured_bytes`); the two are always
equal here now, since nothing at this stage ever truncates. The real gap
R6 looks for shows up instead on `ollama_r6.py`'s own span, further
downstream in Fact-Check & Cite -- not here.

This module does its own PDF-extraction work outside griptape's `Tool`
abstraction (it calls `pypdf` directly), so per otel_griptape.payload_tracking's
own contract, it must call `record_payload_sizes()` itself rather than
relying on otel-griptape's automatic per-Tool-call tracking. This is what
puts `payload.raw_bytes` / `payload.captured_bytes` on the span -- the
exact attribute names Section 4.3.1 says the R6 rule engine queries.

`chaos.tag_r2_raw_content()` is called unconditionally right after
extraction -- it's a no-op unless CHAOS_MODE=1, in which case it may tag
this span with the raw extracted text as an indexed attribute, simulating
R2's canonical failure mode (Section 4.1). R2 is independent of R6 and
still lives here, unchanged.
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

            # No truncation at this stage, ever -- see module docstring.
            # raw_bytes == captured_bytes here unconditionally; R6's real
            # gap (when chaos.py routes a claim-check through Ollama)
            # shows up downstream on ollama_r6.py's span instead.
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
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

`chaos.maybe_truncate_for_context()` is called the same unconditional way,
right after that -- a no-op unless CHAOS_MODE=1, in which case it may
return a fixed small slice of the extracted text instead of the full
text. Section 4.1's canonical R6 case: `sources[filename]` (what actually
reaches the Fact-Check & Report Writer stages -- the LLM's context) then
holds only that slice, while `record_payload_sizes()` below is given the
TRUE full extracted length for `raw_bytes` regardless, so the gap is
checkable via SigNoz even though nothing here raises an error or looks
broken in the pipeline's own output.
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

            # no-op unless CHAOS_MODE=1 and this read wins R6's dice roll,
            # in which case context_text is a fixed small slice of the
            # true full text -- see module docstring.
            context_text = chaos.maybe_truncate_for_context(text)

            # R6 contract (Section 4.3.1): raw_bytes is always the TRUE
            # full extracted size; captured_bytes is the size of whatever
            # actually gets placed into context (context_text) -- equal to
            # raw_bytes when chaos didn't truncate this read, smaller when
            # it did. This is the exact pair R6's rule engine compares.
            raw_bytes = byte_length(text)
            captured_bytes = byte_length(context_text)
            record_payload_sizes(raw_bytes, captured_bytes, span=span)

            sources[filename] = context_text
    return sources


def _extract_pdf_text(path: str) -> str:
    reader = PdfReader(path)
    pages_text = []
    for page in reader.pages:
        pages_text.append(page.extract_text() or "")
    return "\n".join(pages_text)
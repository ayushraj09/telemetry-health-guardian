"""
Stage 8 rework: manually-instrumented real Ollama call for R6.

`fact_check_and_cite.py`'s normal claim-check path runs through griptape's
`Agent()` (OpenAI, via `Defaults._drivers_config`). When `chaos.py`'s
`maybe_use_ollama_for_claim_check()` fires for a given claim, that one
call is routed here instead -- to a REAL local Ollama model with a small
`num_ctx`, so R6 (Section 4.3.1) gets an actual context-window
truncation to detect, not a simulated one.

This module calls Ollama's raw HTTP API (`POST /api/chat`) directly,
rather than going through griptape's `OllamaPromptDriver`, for two
independent reasons:

1. Token usage. griptape's `OllamaPromptDriver` -- verified directly
   against both the project's pinned baseline (1.4.3) and the current
   latest (1.11.0) source -- never populates `Message.usage` for Ollama
   responses. Routing this call through griptape's `Agent()` would leave
   `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` unset on
   every R6-routed span, tripping R1's missing-field check as a side
   effect of testing R6 -- a fake finding contaminating the demo, not a
   deliberate one. Ollama's own raw HTTP response already reports
   `prompt_eval_count` / `eval_count` (its real input/output token
   counts), so calling the HTTP API directly and reading those fields
   sidesteps the gap entirely -- no estimation needed for usage itself.

2. `payload.captured_bytes` (Section 4.3.1). R6's rule needs the size of
   what actually reached the model. Ollama silently drops whatever
   doesn't fit inside `num_ctx`, but doesn't report that truncation point
   as a byte count directly -- only `prompt_eval_count`, the number of
   prompt TOKENS it actually evaluated. This module converts that back to
   an approximate character count using a fixed chars-per-token ratio
   (`_APPROX_CHARS_PER_TOKEN`). This is a heuristic, not an exact
   tokenizer match -- getting an exact figure would mean aligning with
   whichever tokenizer the configured Ollama model uses internally, which
   this module has no local access to. The approximation is good enough
   for what R6 actually checks: that `captured_bytes` ends up visibly
   smaller than `raw_bytes` whenever `num_ctx` truncation really
   happened.

Bypassing griptape here also means this call is invisible to
otel-griptape's automatic instrumentation, so -- like `fetch_and_read.py`
already does for `payload_tracking` -- this module is responsible for
setting its own `gen_ai.*` span attributes by hand, matching the same
semconv names `otel_griptape.semconv` defines for the driver-instrumented
path, so R1's rule engine sees a well-formed span here too.
"""

from __future__ import annotations

import json
import os
import re

import httpx
from opentelemetry import trace

from otel_griptape.payload_tracking import byte_length, record_payload_sizes
from otel_griptape.semconv import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    OPERATION_CHAT,
)

tracer = trace.get_tracer(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_R6_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_R6_TIMEOUT_SECONDS", "60.0"))

# Deliberately duplicated from fact_check_and_cite.py's CLAIM_CHECK_PROMPT
# rather than imported -- fact_check_and_cite.py imports this module, so
# importing the constant back would create a circular import. Keeping this
# module standalone also matches the "bypasses griptape / the normal path
# entirely" design this whole file is built around. Both backends must be
# asked the exact same question, so if you change one, change both.
CLAIM_CHECK_PROMPT = """You are the fact-checking stage of a document research pipeline.
Given a claim and source text, respond with ONLY a JSON object (no prose, no
markdown fences) of the form:

{{
  "verdict": "supported" | "unsupported" | "unclear"
}}

Claim: {claim}

Source text:
{source_text}
"""

# Rough, English-text heuristic for turning an Ollama-reported prompt
# token count back into an approximate character count -- see module
# docstring, point 2. Not exact, but sufficient for R6's purposes.
_APPROX_CHARS_PER_TOKEN = 4


def run_claim_check_via_ollama(claim: str, source_text: str) -> dict:
    """R6-routed replacement for fact_check_and_cite.py's normal claim-check
    call. Sends the FULL `source_text` -- no manual slicing -- to a real
    local Ollama model with `num_ctx` set to `chaos.ollama_num_ctx()`.
    Ollama's own context window then silently drops whatever doesn't fit;
    see module docstring for how that real gap gets recorded via
    `payload.raw_bytes` / `payload.captured_bytes`.

    Imports `chaos` lazily (inside the function, not at module load) to
    avoid a hard dependency at import time for any test that only wants
    to exercise this module's HTTP/parsing logic directly.
    """
    import chaos

    model = chaos.ollama_model()
    num_ctx = chaos.ollama_num_ctx()
    prompt = CLAIM_CHECK_PROMPT.format(claim=claim, source_text=source_text)

    # Span name follows otel-griptape's own convention exactly (see
    # instrumentor.py: `span_name = f"{OPERATION_CHAT} {model}"`) --
    # R1's naming-conformance check (r1_missing_fields.py) expects every
    # gen_ai.operation.name == "chat" span to be named "{operation}
    # {model}". Naming this span anything else (e.g. a free-form
    # "fact_check_and_cite.check_claim_llm") would make every R6-routed
    # claim a spurious R1 naming_convention finding -- a bug in this
    # module, not a real conformance problem in the pipeline.
    with tracer.start_as_current_span(f"{OPERATION_CHAT} {model}") as span:
        span.set_attribute(GEN_AI_SYSTEM, "ollama")
        span.set_attribute(GEN_AI_OPERATION_NAME, OPERATION_CHAT)
        span.set_attribute(GEN_AI_REQUEST_MODEL, model)
        span.set_attribute("chaos.r6_ollama_routed", True)
        span.set_attribute("chaos.r6_num_ctx", num_ctx)

        raw_bytes = byte_length(source_text)

        try:
            response = httpx.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"num_ctx": num_ctx},
                },
                timeout=OLLAMA_R6_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            # Same "don't fail the whole claim over a secondary backend
            # being unreachable" discipline fact_check_and_cite.py's
            # _fetch_citation already uses. R6's demo depends on this call
            # succeeding, but a real pipeline shouldn't crash if it doesn't.
            span.set_attribute("ollama.error", str(exc))
            record_payload_sizes(raw_bytes, raw_bytes, span=span)
            return {"claim": claim, "verdict": "unclear", "citation": ""}

        input_tokens = data.get("prompt_eval_count")
        output_tokens = data.get("eval_count")
        finish_reason = data.get("done_reason") or "unknown"

        if input_tokens is not None:
            span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
        if output_tokens is not None:
            span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)
        span.set_attribute(GEN_AI_RESPONSE_FINISH_REASONS, [finish_reason])

        # captured_bytes: see module docstring, point 2. prompt_eval_count
        # is the real number of prompt tokens Ollama actually evaluated --
        # if num_ctx truncated the prompt, this is smaller than the full
        # prompt's token count would have been. Converting it back to an
        # approximate char count, capped at raw_bytes, is what makes that
        # real gap visible via payload.raw_bytes vs payload.captured_bytes.
        if input_tokens is not None:
            captured_bytes = min(raw_bytes, input_tokens * _APPROX_CHARS_PER_TOKEN)
        else:
            captured_bytes = raw_bytes
        record_payload_sizes(raw_bytes, captured_bytes, span=span)

        message_content = data.get("message", {}).get("content", "")
        return _parse_claim_json(message_content, claim)


def _parse_claim_json(raw_text: str, claim: str) -> dict:
    """Same parsing contract as fact_check_and_cite.py's own
    `_parse_claim_json` -- duplicated here rather than imported for the
    same circular-import reason CLAIM_CHECK_PROMPT is duplicated above.
    """
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    candidate = match.group(0) if match else raw_text
    try:
        parsed = json.loads(candidate)
        verdict = parsed.get("verdict", "unclear")
        citation = parsed.get("citation", "")
        if verdict in ("supported", "unsupported", "unclear"):
            return {"claim": claim, "verdict": verdict, "citation": citation}
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"claim": claim, "verdict": "unclear", "citation": ""}
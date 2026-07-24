"""citation_service.py -- separate HTTP service for the Fact-Check & Cite
stage's citation step (Section 4.1, Stage 8 / R7).

This is deliberately a SEPARATE running process from demo-agent-app, with
its own OTel `service.name` ("citation-service" by default) -- not an
in-process function call. `fact_check_and_cite.py`'s `_fetch_citation`
calls this over real HTTP for every claim. That real service boundary is
what gives R7 (Section 4.3.1) something to check: whether the W3C
`traceparent` header survives the hop from demo-agent-app into this
service, so a claim's fact-check trace and its citation-lookup trace stay
one connected trace instead of silently becoming two.

How incoming trace context is handled (the "do it right" baseline
otel-griptape's `context_propagation.inject_traceparent_header` exists to
feed into): `_trace_context_middleware` below calls
`opentelemetry.propagate.extract` on the incoming request's headers and
`attach()`s whatever it finds *before* starting this service's own span.
  - If the caller sent a `traceparent` header (the correct, un-chaosed
    case): `extract` returns a context carrying the caller's real
    trace_id/span_id, so the span started inside this request is a
    properly parented CHILD span in the SAME trace -- one connected
    trace across the service boundary, exactly what R7's detection logic
    looks for.
  - If the caller sent NO `traceparent` header (chaos.py's R7 trigger
    fired on the demo-agent-app side): `extract` finds nothing to
    restore, so the span started here has no parent context at all and
    becomes a brand-new ROOT span in a brand-new trace -- R7's exact
    "one trace silently becomes two disconnected ones" failure mode,
    produced faithfully rather than simulated.

Run standalone (own port, separate from demo-agent-app and guardian):

    uvicorn citation_service:app --port 8100

Needs the same .env as demo-agent-app (OTEL_EXPORTER_OTLP_ENDPOINT /
OTEL_EXPORTER_OTLP_HEADERS) plus whatever LLM credentials
`_NanoOpenAiDriversConfig` in telemetry.py already requires -- this
service reuses that same Griptape default config for its own citation
lookups, it does not stand up a separate LLM configuration.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

load_dotenv()

from opentelemetry import context as otel_context  # noqa: E402
from opentelemetry import propagate, trace  # noqa: E402

from telemetry import init_telemetry  # noqa: E402  (must run after load_dotenv)

_SERVICE_NAME = "citation-service"
_provider = init_telemetry(service_name=_SERVICE_NAME)

tracer = trace.get_tracer(__name__)
logger = logging.getLogger(_SERVICE_NAME)

from fastapi import FastAPI, Request  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from griptape.structures import Agent  # noqa: E402

app = FastAPI(title="Citation Service")

CITATION_PROMPT = """You verify citations for a document research pipeline.
Given a claim, its fact-check verdict, and the source text it was checked
against, return ONLY a JSON object (no prose, no markdown fences) of the
form:

{{
  "citation": "<a short quote or close paraphrase from the source text that
   best supports or explains the verdict, or empty string if the verdict is
   'unclear' or nothing in the source text is relevant>"
}}

Claim: {claim}
Verdict: {verdict}

Source text:
{source_text}
"""


class CitationRequest(BaseModel):
    claim: str
    source_text: str
    verdict: str = "unclear"


class CitationResponse(BaseModel):
    citation: str


@app.middleware("http")
async def _trace_context_middleware(request: Request, call_next):
    """Extract whatever trace context the caller sent (or didn't) BEFORE
    this request's own span starts -- see module docstring for exactly
    why this is the mechanism R7 depends on being correct here."""
    ctx = propagate.extract(dict(request.headers))
    token = otel_context.attach(ctx)
    try:
        return await call_next(request)
    finally:
        otel_context.detach(token)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": _SERVICE_NAME}


@app.post("/verify_citation", response_model=CitationResponse)
async def verify_citation(body: CitationRequest) -> CitationResponse:
    with tracer.start_as_current_span("citation_service.verify_citation") as span:
        span.set_attribute("claim.text", body.claim[:200])
        span.set_attribute("claim.verdict", body.verdict)
        citation = _run_citation_lookup(body.claim, body.source_text, body.verdict)
        span.set_attribute("citation.found", bool(citation))
        return CitationResponse(citation=citation)


def _run_citation_lookup(claim: str, source_text: str, verdict: str) -> str:
    if verdict == "unclear":
        return ""
    agent = Agent()
    prompt = CITATION_PROMPT.format(claim=claim, verdict=verdict, source_text=source_text)
    result = agent.run(prompt)
    raw_text = _extract_text(result)
    return _parse_citation_json(raw_text)


def _extract_text(agent_result) -> str:
    output = getattr(agent_result, "output", agent_result)
    value = getattr(output, "value", output)
    return str(value)


def _parse_citation_json(raw_text: str) -> str:
    import json
    import re

    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    candidate = match.group(0) if match else raw_text
    try:
        parsed = json.loads(candidate)
        return str(parsed.get("citation", ""))
    except (json.JSONDecodeError, AttributeError):
        return ""


if __name__ == "__main__":
    # Convenience alternative to `uvicorn citation_service:app --port 8100`
    # (still the documented/preferred invocation in the module docstring) --
    # `python citation_service.py` works too, reading the same
    # CITATION_SERVICE_PORT env var fact_check_and_cite.py's
    # CITATION_SERVICE_URL is expected to match (see env.example).
    import os

    import uvicorn

    port = int(os.getenv("CITATION_SERVICE_PORT", "8100"))
    uvicorn.run(app, host="0.0.0.0", port=port)
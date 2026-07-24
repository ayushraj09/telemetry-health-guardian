"""
Stage 3 of the pipeline (Section 4.1): Fact-Check & Cite.

Checks each claim from the Planner's plan against the fetched source text
and attaches a citation. Claims are checked as PARALLEL calls, not one at a
time — this is a real design choice for this domain (checking 5 claims
sequentially would be needlessly slow), and it's also what gives R3 a
natural trigger: Griptape's agent.run() is a blocking/synchronous call, so
running claims concurrently means running them across a thread pool, which
is exactly the scenario where OTel span-context propagation can silently
break if not done correctly.

Stage 1 (here) does not yet manually fix or break context propagation
across those threads — that correctness is otel-griptape's job in Stage 2.
Stage 4's chaos.py explicitly severs it on top of the now-correct baseline.

Stage 8 addition (R7): the verdict check itself stays in-process (unchanged
from Stage 1/4 — this is not what R7 is about), but the citation for each
claim is now fetched from `citation_service.py`, a SEPARATE HTTP service,
over a real outbound request. That real service-to-service hop is what
gives R7 (Section 4.3.1) something to check: whether the W3C `traceparent`
header survives the call, keeping one claim's fact-check trace and its
citation-lookup trace as a single connected trace instead of silently
becoming two. `chaos.py`'s R7 trigger (`maybe_drop_traceparent_for_citation_call`)
decides, per call, whether that header actually gets sent.
"""

import asyncio
import json
import os
import re

import httpx
from griptape.structures import Agent
from opentelemetry import context as otel_context
from opentelemetry import trace

import chaos
from otel_griptape.context_propagation import inject_traceparent_header

tracer = trace.get_tracer(__name__)

CITATION_SERVICE_URL = os.getenv("CITATION_SERVICE_URL", "http://localhost:8100")
CITATION_SERVICE_TIMEOUT_SECONDS = float(os.getenv("CITATION_SERVICE_TIMEOUT_SECONDS", "15.0"))

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


async def fact_check_claims(claims: list[str], sources: dict[str, str]) -> list[dict]:
    """Check every claim concurrently against the combined source text."""
    with tracer.start_as_current_span("fact_check_and_cite.run") as parent_span:
        parent_span.set_attribute("fact_check.claim_count", len(claims))
        parent_span.set_attribute("fact_check.source_count", len(sources))

        combined_source_text = "\n\n---\n\n".join(sources.values())
        tasks = [_check_one_claim(claim, combined_source_text) for claim in claims]
        results = await asyncio.gather(*tasks)
        return list(results)


async def _check_one_claim(claim: str, source_text: str) -> dict:
    with tracer.start_as_current_span("fact_check_and_cite.check_claim") as span:
        span.set_attribute("claim.text", claim[:200])

        loop = asyncio.get_running_loop()

        # chaos.py's R3 trigger: no-op unless CHAOS_MODE=1 and this call
        # wins the dice roll. When it fires, returns a context with a
        # fabricated, non-existent parent span_id (same trace_id) to
        # attach in place of the real ambient one for this one submission
        # -- otel-griptape's context_propagation.py patch (Stage 2) will
        # faithfully snapshot and propagate WHATEVER context is current at
        # submission time, correct or corrupted; it has no way to tell the
        # difference, which is exactly the point.
        chaos_context = chaos.maybe_break_context_for_claim_check()
        token = otel_context.attach(chaos_context) if chaos_context is not None else None
        try:
            # run_in_executor hands this off to a worker thread so multiple
            # claims can be checked in parallel without blocking the event loop.
            result = await loop.run_in_executor(None, _run_claim_check_llm, claim, source_text)
        finally:
            if token is not None:
                otel_context.detach(token)

        span.set_attribute("claim.verdict", result["verdict"])

        # Stage 8 (R7): the citation itself now comes from a real outbound
        # HTTP call to citation_service.py, a separate service -- see this
        # module's docstring for why. This call happens on the SAME ambient
        # context this span establishes (not inside the chaos-corrupted R3
        # window above, and not inside the thread-pool executor), so a
        # normal run propagates a clean, correctly-parented trace context
        # into it; only chaos.py's independent R7 trigger below decides
        # whether the traceparent header actually goes out.
        result["citation"] = await _fetch_citation(claim, source_text, result["verdict"])
        return result


async def _fetch_citation(claim: str, source_text: str, verdict: str) -> str:
    """Outbound HTTP call to citation_service.py's `/verify_citation`
    endpoint -- the real service-to-service hop R7 exists to check.
    Never raises: a citation-service failure (down, timeout, bad
    response) degrades to an empty citation rather than failing the
    whole claim check, same "don't fail the pipeline over a secondary
    signal" discipline `scheduler.py`'s narrative-generation step uses.
    """
    with tracer.start_as_current_span("fact_check_and_cite.fetch_citation") as span:
        span.set_attribute("claim.text", claim[:200])
        span.set_attribute("peer.service", "citation-service")
        span.set_attribute("http.url", f"{CITATION_SERVICE_URL}/verify_citation")

        headers = {"Content-Type": "application/json"}
        if chaos.maybe_drop_traceparent_for_citation_call():
            # R7's chaos trigger fired: deliberately send this request with
            # NO traceparent header. citation_service.py's own middleware
            # then has nothing to extract and starts a brand-new, disconnected
            # trace -- exactly the failure R7's detection logic looks for.
            span.set_attribute("chaos.r7_traceparent_dropped", True)
        else:
            headers = inject_traceparent_header(headers)

        payload = {"claim": claim, "source_text": source_text, "verdict": verdict}
        try:
            async with httpx.AsyncClient(timeout=CITATION_SERVICE_TIMEOUT_SECONDS) as http_client:
                response = await http_client.post(
                    f"{CITATION_SERVICE_URL}/verify_citation", json=payload, headers=headers
                )
                response.raise_for_status()
                return str(response.json().get("citation", ""))
        except httpx.HTTPError as exc:
            span.set_attribute("citation_service.error", str(exc))
            return ""


def _run_claim_check_llm(claim: str, source_text: str) -> dict:
    # NOTE: the full combined source text is passed here deliberately, with
    # no arbitrary character slice. An earlier version hard-truncated this
    # to source_text[:8000], which silently dropped most of a long PDF's
    # content on every run -- not just under chaos.py -- and did so at a
    # point otel-griptape's R6 tracking (payload.raw_bytes/captured_bytes,
    # set in fetch_and_read.py) can't see. That undermined the "healthy by
    # construction until chaos.py fires" baseline this pipeline depends on.
    # R6's truncation should be the only deliberate truncation point in the
    # system, so this stage passes through everything fetch_and_read.py
    # extracted.
    agent = Agent()
    prompt = CLAIM_CHECK_PROMPT.format(claim=claim, source_text=source_text)
    result = agent.run(prompt)
    raw_text = _extract_text(result)
    return _parse_claim_json(raw_text, claim)


def _extract_text(agent_result) -> str:
    output = getattr(agent_result, "output", agent_result)
    value = getattr(output, "value", output)
    return str(value)


def _parse_claim_json(raw_text: str, claim: str) -> dict:
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
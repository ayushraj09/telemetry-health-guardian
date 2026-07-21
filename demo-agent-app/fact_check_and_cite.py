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
"""

import asyncio
import json
import re

from griptape.structures import Agent
from opentelemetry import context as otel_context
from opentelemetry import trace

import chaos

tracer = trace.get_tracer(__name__)

CLAIM_CHECK_PROMPT = """You are the fact-checking stage of a document research pipeline.
Given a claim and source text, respond with ONLY a JSON object (no prose, no
markdown fences) of the form:

{{
  "verdict": "supported" | "unsupported" | "unclear",
  "citation": "<short quote or paraphrase from the source text that justifies the verdict, or empty string if unclear>"
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
        return result


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
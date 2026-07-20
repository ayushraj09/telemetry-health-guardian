"""
Stage 1 of the pipeline (Section 4.1): Planner.

Takes a research question and the list of available source documents, and
produces a short plan: which sources to actually use, and which factual
claims from those sources need to be checked in the Fact-Check & Cite stage.
"""

import json
import re
from pathlib import Path

from griptape.structures import Agent
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

PLANNER_SYSTEM_PROMPT = """You are the planning stage of a document research pipeline.
Given a research question and a list of available source document filenames,
respond with ONLY a JSON object (no prose, no markdown fences) of the form:

{
  "sources_to_use": ["<filename>", ...],
  "claims_to_check": ["<a specific factual claim to verify against the sources>", ...]
}

Produce 3 to 6 claims_to_check. Each claim must be a specific, checkable
factual statement relevant to the research question — not the question
itself and not a vague topic label.
"""


def make_plan(question: str, available_sources: list[str]) -> dict:
    """Run the Planner stage and return {"sources_to_use": [...], "claims_to_check": [...]}."""
    with tracer.start_as_current_span("planner.run") as span:
        span.set_attribute("planner.question", question)
        span.set_attribute("planner.available_sources", ",".join(
            Path(p).name for p in available_sources
        ))

        agent = Agent()
        user_prompt = (
            f"Research question: {question}\n\n"
            f"Available source documents: {', '.join(Path(p).name for p in available_sources)}\n\n"
            f"{PLANNER_SYSTEM_PROMPT}"
        )
        result = agent.run(user_prompt)
        raw_text = _extract_text(result)

        plan = _parse_plan_json(raw_text, fallback_sources=available_sources)

        span.set_attribute("planner.claims_count", len(plan["claims_to_check"]))
        span.set_attribute("planner.sources_selected", len(plan["sources_to_use"]))
        return plan


def _extract_text(agent_result) -> str:
    output = getattr(agent_result, "output", agent_result)
    value = getattr(output, "value", output)
    return str(value)


def _parse_plan_json(raw_text: str, fallback_sources: list[str]) -> dict:
    """Best-effort JSON extraction — models sometimes wrap JSON in prose/fences."""
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    candidate = match.group(0) if match else raw_text

    try:
        parsed = json.loads(candidate)
        sources = parsed.get("sources_to_use") or [Path(p).name for p in fallback_sources]
        claims = parsed.get("claims_to_check") or []
        if isinstance(sources, list) and isinstance(claims, list) and claims:
            return {"sources_to_use": sources, "claims_to_check": claims}
    except (json.JSONDecodeError, AttributeError):
        pass

    # Fallback keeps the pipeline running even if the model didn't return
    # clean JSON — this is a demo app, not a production planner.
    return {
        "sources_to_use": [Path(p).name for p in fallback_sources],
        "claims_to_check": [
            f"A claim relevant to: {raw_text.strip()[:200]}" if raw_text.strip() else
            "No specific claim could be extracted from the plan output."
        ],
    }

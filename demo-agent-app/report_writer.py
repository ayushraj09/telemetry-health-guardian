"""
Stage 4 of the pipeline (Section 4.1): Report Writer.

Synthesizes the plan, extracted source text, and fact-checked/cited claims
into a final written report. This is an ordinary single LLM call — no
special telemetry concerns here (that's exactly R1's point: every stage,
including this one, is an ordinary gen_ai call and should conform).
"""

from griptape.structures import Agent
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

REPORT_PROMPT = """You are the report-writing stage of a document research pipeline.
Write a clear, well-organized report answering the research question below,
using the fact-checked claims as your evidence. For each claim, note whether
it was supported, unsupported, or unclear, and include its citation where
available. Do not invent claims beyond what's provided.

Research question: {question}

Fact-checked claims:
{claims_block}
"""


def write_report(question: str, plan: dict, sources: dict[str, str], checked_claims: list[dict]) -> dict:
    with tracer.start_as_current_span("report_writer.run") as span:
        span.set_attribute("report.question", question)
        span.set_attribute("report.source_count", len(sources))
        span.set_attribute("report.claim_count", len(checked_claims))

        claims_block = "\n".join(
            f"- [{c['verdict']}] {c['claim']} (citation: {c['citation'] or 'none'})"
            for c in checked_claims
        )
        agent = Agent()
        prompt = REPORT_PROMPT.format(question=question, claims_block=claims_block)
        result = agent.run(prompt)
        report_text = _extract_text(result)

        span.set_attribute("report.length_chars", len(report_text))

        return {
            "question": question,
            "sources_used": plan.get("sources_to_use", list(sources.keys())),
            "claims": checked_claims,
            "report": report_text,
        }


def _extract_text(agent_result) -> str:
    output = getattr(agent_result, "output", agent_result)
    value = getattr(output, "value", output)
    return str(value)

"""
Orchestrates the four-stage document research pipeline (Section 4.1):
Planner -> Fetch & Read -> Fact-Check & Cite -> Report Writer.

Usage:
    python app.py --question "What does the report say about X?" --pdf fixtures/long_climate_report.pdf

Requires .env (copied from .env.example at repo root) with at least
OPENAI_API_KEY, OTEL_EXPORTER_OTLP_ENDPOINT, and OTEL_EXPORTER_OTLP_HEADERS
set.
"""

import argparse
import asyncio
import json
import sys

from dotenv import load_dotenv

load_dotenv()

from opentelemetry import trace  # noqa: E402

import otel_griptape  # noqa: E402
from telemetry import init_telemetry  # noqa: E402  (must run after load_dotenv)

_provider = init_telemetry()
otel_griptape.instrument(tracer_provider=_provider)

tracer = trace.get_tracer(__name__)

from fact_check_and_cite import fact_check_claims  # noqa: E402
from fetch_and_read import fetch_and_read_sources  # noqa: E402
from planner import make_plan  # noqa: E402
from report_writer import write_report  # noqa: E402


def run_pipeline(question: str, pdf_paths: list[str]) -> dict:
    with tracer.start_as_current_span("research_pipeline.run") as root_span:
        root_span.set_attribute("pipeline.question", question)
        root_span.set_attribute("pipeline.source_count", len(pdf_paths))

        plan = make_plan(question, pdf_paths)
        sources = fetch_and_read_sources(pdf_paths)
        checked_claims = asyncio.run(
            fact_check_claims(plan["claims_to_check"], sources)
        )
        report = write_report(question, plan, sources, checked_claims)

        root_span.set_attribute("pipeline.final_claim_count", len(checked_claims))
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Document research pipeline demo (Griptape)")
    parser.add_argument("--question", required=True, help="the research question to answer")
    parser.add_argument(
        "--pdf",
        action="append",
        dest="pdfs",
        required=True,
        help="path to a source PDF; repeat --pdf for multiple sources",
    )
    args = parser.parse_args()

    report = run_pipeline(args.question, args.pdfs)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
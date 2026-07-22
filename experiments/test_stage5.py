"""Stage 5 gate check, per the build spec's Section 6 criterion:
"does the same rule-engine output produce a content-equivalent, coherent
natural-language report under both LLM_PROVIDER=openai and
LLM_PROVIDER=ollama?"

Copy this file to the repo root first (same as test_stage3.py / test_stage4.py),
then run it against a live SigNoz window that already has some findings in
it -- easiest is to reuse a chaos run from Stage 3/4's gate checks, or
trigger a fresh one:

    CHAOS_MODE=1 CHAOS_R1_RATE=0 CHAOS_R2_RATE=0 CHAOS_R3_RATE=1.0 CHAOS_R6_RATE=1.0 CHAOS_SEED=42 \\
        python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf

Requires, in .env or the environment:
    OPENAI_API_KEY / OPENAI_MODEL          -- for the OpenAI half
    OLLAMA_BASE_URL / OLLAMA_MODEL         -- for the Ollama half (local
                                               `ollama serve` + the model
                                               pulled, e.g. `ollama pull llama3.1`)

This script fetches the SAME rule-engine output once (one MCP round-trip),
then calls `narrative.generate_report` twice against that single
`AuditFindings` object -- once per provider -- so any difference between
the two reports is attributable to the LLM, not to querying SigNoz twice
and getting a different window of data each time.

Note on Ollama + R6 (Section 4.3.3's own note): Ollama's default `num_ctx`
(2048 tokens unless raised) can itself silently truncate a large findings
JSON payload before the model ever reasons over it -- if the Ollama half of
this script's output looks suspiciously short or omits findings the OpenAI
half caught, that's the live version of exactly the bug this project
detects, not a script bug. Worth noting in the writeup if it happens.
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from guardian import narrative
from guardian.mcp_client import SignozMCPClient
from guardian.rules import r1_missing_fields, r2_cardinality, r3_orphaned_spans, r6_silent_truncation
from guardian.rules.types import AuditWindow


async def main() -> None:
    async with SignozMCPClient() as client:
        window = AuditWindow(time_range="30m")

        r1, r2, r3, r6 = await asyncio.gather(
            r1_missing_fields.run(client, window),
            r2_cardinality.run(client, window),
            r3_orphaned_spans.run(client, window),
            r6_silent_truncation.run(client, window),
        )

    findings = narrative.combine_results(service=None, r1=r1, r2=r2, r3=r3, r6=r6)

    print("=" * 70)
    print("Combined findings JSON (this is what BOTH providers below see):")
    print("=" * 70)
    import json

    print(json.dumps(findings.to_json_dict(), indent=2, default=str))

    for provider in ("openai", "ollama"):
        print("\n" + "=" * 70)
        print(f"Report from provider={provider!r}:")
        print("=" * 70)
        try:
            report = narrative.generate_report(findings, provider=provider)
        except Exception as exc:  # noqa: BLE001 -- this is a manual diagnostic script
            print(f"  FAILED: {exc}")
            continue
        print(report)

        missing = narrative.validate_citations(report, findings)
        if missing:
            print(f"\n  [validate_citations] WARNING: rules that fired but weren't named: {missing}")
        else:
            print("\n  [validate_citations] every rule that fired was named in the report.")

    print("\n" + "=" * 70)
    print("Manual check for the Stage 5 gate: do the two reports above cover")
    print("the SAME findings (same rule IDs, same spans/attributes named),")
    print("even if the wording differs? That's 'content-equivalent' per the")
    print("gate criterion -- word-for-word identical text is NOT required.")
    print("=" * 70)

    if not os.getenv("OPENAI_API_KEY") and not os.getenv("OLLAMA_BASE_URL"):
        print("\nNote: neither OPENAI_API_KEY nor OLLAMA_BASE_URL looked configured --")
        print("this run likely only exercised the error path above for one or both providers.")


asyncio.run(main())

"""
Stage 4 gate check, per the build spec's Section 6 criterion:
"are a chaos-triggered truncation case and a chaos-triggered orphaned-span
case both correctly detected AND reported as distinct findings, not
conflated into one generic 'error'?"

Run baseline (chaos off) and chaos (chaos on) app.py invocations first,
same pattern as test_stage3.py, then run this script (copy it to the repo
root first, same as test_stage3.py):

    # baseline
    python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf > baseline_out.json

    # chaos: force R3 + R6 to fire (R1/R2 rates left at 0 for a clean read of R3/R6 specifically)
    CHAOS_MODE=1 CHAOS_R1_RATE=0 CHAOS_R2_RATE=0 CHAOS_R3_RATE=1.0 CHAOS_R6_RATE=1.0 CHAOS_SEED=42 \
        python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf > chaos_out.json

    python test_stage4.py

Use a narrow time_range (e.g. "15m") right after triggering chaos, same
reasoning as test_stage3.py's R2 note -- R3/R6 aren't windowed-denominator
sensitive the way R2 is, but a narrow window keeps the printed findings
scoped to the run you just triggered rather than every historical run.
"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from guardian.mcp_client import SignozMCPClient
from guardian.rules import r3_orphaned_spans, r6_silent_truncation
from guardian.rules.types import AuditWindow


async def main():
    async with SignozMCPClient() as client:
        window = AuditWindow(time_range="15m")

        r3 = await r3_orphaned_spans.run(client, window)
        print("R3:", r3)
        print(f"  -> {r3.orphaned_spans}/{r3.total_spans_with_parent} spans orphaned, rate={r3.orphaned_span_rate_pct:.1f}%")
        for f in r3.findings:
            print(f"     [{f.rule}] {f.span_id} in trace {f.trace_id[:8]}...: {f.detail}")

        r6 = await r6_silent_truncation.run(client, window)
        print("\nR6:", r6)
        print(f"  -> {r6.truncated_payload_spans}/{r6.total_payload_spans} payload spans truncated, rate={r6.truncation_rate_pct:.1f}%")
        for f in r6.findings:
            print(f"     [{f.rule}/{f.kind}] {f.span_id or '(no span id)'} in trace {f.trace_id[:8]}...: {f.detail}")

        # Gate check: the two failure modes must show up as distinct kinds,
        # never a single generic finding covering both.
        kinds_seen = {f.kind for f in r6.findings}
        print(f"\nR6 finding kinds seen: {kinds_seen or '(none)'}")
        print("R3 and R6 findings above must be readable as two separate, rule-attributed causes -- not one merged 'error'.")


asyncio.run(main())

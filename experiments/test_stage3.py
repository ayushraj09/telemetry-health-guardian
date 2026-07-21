"""
Final Stage 3 gate check, per the build spec's Section 6 criterion:
"does R1/R2 against healthy baseline data return a high score, and does
manually breaking one field via chaos.py visibly drop it?"

Run baseline (chaos off) and chaos (chaos on) app.py invocations, then this
script, same as before -- no narrow-window workaround needed anymore.
"""

import asyncio

from dotenv import load_dotenv

load_dotenv()

from guardian.mcp_client import SignozMCPClient
from guardian.rules.types import AuditWindow
from guardian.rules import r1_missing_fields, r2_cardinality


async def main():
    async with SignozMCPClient() as client:
        window = AuditWindow(time_range="15m")

        r1 = await r1_missing_fields.run(client, window)
        print("R1:", r1)
        print(f"  -> {r1.non_conformant_spans}/{r1.total_gen_ai_spans} spans non-conformant, score={r1.score:.2f}")
        for f in r1.findings:
            print(f"     [{f.kind}] {f.span_id} ({f.trace_id[:8]}...): {f.detail}")

        r2 = await r2_cardinality.run(client, window, key_search_text="document")
        print("\nR2:", r2)
        for f in r2.findings:
            print(f"     {f.detail}")


asyncio.run(main())
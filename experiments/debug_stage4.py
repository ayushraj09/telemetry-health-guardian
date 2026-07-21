"""
Stage 4 debugging aid -- NOT part of the rule engine itself.

R3 is now reading real trace data (110 spans-with-parent) but found 0
orphans on a run where CHAOS_R3_RATE=1.0 should have broken at least one
claim-check span's parent link. R6 found 0 payload spans at all, meaning
`r6_silent_truncation.fetch_payload_spans`'s `payload.raw_bytes EXISTS`
filter isn't matching anything in this SigNoz instance.

This script dumps the RAW, unparsed responses for the specific calls each
rule's fetch side makes, so we can see the actual shape/field-naming this
live server uses and fix the parsing to match -- same debugging loop R1/R2
needed (see their own module docstrings for the shape corrections that
came out of it).

Run from the project root, promptly after a chaos run (within the audit
window), same as test_stage4.py:

    python -m experiments.debug_stage4
"""

import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from guardian.mcp_client import SignozMCPClient
from guardian.rules.types import AuditWindow


def _dump(label: str, obj) -> None:
    print(f"\n=== {label} ===")
    try:
        print(json.dumps(obj, indent=2, default=str)[:4000])
    except TypeError:
        print(repr(obj)[:4000])


async def main():
    window = AuditWindow(time_range="15m")
    start_ms, end_ms = window.as_absolute_ms_range()

    async with SignozMCPClient() as client:
        # --- R6: does ANY span in the window carry payload.raw_bytes at all? ---
        keys_raw = await client.get_field_keys(signal="traces", search_text="payload")
        _dump("get_field_keys(signal='traces', search_text='payload')", keys_raw)

        search_raw = await client.search_traces(
            filter="payload.raw_bytes EXISTS", time_range="15m", limit=5
        )
        _dump("search_traces(filter=\"payload.raw_bytes EXISTS\")", search_raw)

        # Fallback: what does an UNFILTERED search_traces look like, so we
        # can see real field names on a real row?
        unfiltered_raw = await client.search_traces(time_range="15m", limit=3)
        _dump("search_traces(no filter, limit=3) -- inspect real field names on a row", unfiltered_raw)

        # --- R3: pull one real trace's details to see span/parent field names ---
        list_raw = await client.search_traces(time_range="15m", limit=1)
        _dump("search_traces(time_range='15m', limit=1) -- for trace_id extraction", list_raw)

        # crude best-effort trace_id pull just for this diagnostic (not the
        # real rule's parsing path) -- print whatever key looks right so we
        # can grab a real trace_id to feed get_trace_details.
        print("\n>>> Look at the row above for a trace_id / traceId field, then edit this script")
        print(">>> to call client.get_trace_details('<that id>', time_range='15m') and re-run,")
        print(">>> to see the real span-tree response shape (parent_span_id / parentSpanId naming).")


asyncio.run(main())

"""
Stage 4 debugging aid, part 2 -- confirms get_trace_details' raw response
shape before r3_orphaned_spans.py's parsing (_parse_trace_details /
_trace_detail_span_rows) is trusted or changed.

Edit TRACE_ID below to a real trace_id from your instance (e.g. one seen
in debug_stage4.py's output, or SigNoz's UI), then:

    python -m experiments.debug_stage4_trace_details
"""

import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from guardian.mcp_client import SignozMCPClient

# EDIT THIS -- paste a real trace_id from your instance.
TRACE_ID = "279374d005979d3615966bb99e86a4f5"


async def main():
    async with SignozMCPClient() as client:
        raw = await client.get_trace_details(TRACE_ID, time_range="15m")
        print(json.dumps(raw, indent=2, default=str)[:6000])


asyncio.run(main())

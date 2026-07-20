"""
Stage 0 gate check (see BUILD-SPEC.md, Section 6, Stage 0).

Connects to the SigNoz hosted MCP endpoint, lists the available tools,
and calls `signoz_list_services`. This is the ONLY thing this script
does — it exists purely to prove the MCP data path works before any
other code in this project is written.

Usage:
    python scripts/verify_mcp_connection.py

Requires (from .env or the shell environment):
    SIGNOZ_MCP_URL      e.g. https://mcp.us2.signoz.cloud/mcp
    SIGNOZ_API_KEY       the API key generated in your SigNoz Cloud instance
    SIGNOZ_INSTANCE_URL  e.g. https://hip-monkey.us2.signoz.cloud

Exit code 0 = gate passed. Exit code 1 = gate failed -> switch to the
self-hosted Docker fallback (docker-compose.yml) before writing any
other code, per the spec.
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv()

MCP_URL = os.getenv("SIGNOZ_MCP_URL", "https://mcp.us2.signoz.cloud/mcp")
API_KEY = os.getenv("SIGNOZ_API_KEY")
INSTANCE_URL = os.getenv("SIGNOZ_INSTANCE_URL")


async def main() -> int:
    if not API_KEY or not INSTANCE_URL:
        print(
            "FAIL: SIGNOZ_API_KEY and/or SIGNOZ_INSTANCE_URL are not set. "
            "Copy .env.example to .env and fill them in first.",
            file=sys.stderr,
        )
        return 1

    headers = {
        "SIGNOZ-API-KEY": API_KEY,
        "X-SigNoz-URL": INSTANCE_URL,
    }

    print(f"Connecting to SigNoz MCP at {MCP_URL} ...")
    try:
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(MCP_URL, http_client=http_client) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    tools_result = await session.list_tools()
                    tool_names = sorted(t.name for t in tools_result.tools)
                    signoz_tools = [n for n in tool_names if n.startswith("signoz_")]

                    print(f"Connected. {len(tool_names)} tool(s) exposed, "
                          f"{len(signoz_tools)} are signoz_* tools.")
                    if not signoz_tools:
                        print("FAIL: no signoz_* tools visible on this MCP session.",
                              file=sys.stderr)
                        return 1

                    if "signoz_list_services" not in signoz_tools:
                        print(
                            "WARN: signoz_list_services not found by that exact name. "
                            f"Available signoz_* tools: {signoz_tools}",
                            file=sys.stderr,
                        )
                        return 1

                    print("Calling signoz_list_services ...")
                    result = await session.call_tool("signoz_list_services", {})

                    if result.isError:
                        print(f"FAIL: signoz_list_services returned an error: {result.content}",
                              file=sys.stderr)
                        return 1

                    print("Result:")
                    for block in result.content:
                        text = getattr(block, "text", None)
                        print(text if text is not None else block)

                    print("\nPASS: Stage 0 gate satisfied — signoz_* tools are visible "
                          "and callable over MCP.")
                    return 0

    except Exception as exc:  # noqa: BLE001 - top-level gate check, want any failure surfaced
        print(f"FAIL: could not complete MCP round-trip: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

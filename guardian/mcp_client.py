"""SigNoz MCP session wrapper (Section 4.3.4).

Hard requirement from the spec: "Both paths must use the same rule-engine
code -- only the MCP client's connection config changes." This module is
that abstraction boundary. Rule modules (guardian/rules/*.py) only ever
call the typed methods below (`aggregate_traces`, `get_field_keys`, ...);
they never construct an MCP session or know which backend they're talking
to. Only `SignozMCPClient.connect()` differs between the two backends.

Backends, selected via `SIGNOZ_MCP_MODE` env var:

  - "cloud" (default): SigNoz Cloud's hosted MCP endpoint,
    `https://mcp.<region>.signoz.cloud/mcp`. Auth per SigNoz's docs is
    normally interactive OAuth 2.1 (browser flow, handled by whatever MCP
    client library drives this), but the hosted endpoint also accepts
    header-based auth for non-interactive clients like this service --
    that's what we use here: `SIGNOZ-API-KEY` + `X-SigNoz-URL` headers.

  - "selfhost": a self-hosted `signoz-mcp-server` (Docker/binary) reachable
    at `SIGNOZ_MCP_URL` (e.g. `http://localhost:8000/mcp`), using the same
    header-based auth if the server wasn't itself started with
    SIGNOZ_URL/SIGNOZ_API_KEY baked in.

Both are Streamable HTTP MCP servers, so the transport code is identical;
only the URL and headers differ, which is exactly what `_connection_config()`
isolates.

IMPORTANT / honesty note: this was written against the tool parameter
reference in the signoz-mcp-server README (SigNoz/signoz-mcp-server, v0.8.0),
not against a live server -- I don't have network access to a real SigNoz
instance or MCP endpoint from this environment. Parameter *names* below are
taken directly from that README and should be correct. Response *parsing*
(`_parse_tool_result`) assumes each tool returns its JSON payload as a single
text content block, which is the standard MCP tool-result shape and matches
the README's documented response fields (e.g. `hasMore`, `nextOffset`,
`data.items`) -- but this needs a live run against your actual SigNoz Cloud
trial to confirm before trusting it for Stage 3's gate check.
"""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp import ClientSession


class SignozMCPError(RuntimeError):
    """Raised when a signoz_* MCP tool call fails or returns an error result."""


class SignozMCPClient:
    """Thin async wrapper around an MCP `ClientSession` connected to the
    SigNoz MCP server (cloud or self-hosted). Construct via `async with
    SignozMCPClient() as client:` -- the async context manager owns the
    HTTP transport and session lifecycle.
    """

    def __init__(
        self,
        mode: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        signoz_url: str | None = None,
    ) -> None:
        self.mode = mode or os.getenv("SIGNOZ_MCP_MODE", "cloud")
        self._url_override = url
        self._api_key_override = api_key
        self._signoz_url_override = signoz_url
        self._stack: AsyncExitStack | None = None
        self._session: Any | None = None

    # -- connection config: the ONLY part that differs cloud vs self-host ---

    def _connection_config(self) -> tuple[str, dict[str, str]]:
        api_key = self._api_key_override or os.getenv("SIGNOZ_API_KEY")
        headers: dict[str, str] = {}
        if api_key:
            headers["SIGNOZ-API-KEY"] = api_key

        if self.mode == "cloud":
            region = os.getenv("SIGNOZ_CLOUD_REGION", "us")
            url = self._url_override or os.getenv("SIGNOZ_MCP_URL") or f"https://mcp.{region}.signoz.cloud/mcp"
            signoz_instance_url = self._signoz_url_override or os.getenv("SIGNOZ_INSTANCE_URL")
            if signoz_instance_url:
                headers["X-SigNoz-URL"] = signoz_instance_url
            return url, headers

        if self.mode == "selfhost":
            url = self._url_override or os.getenv("SIGNOZ_MCP_URL", "http://localhost:8000/mcp")
            return url, headers

        raise ValueError(f"Unknown SIGNOZ_MCP_MODE {self.mode!r} -- expected 'cloud' or 'selfhost'")

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> SignozMCPClient:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ModuleNotFoundError as exc:
            raise SignozMCPError(
                "Missing optional dependency 'mcp'. Install the project dependencies before using SignozMCPClient."
            ) from exc

        url, headers = self._connection_config()
        self._stack = AsyncExitStack()
        read, write, _get_session_id = await self._stack.enter_async_context(
            streamablehttp_client(url, headers=headers)
        )
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, exc_traceback: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._session = None
        self._stack = None

    # -- generic tool call + response parsing ----------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a signoz_* MCP tool and return its parsed JSON payload.

        Strips out `None`-valued arguments (so callers can pass every
        possible kwarg and let unset ones fall through to the tool's own
        defaults) before calling.
        """
        if self._session is None:
            raise SignozMCPError("SignozMCPClient used outside `async with` -- no active session.")

        clean_args = {k: v for k, v in arguments.items() if v is not None}
        result = await self._session.call_tool(name, clean_args)

        if getattr(result, "isError", False):
            raise SignozMCPError(f"{name} returned an error result: {_first_text(result)}")

        return _parse_tool_result(result, tool_name=name)

    # -- typed wrappers for the tools R1/R2 need -------------------------
    # Parameter names copied verbatim from the signoz-mcp-server README's
    # parameter reference (v0.8.0) -- see module docstring.

    async def aggregate_traces(
        self,
        aggregation: str,
        aggregate_on: str | None = None,
        group_by: str | None = None,
        filter: str | None = None,  # noqa: A002 -- matches the tool's own param name
        service: str | None = None,
        operation: str | None = None,
        error: bool | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        time_range: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> Any:
        return await self.call_tool(
            "signoz_aggregate_traces",
            {
                "aggregation": aggregation,
                "aggregateOn": aggregate_on,
                "groupBy": group_by,
                "filter": filter,
                "service": service,
                "operation": operation,
                "error": error,
                "orderBy": order_by,
                "limit": limit,
                "timeRange": time_range,
                "start": start,
                "end": end,
            },
        )

    async def get_field_keys(
        self,
        signal: str,
        search_text: str | None = None,
        field_context: str | None = None,
        field_data_type: str | None = None,
    ) -> Any:
        return await self.call_tool(
            "signoz_get_field_keys",
            {
                "signal": signal,
                "searchText": search_text,
                "fieldContext": field_context,
                "fieldDataType": field_data_type,
            },
        )

    async def get_field_values(
        self,
        signal: str,
        name: str,
        search_text: str | None = None,
        field_context: str | None = None,
    ) -> Any:
        return await self.call_tool(
            "signoz_get_field_values",
            {
                "signal": signal,
                "name": name,
                "searchText": search_text,
                "fieldContext": field_context,
            },
        )

    async def execute_builder_query(self, query: dict[str, Any]) -> Any:
        return await self.call_tool("signoz_execute_builder_query", {"query": query})

    async def list_services(
        self,
        time_range: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> Any:
        """Needed by R7 (Section 4.3.1) to enumerate every known service
        before walking ordered service pairs for cross-service handoff
        breaks. Parameter names mirrored from the other time-scoped tools
        above -- same honesty caveat as `search_logs`: the
        signoz-mcp-server README's `signoz_list_services` entry wasn't
        available to check directly from this environment, so this needs
        the same live confirmation before trusting it for Stage 8's gate
        check."""
        return await self.call_tool(
            "signoz_list_services",
            {"timeRange": time_range, "start": start, "end": end},
        )

    async def search_traces(
        self,
        filter: str | None = None,  # noqa: A002
        service: str | None = None,
        operation: str | None = None,
        time_range: str | None = None,
        start: int | None = None,
        end: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        return await self.call_tool(
            "signoz_search_traces",
            {
                "filter": filter,
                "service": service,
                "operation": operation,
                "timeRange": time_range,
                "start": start,
                "end": end,
                "limit": limit,
                "offset": offset,
            },
        )

    async def get_trace_details(
        self,
        trace_id: str,
        time_range: str | None = None,
        start: int | None = None,
        end: int | None = None,
        include_spans: bool | None = None,
    ) -> Any:
        return await self.call_tool(
            "signoz_get_trace_details",
            {
                "traceId": trace_id,
                "timeRange": time_range,
                "start": start,
                "end": end,
                "includeSpans": include_spans,
            },
        )

    async def search_logs(
        self,
        filter: str | None = None,  # noqa: A002 -- matches the tool's own param name
        service: str | None = None,
        time_range: str | None = None,
        start: int | None = None,
        end: int | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        """Needed by R6 (Section 4.3.1) to cross-reference
        `gen_ai.response.finish_reasons == "length"` against a truncated
        tool-payload span, in the same trace. Parameter names mirrored from
        `search_traces` above -- same convention, logs signal instead of
        traces -- since the signoz-mcp-server README's search_logs entry
        wasn't available to check directly from this environment (see
        module docstring's honesty note); this needs the same live
        confirmation before trusting it for Stage 4's gate check."""
        return await self.call_tool(
            "signoz_search_logs",
            {
                "filter": filter,
                "service": service,
                "timeRange": time_range,
                "start": start,
                "end": end,
                "limit": limit,
                "offset": offset,
            },
        )


def _first_text(result: Any) -> str:
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return text
    return "<no text content in tool result>"


def _parse_tool_result(result: Any, tool_name: str) -> Any:
    """MCP tool results carry their payload as a list of content blocks;
    signoz-mcp-server's tools return a single text block containing JSON
    (per the README's documented response fields like `hasMore.`).
    Falls back to the raw text if it isn't valid JSON, so a shape we
    didn't anticipate surfaces as an obvious string rather than a crash
    deep in rule-evaluation code.
    """
    text = _first_text(result)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw_text": text, "_tool": tool_name}
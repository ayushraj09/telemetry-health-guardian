"""R3 -- Orphaned/broken span trees within a single service (Section 4.3.1).

Same split as R1/R2: `evaluate` is pure and fully unit-testable; `fetch_traces`
/ `run` are the MCP-fetching adapter, which needs live verification against a
real SigNoz Cloud trial (see mcp_client.py's module docstring for why).

Detection logic, exactly per spec:
  - A span is orphaned if its `parent_span_id` is set but does not resolve
    to any span within the *same* trace.
  - Cross-check with `signoz_search_traces` to confirm the missing parent
    isn't simply outside the query time window -- this avoids false
    positives at window edges. Do not skip this cross-check.

Design decision (documented, not silent): the spec's conceptual framing in
Section 2 ("async/threaded tool calls losing parent trace context") could
also describe a context-loss bug that produces a brand-new ROOT span (no
parent_span_id at all, new trace_id) rather than a span whose parent_span_id
points at something absent. That new-root case does NOT match this rule's
literal detection logic ("parent_span_id is set but does not resolve") --
a span with no parent_span_id at all has nothing to fail to resolve. This
implementation follows 4.3.1's literal text: only "parent set, unresolved"
counts as an R3 orphan. A context-loss bug that produces a disconnected new
trace instead is a different, more severe failure than what 4.3.1 describes
scanning for -- worth flagging to the user, not silently reinterpreting the
spec to cover it. (This is also why `chaos.py`'s R3 trigger fabricates a
bogus-but-present parent_span_id rather than clearing context outright --
see its own docstring.)

Score/rate: like R2 (Section 4.3.1 gives R1 an explicit score formula but
not R3), this implementation defines `orphaned_span_rate_pct` as
`100 * orphaned_spans / total_spans_with_parent` -- spans with no parent at
all (trace roots) are excluded from the denominator since they can't be
orphaned by this rule's definition. This feeds Section 4.3.6's health-score
formula's `orphaned_span_rate_pct` term directly.

CONFIRMED LIVE (2026-07-22, SigNoz Cloud): Stage 4's gate check passed
against a real chaos run -- 5/5 chaos-fired claim-check calls (rate=1.0,
seed=42) showed up as exactly 5 R3 findings (5/51 spans orphaned, 9.8%),
each correctly attributing the fabricated parent_span_id and correctly
NOT suppressed by the window-edge cross-check (since those span_ids never
existed anywhere). `_trace_detail_span_rows`'s fallback to `_extract_rows`
is confirmed correct too -- `get_trace_details` uses the same nested
`results[0].rows[].data` envelope r1/r2 already proved for
`execute_builder_query` / `aggregate_traces`, so no `spans`/`span`/`items`
key ever actually gets hit in practice on this server; the fallback path
handles it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from guardian.rules.r1_missing_fields import _extract_rows
from guardian.rules.types import AuditWindow

if TYPE_CHECKING:
    from guardian.mcp_client import SignozMCPClient

RULE_ID = "R3"


@dataclass(frozen=True)
class SpanNode:
    """A minimal, MCP-backend-agnostic view of one span within a trace."""

    span_id: str
    parent_span_id: str | None
    name: str


@dataclass(frozen=True)
class TraceRecord:
    trace_id: str
    spans: tuple[SpanNode, ...]


@dataclass(frozen=True)
class R3Finding:
    rule: str = field(default=RULE_ID, init=False)
    trace_id: str
    span_id: str
    span_name: str
    missing_parent_span_id: str
    detail: str


@dataclass(frozen=True)
class R3Result:
    orphaned_span_rate_pct: float  # 0-100, see module docstring for normalization
    total_spans_with_parent: int
    orphaned_spans: int
    findings: tuple[R3Finding, ...]


def evaluate(
    traces: list[TraceRecord],
    parent_ids_confirmed_elsewhere: frozenset[str] = frozenset(),
) -> R3Result:
    """Pure R3 detection logic.

    `parent_ids_confirmed_elsewhere` is the result of the spec-mandated
    cross-check: parent_span_ids that a `signoz_search_traces` query found
    to genuinely exist somewhere (just outside this window), and so must
    NOT be flagged as orphaned -- a within-window-only false positive, not
    a real broken link. Defaults to empty so a caller that has already done
    the cross-check filtering upstream (or a unit test not exercising that
    path) doesn't need to pass anything.
    """
    findings: list[R3Finding] = []
    total_with_parent = 0

    for trace_record in traces:
        span_ids_in_trace = {s.span_id for s in trace_record.spans}
        for span in trace_record.spans:
            if not span.parent_span_id:
                continue  # a root span -- nothing to resolve, not evaluable by this rule
            total_with_parent += 1

            if span.parent_span_id in span_ids_in_trace:
                continue  # resolves fine within this trace

            if span.parent_span_id in parent_ids_confirmed_elsewhere:
                continue  # window-edge false positive, per the spec's required cross-check

            findings.append(
                R3Finding(
                    trace_id=trace_record.trace_id,
                    span_id=span.span_id,
                    span_name=span.name,
                    missing_parent_span_id=span.parent_span_id,
                    detail=(
                        f"span '{span.name}' ({span.span_id}) in trace {trace_record.trace_id} "
                        f"has parent_span_id={span.parent_span_id} which does not resolve to any "
                        f"span in this trace, and was not found elsewhere -- broken context "
                        f"propagation, not a window-edge artifact."
                    ),
                )
            )

    orphaned = len(findings)
    rate = 0.0 if total_with_parent == 0 else 100.0 * orphaned / total_with_parent

    return R3Result(
        orphaned_span_rate_pct=rate,
        total_spans_with_parent=total_with_parent,
        orphaned_spans=orphaned,
        findings=tuple(findings),
    )


# -- MCP-fetching adapter -------------------------------------------------
# Needs live verification against a real SigNoz Cloud trial -- see
# mcp_client.py's module docstring. Response-shape parsing below follows
# the same "confirmed live where noted, best-effort fallback otherwise"
# discipline as r1_missing_fields.py / r2_cardinality.py, since this
# environment has no network path to a SigNoz instance to confirm directly.


async def fetch_traces(client: SignozMCPClient, window: AuditWindow) -> list[TraceRecord]:
    """Fetch every trace in the window and its full span tree.

    Two-step, per the spec's named tools (`signoz_get_trace_details`,
    `signoz_search_traces`): first list trace_ids in the window via
    `search_traces`, then pull each one's span tree via
    `get_trace_details`. `search_traces` alone doesn't reliably return a
    trace's *complete* span set (it's built for finding traces matching a
    filter, not enumerating one), which is why `get_trace_details` -- named
    first for R3 in Section 4.3.1 -- does the actual tree walk.
    """
    trace_ids = await _list_trace_ids(client, window)
    detail_results = await asyncio.gather(
        *(client.get_trace_details(trace_id, **window.as_mcp_kwargs()) for trace_id in trace_ids)
    )
    return [
        _parse_trace_details(trace_id, raw)
        for trace_id, raw in zip(trace_ids, detail_results)
    ]


async def _list_trace_ids(client: SignozMCPClient, window: AuditWindow) -> list[str]:
    raw = await client.search_traces(**window.as_mcp_kwargs(), limit=200)
    rows = _extract_rows(raw)
    trace_ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        trace_id = str(row.get("trace_id") or row.get("traceId") or "")
        if trace_id and trace_id not in seen:
            seen.add(trace_id)
            trace_ids.append(trace_id)
    return trace_ids


def _parse_trace_details(trace_id: str, raw: Any) -> TraceRecord:
    """Best-effort normalization of a `signoz_get_trace_details` response
    into a TraceRecord. Unverified against a live server (see module
    docstring) -- degrades to an empty span list on an unrecognized shape
    rather than raising, same discipline as r1's `_parse_span_rows`.
    """
    rows = _trace_detail_span_rows(raw)
    spans = tuple(
        SpanNode(
            span_id=str(row.get("span_id") or row.get("spanId") or ""),
            parent_span_id=(str(p) if (p := (row.get("parent_span_id") or row.get("parentSpanId"))) else None),
            name=str(row.get("name") or row.get("span_name") or ""),
        )
        for row in rows
    )
    return TraceRecord(trace_id=trace_id, spans=spans)


def _trace_detail_span_rows(raw: Any) -> list[dict]:
    """Pull the list of per-span dicts out of a `signoz_get_trace_details`
    response. Tries the same nested-results shape r1/r2 confirmed live for
    `signoz_execute_builder_query` / `signoz_aggregate_traces` first (MCP
    tools in this server family have repeatedly shared that envelope), then
    a flatter `spans` key, which is the more natural shape for a
    "trace details" tool specifically. Falls back to [] rather than
    raising on an unrecognized shape."""
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []

    data = raw.get("data", raw)
    if isinstance(data, dict):
        for key in ("spans", "span", "items"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                return candidate

    rows = _extract_rows(raw)
    return rows


async def _parent_exists_elsewhere(client: SignozMCPClient, parent_span_id: str) -> bool:
    """The spec-mandated cross-check for one candidate orphan: query for
    `parent_span_id` with NO time restriction (rather than the narrow audit
    window), so a parent that merely started before the window opened
    still counts as "exists" and the candidate is excluded as a
    window-edge false positive, not a genuine break."""
    raw = await client.search_traces(filter=f"span_id = '{parent_span_id}'", limit=1)
    rows = _extract_rows(raw)
    return len(rows) > 0


async def run(client: SignozMCPClient, window: AuditWindow) -> R3Result:
    traces = await fetch_traces(client, window)

    # Pass 1 (pure): find within-trace-unresolved candidates, no cross-check yet.
    candidates = evaluate(traces).findings

    # Pass 2: cross-check each distinct missing parent_span_id against a
    # wider/unrestricted search, per the spec's required step.
    distinct_missing_ids = {f.missing_parent_span_id for f in candidates}
    existence_results = await asyncio.gather(
        *(_parent_exists_elsewhere(client, pid) for pid in distinct_missing_ids)
    )
    confirmed_elsewhere = frozenset(
        pid for pid, exists in zip(distinct_missing_ids, existence_results) if exists
    )

    # Pass 3 (pure, final): same detection logic, now with real cross-check data.
    return evaluate(traces, parent_ids_confirmed_elsewhere=confirmed_elsewhere)

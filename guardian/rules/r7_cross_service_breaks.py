"""R7 -- Cross-service trace breaks at agent-to-agent handoffs (Section 4.3.1).

Built in Stage 8, per the build spec's updated Section 6: Stage 7's gate
passed and R7 is no longer stretch/deferred. Same split as every other
rule module: `evaluate` is pure and fully unit-testable; `fetch_*` / `run`
are the MCP-fetching adapter, needing live verification against a real
SigNoz Cloud trial (see mcp_client.py's module docstring for why).

Detection logic, exactly per spec:
  - List all known services via `signoz_list_services`.
  - For each ordered pair (A, B), find spans in A that make an outbound
    HTTP call to B's known endpoint (via `http.url` / `peer.service`
    attributes).
  - Check whether a corresponding *root* span (no parent) appears in B
    within a short time window (< 2 seconds) instead of a properly
    parented child span. A new root span in B where a continuation was
    expected = broken handoff.
  - Must be reported as distinct from R3 in all output: R3 = broken
    within one service's trace tree. R7 = broken across two services'
    trace IDs entirely. Never merge these two into a single finding type.
  - SigNoz MCP tools used: `signoz_list_services`, `signoz_search_traces`
    (queried across both service names, checking for trace-ID
    discontinuity at the call boundary).

Design decision (documented, not silent): "a corresponding root span
appears in B" only tells you something arrived -- it doesn't by itself
distinguish "correctly continues the caller's trace" from "arrived as a
disconnected new trace." The literal check this module implements is:
for each outbound call span in A, look for ANY span in B within the
handoff window (`HANDOFF_WINDOW_MS`); if the closest one shares A's
call's `trace_id`, propagation worked (not a finding); if it doesn't (a
new trace_id shows up in B instead, right when B's response to A's call
was expected), that is exactly the "one trace silently becomes two
disconnected ones" case Section 2 describes, and gets flagged. A call
with NO candidate span in B within the window at all is left unevaluated
(not flagged) rather than treated as a break -- a slow/failed HTTP call,
network hiccup, or a B service that simply hasn't ingested yet is a
different failure mode than a severed trace, and conflating "nothing
observed" with "observed a broken handoff" would be a false positive on
every quiet window, not a genuine finding.

This is the same "candidate found or not, in-window or not" shape
`r3_orphaned_spans.py` already uses for its window-edge cross-check --
reused here deliberately for consistency, not reinvented.

Honesty note (same discipline as every other rules/*.py module): written
against the tool parameter reference in mcp_client.py, not against a live
server -- `signoz_list_services`'s response shape in particular wasn't
independently confirmable from this environment (see
`mcp_client.py::list_services`'s own docstring). Parsing degrades to an
empty list/best-effort fallback rather than raising on an unrecognized
shape, same as r1/r3/r6.

Bug found and fixed here (live run, 2026-07-24): `fetch_outbound_calls`
and `fetch_peer_spans` originally called `signoz_search_traces` with a
`filter` string, the same approach R1 and R6 both tried first and both
had to abandon (see their own module docstrings) -- `search_traces`'s
`filter` doesn't reliably filter, and its row data doesn't reliably
include a custom attribute column (like `peer.service`) unless the tool
happens to include it by default, which it doesn't. Against a live
server this produced `total_handoffs == 0` on every run, chaos or not --
not because no cross-service calls existed, but because the query never
saw `peer.service` at all. Rebuilt on `signoz_execute_builder_query` with
explicit `selectFields`, same fix R1/R6 already applied. This also
surfaced a wrinkle neither R1 nor R6 hit: R7 is the only rule that needs
each row's actual `timestamp` (for the `HANDOFF_WINDOW_MS` join), and
`r1_missing_fields._extract_rows` discards it when unwrapping a row down
to its `data` sub-dict -- `_extract_rows_with_timestamp` below is a
timestamp-preserving variant of that same traversal, used only here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from guardian.rules.r1_missing_fields import _extract_rows
from guardian.rules.types import AuditWindow

if TYPE_CHECKING:
    from guardian.mcp_client import SignozMCPClient

RULE_ID = "R7"

# Per spec: "within a short time window (< 2 seconds)".
HANDOFF_WINDOW_MS = 2000

# Custom attribute names fact_check_and_cite.py's `_fetch_citation` sets on
# its outbound-call span (see that module's docstring) -- needed as
# explicit `selectFields` below, same reason R1 needs
# `_R1_SELECT_FIELD_NAMES` explicit: a raw builder-query response only
# returns attribute columns you actually asked for.
_PEER_SERVICE_ATTR = "peer.service"
_HTTP_URL_ATTR = "http.url"


@dataclass(frozen=True)
class OutboundCallSpan:
    """A minimal, MCP-backend-agnostic view of one span in service A that
    makes an outbound HTTP call to service B (identified by `peer_service`,
    taken from that span's `peer.service` / `http.url` attributes)."""

    service: str
    span_id: str
    trace_id: str
    timestamp_ms: int
    peer_service: str
    url: str | None = None


@dataclass(frozen=True)
class PeerSpanRecord:
    """A minimal view of one span observed in a candidate callee service B,
    used as a candidate continuation for some outbound call into it."""

    service: str
    span_id: str
    trace_id: str
    parent_span_id: str | None
    timestamp_ms: int
    name: str


@dataclass(frozen=True)
class R7Finding:
    rule: str = field(default=RULE_ID, init=False)
    caller_service: str
    callee_service: str
    caller_span_id: str
    caller_trace_id: str
    callee_span_id: str
    callee_trace_id: str
    detail: str


@dataclass(frozen=True)
class R7Result:
    cross_service_break_rate_pct: float  # 0-100, see module docstring for normalization
    total_handoffs: int  # evaluable handoffs only -- see module docstring
    broken_handoffs: int
    findings: tuple[R7Finding, ...]


def evaluate(
    outbound_calls: list[OutboundCallSpan],
    peer_spans_by_service: dict[str, list[PeerSpanRecord]],
) -> R7Result:
    """Pure R7 detection logic -- see module docstring for the exact
    per-call decision rule."""
    findings: list[R7Finding] = []
    evaluable = 0

    for call in outbound_calls:
        candidates = [
            span
            for span in peer_spans_by_service.get(call.peer_service, [])
            if 0 <= (span.timestamp_ms - call.timestamp_ms) <= HANDOFF_WINDOW_MS
        ]
        if not candidates:
            continue  # nothing observed in-window yet -- not evaluable, not a finding

        evaluable += 1

        if any(span.trace_id == call.trace_id for span in candidates):
            continue  # correctly propagated -- one connected trace across the handoff

        # No same-trace continuation arrived in-window; the nearest candidate
        # is a disconnected new trace in B where a continuation was expected.
        broken = min(candidates, key=lambda span: span.timestamp_ms - call.timestamp_ms)
        findings.append(
            R7Finding(
                caller_service=call.service,
                callee_service=call.peer_service,
                caller_span_id=call.span_id,
                caller_trace_id=call.trace_id,
                callee_span_id=broken.span_id,
                callee_trace_id=broken.trace_id,
                detail=(
                    f"outbound call from '{call.service}' (span {call.span_id}, "
                    f"trace {call.trace_id}) to '{call.peer_service}' has no "
                    f"same-trace continuation within {HANDOFF_WINDOW_MS}ms -- the "
                    f"nearest span in '{call.peer_service}' ({broken.span_id}) "
                    f"started a new trace ({broken.trace_id}) instead, "
                    f"parent_span_id={broken.parent_span_id!r} -- W3C traceparent "
                    f"was not propagated across this handoff."
                ),
            )
        )

    rate = 0.0 if evaluable == 0 else 100.0 * len(findings) / evaluable

    return R7Result(
        cross_service_break_rate_pct=rate,
        total_handoffs=evaluable,
        broken_handoffs=len(findings),
        findings=tuple(findings),
    )


# -- MCP-fetching adapter -------------------------------------------------
# Needs live verification against a real SigNoz Cloud trial -- see module
# docstring's honesty note.


def _service_names(raw: Any) -> list[str]:
    """Best-effort normalization of a `signoz_list_services` response into
    a flat list of service name strings.

    Confirmed live shape (SigNoz Cloud, region us2, 2026-07-24):
        {"data": [{"serviceName": "citation-service", "numCalls": 10,
                    "avgDuration": ..., "errorRate": ..., ...}, ...],
         "pagination": {"total": 2, "offset": 0, "limit": 50, ...}}
    i.e. `data` is ALREADY the flat list of per-service dicts -- there is
    no nested `data.services` key, and it's not the
    `data.data.results[...].rows` shape `_extract_rows` expects either
    (that shape is specific to `signoz_execute_builder_query`, a
    different tool). This was the actual bug behind R7 finding 0
    evaluable handoffs on every run regardless of chaos: this function's
    two prior fallback paths (`data.get("services")` and
    `_extract_rows(raw)`) both silently returned [] against this real
    shape, so `fetch_known_services` returned no services at all and the
    caller/callee pair loop below never ran a single iteration.

    Still tries the flatter `data.services` shape and the
    `_extract_rows` nested-query-result shape after the confirmed one, in
    case a different SigNoz deployment/version genuinely returns one of
    those instead -- falls back to [] only if none of the three match.
    """
    if isinstance(raw, list):
        return [str(item.get("serviceName") or item.get("service_name") or item) for item in raw]
    if not isinstance(raw, dict):
        return []

    data = raw.get("data", raw)

    if isinstance(data, list):
        names: list[str] = []
        for item in data:
            if isinstance(item, dict):
                name = item.get("serviceName") or item.get("service_name")
                if name:
                    names.append(str(name))
            elif isinstance(item, str):
                names.append(item)
        return names

    if isinstance(data, dict):
        candidate = data.get("services")
        if isinstance(candidate, list):
            return [str(item.get("serviceName") or item.get("service_name") or item) for item in candidate]

    rows = _extract_rows(raw)
    names = []
    for row in rows:
        name = row.get("serviceName") or row.get("service_name") or row.get("service")
        if name:
            names.append(str(name))
    return names


async def fetch_known_services(client: SignozMCPClient, window: AuditWindow) -> list[str]:
    raw = await client.list_services(**{k: v for k, v in window.as_mcp_kwargs().items() if k != "service"})
    seen: set[str] = set()
    ordered: list[str] = []
    for name in _service_names(raw):
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _extract_rows_with_timestamp(raw: Any) -> list[dict]:
    """Same nested-envelope traversal as `r1_missing_fields._extract_rows`
    (confirmed live shape, 2026-07: rows live at
    `data.data.results[0].rows`, each row shaped
    `{"data": {...selected fields...}, "timestamp": "..."}`) -- but R7 is
    the first rule module that actually needs that per-row `timestamp`
    sibling for anything (every other rule only ever reads the row's own
    `data` dict), and `_extract_rows` unwraps straight to `row["data"]`,
    silently dropping it. This is that same traversal with the sibling
    `timestamp` merged back into the flattened dict under the key
    `"_row_timestamp"` (a name that can't collide with any real selected
    field, since none of ours is named that) instead of discarded.

    Falls back to plain `_extract_rows` (with no timestamp available) on
    any shape this doesn't recognize, so a schema surprise degrades to
    "handoffs found but not time-window-evaluable" rather than a crash --
    same discipline every other rule module's fallback uses.
    """
    if isinstance(raw, dict):
        outer = raw.get("data", raw)
        inner = outer.get("data", outer) if isinstance(outer, dict) else None
        results = inner.get("results") if isinstance(inner, dict) else None
        if isinstance(results, list) and results and isinstance(results[0], dict):
            raw_rows = results[0].get("rows")
            if isinstance(raw_rows, list):
                merged: list[dict] = []
                for row in raw_rows:
                    if not isinstance(row, dict):
                        continue
                    fields = dict(row.get("data", row)) if isinstance(row.get("data", row), dict) else {}
                    if "timestamp" in row:
                        fields["_row_timestamp"] = row["timestamp"]
                    merged.append(fields)
                return merged
    return _extract_rows(raw)


def _parse_timestamp_to_ms(value: Any) -> int:
    """Best-effort normalization of whatever `_row_timestamp` actually
    turns out to be (unconfirmed against a live server -- see this
    module's honesty note) into epoch milliseconds, matching the unit
    `OutboundCallSpan.timestamp_ms` / `PeerSpanRecord.timestamp_ms` and
    `HANDOFF_WINDOW_MS` are defined in.

    Tries, in order: an epoch numeric string/int (inferring
    seconds/ms/us/ns from digit count, since ClickHouse-backed traces
    backends commonly store nanosecond timestamps while this project's
    own `AuditWindow.start_ms`/`end_ms` are milliseconds), then an
    ISO-8601 string (`datetime.fromisoformat`, tolerating a trailing
    'Z'). Returns 0 (never evaluable, never a crash) if none of these
    parse -- same "unrecognized shape degrades to no finding, not an
    exception" discipline as every other fallback in this module.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        digits = len(str(int(value)))
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        digits = len(value.strip().lstrip("-"))
    else:
        digits = None

    if digits is not None:
        num = int(float(value))
        if digits >= 19:
            return num // 1_000_000  # ns -> ms
        if digits >= 16:
            return num // 1_000  # us -> ms
        if digits >= 13:
            return num  # already ms
        if digits >= 10:
            return num * 1000  # s -> ms
        return num

    if isinstance(value, str):
        try:
            from datetime import datetime

            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except (ValueError, TypeError):
            return 0

    return 0


async def fetch_outbound_calls(
    client: SignozMCPClient, caller_service: str, callee_service: str, window: AuditWindow
) -> list[OutboundCallSpan]:
    """Spans in `caller_service` calling out to `callee_service`, per the
    spec's named attributes (`http.url` / `peer.service`).

    Uses `signoz_execute_builder_query` (raw request, explicit
    `selectFields`), NOT `signoz_search_traces` -- the same fix R1's
    `fetch_spans` and R6's `fetch_truncated_tool_spans` already needed
    (see their own module docstrings): `search_traces`'s `filter` doesn't
    reliably filter and its row data doesn't reliably include a custom
    attribute column unless it's explicitly selected. R7 was the one rule
    module still on the old `search_traces` path, which is why it was
    finding 0 evaluable handoffs even with real chaos-triggered breaks
    happening -- the query itself wasn't seeing `peer.service` at all,
    not a detection-logic bug.
    """
    start_ms, end_ms = window.as_absolute_ms_range()
    filter_expression = f"{_PEER_SERVICE_ATTR} = '{callee_service}' AND serviceName = '{caller_service}'"

    query = {
        "start": start_ms,
        "end": end_ms,
        "requestType": "raw",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "filter": {"expression": filter_expression},
                        "selectFields": [
                            {"name": "span_id"},
                            {"name": "trace_id"},
                            {"name": _PEER_SERVICE_ATTR},
                            {"name": _HTTP_URL_ATTR},
                        ],
                        "limit": 200,
                    },
                }
            ],
        },
    }
    raw = await client.execute_builder_query(query)
    rows = _extract_rows_with_timestamp(raw)
    calls: list[OutboundCallSpan] = []
    for row in rows:
        trace_id = str(row.get("trace_id") or row.get("traceId") or "")
        span_id = str(row.get("span_id") or row.get("spanId") or "")
        if not trace_id or not span_id:
            continue
        calls.append(
            OutboundCallSpan(
                service=caller_service,
                span_id=span_id,
                trace_id=trace_id,
                timestamp_ms=_parse_timestamp_to_ms(row.get("_row_timestamp")),
                peer_service=callee_service,
                url=(row.get(_HTTP_URL_ATTR) or row.get("httpUrl")),
            )
        )
    return calls


async def fetch_peer_spans(
    client: SignozMCPClient, callee_service: str, window: AuditWindow
) -> list[PeerSpanRecord]:
    """All spans observed in `callee_service` during the audit window --
    candidate continuations for any outbound call into it. Same
    `execute_builder_query` fix as `fetch_outbound_calls` above -- see
    that function's docstring.

    `parent_span_id` is fetched best-effort here (unlike R3, which gets
    it from `signoz_get_trace_details`'s confirmed span-tree shape): it's
    only used for this module's finding `detail` string, never for
    `evaluate()`'s actual pass/fail decision (that's `trace_id`-based
    only), so an unrecognized/absent value degrading to `None` doesn't
    weaken detection -- only the human-readable explanation.
    """
    start_ms, end_ms = window.as_absolute_ms_range()
    filter_expression = f"serviceName = '{callee_service}'"

    query = {
        "start": start_ms,
        "end": end_ms,
        "requestType": "raw",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "filter": {"expression": filter_expression},
                        "selectFields": [
                            {"name": "span_id"},
                            {"name": "trace_id"},
                            {"name": "parent_span_id"},
                            {"name": "name"},
                        ],
                        "limit": 200,
                    },
                }
            ],
        },
    }
    raw = await client.execute_builder_query(query)
    rows = _extract_rows_with_timestamp(raw)
    spans: list[PeerSpanRecord] = []
    for row in rows:
        trace_id = str(row.get("trace_id") or row.get("traceId") or "")
        span_id = str(row.get("span_id") or row.get("spanId") or "")
        if not trace_id or not span_id:
            continue
        parent = row.get("parent_span_id") or row.get("parentSpanId")
        spans.append(
            PeerSpanRecord(
                service=callee_service,
                span_id=span_id,
                trace_id=trace_id,
                parent_span_id=str(parent) if parent else None,
                timestamp_ms=_parse_timestamp_to_ms(row.get("_row_timestamp")),
                name=str(row.get("name") or row.get("span_name") or ""),
            )
        )
    return spans


async def run(client: SignozMCPClient, window: AuditWindow) -> R7Result:
    """Full R7 audit: enumerate services, then for every ordered pair
    (A, B) where A has calls tagged `peer.service = B`, fetch both sides
    and evaluate. `window.service`, if set, restricts A to just that one
    caller service (matching every other rule module's `service` scoping)
    -- B is still discovered generically since the whole point of R7 is
    catching handoffs to services the caller-scoped audit wouldn't
    otherwise look at.
    """
    services = await fetch_known_services(client, window)
    caller_services = [window.service] if window.service else services

    all_calls: list[OutboundCallSpan] = []
    peer_spans_by_service: dict[str, list[PeerSpanRecord]] = {}

    pairs = [
        (caller, callee)
        for caller in caller_services
        for callee in services
        if caller and callee and caller != callee
    ]

    call_results = await asyncio.gather(
        *(fetch_outbound_calls(client, caller, callee, window) for caller, callee in pairs)
    )
    for calls in call_results:
        all_calls.extend(calls)

    distinct_callees = {callee for calls in call_results for callee in {c.peer_service for c in calls}}
    peer_results = await asyncio.gather(
        *(fetch_peer_spans(client, callee, window) for callee in distinct_callees)
    )
    for callee, spans in zip(distinct_callees, peer_results):
        peer_spans_by_service[callee] = spans

    return evaluate(all_calls, peer_spans_by_service)
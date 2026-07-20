"""R1 -- Required GenAI attribute presence + naming conformance (Section 4.3.1).

Design note: this module is split into a pure evaluation function
(`evaluate`) and a thin MCP-fetching adapter (`fetch_spans` / `run`). The
pure function is where the *exact* detection logic from the spec lives, and
it's fully unit-testable with plain Python data -- no MCP connection, no
network, no SigNoz instance needed. That's deliberate: it's the only part
of this rule I can actually verify from this environment (see mcp_client.py
docstring for why the fetch side needs a live SigNoz run to confirm).

Field-name reuse: REQUIRED_GEN_AI_FIELDS, GEN_AI_OPERATION_NAME, and
OPERATION_CHAT are imported from otel_griptape.semconv rather than
redefined here. Both stage 2 (the instrumentation library that produces
these span attributes) and stage 3 (this rule that checks for them) must
agree on the exact attribute names, or R1 silently checks the wrong thing.
Importing the single shared source avoids the two drifting apart.

Design decision (documented, not silent): "gen_ai-kind span" is scoped to
spans where `gen_ai.operation.name == "chat"` -- i.e. prompt-driver spans.
Tool-call spans (`execute_tool`) never carry the 5 REQUIRED_GEN_AI_FIELDS
by design (see otel_griptape/instrumentor.py's `_observe_tool_run` -- they
carry `gen_ai.tool.name` instead), so including them here would make every
tool-call span a permanent, meaningless R1 violation.

Design decision (documented, not silent): the spec describes two conformance
checks (missing fields, naming convention) then gives one score formula
right after both. This implementation treats "non_conformant_spans" as the
union of the two -- a span with a naming violation but all required fields
present still counts as non-conformant. The two finding *kinds* are kept
separate in the findings list so a caller/LLM layer can still distinguish
them; only the score conflates them, per the spec's placement of the formula.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from otel_griptape.semconv import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    OPERATION_CHAT,
    REQUIRED_GEN_AI_FIELDS,
)

from guardian.rules.types import AuditWindow

if TYPE_CHECKING:
    from guardian.mcp_client import SignozMCPClient

RULE_ID = "R1"


@dataclass(frozen=True)
class SpanRecord:
    """A minimal, MCP-backend-agnostic view of one span. Whatever
    `fetch_spans` gets back from SigNoz gets normalized into this before
    `evaluate` ever sees it."""

    span_id: str
    trace_id: str
    name: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class R1Finding:
    rule: str = field(default=RULE_ID, init=False)
    kind: str  # "missing_field" | "naming_convention"
    span_id: str
    trace_id: str
    span_name: str
    detail: str


@dataclass(frozen=True)
class R1Result:
    score: float  # 1.0 - (non_conformant_spans / total_gen_ai_spans), or 1.0 if none in window
    total_gen_ai_spans: int
    non_conformant_spans: int
    findings: tuple[R1Finding, ...]


def evaluate(spans: list[SpanRecord]) -> R1Result:
    """Pure R1 detection logic -- see module docstring for the two
    documented scoping/scoring decisions."""
    chat_spans = [s for s in spans if s.attributes.get(GEN_AI_OPERATION_NAME) == OPERATION_CHAT]

    findings: list[R1Finding] = []
    non_conformant_ids: set[str] = set()

    for span in chat_spans:
        missing = [f for f in REQUIRED_GEN_AI_FIELDS if span.attributes.get(f) is None]
        for missing_field in missing:
            findings.append(
                R1Finding(
                    kind="missing_field",
                    span_id=span.span_id,
                    trace_id=span.trace_id,
                    span_name=span.name,
                    detail=f"missing required attribute '{missing_field}'",
                )
            )
        if missing:
            non_conformant_ids.add(span.span_id)

        model = span.attributes.get(GEN_AI_REQUEST_MODEL)
        operation = span.attributes.get(GEN_AI_OPERATION_NAME)
        if model is not None and operation is not None:
            expected_name = f"{operation} {model}"
            if span.name != expected_name:
                findings.append(
                    R1Finding(
                        kind="naming_convention",
                        span_id=span.span_id,
                        trace_id=span.trace_id,
                        span_name=span.name,
                        detail=f"span name '{span.name}' does not match expected '{expected_name}'",
                    )
                )
                non_conformant_ids.add(span.span_id)

    total = len(chat_spans)
    non_conformant = len(non_conformant_ids)
    score = 1.0 if total == 0 else 1.0 - (non_conformant / total)

    return R1Result(
        score=score,
        total_gen_ai_spans=total,
        non_conformant_spans=non_conformant,
        findings=tuple(findings),
    )


# -- MCP-fetching adapter -------------------------------------------------
# Needs live verification against a real SigNoz Cloud trial -- see
# mcp_client.py's module docstring.

_R1_BUILDER_QUERY_FIELDS = (
    "GEN_AI_SYSTEM",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "GEN_AI_RESPONSE_FINISH_REASONS",
)


async def fetch_spans(client: SignozMCPClient, window: AuditWindow) -> list[SpanRecord]:
    """Fetch chat-operation spans for the window via `signoz_execute_builder_query`
    (a raw list of spans with attributes is what R1 needs to check per-span
    presence and naming -- `signoz_aggregate_traces` alone can only give
    counts, not which specific spans/fields are missing, so it's used for
    smaller pre-checks like total-count sanity, not the primary fetch here).
    """
    query = {
        "compositeQuery": {
            "queryType": "builder",
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "filter": f"{GEN_AI_OPERATION_NAME} = 'chat'",
                        **window.as_mcp_kwargs(),
                        "limit": 1000,
                    },
                }
            ],
        }
    }
    raw = await client.execute_builder_query(query)
    return _parse_span_rows(raw)


def _parse_span_rows(raw: Any) -> list[SpanRecord]:
    """Best-effort normalization of a builder-query response into
    SpanRecords. The exact response envelope (nesting under `data.result`
    vs `data.rows`, etc.) needs confirming against a live call -- this
    covers the shapes documented across SigNoz's Query Builder v5 docs and
    degrades to an empty list (rather than raising) on an unrecognized
    shape, so a schema surprise shows up as "0 spans found" rather than a
    crash in the audit loop.
    """
    rows = _extract_rows(raw)
    records: list[SpanRecord] = []
    for row in rows:
        attrs = row.get("attributes") or row.get("attribute") or {k: v for k, v in row.items() if "." in k}
        records.append(
            SpanRecord(
                span_id=str(row.get("span_id") or row.get("spanId") or ""),
                trace_id=str(row.get("trace_id") or row.get("traceId") or ""),
                name=str(row.get("name") or row.get("span_name") or ""),
                attributes=attrs,
            )
        )
    return records


def _extract_rows(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    data = raw.get("data", raw)
    for key in ("result", "rows", "items"):
        candidate = data.get(key) if isinstance(data, dict) else None
        if isinstance(candidate, list):
            return candidate
    return []


async def run(client: SignozMCPClient, window: AuditWindow) -> R1Result:
    spans = await fetch_spans(client, window)
    return evaluate(spans)
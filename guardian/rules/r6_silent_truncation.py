"""R6 -- Silent context-window / tool-result truncation (Section 4.3.1).

Same split as R1/R2/R3: `evaluate` is pure and fully unit-testable;
`fetch_payload_spans` / `fetch_length_finish_reasons` / `run` are the
MCP-fetching adapter, needing live verification against a real SigNoz
Cloud trial (see mcp_client.py's module docstring for why).

Detection logic, exactly per spec:
  - For every tool-call span, compare `payload.raw_bytes` (set by
    otel-griptape) against `payload.captured_bytes`.
  - Flag when `captured_bytes < raw_bytes * 0.95`. The 5% slack is fixed,
    not configurable without a documented reason -- see TRUNCATION_SLACK.
  - Cross-reference with `signoz_search_logs` for
    `gen_ai.response.finish_reasons == "length"` in the *same trace* to
    distinguish "tool payload was truncated" from "the model itself hit
    its output token limit." These are different bugs and MUST be
    reported as distinct findings, never merged into one generic
    "truncation" flag -- enforced here via `R6Finding.kind`.

IMPORTANT / honesty note (flag to the user, don't silently paper over):
otel-griptape's instrumentor currently records `gen_ai.response.finish_reasons`
only as a SPAN attribute (see semconv.py / instrumentor.py's
`_finish_reason_capture`), not as a separate OTel log record. The spec
names `signoz_search_logs` specifically for this cross-reference, so
`fetch_length_finish_reasons` below queries the logs signal as specified --
but until otel-griptape (or SigNoz's own span->log correlation) actually
produces a log record carrying that attribute, this half of R6 will find
nothing to cross-reference against, the same "fires never, doesn't error"
behavior Section 9 describes for R6 on a target app that doesn't set the
payload attributes at all. This is a real gap worth closing in
otel-griptape (emit a log record when finish_reason == "length") before
relying on this cross-reference for a live demo -- not something to
silently fix by switching this rule to query spans instead, since that
would contradict the spec's explicit tool choice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from otel_griptape.semconv import PAYLOAD_CAPTURED_BYTES, PAYLOAD_RAW_BYTES

from guardian.rules.r1_missing_fields import _extract_rows
from guardian.rules.types import AuditWindow

if TYPE_CHECKING:
    from guardian.mcp_client import SignozMCPClient

RULE_ID = "R6"

# 5% slack, per spec, exact: "absorbs harmless whitespace/encoding
# differences without missing real truncation -- do not use a stricter or
# looser threshold without a documented reason." No such reason exists here.
TRUNCATION_SLACK = 0.95

FINDING_KIND_TOOL_PAYLOAD_TRUNCATED = "tool_payload_truncated"
FINDING_KIND_MODEL_OUTPUT_TRUNCATED = "model_output_truncated"


@dataclass(frozen=True)
class PayloadSpanRecord:
    """A minimal, MCP-backend-agnostic view of one tool-call span's
    payload-tracking attributes."""

    span_id: str
    trace_id: str
    span_name: str
    raw_bytes: int
    captured_bytes: int


@dataclass(frozen=True)
class FinishReasonLogRecord:
    """One log record (per the spec's `signoz_search_logs` cross-reference)
    reporting `gen_ai.response.finish_reasons == "length"` for some trace."""

    trace_id: str
    span_id: str | None
    span_name: str | None


@dataclass(frozen=True)
class R6Finding:
    rule: str = field(default=RULE_ID, init=False)
    kind: str  # FINDING_KIND_TOOL_PAYLOAD_TRUNCATED | FINDING_KIND_MODEL_OUTPUT_TRUNCATED
    trace_id: str
    span_id: str
    span_name: str
    detail: str


@dataclass(frozen=True)
class R6Result:
    truncation_rate_pct: float  # 0-100, see module docstring for normalization
    total_payload_spans: int
    truncated_payload_spans: int
    findings: tuple[R6Finding, ...]


def evaluate(
    payload_spans: list[PayloadSpanRecord],
    finish_reason_logs: list[FinishReasonLogRecord] = (),  # type: ignore[assignment]
) -> R6Result:
    """Pure R6 detection logic. The two finding kinds are computed and
    appended independently -- a trace that has BOTH a truncated tool
    payload and a model that hit `finish_reasons == "length"` produces TWO
    findings, never one merged finding, per the spec's explicit
    requirement."""
    findings: list[R6Finding] = []
    truncated_ids: set[str] = set()

    for span in payload_spans:
        if span.raw_bytes <= 0:
            continue  # nothing to compare a truncation ratio against
        if span.captured_bytes < span.raw_bytes * TRUNCATION_SLACK:
            truncated_ids.add(span.span_id)
            findings.append(
                R6Finding(
                    kind=FINDING_KIND_TOOL_PAYLOAD_TRUNCATED,
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                    span_name=span.span_name,
                    detail=(
                        f"R6 fired: '{span.span_name}' returned {span.raw_bytes}B but only "
                        f"{span.captured_bytes}B reached the agent's context "
                        f"(threshold: captured < raw * {TRUNCATION_SLACK})."
                    ),
                )
            )

    for log in finish_reason_logs:
        findings.append(
            R6Finding(
                kind=FINDING_KIND_MODEL_OUTPUT_TRUNCATED,
                trace_id=log.trace_id,
                span_id=log.span_id or "",
                span_name=log.span_name or "",
                detail=(
                    f"R6 fired: model call in trace {log.trace_id} reported "
                    f"gen_ai.response.finish_reasons == 'length' -- the model itself hit its "
                    f"output token limit. This is a distinct bug from tool-payload truncation "
                    f"and must not be conflated with it."
                ),
            )
        )

    total = len(payload_spans)
    truncated = len(truncated_ids)
    rate = 0.0 if total == 0 else 100.0 * truncated / total

    return R6Result(
        truncation_rate_pct=rate,
        total_payload_spans=total,
        truncated_payload_spans=truncated,
        findings=tuple(findings),
    )


# -- MCP-fetching adapter -------------------------------------------------
# Needs live verification against a real SigNoz Cloud trial -- see
# mcp_client.py's module docstring, and this module's own honesty note
# about the finish_reasons/logs cross-reference above.


async def fetch_payload_spans(client: SignozMCPClient, window: AuditWindow) -> list[PayloadSpanRecord]:
    """Fetch every span carrying both R6 payload-tracking attributes, via
    `signoz_search_traces` (the tool named for R6 in Section 4.3.1) filtered
    on `payload.raw_bytes EXISTS`."""
    filter_expression = f"{PAYLOAD_RAW_BYTES} EXISTS"
    if window.service:
        filter_expression += f" AND serviceName = '{window.service}'"

    raw = await client.search_traces(filter=filter_expression, **_window_time_kwargs(window), limit=1000)
    rows = _extract_rows(raw)

    spans: list[PayloadSpanRecord] = []
    for row in rows:
        raw_bytes = row.get(PAYLOAD_RAW_BYTES)
        captured_bytes = row.get(PAYLOAD_CAPTURED_BYTES)
        if raw_bytes is None or captured_bytes is None:
            continue
        spans.append(
            PayloadSpanRecord(
                span_id=str(row.get("span_id") or row.get("spanId") or ""),
                trace_id=str(row.get("trace_id") or row.get("traceId") or ""),
                span_name=str(row.get("name") or row.get("span_name") or ""),
                raw_bytes=int(raw_bytes),
                captured_bytes=int(captured_bytes),
            )
        )
    return spans


async def fetch_length_finish_reasons(client: SignozMCPClient, window: AuditWindow) -> list[FinishReasonLogRecord]:
    """Fetch log records reporting `gen_ai.response.finish_reasons ==
    "length"`, via `signoz_search_logs` (the tool named for R6 in Section
    4.3.1) -- see this module's honesty note for the real gap in what
    otel-griptape currently emits for this to find."""
    filter_expression = "gen_ai.response.finish_reasons = 'length'"
    if window.service:
        filter_expression += f" AND serviceName = '{window.service}'"

    raw = await client.search_logs(filter=filter_expression, **_window_time_kwargs(window), limit=1000)
    rows = _extract_rows(raw)

    logs: list[FinishReasonLogRecord] = []
    for row in rows:
        trace_id = str(row.get("trace_id") or row.get("traceId") or "")
        if not trace_id:
            continue
        logs.append(
            FinishReasonLogRecord(
                trace_id=trace_id,
                span_id=(str(sid) if (sid := (row.get("span_id") or row.get("spanId"))) else None),
                span_name=(str(n) if (n := (row.get("name") or row.get("span_name"))) else None),
            )
        )
    return logs


def _window_time_kwargs(window: AuditWindow) -> dict[str, Any]:
    """`as_mcp_kwargs()` already includes `service` -- strip it back out
    here since both fetch functions above build their own service clause
    directly into the filter expression (so it composes with the
    EXISTS / equality clause in one `filter` string, matching how
    r1_missing_fields.fetch_spans builds its filter expressions)."""
    kwargs = window.as_mcp_kwargs()
    kwargs.pop("service", None)
    return kwargs


async def run(client: SignozMCPClient, window: AuditWindow) -> R6Result:
    payload_spans, finish_reason_logs = await asyncio.gather(
        fetch_payload_spans(client, window),
        fetch_length_finish_reasons(client, window),
    )
    return evaluate(payload_spans, finish_reason_logs)
"""R2 -- Cardinality risk (Section 4.3.1).

Same split as R1: `evaluate` is pure and fully unit-testable; `fetch_field_stats`
/ `run` are the MCP-fetching adapter, which needs live verification against
a real SigNoz Cloud trial (see mcp_client.py's module docstring for why).

The spec is explicit and non-negotiable about the two-condition check:

    Flag an attribute key when BOTH conditions hold:
      - distinct_values / total_spans > 0.8
      - average value byte-length > 200 characters
    Do not implement a single-condition version of this check.

`evaluate` enforces both conditions with a single `and`; there's no
separate single-condition code path to accidentally fall into.

Design decision (documented, not silent): the spec's health-score formula
(Section 4.3.6) takes a "cardinality_risk_score, normalized 0-100" as an
R2 input, but 4.3.1 doesn't define how to compute that number -- only the
flag condition per key. This implementation defines it as the percentage
of *evaluated* keys that got flagged: `100 * flagged_keys / evaluated_keys`.
This is a reasonable, simple choice, but it's mine, not the spec's -- flag
this to the user before wiring it into the health-score formula in a later
stage, in case they want a different normalization (e.g. weighted by each
flagged key's actual span volume).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from guardian.rules.types import AuditWindow

if TYPE_CHECKING:
    from guardian.mcp_client import SignozMCPClient

RULE_ID = "R2"

DISTINCT_RATIO_THRESHOLD = 0.8
AVG_BYTE_LENGTH_THRESHOLD = 200


@dataclass(frozen=True)
class FieldValueStats:
    """Per-attribute-key stats needed for the two-condition check.

    `distinct_values` and `total_spans` together give the distinct-ratio;
    `avg_value_bytes` is the average UTF-8 byte length of the values seen
    for this key. `sampled` flags when `distinct_values` came from a
    capped/paginated tool response rather than an exhaustive count (see
    fetch_field_stats) -- evaluate() still applies the same threshold, but
    a caller surfacing findings to a human should treat sampled ratios as
    a lower bound, not exact.
    """

    key: str
    distinct_values: int
    total_spans: int
    avg_value_bytes: float
    sampled: bool = False


@dataclass(frozen=True)
class R2Finding:
    rule: str = field(default=RULE_ID, init=False)
    field_key: str
    distinct_ratio: float
    avg_value_bytes: float
    detail: str


@dataclass(frozen=True)
class R2Result:
    cardinality_risk_score: float  # 0-100, see module docstring for normalization
    evaluated_keys: int
    flagged_keys: int
    findings: tuple[R2Finding, ...]


def evaluate(stats: list[FieldValueStats]) -> R2Result:
    """Pure R2 detection logic. Both conditions are required -- see module
    docstring; this is the one thing this function must never relax."""
    findings: list[R2Finding] = []

    for stat in stats:
        if stat.total_spans <= 0:
            continue  # can't compute a ratio; not evaluable, not flagged
        distinct_ratio = stat.distinct_values / stat.total_spans

        high_cardinality = distinct_ratio > DISTINCT_RATIO_THRESHOLD
        long_values = stat.avg_value_bytes > AVG_BYTE_LENGTH_THRESHOLD

        if high_cardinality and long_values:
            findings.append(
                R2Finding(
                    field_key=stat.key,
                    distinct_ratio=distinct_ratio,
                    avg_value_bytes=stat.avg_value_bytes,
                    detail=(
                        f"'{stat.key}' is {distinct_ratio:.0%} distinct "
                        f"({stat.distinct_values}/{stat.total_spans} spans) with an average "
                        f"value length of {stat.avg_value_bytes:.0f} bytes -- likely raw "
                        f"content leaking into an indexed span attribute, not a legitimate "
                        f"high-cardinality-but-short field like trace_id/span_id."
                    ),
                )
            )

    evaluated = len(stats)
    flagged = len(findings)
    risk_score = 0.0 if evaluated == 0 else 100.0 * flagged / evaluated

    return R2Result(
        cardinality_risk_score=risk_score,
        evaluated_keys=evaluated,
        flagged_keys=flagged,
        findings=tuple(findings),
    )


# -- MCP-fetching adapter -------------------------------------------------
# Needs live verification against a real SigNoz Cloud trial -- see
# mcp_client.py's module docstring. In particular: `signoz_get_field_values`
# returning a *sampled/paginated* list of values (bounded by whatever limit
# the tool applies) rather than a true distinct-count is the biggest
# unverified assumption here -- see FieldValueStats.sampled.

_EXCLUDED_KEY_SUBSTRINGS = ("trace_id", "span_id", "traceId", "spanId")


async def fetch_field_stats(
    client: SignozMCPClient,
    window: AuditWindow,
    key_search_text: str | None = None,
) -> list[FieldValueStats]:
    """Pull field keys + per-key value samples for the window, and the
    total span count needed to compute each key's distinct-ratio.

    `key_search_text` lets a caller scope to e.g. only `gen_ai.*` /
    `payload.*` keys instead of every attribute key in the window, which
    matters in practice: querying every field key's value distribution
    across a real trace volume is expensive, and R2 only cares about
    gen_ai/tool-content-shaped attributes in the first place.
    """
    total_spans = await _fetch_total_span_count(client, window)
    if total_spans == 0:
        return []

    keys_response = await client.get_field_keys(signal="traces", search_text=key_search_text)
    keys = _extract_field_keys(keys_response)

    stats: list[FieldValueStats] = []
    for key in keys:
        if any(excluded in key for excluded in _EXCLUDED_KEY_SUBSTRINGS):
            # trace_id/span_id are the canonical example the spec calls out
            # by name as a false-positive risk this two-condition check
            # exists to avoid -- skip querying them entirely rather than
            # relying on the byte-length half of the check alone.
            continue

        values_response = await client.get_field_values(signal="traces", name=key)
        values = _extract_field_values(values_response)
        if not values:
            continue

        distinct_values = len({v for v in values})
        avg_bytes = sum(len(v.encode("utf-8")) for v in values) / len(values)

        stats.append(
            FieldValueStats(
                key=key,
                distinct_values=distinct_values,
                total_spans=total_spans,
                avg_value_bytes=avg_bytes,
                sampled=True,  # get_field_values is a capped/paginated call, not exhaustive
            )
        )

    return stats


async def _fetch_total_span_count(client: SignozMCPClient, window: AuditWindow) -> int:
    result = await client.aggregate_traces(aggregation="count", **window.as_mcp_kwargs())
    return _extract_scalar_count(result)


def _extract_scalar_count(raw: Any) -> int:
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        for key in ("count", "value", "result"):
            candidate = data.get(key) if isinstance(data, dict) else None
            if isinstance(candidate, (int, float)):
                return int(candidate)
            if isinstance(candidate, list) and candidate:
                first = candidate[0]
                if isinstance(first, dict):
                    for v in first.values():
                        if isinstance(v, (int, float)):
                            return int(v)
    return 0


def _extract_field_keys(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) if not isinstance(item, dict) else str(item.get("name") or item.get("key")) for item in raw]
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        candidate = data.get("keys") if isinstance(data, dict) else None
        if isinstance(candidate, list):
            return [str(item) if not isinstance(item, dict) else str(item.get("name") or item.get("key")) for item in candidate]
    return []


def _extract_field_values(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(v) for v in raw]
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        candidate = data.get("values") if isinstance(data, dict) else None
        if isinstance(candidate, list):
            return [str(v) for v in candidate]
    return []


async def run(client: SignozMCPClient, window: AuditWindow, key_search_text: str | None = None) -> R2Result:
    stats = await fetch_field_stats(client, window, key_search_text=key_search_text)
    return evaluate(stats)
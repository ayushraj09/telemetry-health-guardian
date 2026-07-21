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

from guardian.rules.r1_missing_fields import _extract_rows
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
    `total_spans` is the count of spans carrying THIS key specifically
    (see `fetch_field_stats`'s docstring for why, not every span in the
    audit window). `avg_value_bytes` is the average UTF-8 byte length of
    the values seen for this key. `sampled` flags when `distinct_values`
    came from a capped/paginated tool response rather than an exhaustive
    count (see fetch_field_stats) -- evaluate() still applies the same
    threshold, but a caller surfacing findings to a human should treat
    sampled ratios as a lower bound, not exact.
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
# Confirmed live against SigNoz Cloud (2026-07-21): `signoz_get_field_values`
# returned an empty value list for a key holding several KB of raw text
# (see `_fetch_raw_field_values`'s docstring), so value sampling goes
# through a raw `signoz_execute_builder_query` instead, capped at 1000
# rows/spans -- still a sample, not a true distinct-count, hence
# `FieldValueStats.sampled` stays True.

_EXCLUDED_KEY_SUBSTRINGS = ("trace_id", "span_id", "traceId", "spanId")


async def fetch_field_stats(
    client: SignozMCPClient,
    window: AuditWindow,
    key_search_text: str | None = None,
) -> list[FieldValueStats]:
    """Pull field keys + per-key value samples for the window.

    `key_search_text` lets a caller scope to e.g. only `gen_ai.*` /
    `payload.*` keys instead of every attribute key in the window, which
    matters in practice: querying every field key's value distribution
    across a real trace volume is expensive, and R2 only cares about
    gen_ai/tool-content-shaped attributes in the first place.

    Design correction (2026-07-21, re-reading Section 4.1's own framing of
    R2 -- "if it's ever captured as an indexed span attribute... that's a
    genuine R2 case, not a manufactured one"): `total_spans` in the
    `distinct_values / total_spans > 0.8` check is the count of spans that
    carry *this specific key*, not every span anywhere in the audit
    window. A whole-window denominator would silently dilute any narrowly-
    scoped attribute (which is most of them, by nature -- a raw-content
    leak is set by exactly one call site) below the 0.8 threshold
    regardless of window width, making the rule effectively unfireable in
    ordinary use. The trace_id/span_id false-positive the spec calls out
    still gets excluded correctly under this reading -- via the avg-byte-
    length half of the check, not the denominator choice -- since those
    keys are short-valued even though they're on every span and all-
    distinct.

    A cheap whole-window span count is still fetched first purely as an
    early-exit: an empty window has nothing to evaluate at all.
    """
    total_spans_in_window = await _fetch_total_span_count(client, window)
    if total_spans_in_window == 0:
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

        values = await _fetch_raw_field_values(client, window, key)
        if not values:
            continue

        distinct_values = len({v for v in values})
        avg_bytes = sum(len(v.encode("utf-8")) for v in values) / len(values)

        stats.append(
            FieldValueStats(
                key=key,
                distinct_values=distinct_values,
                total_spans=len(values),  # spans carrying THIS key, not the whole window
                avg_value_bytes=avg_bytes,
                sampled=True,  # raw query is capped at 1000 rows, not exhaustive
            )
        )

    return stats


async def _fetch_raw_field_values(client: SignozMCPClient, window: AuditWindow, key: str) -> list[str]:
    """Fetch this key's actual per-span values via a raw builder query,
    instead of `signoz_get_field_values`.

    Confirmed live (2026-07-21, SigNoz Cloud): `get_field_values` returned
    an empty `{"values": {}}` for `document.raw_extracted_text` even though
    `get_field_keys` confirms the key exists and is populated -- it's an
    autocomplete/typeahead-style tool that appears to decline to sample
    values for a field holding several KB of raw text. That's precisely the
    shape of value R2 exists to catch, so this rule can't depend on that
    tool for exactly the keys it cares most about. Mirrors R1's proven raw
    `signoz_execute_builder_query` fetch instead, which has no such limit.
    """
    start_ms, end_ms = window.as_absolute_ms_range()
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
                        "filter": {"expression": f"{key} EXISTS"},
                        "selectFields": [{"name": "span_id"}, {"name": key}],
                        "limit": 1000,
                    },
                }
            ],
        },
    }
    raw = await client.execute_builder_query(query)
    rows = _extract_rows(raw)
    values: list[str] = []
    for row in rows:
        v = row.get(key)
        if v is not None:
            values.append(str(v))
    return values


async def _fetch_total_span_count(client: SignozMCPClient, window: AuditWindow) -> int:
    result = await client.aggregate_traces(aggregation="count", **window.as_mcp_kwargs())
    return _extract_scalar_count(result)


def _extract_scalar_count(raw: Any) -> int:
    """Pull a single scalar aggregation value out of a
    `signoz_aggregate_traces` response.

    Confirmed live shape (SigNoz Cloud, region us2, 2026-07):
        {"status": "success",
         "data": {"type": "scalar", "meta": {...},
                   "data": {"results": [{"queryName": "A",
                                          "columns": [...],
                                          "data": [[<value>]]}]}}}
    i.e. the count is at data.data.results[0].data[0][0] -- the previous
    version of this function only looked one level into `data` for a
    `count`/`value`/`result` key, none of which exist at that level, so it
    always silently returned 0 even with real spans present. Falls back to
    a couple of flatter shapes in case another tool/version differs, before
    giving up and returning 0.
    """
    if not isinstance(raw, dict):
        return 0

    outer = raw.get("data", raw)
    inner = outer.get("data", outer) if isinstance(outer, dict) else None
    results = inner.get("results") if isinstance(inner, dict) else None
    if isinstance(results, list) and results and isinstance(results[0], dict):
        rows = results[0].get("data")
        if isinstance(rows, list) and rows:
            first_row = rows[0]
            if isinstance(first_row, list) and first_row and isinstance(first_row[0], (int, float)):
                return int(first_row[0])
            if isinstance(first_row, dict):
                for v in first_row.values():
                    if isinstance(v, (int, float)):
                        return int(v)

    # Fallback: flatter shapes some tools/versions may return.
    data = outer if isinstance(outer, dict) else raw
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
    """Pull the list of field-key names out of a `signoz_get_field_keys`
    response.

    Confirmed live shape (SigNoz Cloud, region us2, 2026-07):
        {"status": "success",
         "data": {"keys": {"<field.name>": [{...meta...}], ...}}}
    i.e. `data.keys` is a dict keyed by field name (mapping to a list of
    per-context metadata objects), not a list -- the previous version of
    this function only handled the list case, so it silently returned []
    against this server even with real attribute keys present. Falls back
    to the list-shaped case (some other tool/version) before giving up and
    returning [].
    """
    if isinstance(raw, list):
        return [str(item) if not isinstance(item, dict) else str(item.get("name") or item.get("key")) for item in raw]
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        candidate = data.get("keys") if isinstance(data, dict) else None
        if isinstance(candidate, dict):
            return list(candidate.keys())
        if isinstance(candidate, list):
            return [str(item) if not isinstance(item, dict) else str(item.get("name") or item.get("key")) for item in candidate]
    return []


def _extract_field_values(raw: Any) -> list[str]:
    """Pull the list of sample values out of a `signoz_get_field_values`
    response.

    Confirmed live shape (SigNoz Cloud, region us2, 2026-07):
        {"status": "success",
         "data": {"values": {"stringValues": [...]}, "complete": true}}
    i.e. `data.values` is a dict split by value-type bucket (`stringValues`,
    and presumably `numberValues`/`boolValues` for other field data types),
    not a flat list -- the previous version of this function only handled
    the list case, so it silently returned [] for every key against this
    server. Flattens across whichever `*Values` buckets are present. Falls
    back to the list-shaped case before giving up and returning [].
    """
    if isinstance(raw, list):
        return [str(v) for v in raw]
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        candidate = data.get("values") if isinstance(data, dict) else None
        if isinstance(candidate, dict):
            out: list[str] = []
            for bucket_key, bucket in candidate.items():
                if bucket_key.endswith("Values") and isinstance(bucket, list):
                    out.extend(str(v) for v in bucket)
            return out
        if isinstance(candidate, list):
            return [str(v) for v in candidate]
    return []


async def run(client: SignozMCPClient, window: AuditWindow, key_search_text: str | None = None) -> R2Result:
    stats = await fetch_field_stats(client, window, key_search_text=key_search_text)
    return evaluate(stats)
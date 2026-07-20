"""Shared types for the Guardian rule engine (Section 4.3.1).

Kept deliberately tiny -- each rule module (r1_missing_fields.py,
r2_cardinality.py, ...) defines its own Finding/Result dataclasses so a
finding from one rule can never be silently conflated with another's (the
build spec is explicit about this for R3/R6/R7; we apply the same
discipline to R1/R2 for consistency).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# signoz-mcp-server's convenience tools (aggregate_traces, search_traces, ...)
# accept a relative `timeRange` string. The *raw* `signoz_execute_builder_query`
# payload (SigNoz Query Builder v5 JSON) does not -- per SigNoz's own Trace
# API payload-model docs and the Query Builder v5 migration guide, that
# envelope only takes absolute epoch-millisecond `start`/`end` at the top
# level. This table backs the relative -> absolute conversion `fetch_spans`
# needs for that one tool.
_RANGE_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def _parse_relative_range_ms(time_range: str) -> int:
    time_range = time_range.strip()
    unit = time_range[-1]
    if unit not in _RANGE_UNIT_MS or not time_range[:-1].isdigit():
        raise ValueError(
            f"Unrecognized time_range {time_range!r} -- expected e.g. '15m', '1h', '24h'"
        )
    return int(time_range[:-1]) * _RANGE_UNIT_MS[unit]


@dataclass(frozen=True)
class AuditWindow:
    """The time window + service scope a rule audits.

    Mirrors the SigNoz MCP tools' own `timeRange` / `start` / `end` /
    `service` parameters (see signoz-mcp-server README) so callers can
    pass this straight through to `mcp_client.py` without translation.
    """

    service: str | None = None
    time_range: str | None = "1h"  # e.g. '30m', '1h', '6h', '24h' -- ignored if start/end given
    start_ms: int | None = None
    end_ms: int | None = None

    def as_mcp_kwargs(self) -> dict:
        """Return only the time/service keys the SigNoz MCP tools expect,
        omitting anything unset so tool defaults apply."""
        kwargs: dict = {}
        if self.service:
            kwargs["service"] = self.service
        if self.start_ms is not None and self.end_ms is not None:
            kwargs["start"] = self.start_ms
            kwargs["end"] = self.end_ms
        elif self.time_range:
            kwargs["time_range"] = self.time_range
        return kwargs

    def as_absolute_ms_range(self) -> tuple[int, int]:
        """Resolve this window to an absolute `(start_ms, end_ms)` pair,
        for callers (currently only `r1_missing_fields.fetch_spans`) that
        build a raw `signoz_execute_builder_query` payload rather than
        calling one of the convenience tools via `as_mcp_kwargs()`.

        If explicit `start_ms`/`end_ms` were given, those are returned
        as-is. Otherwise the relative `time_range` (e.g. '15m') is
        resolved against "now" at call time.
        """
        if self.start_ms is not None and self.end_ms is not None:
            return self.start_ms, self.end_ms
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - _parse_relative_range_ms(self.time_range or "1h")
        return start_ms, end_ms
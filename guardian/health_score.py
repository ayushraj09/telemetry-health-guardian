"""Health Score Formula (Section 4.3.6).

Pure function, same discipline as `guardian/rules/*.py::evaluate` -- no MCP,
no I/O, fully unit-testable with plain `Result` objects. `writeback.py` is
the only caller that feeds this a real `AuditFindings` from a live audit.

Field-name mapping from `guardian/narrative.py::AuditFindings` (verified
against the actual dataclass fields in each rules/*.py module -- see
r1..r6 Result docstrings):
  - R1Result.score               : float, 1.0 - (non_conformant/total) -- a
                                    *fraction*, so the formula's
                                    `missing_field_rate_pct` term is
                                    `(1 - score) * 100`, not `score` directly.
  - R2Result.cardinality_risk_score : float, already 0-100.
  - R3Result.orphaned_span_rate_pct : float, already 0-100.
  - R6Result.truncation_rate_pct    : float, already 0-100.

R7 term is only included when an `r7` result is passed in -- per spec,
"If R7 is not built, omit its term entirely from the formula (don't leave
a zero-value placeholder)." `r7` is typed `Any` (not `R7Result`) so this
module needs zero changes when Stage 7 eventually builds
`r7_cross_service_breaks.py`, same reasoning as `narrative.py`'s
`AuditFindings.r7`. Any object with a numeric `cross_service_break_rate_pct`
attribute works.

Design decision (documented, not silent): the raw formula can in theory
dip below 0 (e.g. every term maxed with an R7 penalty on top) or, with
negative inputs from a malformed Result, exceed 100. `compute` clamps the
final score to [0, 100] since it's presented as a 0-100 health score
everywhere else in the spec (Section 4.3.6's header, the dashboard panel,
the alert threshold) -- an unclamped -12 or 143 would be a confusing
number to show on a live panel. The clamp is applied only to the final
score, never to the intermediate per-rule terms, so `terms` in the result
always reflects the rule engine's real, unclamped numbers for debugging.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

R1_WEIGHT = 0.30
R2_WEIGHT = 0.25
R3_WEIGHT = 0.20
R6_WEIGHT = 0.20
R7_WEIGHT = 0.05


@dataclass(frozen=True)
class HealthScoreResult:
    service: str | None
    score: float  # clamped to [0, 100]
    raw_score: float  # unclamped, for debugging a would-be out-of-range result
    terms: dict[str, float]  # {"missing_field_rate_pct": ..., ...} -- unclamped inputs
    r7_included: bool


def compute_health_score(findings: Any) -> HealthScoreResult:
    """`findings` is a `guardian.narrative.AuditFindings` (or anything with
    the same `.service`/`.r1`/`.r2`/`.r3`/`.r6`/`.r7` shape) -- kept as
    `Any` rather than importing `AuditFindings` to avoid a hard import-time
    dependency on `narrative.py` for callers that just want the formula
    (e.g. a future test importing only this module).
    """
    r1, r2, r3, r6, r7 = findings.r1, findings.r2, findings.r3, findings.r6, getattr(findings, "r7", None)

    missing_field_rate_pct = (1.0 - r1.score) * 100.0
    cardinality_risk_score = r2.cardinality_risk_score
    orphaned_span_rate_pct = r3.orphaned_span_rate_pct
    truncation_rate_pct = r6.truncation_rate_pct

    terms: dict[str, float] = {
        "missing_field_rate_pct": missing_field_rate_pct,
        "cardinality_risk_score": cardinality_risk_score,
        "orphaned_span_rate_pct": orphaned_span_rate_pct,
        "truncation_rate_pct": truncation_rate_pct,
    }

    raw_score = (
        100.0
        - missing_field_rate_pct * R1_WEIGHT
        - cardinality_risk_score * R2_WEIGHT
        - orphaned_span_rate_pct * R3_WEIGHT
        - truncation_rate_pct * R6_WEIGHT
    )

    r7_included = r7 is not None
    if r7_included:
        cross_service_break_rate_pct = r7.cross_service_break_rate_pct
        terms["cross_service_break_rate_pct"] = cross_service_break_rate_pct
        raw_score -= cross_service_break_rate_pct * R7_WEIGHT

    score = max(0.0, min(100.0, raw_score))

    return HealthScoreResult(
        service=findings.service,
        score=score,
        raw_score=raw_score,
        terms=terms,
        r7_included=r7_included,
    )

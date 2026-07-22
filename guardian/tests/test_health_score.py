"""Unit tests for guardian/health_score.py -- pure formula, no MCP."""

from guardian.health_score import compute_health_score
from guardian.narrative import combine_results
from guardian.rules.r1_missing_fields import R1Result
from guardian.rules.r2_cardinality import R2Result
from guardian.rules.r3_orphaned_spans import R3Result
from guardian.rules.r6_silent_truncation import R6Result


def _findings(*, r1_score=1.0, r2_risk=0.0, r3_rate=0.0, r6_rate=0.0, r7=None):
    return combine_results(
        service="demo-agent-app",
        r1=R1Result(score=r1_score, total_gen_ai_spans=10, non_conformant_spans=0, findings=()),
        r2=R2Result(cardinality_risk_score=r2_risk, evaluated_keys=5, flagged_keys=0, findings=()),
        r3=R3Result(orphaned_span_rate_pct=r3_rate, total_spans_with_parent=20, orphaned_spans=0, findings=()),
        r6=R6Result(truncation_rate_pct=r6_rate, total_payload_spans=4, truncated_payload_spans=0, findings=()),
        r7=r7,
    )


def test_perfect_scores_yield_100():
    result = compute_health_score(_findings())
    assert result.score == 100.0
    assert result.raw_score == 100.0
    assert result.r7_included is False


def test_r1_score_is_a_fraction_converted_to_a_rate_before_weighting():
    # r1_score=0.5 -> missing_field_rate_pct = 50 -> penalty = 50 * 0.30 = 15
    result = compute_health_score(_findings(r1_score=0.5))
    assert result.terms["missing_field_rate_pct"] == 50.0
    assert result.score == 85.0


def test_weights_match_section_4_3_6_formula():
    result = compute_health_score(_findings(r1_score=0.0, r2_risk=100.0, r3_rate=100.0, r6_rate=100.0))
    # 100 - 100*0.30 - 100*0.25 - 100*0.20 - 100*0.20 = 100 - 95 = 5
    assert result.raw_score == 5.0
    assert result.score == 5.0


def test_score_is_clamped_to_zero_not_negative():
    # Deliberately out-of-range inputs (a malformed Result) to exercise the
    # defensive clamp documented in health_score.py -- raw_score goes
    # negative, score must not.
    result = compute_health_score(_findings(r1_score=0.0, r2_risk=100.0, r3_rate=200.0, r6_rate=100.0, r7=_FakeR7(100.0)))
    assert result.raw_score < 0
    assert result.score == 0.0
    assert result.r7_included is True
    assert result.terms["cross_service_break_rate_pct"] == 100.0


def test_r7_term_omitted_entirely_when_r7_not_built():
    result = compute_health_score(_findings())
    assert "cross_service_break_rate_pct" not in result.terms
    assert result.r7_included is False


class _FakeR7:
    """Stand-in for a not-yet-built R7Result -- only needs the one attribute
    `compute_health_score` reads, per its Any-typed r7 param."""

    def __init__(self, cross_service_break_rate_pct: float) -> None:
        self.cross_service_break_rate_pct = cross_service_break_rate_pct

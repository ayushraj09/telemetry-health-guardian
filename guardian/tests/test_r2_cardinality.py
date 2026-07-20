"""Unit tests for R2's pure `evaluate()` -- no MCP, no network, no SigNoz.

The two-condition check is the one thing the spec forbids relaxing
("Do not implement a single-condition version of this check"), so the
bulk of these tests exist to pin that down from both directions: high
cardinality alone must NOT flag, long values alone must NOT flag, only
both together may flag.
"""

from guardian.rules.r2_cardinality import FieldValueStats, evaluate


def test_healthy_low_cardinality_short_values_not_flagged():
    stats = [FieldValueStats(key="http.method", distinct_values=4, total_spans=1000, avg_value_bytes=4.0)]
    result = evaluate(stats)
    assert result.flagged_keys == 0
    assert result.cardinality_risk_score == 0.0
    assert result.findings == ()


def test_trace_id_like_field_high_cardinality_short_values_not_flagged():
    """The exact false-positive case the spec calls out by name: near-unique
    but short values (trace_id/span_id shape) must not be flagged."""
    stats = [FieldValueStats(key="internal.trace_span_ref", distinct_values=990, total_spans=1000, avg_value_bytes=16.0)]
    result = evaluate(stats)
    assert result.flagged_keys == 0


def test_long_but_low_cardinality_values_not_flagged():
    """E.g. a long-but-repeated boilerplate string (a fixed system prompt
    reused on every call) -- long values alone must not trip this."""
    stats = [FieldValueStats(key="gen_ai.system_prompt_id", distinct_values=2, total_spans=1000, avg_value_bytes=500.0)]
    result = evaluate(stats)
    assert result.flagged_keys == 0


def test_both_conditions_together_flags():
    """The actual target case: near-unique AND long -- raw content leaking
    into an indexed attribute."""
    stats = [FieldValueStats(key="tool.raw_result", distinct_values=950, total_spans=1000, avg_value_bytes=340.0)]
    result = evaluate(stats)
    assert result.flagged_keys == 1
    finding = result.findings[0]
    assert finding.rule == "R2"
    assert finding.field_key == "tool.raw_result"
    assert finding.distinct_ratio == 0.95
    assert finding.avg_value_bytes == 340.0


def test_threshold_boundaries_are_strictly_greater_than():
    # exactly at 0.8 ratio and exactly at 200 bytes -- spec says '> 0.8' and
    # '> 200 characters', not '>=', so boundary values must NOT flag.
    stats = [FieldValueStats(key="edge.exact_boundary", distinct_values=800, total_spans=1000, avg_value_bytes=200.0)]
    result = evaluate(stats)
    assert result.flagged_keys == 0

    stats_just_over = [FieldValueStats(key="edge.just_over", distinct_values=801, total_spans=1000, avg_value_bytes=200.01)]
    result_over = evaluate(stats_just_over)
    assert result_over.flagged_keys == 1


def test_zero_total_spans_is_not_evaluable_and_not_flagged():
    stats = [FieldValueStats(key="whatever", distinct_values=0, total_spans=0, avg_value_bytes=0.0)]
    result = evaluate(stats)
    assert result.evaluated_keys == 1  # still counted as "evaluated" (it was inspected)
    assert result.flagged_keys == 0


def test_cardinality_risk_score_is_percentage_of_flagged_keys():
    stats = [
        FieldValueStats(key="risky.one", distinct_values=950, total_spans=1000, avg_value_bytes=300.0),
        FieldValueStats(key="risky.two", distinct_values=960, total_spans=1000, avg_value_bytes=400.0),
        FieldValueStats(key="fine.one", distinct_values=5, total_spans=1000, avg_value_bytes=4.0),
        FieldValueStats(key="fine.two", distinct_values=900, total_spans=1000, avg_value_bytes=10.0),
    ]
    result = evaluate(stats)
    assert result.evaluated_keys == 4
    assert result.flagged_keys == 2
    assert result.cardinality_risk_score == 50.0


def test_no_keys_evaluated_gives_zero_risk_score_not_a_crash():
    result = evaluate([])
    assert result.evaluated_keys == 0
    assert result.flagged_keys == 0
    assert result.cardinality_risk_score == 0.0

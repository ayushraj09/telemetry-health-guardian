"""Unit tests for R6's pure `evaluate()` -- no MCP, no network, no SigNoz.

The spec forbids merging the two finding kinds ("must be reported as
distinct findings, never merged into one generic 'truncation' flag"), so
several tests here exist specifically to pin that down.
"""

from guardian.rules.r6_silent_truncation import (
    FINDING_KIND_MODEL_OUTPUT_TRUNCATED,
    FINDING_KIND_TOOL_PAYLOAD_TRUNCATED,
    FinishReasonLogRecord,
    PayloadSpanRecord,
    evaluate,
)


def test_no_truncation_not_flagged():
    spans = [PayloadSpanRecord(span_id="s1", trace_id="t1", span_name="fetch_and_read.read_pdf", raw_bytes=1000, captured_bytes=1000)]
    result = evaluate(spans)
    assert result.truncated_payload_spans == 0
    assert result.truncation_rate_pct == 0.0
    assert result.findings == ()


def test_within_slack_not_flagged():
    # captured=960/raw=1000 = 0.96, above the 0.95 slack threshold -- not truncation.
    spans = [PayloadSpanRecord(span_id="s1", trace_id="t1", span_name="tool", raw_bytes=1000, captured_bytes=960)]
    result = evaluate(spans)
    assert result.truncated_payload_spans == 0


def test_just_under_slack_is_flagged():
    # captured=940/raw=1000 = 0.94, below 0.95 -- genuine truncation.
    spans = [PayloadSpanRecord(span_id="s1", trace_id="t1", span_name="web_search", raw_bytes=1000, captured_bytes=940)]
    result = evaluate(spans)
    assert result.truncated_payload_spans == 1
    finding = result.findings[0]
    assert finding.rule == "R6"
    assert finding.kind == FINDING_KIND_TOOL_PAYLOAD_TRUNCATED
    assert "web_search" in finding.detail
    assert "1000" in finding.detail and "940" in finding.detail


def test_canonical_r6_case_48kb_to_700_bytes():
    spans = [PayloadSpanRecord(span_id="s1", trace_id="t1", span_name="web_search", raw_bytes=48_000, captured_bytes=700)]
    result = evaluate(spans)
    assert result.truncated_payload_spans == 1
    assert result.truncation_rate_pct == 100.0


def test_zero_raw_bytes_not_evaluable_and_not_flagged():
    spans = [PayloadSpanRecord(span_id="s1", trace_id="t1", span_name="empty_tool", raw_bytes=0, captured_bytes=0)]
    result = evaluate(spans)
    assert result.total_payload_spans == 1
    assert result.truncated_payload_spans == 0


def test_model_finish_reason_length_is_a_separate_finding_kind():
    logs = [FinishReasonLogRecord(trace_id="t1", span_id="chat-span-1", span_name="chat gpt-4o-mini")]
    result = evaluate([], finish_reason_logs=logs)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.kind == FINDING_KIND_MODEL_OUTPUT_TRUNCATED
    assert finding.trace_id == "t1"
    # finish-reason findings don't affect the payload-truncation rate/denominator
    assert result.truncation_rate_pct == 0.0
    assert result.total_payload_spans == 0


def test_co_occurring_truncation_and_finish_reason_are_two_distinct_findings_not_merged():
    """Both bugs firing in the same trace must produce two findings with
    different `kind` values -- never one generic 'truncation' finding."""
    spans = [PayloadSpanRecord(span_id="s1", trace_id="t1", span_name="web_search", raw_bytes=48_000, captured_bytes=700)]
    logs = [FinishReasonLogRecord(trace_id="t1", span_id="chat-span-1", span_name="chat gpt-4o-mini")]
    result = evaluate(spans, finish_reason_logs=logs)
    assert len(result.findings) == 2
    kinds = {f.kind for f in result.findings}
    assert kinds == {FINDING_KIND_TOOL_PAYLOAD_TRUNCATED, FINDING_KIND_MODEL_OUTPUT_TRUNCATED}


def test_truncation_rate_pct_is_percentage_of_payload_spans():
    spans = [
        PayloadSpanRecord(span_id="s1", trace_id="t1", span_name="a", raw_bytes=1000, captured_bytes=100),
        PayloadSpanRecord(span_id="s2", trace_id="t1", span_name="b", raw_bytes=1000, captured_bytes=1000),
        PayloadSpanRecord(span_id="s3", trace_id="t1", span_name="c", raw_bytes=1000, captured_bytes=1000),
        PayloadSpanRecord(span_id="s4", trace_id="t1", span_name="d", raw_bytes=1000, captured_bytes=50),
    ]
    result = evaluate(spans)
    assert result.total_payload_spans == 4
    assert result.truncated_payload_spans == 2
    assert result.truncation_rate_pct == 50.0


def test_no_payload_spans_gives_zero_rate_not_a_crash():
    result = evaluate([])
    assert result.total_payload_spans == 0
    assert result.truncated_payload_spans == 0
    assert result.truncation_rate_pct == 0.0

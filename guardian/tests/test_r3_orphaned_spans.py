"""Unit tests for R3's pure `evaluate()` -- no MCP, no network, no SigNoz."""

from guardian.rules.r3_orphaned_spans import SpanNode, TraceRecord, evaluate


def test_healthy_trace_no_orphans():
    trace = TraceRecord(
        trace_id="t1",
        spans=(
            SpanNode(span_id="root", parent_span_id=None, name="research_pipeline.run"),
            SpanNode(span_id="child1", parent_span_id="root", name="fetch_and_read.read_pdf"),
            SpanNode(span_id="grandchild1", parent_span_id="child1", name="chat gpt-4o-mini"),
        ),
    )
    result = evaluate([trace])
    assert result.orphaned_spans == 0
    assert result.orphaned_span_rate_pct == 0.0
    assert result.findings == ()
    assert result.total_spans_with_parent == 2  # root excluded, it has no parent


def test_root_span_with_no_parent_is_not_evaluable():
    trace = TraceRecord(trace_id="t1", spans=(SpanNode(span_id="root", parent_span_id=None, name="root"),))
    result = evaluate([trace])
    assert result.total_spans_with_parent == 0
    assert result.orphaned_spans == 0


def test_unresolved_parent_within_trace_is_orphaned():
    trace = TraceRecord(
        trace_id="t1",
        spans=(
            SpanNode(span_id="root", parent_span_id=None, name="root"),
            SpanNode(span_id="orphan", parent_span_id="ghost-span-id", name="fact_check_and_cite.check_claim"),
        ),
    )
    result = evaluate([trace])
    assert result.orphaned_spans == 1
    finding = result.findings[0]
    assert finding.rule == "R3"
    assert finding.span_id == "orphan"
    assert finding.missing_parent_span_id == "ghost-span-id"
    assert finding.trace_id == "t1"


def test_confirmed_elsewhere_is_a_window_edge_false_positive_not_flagged():
    """The spec's required cross-check: a parent that genuinely exists
    (just outside the audit window) must NOT be flagged."""
    trace = TraceRecord(
        trace_id="t1",
        spans=(SpanNode(span_id="child", parent_span_id="parent-outside-window", name="child"),),
    )
    result = evaluate([trace], parent_ids_confirmed_elsewhere=frozenset({"parent-outside-window"}))
    assert result.orphaned_spans == 0
    assert result.findings == ()
    # still counted in the denominator -- it was evaluated, just not flagged
    assert result.total_spans_with_parent == 1


def test_orphan_does_not_leak_across_traces():
    """A span_id that resolves in a DIFFERENT trace must not count as
    resolving this trace's dangling parent link -- orphan detection is
    strictly per-trace."""
    trace_a = TraceRecord(trace_id="a", spans=(SpanNode(span_id="span-x", parent_span_id=None, name="root-a"),))
    trace_b = TraceRecord(
        trace_id="b",
        spans=(SpanNode(span_id="child-b", parent_span_id="span-x", name="child-b"),),
    )
    result = evaluate([trace_a, trace_b])
    assert result.orphaned_spans == 1
    assert result.findings[0].trace_id == "b"


def test_orphaned_span_rate_pct_is_percentage_of_spans_with_parent():
    trace = TraceRecord(
        trace_id="t1",
        spans=(
            SpanNode(span_id="root", parent_span_id=None, name="root"),
            SpanNode(span_id="ok1", parent_span_id="root", name="ok1"),
            SpanNode(span_id="ok2", parent_span_id="root", name="ok2"),
            SpanNode(span_id="orphan1", parent_span_id="ghost1", name="orphan1"),
        ),
    )
    result = evaluate([trace])
    assert result.total_spans_with_parent == 3
    assert result.orphaned_spans == 1
    assert round(result.orphaned_span_rate_pct, 4) == round(100.0 / 3.0, 4)

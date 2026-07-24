"""Unit tests for R7's pure `evaluate()` -- no MCP, no network, no SigNoz."""

from guardian.rules.r7_cross_service_breaks import OutboundCallSpan, PeerSpanRecord, evaluate


def _call(trace_id="t1", timestamp_ms=1000, peer="citation-service", span_id="call-1"):
    return OutboundCallSpan(
        service="demo-agent-app",
        span_id=span_id,
        trace_id=trace_id,
        timestamp_ms=timestamp_ms,
        peer_service=peer,
    )


def _peer_span(trace_id, timestamp_ms, parent_span_id=None, span_id="peer-1"):
    return PeerSpanRecord(
        service="citation-service",
        span_id=span_id,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        timestamp_ms=timestamp_ms,
        name="citation_service.verify_citation",
    )


def test_correctly_propagated_handoff_is_not_a_finding():
    call = _call(trace_id="t1", timestamp_ms=1000)
    # correct baseline: B's span shares A's trace_id (traceparent forwarded)
    peer_span = _peer_span(trace_id="t1", timestamp_ms=1200, parent_span_id="call-1")

    result = evaluate([call], {"citation-service": [peer_span]})

    assert result.total_handoffs == 1
    assert result.broken_handoffs == 0
    assert result.findings == ()
    assert result.cross_service_break_rate_pct == 0.0


def test_new_root_span_in_callee_is_a_broken_handoff():
    call = _call(trace_id="t1", timestamp_ms=1000)
    # broken: a DIFFERENT trace_id shows up in B, as a root span (no parent) --
    # exactly the "traceparent not propagated" failure mode.
    peer_span = _peer_span(trace_id="t2-disconnected", timestamp_ms=1200, parent_span_id=None)

    result = evaluate([call], {"citation-service": [peer_span]})

    assert result.total_handoffs == 1
    assert result.broken_handoffs == 1
    assert result.cross_service_break_rate_pct == 100.0
    finding = result.findings[0]
    assert finding.rule == "R7"
    assert finding.caller_service == "demo-agent-app"
    assert finding.callee_service == "citation-service"
    assert finding.caller_trace_id == "t1"
    assert finding.callee_trace_id == "t2-disconnected"


def test_no_candidate_in_window_is_unevaluated_not_a_finding():
    """No span observed in B within the handoff window at all -- a slow/
    failed call or ingestion lag, not evidence of a severed trace. Must
    not be flagged, per the module's documented design decision."""
    call = _call(trace_id="t1", timestamp_ms=1000)
    peer_span = _peer_span(trace_id="t2", timestamp_ms=1000 + 5000, parent_span_id=None)  # way outside window

    result = evaluate([call], {"citation-service": [peer_span]})

    assert result.total_handoffs == 0
    assert result.broken_handoffs == 0
    assert result.findings == ()
    assert result.cross_service_break_rate_pct == 0.0


def test_no_peer_spans_for_service_at_all_is_unevaluated():
    call = _call(trace_id="t1", peer="unknown-service")
    result = evaluate([call], {})
    assert result.total_handoffs == 0
    assert result.findings == ()


def test_multiple_handoffs_only_broken_ones_are_flagged():
    healthy_call = _call(trace_id="t1", timestamp_ms=1000, span_id="call-healthy")
    broken_call = _call(trace_id="t2", timestamp_ms=2000, span_id="call-broken")
    peer_spans = [
        _peer_span(trace_id="t1", timestamp_ms=1100, parent_span_id="call-healthy", span_id="peer-healthy"),
        _peer_span(trace_id="t3-disconnected", timestamp_ms=2100, parent_span_id=None, span_id="peer-broken"),
    ]

    result = evaluate([healthy_call, broken_call], {"citation-service": peer_spans})

    assert result.total_handoffs == 2
    assert result.broken_handoffs == 1
    assert result.cross_service_break_rate_pct == 50.0
    assert result.findings[0].caller_span_id == "call-broken"


def test_r3_and_r7_findings_are_never_the_same_shape():
    """R3 findings describe a broken parent link within one trace; R7
    findings always carry two distinct trace_ids (caller vs callee) --
    structurally distinct, per the spec's 'never merge these two' rule."""
    call = _call(trace_id="t1")
    peer_span = _peer_span(trace_id="t2-disconnected", timestamp_ms=1200, parent_span_id=None)
    result = evaluate([call], {"citation-service": [peer_span]})

    finding = result.findings[0]
    assert finding.caller_trace_id != finding.callee_trace_id
    assert not hasattr(finding, "missing_parent_span_id")  # that's R3Finding's field, not R7's
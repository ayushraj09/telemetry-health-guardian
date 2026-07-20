"""Unit tests for R1's pure `evaluate()` -- no MCP, no network, no SigNoz.

These are the tests that actually stand behind Stage 3's gate check claim
("does R1 against healthy baseline data return a high score, and does
breaking one field visibly drop it") -- the fetch/adapter side can't be
exercised this way (see mcp_client.py's docstring), but the scoring math
itself is fully verifiable here.
"""

from otel_griptape.semconv import (
    GEN_AI_OPERATION_NAME,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_RESPONSE_FINISH_REASONS,
    GEN_AI_SYSTEM,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    OPERATION_CHAT,
    OPERATION_EXECUTE_TOOL,
)

from guardian.rules.r1_missing_fields import SpanRecord, evaluate


def _healthy_span(span_id: str = "s1", trace_id: str = "t1", model: str = "gpt-4o-mini") -> SpanRecord:
    return SpanRecord(
        span_id=span_id,
        trace_id=trace_id,
        name=f"{OPERATION_CHAT} {model}",
        attributes={
            GEN_AI_OPERATION_NAME: OPERATION_CHAT,
            GEN_AI_SYSTEM: "openai",
            GEN_AI_REQUEST_MODEL: model,
            GEN_AI_USAGE_INPUT_TOKENS: 100,
            GEN_AI_USAGE_OUTPUT_TOKENS: 20,
            GEN_AI_RESPONSE_FINISH_REASONS: ["stop"],
        },
    )


def test_healthy_baseline_scores_1_0():
    spans = [_healthy_span(f"s{i}") for i in range(5)]
    result = evaluate(spans)
    assert result.total_gen_ai_spans == 5
    assert result.non_conformant_spans == 0
    assert result.score == 1.0
    assert result.findings == ()


def test_empty_window_scores_1_0_not_a_false_high_score_masquerading_as_perfect():
    result = evaluate([])
    assert result.total_gen_ai_spans == 0
    assert result.score == 1.0  # trivially "clean" -- caller must check total before trusting this


def test_missing_one_required_field_drops_score_and_is_reported():
    healthy = [_healthy_span(f"s{i}") for i in range(4)]
    broken = _healthy_span("s5")
    broken.attributes.pop(GEN_AI_USAGE_OUTPUT_TOKENS)

    result = evaluate([*healthy, broken])

    assert result.total_gen_ai_spans == 5
    assert result.non_conformant_spans == 1
    assert result.score == 0.8
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule == "R1"
    assert finding.kind == "missing_field"
    assert finding.span_id == "s5"
    assert GEN_AI_USAGE_OUTPUT_TOKENS in finding.detail


def test_missing_multiple_fields_on_one_span_still_counts_as_one_non_conformant_span():
    broken = _healthy_span("s1")
    broken.attributes.pop(GEN_AI_USAGE_INPUT_TOKENS)
    broken.attributes.pop(GEN_AI_USAGE_OUTPUT_TOKENS)

    result = evaluate([broken])

    assert result.total_gen_ai_spans == 1
    assert result.non_conformant_spans == 1  # one span, not double-counted per missing field
    assert len(result.findings) == 2  # but both missing fields are individually reported
    assert {f.detail for f in result.findings} == {
        f"missing required attribute '{GEN_AI_USAGE_INPUT_TOKENS}'",
        f"missing required attribute '{GEN_AI_USAGE_OUTPUT_TOKENS}'",
    }


def test_naming_convention_violation_flagged_even_with_all_fields_present():
    span = _healthy_span("s1")
    object.__setattr__(span, "name", "wrong-span-name")  # frozen dataclass, override for the test

    result = evaluate([span])

    assert result.non_conformant_spans == 1
    assert result.score == 0.0
    kinds = {f.kind for f in result.findings}
    assert kinds == {"naming_convention"}
    assert "wrong-span-name" in result.findings[0].detail
    assert f"{OPERATION_CHAT} gpt-4o-mini" in result.findings[0].detail


def test_tool_call_spans_are_excluded_from_r1_entirely():
    """execute_tool spans never carry the 5 REQUIRED_GEN_AI_FIELDS by
    design (they carry gen_ai.tool.name instead) -- R1 must not flag them."""
    tool_span = SpanRecord(
        span_id="tool1",
        trace_id="t1",
        name=f"{OPERATION_EXECUTE_TOOL} web_search",
        attributes={GEN_AI_OPERATION_NAME: OPERATION_EXECUTE_TOOL, "gen_ai.tool.name": "web_search"},
    )
    result = evaluate([tool_span])
    assert result.total_gen_ai_spans == 0
    assert result.score == 1.0
    assert result.findings == ()


def test_missing_field_and_naming_violation_on_same_span_produce_two_findings_one_non_conformant_span():
    span = _healthy_span("s1")
    span.attributes.pop(GEN_AI_SYSTEM)
    object.__setattr__(span, "name", "totally-wrong")

    result = evaluate([span])

    assert result.non_conformant_spans == 1
    kinds = sorted(f.kind for f in result.findings)
    assert kinds == ["missing_field", "naming_convention"]

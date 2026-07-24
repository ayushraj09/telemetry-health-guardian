"""Unit tests for guardian/narrative.py's pure parts (Section 4.3.2):
`combine_results` / `to_json_dict`, `build_prompt` / `build_chat_prompt`,
and `validate_citations`. No MCP, no real LLM call.

`generate_report` / `answer_question` are thin wrappers around
`llm_client.generate` (already covered by test_llm_client.py's mocked
completion tests) -- they get one light integration test here confirming
they actually reach `llm_client.generate` with a prompt built from the
findings, not their own re-tested request-shaping.
"""

import json

from guardian import llm_client
from guardian.narrative import (
    AuditFindings,
    answer_question,
    build_chat_prompt,
    build_prompt,
    combine_results,
    fired_rule_ids,
    generate_report,
    probable_fixes,
    validate_citations,
)
from guardian.rules.r1_missing_fields import R1Result
from guardian.rules.r2_cardinality import R2Result
from guardian.rules.r3_orphaned_spans import R3Finding, R3Result
from guardian.rules.r6_silent_truncation import (
    FINDING_KIND_TOOL_PAYLOAD_TRUNCATED,
    R6Finding,
    R6Result,
)


def _clean_findings() -> AuditFindings:
    return combine_results(
        service="demo-agent-app",
        r1=R1Result(score=1.0, total_gen_ai_spans=10, non_conformant_spans=0, findings=()),
        r2=R2Result(cardinality_risk_score=0.0, evaluated_keys=5, flagged_keys=0, findings=()),
        r3=R3Result(orphaned_span_rate_pct=0.0, total_spans_with_parent=20, orphaned_spans=0, findings=()),
        r6=R6Result(truncation_rate_pct=0.0, total_payload_spans=4, truncated_payload_spans=0, findings=()),
    )


def _dirty_findings() -> AuditFindings:
    r6_finding = R6Finding(
        kind=FINDING_KIND_TOOL_PAYLOAD_TRUNCATED,
        trace_id="t1",
        span_id="s1",
        span_name="fetch_and_read.read_pdf",
        detail=(
            "R6 fired: 'fetch_and_read.read_pdf' returned 26737B but only "
            "704B reached the agent's context."
        ),
    )
    r3_finding = R3Finding(
        trace_id="t2",
        span_id="s9",
        span_name="check_claim",
        missing_parent_span_id="bogus",
        detail="span 'check_claim' (s9) in trace t2 has parent_span_id=bogus which does not resolve",
    )
    return combine_results(
        service="demo-agent-app",
        r1=R1Result(score=1.0, total_gen_ai_spans=10, non_conformant_spans=0, findings=()),
        r2=R2Result(cardinality_risk_score=0.0, evaluated_keys=5, flagged_keys=0, findings=()),
        r3=R3Result(orphaned_span_rate_pct=9.8, total_spans_with_parent=51, orphaned_spans=1, findings=(r3_finding,)),
        r6=R6Result(truncation_rate_pct=25.0, total_payload_spans=4, truncated_payload_spans=1, findings=(r6_finding,)),
    )


# -- combine_results / to_json_dict: pure --------------------------------


def test_to_json_dict_is_json_serializable_and_carries_scores():
    findings = _dirty_findings()
    payload = findings.to_json_dict()

    json.dumps(payload)  # must not raise

    assert payload["service"] == "demo-agent-app"
    assert payload["r6"]["truncation_rate_pct"] == 25.0
    assert payload["r3"]["orphaned_span_rate_pct"] == 9.8
    assert len(payload["r6"]["findings"]) == 1
    assert payload["r6"]["findings"][0]["rule"] == "R6"
    assert payload["r6"]["findings"][0]["kind"] == FINDING_KIND_TOOL_PAYLOAD_TRUNCATED


def test_to_json_dict_omits_r7_key_when_r7_not_built():
    findings = _clean_findings()
    payload = findings.to_json_dict()

    assert "r7" not in payload  # no placeholder for a rule that isn't built yet


def test_to_json_dict_clean_findings_have_empty_findings_lists():
    findings = _clean_findings()
    payload = findings.to_json_dict()

    assert payload["r1"]["findings"] == []
    assert payload["r2"]["findings"] == []
    assert payload["r3"]["findings"] == []
    assert payload["r6"]["findings"] == []


# -- build_prompt / build_chat_prompt: pure -------------------------------


def test_build_prompt_embeds_every_finding_detail_and_rule_id():
    findings = _dirty_findings()
    prompt = build_prompt(findings)

    assert "R6" in prompt
    assert "fetch_and_read.read_pdf" in prompt
    assert "R3" in prompt
    assert "check_claim" in prompt
    assert "2 total findings" in prompt


def test_build_prompt_on_clean_findings_reports_zero_findings():
    findings = _clean_findings()
    prompt = build_prompt(findings)

    assert "0 total findings" in prompt


def test_build_chat_prompt_includes_question_and_findings():
    findings = _dirty_findings()
    prompt = build_chat_prompt(findings, "why did the score drop?")

    assert "why did the score drop?" in prompt
    assert "R6" in prompt
    assert "fetch_and_read.read_pdf" in prompt


# -- validate_citations: pure, best-effort post-hoc check ------------------


def test_validate_citations_flags_uncited_fired_rule():
    findings = _dirty_findings()
    narrative_missing_r3 = "R6 fired: fetch_and_read.read_pdf's payload was truncated."

    missing = validate_citations(narrative_missing_r3, findings)

    assert missing == ["R3"]


def test_validate_citations_empty_when_every_fired_rule_named():
    findings = _dirty_findings()
    narrative = "R6 fired on fetch_and_read.read_pdf. R3 fired on check_claim."

    assert validate_citations(narrative, findings) == []


def test_validate_citations_ignores_rules_with_no_findings():
    findings = _clean_findings()

    assert validate_citations("everything looks healthy, no issues found", findings) == []


def test_fired_rule_ids_returns_canonical_order():
    assert fired_rule_ids(_dirty_findings()) == ["R3", "R6"]


def test_probable_fixes_only_returns_fired_rules():
    fixes = probable_fixes(_dirty_findings())

    assert sorted(fixes) == ["R3", "R6"]
    assert "context" in fixes["R3"]
    assert "payload" in fixes["R6"]


# -- generate_report / answer_question: mocked-completion integration ------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


def test_generate_report_reaches_llm_client_with_findings_grounded_prompt(monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _FakeResponse("R6 fired: fetch_and_read.read_pdf truncated. R3 fired: check_claim orphaned.")

    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)

    findings = _dirty_findings()
    report = generate_report(findings)

    assert report == "R6 fired: fetch_and_read.read_pdf truncated. R3 fired: check_claim orphaned."
    user_message = captured["messages"][-1]["content"]
    assert "fetch_and_read.read_pdf" in user_message
    assert "check_claim" in user_message
    system_message = captured["messages"][0]["content"]
    assert "rule ID" in system_message


def test_answer_question_reaches_llm_client_with_question_and_findings(monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _FakeResponse("R6 fired on fetch_and_read.read_pdf, which is why the score dropped.")

    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)

    findings = _dirty_findings()
    answer = answer_question(findings, "why did the score drop?")

    assert "R6" in answer
    user_message = captured["messages"][-1]["content"]
    assert "why did the score drop?" in user_message

"""Guardian LLM Reasoning Layer (Section 4.3.2).

Takes the rule engine's (R1/R2/R3/R6[/R7]) structured `Result` objects and
produces the human-facing output: a natural-language root-cause narrative,
prioritization across co-occurring issues, and answers to free-form chat
questions.

Same pure/adapter discipline as `guardian/rules/*.py` and `llm_client.py`:
  - `combine_results` / `AuditFindings.to_json_dict`: pure. Assembles the
    rule engine's per-rule Result objects into the one combined,
    JSON-serializable structure the spec calls "the rule engine's combined
    JSON output" -- fully unit-testable with plain Result objects, no MCP
    or LLM involved.
  - `build_prompt` / `build_chat_prompt`: pure prompt construction. Also
    fully unit-testable: assert every finding's rule ID and offending
    span/attribute text actually appears in the built prompt, since a
    prompt that omits a finding's specifics can't possibly produce a
    narrative that cites it correctly.
  - `generate_report` / `answer_question`: the LLM-calling adapters, thin
    wrappers around `llm_client.generate`. Not independently retested here
    beyond a light mocked-completion integration check -- the request
    shape itself is already covered by `test_llm_client.py`.

Required behavior, verbatim from spec (4.3.2): "every narrative sentence
about a finding must name the specific rule that fired and the specific
offending span/attribute -- e.g. 'R6 fired: tool `web_search` returned 48KB
but only 700 bytes reached the agent's context.' Generic language like
'something seems truncated' without naming the rule ID is not acceptable
output." This is enforced two ways here: (1) `SYSTEM_PROMPT` states the
requirement explicitly, using that exact worked example, and embeds the
full findings JSON so the model has real span names/attributes to cite
rather than needing to invent them; (2) `validate_citations` is a
best-effort *post-hoc* check a caller can run against the LLM's actual
output. (1) can't be perfectly enforced -- LLM output isn't constrained
the way `evaluate()`'s pure detection logic is -- so (2) exists as a
signal to log or retry on, not a guarantee.

Design decision (documented, not silent): R3 vs R6 must never be
conflated (Section 4.3.1's requirement for R6's two finding kinds applies
equally here) -- `SYSTEM_PROMPT` calls this out by name rather than
leaving it implicit, since it's the one pair of rules in this project the
spec repeatedly stresses must stay distinguishable in every output layer,
not just the rule engine's own findings list.

Design decision (documented, not silent): `AuditFindings.r7` is typed
`Any | None` rather than importing an `R7Result` that doesn't exist yet
(`r7_cross_service_breaks.py` isn't created until Stage 7, per Section 6 --
importing it here would force that file into existence early). `_result_to_dict`
is written generically (reads `.findings` / a named score attribute off
*any* Result-shaped object via `getattr`) specifically so this module
needs zero changes when R7 is eventually built -- a caller just starts
passing an `r7=` value into `combine_results`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any

from guardian.llm_client import generate
from guardian.rules.r1_missing_fields import R1Result
from guardian.rules.r2_cardinality import R2Result
from guardian.rules.r3_orphaned_spans import R3Result
from guardian.rules.r6_silent_truncation import R6Result

SYSTEM_PROMPT = (
    "You are the Telemetry Health Guardian's reasoning layer. You audit "
    "whether an AI agent's OpenTelemetry telemetry can be trusted -- you do "
    "not evaluate or narrate the agent's own behavior, only its "
    "instrumentation's integrity.\n\n"
    "You will be given the rule engine's structured findings as JSON. "
    "Required behavior, non-negotiable: every sentence you write about a "
    "specific finding MUST name the rule ID that fired (e.g. 'R6') and the "
    "specific offending span name or attribute, drawn only from the JSON "
    "given -- never invent one. Required style, verbatim example: "
    '"R6 fired: tool `web_search` returned 48KB but only 700 bytes reached '
    'the agent\'s context." Generic language like "something seems '
    'truncated" without the rule ID is not acceptable output.\n\n'
    "R3 (orphaned spans within one service's trace tree) and R6 (silent "
    "truncation -- either a tool payload or the model's own output hitting "
    "its token limit, which are themselves two distinct R6 finding kinds) "
    "are different bugs. Never describe two different findings, or two "
    "different kinds of the same rule, with one merged sentence.\n\n"
    "If a report covers more than one finding, prioritize by severity -- "
    "lower scores / higher rates first -- and say which one to fix first "
    "and why. If a rule's findings list is empty, say plainly that it's "
    "clean; do not invent a problem for it."
)


@dataclass(frozen=True)
class AuditFindings:
    """Combined rule-engine output for one audit run -- Section 4.3.2's
    "the rule engine's combined JSON output," assembled by
    `combine_results`. This is also the shape a future
    `GET /audit/report/{service}` endpoint (Section 4.3.5, not yet built)
    would hand back as-is via `to_json_dict()`.
    """

    service: str | None
    r1: R1Result
    r2: R2Result
    r3: R3Result
    r6: R6Result
    r7: Any | None = None  # r7_cross_service_breaks.R7Result, once Stage 7 builds it

    def to_json_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "service": self.service,
            "r1": _result_to_dict(self.r1, score_field="score"),
            "r2": _result_to_dict(self.r2, score_field="cardinality_risk_score"),
            "r3": _result_to_dict(self.r3, score_field="orphaned_span_rate_pct"),
            "r6": _result_to_dict(self.r6, score_field="truncation_rate_pct"),
        }
        if self.r7 is not None:
            out["r7"] = _result_to_dict(self.r7, score_field=None)
        return out


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """`{field.name: getattr(obj, field.name) ...}` over `dataclasses.fields(obj)`,
    NOT `vars(obj)`/`obj.__dict__`.

    Bug found and fixed here (2026-07-22): every `*Finding` dataclass
    (R1Finding, R2Finding, R3Finding, R6Finding) declares its `rule` field
    as `field(default=RULE_ID, init=False)`. For a field like that -- a
    plain (non-factory) default combined with `init=False` -- the
    generated `__init__` never assigns it on the instance at all; it's
    served as a *class* attribute via normal attribute lookup instead.
    `vars(f)` / `f.__dict__` only reflects instance attributes, so it
    silently omitted `rule` from every finding's flattened dict here,
    while `f.rule` itself worked fine. `dataclasses.fields()` + `getattr()`
    (same approach `dataclasses.asdict()` uses internally) reads through
    that class-attribute fallback correctly. Caught by
    `test_narrative.py::test_to_json_dict_is_json_serializable_and_carries_scores`
    against a real R6Finding once tests were actually run (this sandbox
    can't run pytest -- see repo-wide honesty notes on that).
    """
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


def _result_to_dict(result: Any, score_field: str | None) -> dict[str, Any]:
    """Generic, dataclass-agnostic flattening of one rule Result (+ its
    `findings` tuple) into plain JSON-safe types. Written once, generically,
    rather than as four near-identical per-rule flatteners, since every
    rule Result already shares the same "some summary fields + a findings
    tuple of small frozen dataclasses" shape (see module docstring)."""
    findings = [_dataclass_to_dict(f) for f in getattr(result, "findings", ())]
    out: dict[str, Any] = {
        f.name: getattr(result, f.name)
        for f in fields(result)
        if f.name != "findings"
    }
    out["findings"] = findings
    if score_field is not None:
        out.setdefault(score_field, getattr(result, score_field, None))
    return out


def combine_results(
    service: str | None,
    r1: R1Result,
    r2: R2Result,
    r3: R3Result,
    r6: R6Result,
    r7: Any | None = None,
) -> AuditFindings:
    """Pure assembly -- see `AuditFindings` docstring."""
    return AuditFindings(service=service, r1=r1, r2=r2, r3=r3, r6=r6, r7=r7)


def _total_finding_count(findings: AuditFindings) -> int:
    payload = findings.to_json_dict()
    return sum(len(v["findings"]) for k, v in payload.items() if isinstance(v, dict) and "findings" in v)


def build_prompt(findings: AuditFindings) -> str:
    """Pure prompt construction for the standing narrative report. Embeds
    the combined findings JSON directly so the model has everything it
    needs to satisfy the citation requirement without inventing details."""
    payload = json.dumps(findings.to_json_dict(), indent=2, default=str)
    rules_present = "R1/R2/R3/R6" + ("/R7" if findings.r7 is not None else "")
    return (
        f"Rule engine findings for service {findings.service or '(all services)'} "
        f"({_total_finding_count(findings)} total findings across {rules_present}):\n\n"
        f"```json\n{payload}\n```\n\n"
        "Write a short natural-language telemetry health report: one sentence "
        "per finding (or per closely related group of findings from the same "
        "rule and kind), each naming its rule ID and the offending span/"
        "attribute, followed by a one-sentence prioritization of which issue "
        "to fix first and why."
    )


def build_chat_prompt(findings: AuditFindings, question: str) -> str:
    """Pure prompt construction for the free-form chat case (Section
    4.3.2's "answers to free-form chat questions"). Same grounding + same
    citation requirement as `build_prompt`, phrased as a question-answer
    task instead of a standing report."""
    payload = json.dumps(findings.to_json_dict(), indent=2, default=str)
    return (
        f"Rule engine findings for service {findings.service or '(all services)'}:\n\n"
        f"```json\n{payload}\n```\n\n"
        f"Answer this question about the findings above: {question}\n\n"
        "Ground your answer only in the JSON given -- name the specific rule "
        "ID(s) and span/attribute(s) involved. If the JSON doesn't contain "
        "enough information to answer, say so plainly instead of guessing."
    )


def generate_report(findings: AuditFindings, provider: str | None = None) -> str:
    """LLM-calling adapter for the standing narrative report (Section
    4.3.2). `provider` overrides `LLM_PROVIDER` for this call only --
    used by the Stage 5 dual-provider gate check to request both
    providers against the same `findings` in one process."""
    return generate(build_prompt(findings), system=SYSTEM_PROMPT, provider=provider)


def answer_question(findings: AuditFindings, question: str, provider: str | None = None) -> str:
    """LLM-calling adapter for the free-form chat case (Section 4.3.2)."""
    return generate(build_chat_prompt(findings, question), system=SYSTEM_PROMPT, provider=provider)


def validate_citations(narrative: str, findings: AuditFindings) -> list[str]:
    """Best-effort post-hoc check (see module docstring -- not a hard
    guarantee): which rule IDs that actually fired (have >=1 finding) are
    never mentioned anywhere in `narrative`.

    An empty list means every rule that fired got at least one citation.
    A non-empty list is a signal a caller can log or retry the generation
    on -- not proof the narrative is wrong (one sentence can legitimately
    cover several findings from the same rule)."""
    fired_rule_ids = [
        rule_id
        for rule_id, result in (
            ("R1", findings.r1),
            ("R2", findings.r2),
            ("R3", findings.r3),
            ("R6", findings.r6),
        )
        if getattr(result, "findings", ())
    ]
    if findings.r7 is not None and getattr(findings.r7, "findings", ()):
        fired_rule_ids.append("R7")

    return [rule_id for rule_id in fired_rule_ids if rule_id not in narrative]
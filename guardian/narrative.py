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

# Rule ID -> the class of code-level cause that typically produces that
# rule's finding, in this specific codebase. This exists because the LLM
# only ever sees the rule engine's JSON output, never demo-agent-app's or
# otel-griptape's actual source -- without this table it can only restate
# the finding, not suggest where to look. Keep each hint one sentence,
# pointed at a real file/mechanism (not "check your instrumentation"),
# and update it if a rule's real-world cause set changes.
RULE_FIX_HINTS: dict[str, str] = {
    "R1": (
        "A required token-usage field is missing on a chat span. Check the "
        "instrumentor call order in otel_griptape -- a field dropped here "
        "usually means set_attribute ran before the usage value was "
        "resolved, or a chaos-mode fault (chaos.py's R1 path) is installed."
    ),
    "R2": (
        "Raw document/tool content was attached as an indexed span "
        "attribute instead of an event. Look at the call site right after "
        "extraction (e.g. fetch_and_read.py's fetch_and_read.read_pdf "
        "span) for a set_attribute call carrying full text -- it should "
        "use otel-griptape's event-based capture instead."
    ),
    "R3": (
        "A span's parent_span_id doesn't resolve to any span in the same "
        "trace. Look at whichever call schedules work onto a thread pool "
        "or async task (e.g. fact_check_and_cite.py's parallel claim "
        "checks) for a context that wasn't attach()'d before submission."
    ),
    "R6": (
        "Less context reached the model than was actually extracted. "
        "Check which backend handled this claim-check call and its "
        "context-window size (ollama_r6.py's num_ctx is the usual "
        "suspect) -- the gap is a real context-window truncation, not a "
        "fixed character slice."
    ),
    "R7": (
        "One trace silently became two. Check the outbound HTTP call just "
        "before this span (e.g. fact_check_and_cite.py's citation fetch) "
        "for a missing W3C traceparent header -- it should be injected via "
        "otel_griptape.context_propagation.inject_traceparent_header."
    ),
}


def _fix_hints_block(fired_rule_ids: list[str]) -> str:
    """Only the hints for rules that actually fired -- no point grounding
    the model in remediation advice for a clean rule."""
    if not fired_rule_ids:
        return ""
    lines = [f"- {rid}: {RULE_FIX_HINTS[rid]}" for rid in fired_rule_ids if rid in RULE_FIX_HINTS]
    if not lines:
        return ""
    return (
        "\n\nReference -- likely code-level cause per rule (use this to "
        "suggest a probable fix; don't invent a cause not grounded here or "
        "in the findings JSON):\n" + "\n".join(lines)
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


def fired_rule_ids(findings: AuditFindings) -> list[str]:
    """Which rule IDs have >=1 finding, in canonical order. Shared by
    `build_chat_prompt` (to state the required-coverage list explicitly),
    `validate_citations` (to check the model actually met it), and
    `probable_fixes` (to know which hints to surface). Public: the API
    layer (main.py) also needs this to build a deterministic response
    field, not just this module internally."""
    fired = [
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
        fired.append("R7")
    return fired


def build_chat_prompt(findings: AuditFindings, question: str) -> str:
    """Pure prompt construction for the free-form chat case (Section
    4.3.2's "answers to free-form chat questions"). Same grounding + same
    citation requirement as `build_prompt`, phrased as a question-answer
    task instead of a standing report.

    Design decision (documented, not silent): causal questions like "why
    did the score drop?" invite the model to reach for the single most
    severe finding and stop there -- observed in practice, where a chat
    answer covered only R6 (the most urgent rule per the standing report)
    and silently dropped R1/R2/R3 even though all four fired. Naming the
    exact required rule ID list below, computed from the same findings
    data rather than left for the model to infer, closes that gap: the
    model can't reach "just the worst one" as a good-faith reading of an
    explicit checklist the way it can with an open-ended instruction to
    "ground your answer in the JSON."
    """
    payload = json.dumps(findings.to_json_dict(), indent=2, default=str)
    fired = fired_rule_ids(findings)
    if fired:
        coverage_instruction = (
            f"The following rules fired this audit and have at least one finding: "
            f"{', '.join(fired)}. Your answer MUST mention every one of these rule "
            "IDs at least once, even if the question seems to point at only one "
            "issue -- multiple distinct problems can all contribute to the same "
            "score drop, and omitting any of them is an incomplete answer. Do not "
            "silently focus on only the most severe rule."
        )
    else:
        coverage_instruction = (
            "No rules fired this audit (all findings lists are empty) -- say so "
            "plainly rather than inventing an issue."
        )
    fix_instruction = (
        "\n\nFor every rule you cite, also suggest a probable fix as a "
        "separate short sentence (don't merge it into the diagnosis "
        "sentence) -- ground it in the reference hints below, and say so "
        "plainly if a fired rule has no matching hint rather than "
        "inventing one." if fired else ""
    )
    return (
        f"Rule engine findings for service {findings.service or '(all services)'}:\n\n"
        f"```json\n{payload}\n```\n\n"
        f"Answer this question about the findings above: {question}\n\n"
        "Ground your answer only in the JSON given -- name the specific rule "
        "ID(s) and span/attribute(s) involved. If the JSON doesn't contain "
        "enough information to answer, say so plainly instead of guessing.\n\n"
        f"{coverage_instruction}"
        f"{fix_instruction}"
        f"{_fix_hints_block(fired)}"
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
    return [rule_id for rule_id in fired_rule_ids(findings) if rule_id not in narrative]


def probable_fixes(findings: AuditFindings) -> dict[str, str]:
    """Deterministic rule_id -> fix hint for every rule that fired this
    audit, straight from RULE_FIX_HINTS -- no LLM call involved.

    `build_chat_prompt`'s fix_instruction asks the model to weave a fix
    suggestion into its prose, which is good for a readable chat answer,
    but prose compliance isn't guaranteed the way a rule engine's own
    detection logic is (see this module's docstring on why (2)-style
    post-hoc checks exist alongside (1)-style prompt instructions). This
    function is the (2)-style guarantee for fixes specifically: a caller
    (the `/chat` API layer) can always attach a reliable fix list to the
    response regardless of what the LLM actually wrote, and a frontend
    can render it as its own "probable fix" panel instead of parsing the
    LLM's prose for one.

    A fired rule with no entry in RULE_FIX_HINTS is simply omitted here --
    never fabricate a hint that isn't in the table."""
    return {
        rule_id: RULE_FIX_HINTS[rule_id]
        for rule_id in fired_rule_ids(findings)
        if rule_id in RULE_FIX_HINTS
    }
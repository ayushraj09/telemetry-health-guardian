"""Unit tests for guardian/scheduler.py (Section 4.3.5, Stage 7).

No real MCP session and no real LLM call, same discipline as
test_llm_client.py/test_narrative.py: `SignozMCPClient` and each rule
module's `run()` are monkeypatched, and `narrative.generate_report` is
monkeypatched too. What's actually under test is scheduler.py's own
orchestration -- does it call the right sequence, combine/score/cache
correctly, and degrade gracefully when the narrative step fails -- not
whether MCP or an LLM provider works (already covered elsewhere).
"""

from __future__ import annotations

import pytest

from guardian import scheduler
from guardian.rules.r1_missing_fields import R1Result
from guardian.rules.r2_cardinality import R2Result
from guardian.rules.r3_orphaned_spans import R3Result
from guardian.rules.r6_silent_truncation import R6Result
from guardian.rules.r7_cross_service_breaks import R7Result


def _clean_result_set() -> dict:
    return dict(
        r1=R1Result(score=1.0, total_gen_ai_spans=10, non_conformant_spans=0, findings=()),
        r2=R2Result(cardinality_risk_score=0.0, evaluated_keys=5, flagged_keys=0, findings=()),
        r3=R3Result(orphaned_span_rate_pct=0.0, total_spans_with_parent=20, orphaned_spans=0, findings=()),
        r6=R6Result(truncation_rate_pct=0.0, total_payload_spans=4, truncated_payload_spans=0, findings=()),
        r7=R7Result(cross_service_break_rate_pct=0.0, total_handoffs=2, broken_handoffs=0, findings=()),
    )


class _FakeMCPClient:
    """Stands in for `async with SignozMCPClient() as client:` -- the
    object handed to `client` is never actually used by the monkeypatched
    rule `.run()` functions below, so it doesn't need to do anything.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch_mcp_and_rules(monkeypatch, results: dict | None = None):
    results = results or _clean_result_set()
    monkeypatch.setattr(scheduler, "SignozMCPClient", _FakeMCPClient)

    async def _r1_run(client, window):
        return results["r1"]

    async def _r2_run(client, window):
        return results["r2"]

    async def _r3_run(client, window):
        return results["r3"]

    async def _r6_run(client, window):
        return results["r6"]

    async def _r7_run(client, window):
        return results["r7"]

    monkeypatch.setattr(scheduler.r1_missing_fields, "run", _r1_run)
    monkeypatch.setattr(scheduler.r2_cardinality, "run", _r2_run)
    monkeypatch.setattr(scheduler.r3_orphaned_spans, "run", _r3_run)
    monkeypatch.setattr(scheduler.r6_silent_truncation, "run", _r6_run)
    monkeypatch.setattr(scheduler.r7_cross_service_breaks, "run", _r7_run)


# -- service_key ----------------------------------------------------------


def test_service_key_none_is_all_services_bucket():
    assert scheduler.service_key(None) == "_all_"


def test_service_key_passes_through_named_service():
    assert scheduler.service_key("demo-agent-app") == "demo-agent-app"


# -- AuditStore -------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_store_round_trip():
    store = scheduler.AuditStore()
    assert await store.get("demo-agent-app") is None

    result = scheduler.AuditCycleResult(
        findings=None, health=None, narrative="clean", narrative_error=None
    )
    await store.set("demo-agent-app", result)

    assert await store.get("demo-agent-app") is result
    assert (await store.all())["demo-agent-app"] is result


@pytest.mark.asyncio
async def test_audit_store_keys_none_service_as_all():
    store = scheduler.AuditStore()
    result = scheduler.AuditCycleResult(findings=None, health=None, narrative=None, narrative_error=None)

    await store.set(None, result)

    assert await store.get(None) is result
    assert (await store.all())["_all_"] is result


# -- run_audit_cycle --------------------------------------------------------


@pytest.mark.asyncio
async def test_run_audit_cycle_combines_scores_and_caches(monkeypatch):
    _patch_mcp_and_rules(monkeypatch)

    def fake_generate_report(findings, provider=None):
        return f"narrative for {findings.service}"

    monkeypatch.setattr(scheduler, "generate_report", fake_generate_report)

    store = scheduler.AuditStore()
    result = await scheduler.run_audit_cycle("demo-agent-app", store)

    assert result.findings.service == "demo-agent-app"
    assert result.health.score == pytest.approx(100.0)
    assert result.narrative == "narrative for demo-agent-app"
    assert result.narrative_error is None
    assert (await store.get("demo-agent-app")) is result


@pytest.mark.asyncio
async def test_run_audit_cycle_narrative_failure_does_not_fail_the_cycle(monkeypatch):
    _patch_mcp_and_rules(monkeypatch)

    def fake_generate_report(findings, provider=None):
        raise RuntimeError("no LLM_PROVIDER configured")

    monkeypatch.setattr(scheduler, "generate_report", fake_generate_report)

    store = scheduler.AuditStore()
    result = await scheduler.run_audit_cycle(None, store)

    assert result.narrative is None
    assert result.narrative_error == "no LLM_PROVIDER configured"
    # the audit itself (scoring + caching) still completed despite the
    # narrative step failing -- this is the module docstring's explicit
    # design decision, not an accident.
    assert result.health.score == pytest.approx(100.0)
    assert (await store.get(None)) is result


@pytest.mark.asyncio
async def test_run_audit_cycle_skips_narrative_when_disabled(monkeypatch):
    calls = []
    _patch_mcp_and_rules(monkeypatch)
    monkeypatch.setattr(scheduler, "generate_report", lambda *a, **k: calls.append(1) or "unused")

    store = scheduler.AuditStore()
    result = await scheduler.run_audit_cycle(None, store, generate_narrative=False)

    assert result.narrative is None
    assert result.narrative_error is None
    assert calls == []


@pytest.mark.asyncio
async def test_run_audit_cycle_calls_writeback_when_provided(monkeypatch):
    _patch_mcp_and_rules(monkeypatch)
    monkeypatch.setattr(scheduler, "generate_report", lambda *a, **k: "ok")

    calls = []

    class _FakeWriteback:
        def write_audit_result(self, findings, health):
            calls.append((findings, health))

    store = scheduler.AuditStore()
    await scheduler.run_audit_cycle(None, store, writeback=_FakeWriteback())

    assert len(calls) == 1


# -- configured_services -----------------------------------------------------


def test_configured_services_defaults_to_all_services_scope(monkeypatch):
    monkeypatch.delenv("AUDIT_SERVICES", raising=False)
    assert scheduler.configured_services() == [None]


def test_configured_services_parses_comma_separated_list(monkeypatch):
    monkeypatch.setenv("AUDIT_SERVICES", "demo-agent-app, citation-service")
    assert scheduler.configured_services() == ["demo-agent-app", "citation-service"]


# -- build_scheduler ----------------------------------------------------------


def test_build_scheduler_registers_the_audit_loop_job(monkeypatch):
    monkeypatch.setenv("AUDIT_INTERVAL_MINUTES", "5")
    store = scheduler.AuditStore()

    sched = scheduler.build_scheduler(store, writeback=None, services=["demo-agent-app"])

    job = sched.get_job("guardian-audit-loop")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 5 * 60

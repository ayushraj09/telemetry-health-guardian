"""Unit tests for guardian/main.py's FastAPI routes (Section 4.3.5, Stage 7).

No real MCP session, no real LLM call, and no real OTLP export --
`guardian.main.run_audit_cycle`/`HealthWriteback`/`build_scheduler`/alert
provisioning are all monkeypatched at the module level `main.py` imports
them into, same pattern `test_scheduler.py` uses for `scheduler.py`'s own
MCP/rule-module calls. What's under test here is route wiring, request/
response shaping, and error-status mapping -- not the audit pipeline
itself (covered by test_scheduler.py / test_health_score.py / the
per-rule test files).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from guardian import main as guardian_main
from guardian.health_score import compute_health_score
from guardian.mcp_client import SignozMCPError
from guardian.narrative import combine_results
from guardian.rules.r1_missing_fields import R1Result
from guardian.rules.r2_cardinality import R2Result
from guardian.rules.r3_orphaned_spans import R3Result
from guardian.rules.r6_silent_truncation import R6Result
from guardian.scheduler import AuditCycleResult


class _FakeWriteback:
    def __init__(self, *a, **k):
        self.flushed = False

    def flush(self):
        self.flushed = True


class _FakeScheduler:
    def __init__(self):
        self.started = False
        self.shutdown_called = False

    def start(self):
        self.started = True

    def shutdown(self, wait: bool = True):
        self.shutdown_called = True


def _clean_result(service: str | None = "demo-agent-app") -> AuditCycleResult:
    findings = combine_results(
        service=service,
        r1=R1Result(score=1.0, total_gen_ai_spans=10, non_conformant_spans=0, findings=()),
        r2=R2Result(cardinality_risk_score=0.0, evaluated_keys=5, flagged_keys=0, findings=()),
        r3=R3Result(orphaned_span_rate_pct=0.0, total_spans_with_parent=20, orphaned_spans=0, findings=()),
        r6=R6Result(truncation_rate_pct=0.0, total_payload_spans=4, truncated_payload_spans=0, findings=()),
    )
    health = compute_health_score(findings)
    return AuditCycleResult(findings=findings, health=health, narrative="all clean", narrative_error=None)


@pytest.fixture()
def client(monkeypatch):
    """A TestClient with process-lifecycle side effects (OTLP writeback,
    alert provisioning, the background scheduler) stubbed out, so
    `HealthWriteback()`'s real constructor -- which requires
    `OTEL_EXPORTER_OTLP_ENDPOINT` to be set, per writeback.py -- is never
    reached."""
    monkeypatch.setattr(guardian_main, "HealthWriteback", _FakeWriteback)
    monkeypatch.setattr(guardian_main, "build_scheduler", lambda *a, **k: _FakeScheduler())

    async def _no_op_provision():
        return None

    async def _no_op_initial_audits(services, writeback):
        return None

    monkeypatch.setattr(guardian_main, "_provision_alerts_if_configured", _no_op_provision)
    monkeypatch.setattr(guardian_main, "_run_initial_audits", _no_op_initial_audits)

    # each test gets a clean cache -- module-level `store` is a singleton
    guardian_main.store = guardian_main.AuditStore()

    with TestClient(guardian_main.app) as test_client:
        yield test_client


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("path,method", [
    ("/health", "GET"),
    ("/audit/run", "POST"),
])
def test_cors_preflight_allows_browser_requests(client, path, method):
    resp = client.options(
        path,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": method,
        },
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_audit_run_triggers_a_cycle_and_returns_serialized_result(client, monkeypatch):
    async def fake_run_audit_cycle(service, store, writeback=None, **kwargs):
        result = _clean_result(service)
        await store.set(service, result)
        return result

    monkeypatch.setattr(guardian_main, "run_audit_cycle", fake_run_audit_cycle)

    resp = client.post("/audit/run", json={"service": "demo-agent-app"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "demo-agent-app"
    assert body["health_score"]["score"] == pytest.approx(100.0)
    assert body["narrative"] == "all clean"


def test_audit_run_defaults_to_all_services_scope_with_no_body(client, monkeypatch):
    seen = []

    async def fake_run_audit_cycle(service, store, writeback=None, **kwargs):
        seen.append(service)
        result = _clean_result(service)
        await store.set(service, result)
        return result

    monkeypatch.setattr(guardian_main, "run_audit_cycle", fake_run_audit_cycle)

    resp = client.post("/audit/run", json={})

    assert resp.status_code == 200
    assert seen == [None]


def test_audit_run_maps_mcp_error_to_502(client, monkeypatch):
    async def fake_run_audit_cycle(service, store, writeback=None, **kwargs):
        raise SignozMCPError("signoz_aggregate_traces returned an error result")

    monkeypatch.setattr(guardian_main, "run_audit_cycle", fake_run_audit_cycle)

    resp = client.post("/audit/run", json={"service": "demo-agent-app"})

    assert resp.status_code == 502


def test_audit_report_404_when_nothing_cached_yet(client):
    resp = client.get("/audit/report/demo-agent-app")
    assert resp.status_code == 404


def test_audit_report_returns_cached_result(client):
    result = _clean_result("demo-agent-app")

    import asyncio

    asyncio.run(guardian_main.store.set("demo-agent-app", result))

    resp = client.get("/audit/report/demo-agent-app")

    assert resp.status_code == 200
    assert resp.json()["service"] == "demo-agent-app"


def test_audit_report_accepts_all_alias_for_no_scope(client):
    result = _clean_result(None)

    import asyncio

    asyncio.run(guardian_main.store.set(None, result))

    resp = client.get("/audit/report/all")

    assert resp.status_code == 200
    assert resp.json()["service"] is None


def test_chat_uses_cached_findings_and_returns_answer(client, monkeypatch):
    result = _clean_result("demo-agent-app")

    import asyncio

    asyncio.run(guardian_main.store.set("demo-agent-app", result))

    def fake_answer_question(findings, question, provider=None):
        assert findings.service == "demo-agent-app"
        return f"Answer to: {question}"

    monkeypatch.setattr(guardian_main, "answer_question", fake_answer_question)
    monkeypatch.setattr(guardian_main, "validate_citations", lambda answer, findings: [])

    resp = client.post("/chat", json={"question": "why is it clean?", "service": "demo-agent-app"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Answer to: why is it clean?"
    assert body["rules_fired_but_uncited"] == []
    assert body["probable_fixes"] == {}


def test_chat_runs_a_fresh_audit_when_nothing_cached(client, monkeypatch):
    calls = []

    async def fake_run_audit_cycle(service, store, writeback=None, **kwargs):
        calls.append(service)
        result = _clean_result(service)
        await store.set(service, result)
        return result

    monkeypatch.setattr(guardian_main, "run_audit_cycle", fake_run_audit_cycle)
    monkeypatch.setattr(guardian_main, "answer_question", lambda findings, question, provider=None: "ok")
    monkeypatch.setattr(guardian_main, "validate_citations", lambda answer, findings: [])

    resp = client.post("/chat", json={"question": "anything wrong?", "service": "demo-agent-app"})

    assert resp.status_code == 200
    assert calls == ["demo-agent-app"]


def test_chat_maps_llm_failure_to_502(client, monkeypatch):
    result = _clean_result("demo-agent-app")

    import asyncio

    asyncio.run(guardian_main.store.set("demo-agent-app", result))

    def fake_answer_question(findings, question, provider=None):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(guardian_main, "answer_question", fake_answer_question)

    resp = client.post("/chat", json={"question": "why?", "service": "demo-agent-app"})

    assert resp.status_code == 502

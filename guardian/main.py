"""FastAPI backend (Section 4.3.5, Stage 7) -- routes: `POST /audit/run`,
`GET /audit/report/{service}`, `POST /chat`, `GET /health`.

This is the thin HTTP layer over the pipeline already built and gate-passed
in Stages 3-6: `guardian/scheduler.py::run_audit_cycle` (which itself wraps
the R1/R2/R3/R6 rule modules, `health_score.compute_health_score`,
`narrative.generate_report`, and `writeback.HealthWriteback`). This module
adds no new audit logic -- only routing, request/response shaping, and
process lifecycle (starting the background audit loop, provisioning
alerts once at startup).

Startup sequence (`lifespan`):
  1. Construct one `HealthWriteback` (owns this process's OTLP
     meter/logger providers -- Section 4.3.5's write-back path).
  2. If `GUARDIAN_ALERT_WEBHOOK_URL` is set, provision the notification
     channel + the three Section 4.5 alerts once via MCP (idempotent per
     `writeback.ensure_notification_channel`'s retry-on-duplicate-name
     handling). If unset, this is skipped with a log line rather than
     failing startup -- alerts can also be provisioned out-of-band via
     `experiments/test_stage6.py --provision-alerts`, which is what the
     Stage 6 gate was actually verified against.
  3. Build the `AsyncIOScheduler` audit loop (`scheduler.build_scheduler`)
     and kick off one immediate audit cycle per configured service as a
     background task, so `GET /audit/report` has something to return
     without a caller having to wait out a full `AUDIT_INTERVAL_MINUTES`
     first -- then start the recurring scheduler on top of that.

Design decision (documented, not silent): `POST /audit/run` and the
scheduler's periodic tick both call the exact same
`scheduler.run_audit_cycle` function -- there is deliberately no second,
HTTP-specific audit implementation here. `POST /chat` reads whatever
`AuditStore` already has cached for the requested service, and only falls
back to running a fresh cycle itself if nothing has ever been cached for
that service yet (e.g. a brand-new `AUDIT_SERVICES` entry before the
scheduler's first tick reaches it) -- it does not re-run an audit on every
chat message, since Section 4.6's frontend spec describes chat as reading
the standing report, not re-auditing per question.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from guardian.health_score import HealthScoreResult
from guardian.mcp_client import SignozMCPClient, SignozMCPError
from guardian.narrative import AuditFindings, answer_question, probable_fixes, validate_citations
from guardian.scheduler import (
    AuditCycleResult,
    AuditStore,
    build_scheduler,
    configured_services,
    run_audit_cycle,
)
from guardian.writeback import HealthWriteback, ensure_alerts, ensure_notification_channel

logging.basicConfig(level=os.getenv("GUARDIAN_LOG_LEVEL", "INFO"))
logger = logging.getLogger("guardian.main")

# Literal path segments a caller can use for the "no service scope" (all
# services combined) audit -- `scheduler.service_key(None)` produces
# `"_all_"` internally; `"all"` is accepted too since it reads more
# naturally in a URL from the Streamlit frontend (Section 4.6).
_NO_SCOPE_ALIASES = {"_all_", "all"}


def _resolve_service_arg(service: str) -> str | None:
    """Path-param `service` -> the `service: str | None` argument every
    rule module / `AuditWindow` / `AuditStore` actually expects."""
    return None if service in _NO_SCOPE_ALIASES else service


store = AuditStore()
_state: dict[str, Any] = {"writeback": None, "scheduler": None}


async def _provision_alerts_if_configured() -> None:
    webhook_url = os.getenv("GUARDIAN_ALERT_WEBHOOK_URL")
    if not webhook_url:
        logger.info(
            "GUARDIAN_ALERT_WEBHOOK_URL not set -- skipping alert provisioning at startup. "
            "Run `python experiments/test_stage6.py --provision-alerts --webhook-url ...` "
            "manually if you want the Section 4.5 alerts created."
        )
        return
    try:
        async with SignozMCPClient() as client:
            channel_name = await ensure_notification_channel(client, webhook_url)
            rule_ids = await ensure_alerts(client, channel_name)
        logger.info("Provisioned Guardian alerts at startup: %s", rule_ids)
    except SignozMCPError:
        logger.exception("Alert provisioning failed at startup -- continuing without alerts.")


async def _run_initial_audits(services: list[str | None], writeback: HealthWriteback) -> None:
    for svc in services:
        try:
            await run_audit_cycle(svc, store, writeback=writeback)
        except Exception:  # noqa: BLE001 -- startup must not crash the whole app over one bad audit
            logger.exception("Initial audit cycle failed for service=%r", svc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    writeback = HealthWriteback()
    _state["writeback"] = writeback

    await _provision_alerts_if_configured()

    services = configured_services()
    # Fire the first cycle in the background rather than awaiting it here --
    # a live MCP + LLM round trip can take a few seconds per service, and
    # the API/health endpoint shouldn't be blocked on it before serving
    # traffic. `GET /audit/report` simply 404s (see below) until this
    # completes, same as it would before the first scheduled tick anyway.
    asyncio.create_task(_run_initial_audits(services, writeback))

    scheduler = build_scheduler(store, writeback, services=services)
    scheduler.start()
    _state["scheduler"] = scheduler
    logger.info(
        "Guardian audit loop started (services=%s, interval=%sm).",
        services,
        os.getenv("AUDIT_INTERVAL_MINUTES", "10"),
    )
    try:
        yield
    finally:
        if _state["scheduler"] is not None:
            _state["scheduler"].shutdown(wait=False)
        writeback.flush()


app = FastAPI(title="Telemetry Health Guardian", lifespan=lifespan)


class AuditRunRequest(BaseModel):
    service: str | None = None


class ChatRequest(BaseModel):
    question: str
    service: str | None = None


def _serialize_cycle(result: AuditCycleResult) -> dict[str, Any]:
    health: HealthScoreResult = result.health
    return {
        "service": result.findings.service,
        "findings": result.findings.to_json_dict(),
        "health_score": {
            "score": health.score,
            "raw_score": health.raw_score,
            "terms": health.terms,
            "r7_included": health.r7_included,
        },
        "narrative": result.narrative,
        "narrative_error": result.narrative_error,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/audit/run")
async def audit_run(body: AuditRunRequest | None = None) -> dict[str, Any]:
    service = body.service if body else None
    writeback: HealthWriteback | None = _state["writeback"]
    try:
        result = await run_audit_cycle(service, store, writeback=writeback)
    except SignozMCPError as exc:
        raise HTTPException(status_code=502, detail=f"SigNoz MCP call failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 -- surfaced as a 500 rather than an opaque connection reset
        logger.exception("Manual audit run failed for service=%r", service)
        raise HTTPException(status_code=500, detail=f"Audit run failed: {exc}") from exc
    return _serialize_cycle(result)


@app.get("/audit/report/{service}")
async def audit_report(service: str) -> dict[str, Any]:
    result = await store.get(_resolve_service_arg(service))
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No audit has completed yet for service={service!r}. "
                "Either wait for the background audit loop's first cycle, "
                "or POST /audit/run to trigger one now."
            ),
        )
    return _serialize_cycle(result)


@app.post("/chat")
async def chat(body: ChatRequest) -> dict[str, Any]:
    service = body.service
    result = await store.get(service)
    if result is None:
        # No cached audit yet for this service (see module docstring) --
        # run one now rather than telling the user to go call /audit/run
        # first themselves.
        writeback: HealthWriteback | None = _state["writeback"]
        try:
            result = await run_audit_cycle(service, store, writeback=writeback)
        except SignozMCPError as exc:
            raise HTTPException(status_code=502, detail=f"SigNoz MCP call failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallback audit run for chat failed for service=%r", service)
            raise HTTPException(status_code=500, detail=f"Could not audit service before answering: {exc}") from exc

    findings: AuditFindings = result.findings
    try:
        answer = answer_question(findings, body.question)
    except Exception as exc:  # noqa: BLE001 -- LLM/provider failures shouldn't be a bare 500 with a stack trace to the frontend
        logger.exception("LLM chat answer failed for service=%r", service)
        raise HTTPException(status_code=502, detail=f"LLM reasoning layer failed: {exc}") from exc

    return {
        "service": findings.service,
        "question": body.question,
        "answer": answer,
        # Best-effort signal (see narrative.py::validate_citations) -- rule
        # IDs that fired in this audit but weren't named in the answer.
        "rules_fired_but_uncited": validate_citations(answer, findings),
        # Deterministic, LLM-independent (see narrative.py::probable_fixes)
        # -- rule_id -> fix hint for every rule that fired. The frontend
        # renders this as its own panel rather than parsing the answer's
        # prose for a fix suggestion.
        "probable_fixes": probable_fixes(findings),
    }

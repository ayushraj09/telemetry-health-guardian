"""APScheduler periodic audit loop (Section 4.3.5) -- Stage 7.

This module owns two things:

1. `run_audit_cycle` / `AuditStore` -- the ONE orchestration path that
   fetches R1/R2/R3/R6/R7 via MCP, combines them, scores them,
   generates the LLM narrative, and (optionally) writes back to SigNoz.
   This is the exact sequence `experiments/test_stage6.py`'s
   `run_audit_and_writeback` already proved live at the Stage 6 gate --
   copied here verbatim rather than reinvented, plus a narrative-generation
   step (Stage 5's `narrative.generate_report`) and an `AuditStore` cache so
   a result computed here is what `guardian/main.py`'s `GET
   /audit/report/{service}` and `POST /chat` read back, instead of every
   HTTP request re-running a live MCP+LLM round trip.

2. `build_scheduler` -- wraps `run_audit_cycle` in an `AsyncIOScheduler`
   job that fires every `AUDIT_INTERVAL_MINUTES` (Section 5's
   "APScheduler running the audit loop every N minutes in the background"),
   once per configured service.

`guardian/main.py` is the only importer of this module -- it constructs one
`AuditStore` at startup, builds and starts the scheduler from it, and reuses
`run_audit_cycle` directly for the manual `POST /audit/run` trigger and the
`POST /chat` cache-miss fallback, so there is exactly one audit code path
in the whole service, not two that could silently drift apart.

Design decision (documented, not silent): narrative generation happens
inside `run_audit_cycle` itself (not left to callers) so a cached
`AuditCycleResult` always has a report ready for `GET /audit/report`
without a second LLM round trip on every read. If `narrative.generate_report`
raises (e.g. no LLM provider configured in a given environment), that is
logged and `narrative` is left `None` on the result rather than failing the
whole audit cycle -- a health score and structured findings are still
useful without prose on top of them.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from guardian.health_score import HealthScoreResult, compute_health_score
from guardian.mcp_client import SignozMCPClient
from guardian.narrative import AuditFindings, combine_results, generate_report
from guardian.rules import (
    r1_missing_fields,
    r2_cardinality,
    r3_orphaned_spans,
    r6_silent_truncation,
    r7_cross_service_breaks,
)
from guardian.rules.types import AuditWindow
from guardian.writeback import HealthWriteback

logger = logging.getLogger("guardian.scheduler")

DEFAULT_AUDIT_WINDOW = os.getenv("AUDIT_WINDOW", "5m")
_NO_SERVICE_SCOPE_KEY = "_all_"  # matches writeback.py's convention for service=None


def service_key(service: str | None) -> str:
    """The cache/metric key for `service` -- `"_all_"` for the no-scope
    aggregate audit (service=None), matching `writeback.py`'s existing
    convention so the two stay consistent."""
    return service or _NO_SERVICE_SCOPE_KEY


@dataclass(frozen=True)
class AuditCycleResult:
    """One completed audit cycle's full output -- what `AuditStore` caches
    and what `guardian/main.py`'s endpoints serialize back to callers."""

    findings: AuditFindings
    health: HealthScoreResult
    narrative: str | None
    narrative_error: str | None  # set instead of raising, see module docstring


class AuditStore:
    """In-memory cache of the most recent `AuditCycleResult` per service
    key. Not persisted -- a restart clears it, same as `HealthWriteback`'s
    own gauge caches in `writeback.py`; the durable record of history is
    SigNoz itself (the metrics/logs `write_audit_result` already writes),
    not this process's memory. `asyncio.Lock` guards concurrent access
    since both the scheduler's background job and FastAPI request handlers
    read/write it from the same event loop.
    """

    def __init__(self) -> None:
        self._results: dict[str, AuditCycleResult] = {}
        self._lock = asyncio.Lock()

    async def set(self, service: str | None, result: AuditCycleResult) -> None:
        async with self._lock:
            self._results[service_key(service)] = result

    async def get(self, service: str | None) -> AuditCycleResult | None:
        async with self._lock:
            return self._results.get(service_key(service))

    async def all(self) -> dict[str, AuditCycleResult]:
        async with self._lock:
            return dict(self._results)


async def run_audit_cycle(
    service: str | None,
    store: AuditStore,
    *,
    writeback: HealthWriteback | None = None,
    generate_narrative: bool = True,
    time_range: str | None = None,
) -> AuditCycleResult:
    """Run one full R1+R2+R3+R6+R7 audit for `service` (`None` = all services,
    same semantics `AuditWindow`/every rule module already use), score it,
    narrate it, optionally write it back to SigNoz, cache it in `store`,
    and return it.

    This is `experiments/test_stage6.py::run_audit_and_writeback`'s proven
    MCP call sequence, unchanged, with narrative generation added and the
    result cached instead of only printed. Kept as a free function (not a
    method) so `guardian/main.py` can call it directly for both the
    scheduler's periodic job and a manual `POST /audit/run` trigger without
    needing a scheduler instance.
    """
    window = AuditWindow(time_range=time_range or DEFAULT_AUDIT_WINDOW, service=service)
    async with SignozMCPClient() as client:
        r1 = await r1_missing_fields.run(client, window)
        r2 = await r2_cardinality.run(client, window)
        r3 = await r3_orphaned_spans.run(client, window)
        r6 = await r6_silent_truncation.run(client, window)
        r7 = await r7_cross_service_breaks.run(client, window)

    findings = combine_results(service=service, r1=r1, r2=r2, r3=r3, r6=r6, r7=r7)
    health = compute_health_score(findings)

    narrative_text: str | None = None
    narrative_error: str | None = None
    if generate_narrative:
        try:
            narrative_text = generate_report(findings)
        except Exception as exc:  # noqa: BLE001 -- see module docstring: don't fail the audit over a narrative miss
            logger.exception("Narrative generation failed for service=%r", service)
            narrative_error = str(exc)

    if writeback is not None:
        writeback.write_audit_result(findings, health)

    result = AuditCycleResult(
        findings=findings, health=health, narrative=narrative_text, narrative_error=narrative_error
    )
    await store.set(service, result)
    logger.info(
        "Audit cycle complete for service=%r: score=%.1f, findings=R1:%d/R2:%d/R3:%d/R6:%d/R7:%d",
        service,
        health.score,
        len(r1.findings),
        len(r2.findings),
        len(r3.findings),
        len(r6.findings),
        len(r7.findings),
    )
    return result


def configured_services() -> list[str | None]:
    """`AUDIT_SERVICES` env var -- comma-separated service names to audit
    each cycle. Unset/empty means one audit covering all services
    (`service=None`, same default every rule module and
    `experiments/test_stage6.py` already used) -- the common case for a
    single-target demo app like this project's, per Section 4.1.
    """
    raw = os.getenv("AUDIT_SERVICES", "").strip()
    if not raw:
        return [None]
    return [s.strip() for s in raw.split(",") if s.strip()]


def build_scheduler(
    store: AuditStore,
    writeback: HealthWriteback | None,
    *,
    services: list[str | None] | None = None,
    interval_minutes: int | None = None,
) -> AsyncIOScheduler:
    """Build (but do not start) the `AUDIT_INTERVAL_MINUTES` background
    audit loop (Section 5), one `run_audit_cycle` call per configured
    service per tick. Caller (`guardian/main.py`'s startup) owns
    `.start()`/`.shutdown()`.
    """
    services = services if services is not None else configured_services()
    interval = interval_minutes or int(os.getenv("AUDIT_INTERVAL_MINUTES", "10"))

    scheduler = AsyncIOScheduler()

    async def _tick() -> None:
        for svc in services:
            try:
                await run_audit_cycle(svc, store, writeback=writeback)
            except Exception:  # noqa: BLE001 -- one service's failure must not cancel the others' turn or kill the job
                logger.exception("Scheduled audit cycle failed for service=%r", svc)

    scheduler.add_job(
        _tick,
        trigger=IntervalTrigger(minutes=interval),
        id="guardian-audit-loop",
        replace_existing=True,
    )
    return scheduler
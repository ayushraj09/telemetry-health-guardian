"""
Stage 6 gate check, per Section 6: "is the health-score panel a live time
series in SigNoz, and does running chaos.py visibly move the panel and
fire an alert, observed live -- not just confirmed in logs?"

Copy to repo root first (same as test_stage3.py/test_stage4.py/test_stage5.py), then:

    # one-time setup: create the notification channel + the 3 alert rules
    python test_stage6.py --provision-alerts --webhook-url https://<your-webhook-sink>

    # baseline: clean run
    python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf > baseline_out.json
    python test_stage6.py

    # chaos: drop the health score and check the panel/alert move live
    CHAOS_MODE=1 CHAOS_R1_RATE=1.0 CHAOS_R2_RATE=1.0 CHAOS_R3_RATE=1.0 CHAOS_R6_RATE=1.0 CHAOS_SEED=42 \\
        python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf > chaos_out.json
    python test_stage6.py

Then in the SigNoz UI: open the "Telemetry Health Guardian" dashboard
(after running scripts/provision_dashboards.py once) and confirm the
Telemetry Health Score panel actually dropped between the two runs, and
that the "health score below 70" alert transitions to firing if the chaos
run's score is low enough.
"""

import argparse
import asyncio

from dotenv import load_dotenv

load_dotenv()

from guardian.health_score import compute_health_score
from guardian.mcp_client import SignozMCPClient
from guardian.narrative import combine_results
from guardian.rules import r1_missing_fields, r2_cardinality, r3_orphaned_spans, r6_silent_truncation
from guardian.rules.types import AuditWindow
from guardian.writeback import HealthWriteback, ensure_alerts, ensure_notification_channel


async def run_audit_and_writeback(service: str | None = None) -> None:
    window = AuditWindow(time_range="15m", service=service)
    async with SignozMCPClient() as client:
        r1 = await r1_missing_fields.run(client, window)
        r2 = await r2_cardinality.run(client, window)
        r3 = await r3_orphaned_spans.run(client, window)
        r6 = await r6_silent_truncation.run(client, window)

    findings = combine_results(service=service, r1=r1, r2=r2, r3=r3, r6=r6)
    health = compute_health_score(findings)

    print(f"Health score for {service or '(all services)'}: {health.score:.1f} (raw={health.raw_score:.1f})")
    for term, value in health.terms.items():
        print(f"  {term}: {value:.2f}")
    for rule_name, result in (("R1", r1), ("R2", r2), ("R3", r3), ("R6", r6)):
        print(f"{rule_name}: {len(result.findings)} findings")

    writeback = HealthWriteback()
    writeback.write_audit_result(findings, health)
    print("\nWrote telemetry.health_score + issue logs via OTLP and flushed. "
          "Check the SigNoz dashboard/alert now -- OTLP export is async even "
          "after force_flush() returns, allow a few seconds for ingestion.")


async def provision_alerts(webhook_url: str) -> None:
    async with SignozMCPClient() as client:
        channel_id = await ensure_notification_channel(client, webhook_url)
        print(f"Created notification channel: {channel_id}")
        rule_ids = await ensure_alerts(client, channel_id)
        print("Created alert rules:", rule_ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provision-alerts", action="store_true")
    parser.add_argument("--webhook-url", default=None)
    parser.add_argument("--service", default=None)
    args = parser.parse_args()

    if args.provision_alerts:
        if not args.webhook_url:
            raise SystemExit("--provision-alerts requires --webhook-url")
        asyncio.run(provision_alerts(args.webhook_url))
    else:
        asyncio.run(run_audit_and_writeback(args.service))

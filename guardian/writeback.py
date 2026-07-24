"""Write-back: health-score metric, issue logs, alert rules (Section 4.3.5,
Section 4.5). Two genuinely different mechanisms live in this one file,
per the repo layout comment ("writes metrics/logs back to SigNoz + MCP
alert creation") -- they are NOT interchangeable, and mixing them up is a
correctness bug, not just a style choice:

1. METRICS + LOGS -- direct OTLP export (`HealthWriteback` below).
   Confirmed against the live `signoz-mcp-server` tool list (GitHub
   SigNoz/signoz-mcp-server README, checked 2026-07-22): there is no
   `signoz_write_metric` / `signoz_create_log` tool or equivalent. The only
   MCP-writable resources are dashboards, alert rules, notification
   channels, and saved views -- not raw telemetry. Section 1's "MCP is the
   only data path" and the architecture diagram's "writes back ... -> MCP"
   arrow describe an aspiration the actual tool surface doesn't support.
   Direct OTLP is the only mechanism that can put a metric/log into SigNoz
   at all, and it's the same mechanism `otel-griptape` already uses for
   spans (see `demo-agent-app/telemetry.py`) -- reusing
   `OTEL_EXPORTER_OTLP_ENDPOINT`/`OTEL_EXPORTER_OTLP_HEADERS` here keeps a
   single OTLP target for the whole project. Confirmed with the user
   (2026-07-22) this is the intended resolution, not a silent workaround.
   All *reads* (the rule engine in guardian/rules/*.py) still go through
   MCP exclusively, per spec -- this file doesn't change that.

2. ALERTS + NOTIFICATION CHANNEL -- MCP (`ensure_notification_channel`,
   `ensure_alerts` below), via `signoz_create_notification_channel` and
   `signoz_create_alert`. These DO have real MCP tools, so they go through
   `SignozMCPClient` like every other MCP call in this project.

Honesty note on the alert payloads: `signoz_create_alert`'s schema
(v2alpha1 for threshold_rule, v1 for anomaly_rule) is taken from the
signoz-mcp-server README's parameter reference, not from the
`signoz://alert/examples` MCP resource (the ten canonical payloads) --
that resource needs a live MCP session to read and wasn't reachable from
this environment. Same situation `mcp_client.py`'s module docstring
already flags for the query tools. If `signoz_create_alert` 400s, the
fastest fix is: connect to the MCP server yourself and read resource
`signoz://alert/examples`, then send me one canonical threshold_rule and
one anomaly_rule payload and I'll correct `_threshold_rule_payload` /
`_anomaly_rule_payload` in one pass -- same pattern as the two live-run
fixes in Stage 4.

Design decision (documented, not silent): the four gauges below
(`telemetry.health_score`, `telemetry.cardinality_risk_score`,
`telemetry.orphaned_span_rate_pct`, `telemetry.truncation_rate_pct`) are
self-emitted metrics the Guardian writes from its own already-computed
Result objects, rather than dashboards re-deriving R1-R6's detection logic
as raw ClickHouse/trace-tree queries against SigNoz. This is what a
"write results back as metrics" path is *for* -- it also sidesteps
needing to guess at unverified SigNoz Query Builder v5 features (e.g. a
`having`-clause on a distinct-count aggregation) this environment can't
confirm live. `guardian/dashboards/health_dashboard.json` and
`scripts/provision_dashboards.py` build every panel off these four
metrics, per Section 4.4's table.
"""

from __future__ import annotations

import atexit
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opentelemetry import _logs as otel_logs
from opentelemetry import metrics
from opentelemetry._logs import LogRecord
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from guardian.mcp_client import SignozMCPError

if TYPE_CHECKING:
    from guardian.health_score import HealthScoreResult
    from guardian.mcp_client import SignozMCPClient
    from guardian.narrative import AuditFindings

METRIC_HEALTH_SCORE = "telemetry.health_score"
METRIC_MISSING_FIELD_RATE = "telemetry.missing_field_rate_pct"
METRIC_CARDINALITY_RISK = "telemetry.cardinality_risk_score"
METRIC_ORPHANED_RATE = "telemetry.orphaned_span_rate_pct"
METRIC_TRUNCATION_RATE = "telemetry.truncation_rate_pct"
METRIC_CROSS_SERVICE_BREAK_RATE = "telemetry.cross_service_break_rate_pct"

_DEFAULT_EXPORT_INTERVAL_MS = 15_000  # see HealthWriteback docstring


def _otlp_headers_from_env() -> dict[str, str]:
    raw = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        headers[key.strip()] = value.strip()
    return headers


def _otlp_endpoint(signal_path: str) -> str:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise RuntimeError(
            "OTEL_EXPORTER_OTLP_ENDPOINT is not set -- copy .env.example to .env "
            "and fill in your SigNoz Cloud ingestion endpoint first (same value "
            "demo-agent-app/telemetry.py uses)."
        )
    return endpoint if endpoint.endswith(signal_path) else f"{endpoint}{signal_path}"


class HealthWriteback:
    """Owns the Guardian's own OTLP MeterProvider/LoggerProvider and pushes
    the audit engine's results into them.

    Metrics use `ObservableGauge` (callback-based) rather than a
    synchronous `Gauge` instrument: synchronous `Gauge` is a newer,
    still-shifting addition to the OTel Python SDK and `otel-griptape`
    pins `opentelemetry-sdk>=1.24.0`, an older floor where it may not
    exist. `ObservableGauge` has been stable since early 1.x and reading
    from a plain cache dict in the callback is simple and correct here --
    the Guardian always has a fresh value cached the instant an audit
    cycle finishes, there's no async-computation gap to worry about.

    `PeriodicExportingMetricReader` still runs on a timer
    (`_DEFAULT_EXPORT_INTERVAL_MS`) as a safety net, but `flush()` forces
    an immediate export right after each audit cycle's `write_*` calls --
    that's what makes the Stage 6 gate's "does the panel drop live ...
    within one audit cycle, observed live" hold without waiting up to 15s
    for the next periodic tick.
    """

    def __init__(self, service_name: str | None = None) -> None:
        self.service_name = service_name or os.getenv(
            "GUARDIAN_OTEL_SERVICE_NAME", "telemetry-health-guardian"
        )
        headers = _otlp_headers_from_env()
        resource = Resource.create({"service.name": self.service_name})

        self._health_score: dict[tuple[str, ...], float] = {}
        self._missing_field_rate: dict[tuple[str, ...], float] = {}
        self._cardinality_risk: dict[tuple[str, ...], float] = {}
        self._orphaned_rate: dict[tuple[str, ...], float] = {}
        self._truncation_rate: dict[tuple[str, ...], float] = {}
        self._cross_service_break_rate: dict[tuple[str, ...], float] = {}

        metric_exporter = OTLPMetricExporter(
            endpoint=_otlp_endpoint("/v1/metrics"), headers=headers
        )
        reader = PeriodicExportingMetricReader(
            metric_exporter, export_interval_millis=_DEFAULT_EXPORT_INTERVAL_MS
        )
        self._meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = self._meter_provider.get_meter("telemetry-health-guardian")

        meter.create_observable_gauge(
            METRIC_HEALTH_SCORE,
            callbacks=[self._gauge_callback(self._health_score, ("service.name",))],
            unit="1",
            description="Telemetry Health Guardian's per-service health score (0-100, Section 4.3.6).",
        )
        meter.create_observable_gauge(
            METRIC_MISSING_FIELD_RATE,
            callbacks=[self._gauge_callback(self._missing_field_rate, ("service.name",))],
            unit="1",
            description="R1 missing-required-GenAI-field rate (%), including naming-convention non-conformance.",
        )
        meter.create_observable_gauge(
            METRIC_CARDINALITY_RISK,
            callbacks=[self._gauge_callback(self._cardinality_risk, ("service.name", "field_key"))],
            unit="1",
            description="R2 cardinality risk score (0-100) per flagged attribute key.",
        )
        meter.create_observable_gauge(
            METRIC_ORPHANED_RATE,
            callbacks=[self._gauge_callback(self._orphaned_rate, ("service.name",))],
            unit="1",
            description="R3 orphaned-span rate (%) in the most recent audit window.",
        )
        meter.create_observable_gauge(
            METRIC_TRUNCATION_RATE,
            callbacks=[self._gauge_callback(self._truncation_rate, ("service.name", "tool"))],
            unit="1",
            description="R6 truncation rate (%), overall (tool='_all_') and per truncated tool.",
        )
        meter.create_observable_gauge(
            METRIC_CROSS_SERVICE_BREAK_RATE,
            callbacks=[
                self._gauge_callback(self._cross_service_break_rate, ("caller_service", "callee_service"))
            ],
            unit="1",
            description=(
                "R7 cross-service handoff break rate (%) per caller/callee service pair "
                "-- traceparent not propagated across an outbound HTTP call."
            ),
        )

        log_exporter = OTLPLogExporter(endpoint=_otlp_endpoint("/v1/logs"), headers=headers)
        self._logger_provider = LoggerProvider(resource=resource)
        self._logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

        atexit.register(self._meter_provider.shutdown)
        atexit.register(self._logger_provider.shutdown)

    @staticmethod
    def _gauge_callback(cache: dict[tuple[str, ...], float], label_names: tuple[str, ...]):
        def callback(_options: metrics.CallbackOptions):
            for label_values, value in list(cache.items()):
                yield metrics.Observation(value, dict(zip(label_names, label_values)))

        return callback

    # -- writers -----------------------------------------------------

    def write_health_score(self, result: HealthScoreResult) -> None:
        service = result.service or "_all_"
        self._health_score[(service,)] = result.score

    def write_findings(self, findings: AuditFindings) -> None:
        """Populates the R2/R3/R6 gauges and emits one OTLP log record per
        finding (the "issue logs" half of write-back). Findings are
        emitted with ERROR severity for R6 tool-payload truncation and
        model-output truncation (both are silent-data-loss bugs) and WARN
        for R1/R2/R3, matching the relative severity implied by the
        formula's weighting (Section 4.3.6 weights R1 highest at 0.30, but
        R6 is the spec's flagged "headline case" -- Section 4.1 -- so it's
        elevated here independent of formula weight).
        """
        service = findings.service or "_all_"

        self._missing_field_rate[(service,)] = (1.0 - findings.r1.score) * 100.0
        self._orphaned_rate[(service,)] = findings.r3.orphaned_span_rate_pct
        self._truncation_rate[(service, "_all_")] = findings.r6.truncation_rate_pct

        for f in findings.r1.findings:
            self._emit_log(service, "WARN", f.rule, getattr(f, "kind", None), f.detail, f.span_id, f.trace_id)
        for f in findings.r2.findings:
            self._cardinality_risk[(service, f.field_key)] = f.distinct_ratio * 100.0
            self._emit_log(service, "WARN", f.rule, None, f.detail, None, None)
        for f in findings.r3.findings:
            self._emit_log(service, "WARN", f.rule, None, f.detail, f.span_id, f.trace_id)
        for f in findings.r6.findings:
            self._truncation_rate[(service, f.span_name)] = 100.0
            self._emit_log(service, "ERROR", f.rule, f.kind, f.detail, f.span_id, f.trace_id)

        r7 = getattr(findings, "r7", None)
        if r7 is not None:
            self._cross_service_break_rate[(service, "_all_")] = r7.cross_service_break_rate_pct
            for f in r7.findings:
                self._cross_service_break_rate[(f.caller_service, f.callee_service)] = 100.0
                self._emit_log(
                    service,
                    "ERROR",
                    f.rule,
                    None,
                    f.detail,
                    f.caller_span_id,
                    f.caller_trace_id,
                )

    def _emit_log(
        self,
        service: str,
        severity_text: str,
        rule: str,
        kind: str | None,
        detail: str,
        span_id: str | None,
        trace_id: str | None,
    ) -> None:
        logger = self._logger_provider.get_logger("telemetry-health-guardian")
        attributes: dict[str, Any] = {"guardian.rule": rule, "service.name": service}
        if kind is not None:
            attributes["guardian.kind"] = kind
        if span_id is not None:
            attributes["guardian.span_id"] = span_id
        if trace_id is not None:
            attributes["guardian.trace_id"] = trace_id
        severity_number = otel_logs.SeverityNumber.ERROR if severity_text == "ERROR" else otel_logs.SeverityNumber.WARN
        # `opentelemetry._logs.LogRecord` (the API package's class) is what
        # `Logger.emit()` expects on this project's pinned floor
        # (opentelemetry-sdk>=1.24.0) -- confirmed live 2026-07-22. The SDK's
        # own `opentelemetry.sdk._logs.LogRecord` looked like the more
        # "concrete" choice but isn't the class `emit()` accepts; if this
        # breaks again on a different installed version, the fix is almost
        # certainly just the import path/class name here, not the
        # surrounding logic.
        logger.emit(
            LogRecord(
                timestamp=time.time_ns(),
                body=detail,
                severity_text=severity_text,
                severity_number=severity_number,
                attributes=attributes,
            )
        )

    def write_audit_result(self, findings: AuditFindings, health: HealthScoreResult) -> None:
        """Convenience: the one call an audit loop (Stage 7's
        `scheduler.py`, or a driver script today) actually needs per cycle."""
        self.write_health_score(health)
        self.write_findings(findings)
        self.flush()

    def flush(self) -> None:
        self._meter_provider.force_flush()
        self._logger_provider.force_flush()


# ---------------------------------------------------------------------
# Alerts + notification channel -- MCP, per Section 4.5
# ---------------------------------------------------------------------


async def ensure_notification_channel(
    client: SignozMCPClient, webhook_url: str, name: str = "guardian-alerts"
) -> str:
    """Creates a webhook notification channel via
    `signoz_create_notification_channel` and returns its NAME (not its
    UUID) for use in `ensure_alerts`'s channel routing.

    Why the name, not the id (corrected 2026-07-22, confirmed by
    SigNoz's own official `signoz-creating-alerts` agent skill): alert
    rule channel-routing fields (`condition.thresholds.spec[].channels`
    for threshold/promql rules, top-level `preferredChannels` for
    anomaly rules) take the channel's exact NAME string as returned by
    `signoz_list_notification_channels`, not its UUID. Passing the UUID
    there is the same class of bug as the `builderQueries`-vs-`queries`
    issue fixed earlier in this file: the API accepts it and the rule
    saves successfully, but it silently never routes a notification
    anywhere. This function still creates the channel (which does
    return a UUID in its response, parsed below only to confirm creation
    succeeded) but returns the name, since that's what callers actually
    need downstream.

    Idempotency note: the MCP tool table has no "get channel by name" /
    upsert semantics confirmed live, so this cannot look up and reuse an
    existing channel by name -- `signoz_list_notification_channels`'s
    response shape has never been confirmed against a live server from
    this environment, so guessing at parsing it here would just trade
    one unverified assumption for another.

    What IS confirmed live (2026-07-22): the server rejects a duplicate
    name with a 400 whose message is exactly
    "the receiver name has to be unique, please choose a different name".
    Since re-running this script against a server that already has a
    `guardian-alerts` channel from a prior run is the common case, on
    that specific, confirmed error this retries once with a uniquified
    name (`{name}-{unix timestamp}`) so the run can proceed without
    creating an unbounded pile of same-named channels or requiring you
    to manually delete the old one first. Any other error is re-raised
    as-is.
    """
    payload = {"type": "webhook", "name": name, "webhook_url": webhook_url}
    try:
        result = await client.call_tool("signoz_create_notification_channel", payload)
    except SignozMCPError as exc:
        if "receiver name has to be unique" not in str(exc):
            raise
        payload["name"] = f"{name}-{int(time.time())}"
        result = await client.call_tool("signoz_create_notification_channel", payload)
    # Confirmed live response shape (2026-07-22): the tool wraps the actual
    # channel record under `channel.data`, alongside a `test_notification`
    # status field -- e.g.
    #   {"channel": {"status": "success", "data": {"id": "...", ...}},
    #    "test_notification": {"success": true, ...}}
    # rather than a flat `{"id": ...}` or `{"channel_id": ...}` as originally
    # guessed. Used here only as a creation-succeeded sanity check.
    created_ok = False
    if isinstance(result, dict):
        channel = result.get("channel")
        if isinstance(channel, dict):
            data = channel.get("data")
            if isinstance(data, dict) and data.get("id"):
                created_ok = True
        if not created_ok and (result.get("id") or result.get("channel_id")):
            created_ok = True
    if not created_ok:
        raise RuntimeError(
            f"signoz_create_notification_channel didn't return a recognizable id: {result!r}"
        )
    return str(payload["name"])


def _threshold_rule_payload(
    *, alert_name: str, metric_name: str, comparison: str, target: float, channel_name: str, group_by: list[str] | None = None
) -> dict[str, Any]:
    """v2alpha1 threshold_rule payload for `signoz_create_alert`.

    Three parts of this were confirmed WRONG on live runs (2026-07-22)
    and are now fixed against independently-confirmed sources, not
    guesses:

    1. `condition.compositeQuery` must use a `queries` ARRAY of
       `{"type": "builder_query", "spec": {...}}` envelopes, not a
       `builderQueries` MAP. Confirmed by SigNoz/signoz#10823 ("Alert
       rules created via API use builderQueries but ruler reads
       queries -- rules never fire"): the API silently accepts
       `builderQueries` but the ruler's v5 evaluation path only ever
       reads `queries`, so a `builderQueries`-shaped rule is created
       successfully yet never actually fires.
    2. `evaluation` must be `{"kind": "rolling", "spec": {"evalWindow":
       ..., "frequency": ...}}`. Confirmed by a working
       terraform-provider-signoz v2alpha1 alert
       (SigNoz/terraform-provider-signoz#75).
    3. `condition.thresholds` must be `{"kind": "basic", "spec": [...]}`,
       not a flat list -- confirmed directly by the live validation
       error ("condition.thresholds: is required (v2alpha1 schema); use
       condition.thresholds with kind and spec array"). Each spec entry's
       `op`/`matchType` use the human-readable words from SigNoz's own
       official `signoz-creating-alerts` agent skill (not the numeric
       string codes an earlier revision worried about guessing):
       valid `op` words are `above`, `below`, `equal`, `not_equal`,
       `above_or_equal`, `below_or_equal`, `outside_bounds` (`equals` is
       invalid). The skill's default pairing table:
         above  -> matchType "at_least_once" (breach at any point)
         below  -> matchType "all_the_times" (breach for entire window)
       `channels` inside each threshold spec take the channel's exact
       NAME (from `signoz_list_notification_channels`), not its UUID --
       same skill's guardrail. Passing the UUID there would be the same
       silent-failure pattern as (1): rule saves, never routes a
       notification. `channel_name` here is exactly the string
       `ensure_notification_channel` returns.

    `comparison` is "above" or "below" (not a symbol) -- callers were
    updated accordingly in `ensure_alerts`.
    """
    match_type = "at_least_once" if comparison == "above" else "all_the_times"
    return {
        "alert": alert_name,
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "threshold_rule",
        "schemaVersion": "v2alpha1",
        "condition": {
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "queries": [
                    {
                        "type": "builder_query",
                        "spec": {
                            "name": "A",
                            "signal": "metrics",
                            "aggregations": [
                                {
                                    "metricName": metric_name,
                                    "temporality": "unspecified",
                                    "timeAggregation": "avg",
                                    "spaceAggregation": "avg",
                                }
                            ],
                            "groupBy": [{"name": key} for key in (group_by or [])],
                        },
                    }
                ],
            },
            "selectedQueryName": "A",
            "thresholds": {
                "kind": "basic",
                "spec": [
                    {
                        "name": "CRITICAL",
                        "target": target,
                        "targetUnit": "",
                        "recoveryTarget": None,
                        "matchType": match_type,
                        "op": comparison,
                        "channels": [channel_name],
                    }
                ],
            },
        },
        "evaluation": {"kind": "rolling", "spec": {"evalWindow": "5m", "frequency": "1m"}},
        "labels": {"severity": "critical"},
    }


def _anomaly_rule_payload(
    *, alert_name: str, metric_name: str, channel_name: str, group_by: list[str] | None = None
) -> dict[str, Any]:
    """v1 anomaly_rule payload -- top-level `evalWindow`/`frequency`,
    `condition.op`/`matchType`/`target`/`algorithm`/`seasonality`, anomaly
    function nested in `compositeQuery.queries[].spec.functions`.

    Two fixes applied here (2026-07-22) from the same confirmed source as
    `_threshold_rule_payload`'s fix #3 (SigNoz's official
    `signoz-creating-alerts` agent skill):

    - `op` is `"above"` (the skill: "Use `above` for anomaly rules: their
      absolute score covers spikes and drops" -- an anomaly z-score is
      unsigned, so `above` is the only sensible comparison regardless of
      whether the underlying metric spiked or dropped). Previously this
      was the symbol `">"`, which isn't one of the skill's valid `op`
      words at all.
    - Channel routing: `anomaly_rule` forbids `condition.thresholds`
      entirely, so per the skill's guardrail ("thresholds are forbidden
      [for anomaly_rule], so put channel names in the top-level
      preferredChannels array") channels go in a top-level
      `preferredChannels` list of channel NAMEs, not a `notificationSettings`
      object (which isn't a field this schema uses) and not UUIDs.

    NOT changed here (still an open question, flagged rather than
    guessed): the docs I could find list only "Standard" as an anomaly
    `algorithm` option in the UI, alongside a separate `seasonality`
    field (Hourly/Daily/Weekly) -- this payload's `"algorithm": "seasonal"`
    doesn't obviously match that. I haven't found a confirmed source for
    the exact JSON string SigNoz expects here, so I'm not guessing at it;
    if this rule creates but its state stays permanently inactive, this
    field is the next thing to check via `signoz_get_alert`.
    """
    return {
        "alert": alert_name,
        "alertType": "METRIC_BASED_ALERT",
        "ruleType": "anomaly_rule",
        "evalWindow": "5m",
        "frequency": "1m",
        "condition": {
            "op": "above",
            "matchType": "at_least_once",
            "target": 3.0,  # standard-deviations threshold
            "algorithm": "seasonal",
            "seasonality": "daily",
            "compositeQuery": {
                "queryType": "builder",
                "panelType": "graph",
                "queries": [
                    {
                        "type": "builder_query",
                        "spec": {
                            "name": "A",
                            "signal": "metrics",
                            "aggregations": [{"metricName": metric_name, "temporality": "unspecified", "timeAggregation": "avg", "spaceAggregation": "avg"}],
                            "groupBy": [{"name": key} for key in (group_by or [])],
                            "functions": [{"name": "anomaly"}],
                        },
                    }
                ]
            },
        },
        "labels": {"severity": "warning"},
        "preferredChannels": [channel_name],
    }


async def ensure_alerts(client: SignozMCPClient, channel_name: str) -> dict[str, str]:
    """Creates the four alerts required by Section 4.5 via
    `signoz_create_alert`. Returns `{alert_name: ruleId}`. Called once at
    Guardian startup -- per spec, "created programmatically via MCP at
    startup," not on every audit cycle; a future `main.py` should call
    this once, not from inside `scheduler.py`'s per-cycle loop.

    `channel_name` (renamed from `channel_id` 2026-07-22): this is the
    notification channel's NAME, not its UUID -- see
    `ensure_notification_channel`'s docstring for why. The `test_stage6.py`
    driver that calls this passes straight through whatever
    `ensure_notification_channel` returns, so no caller-side change was
    needed there.
    """
    payloads = {
        "guardian-health-score-below-70": _threshold_rule_payload(
            alert_name="Telemetry health score below 70",
            metric_name=METRIC_HEALTH_SCORE,
            comparison="below",
            target=70.0,
            channel_name=channel_name,
            group_by=["service.name"],
        ),
        "guardian-cardinality-spike": _anomaly_rule_payload(
            alert_name="Sudden cardinality risk spike",
            metric_name=METRIC_CARDINALITY_RISK,
            channel_name=channel_name,
            group_by=["service.name", "field_key"],
        ),
        "guardian-truncation-rate-above-5pct": _threshold_rule_payload(
            alert_name="Truncation rate above 5% for a tool",
            metric_name=METRIC_TRUNCATION_RATE,
            comparison="above",
            target=5.0,
            channel_name=channel_name,
            group_by=["service.name", "tool"],
        ),
        "guardian-cross-service-break-any-occurrence": _threshold_rule_payload(
            alert_name="Cross-service handoff break detected",
            metric_name=METRIC_CROSS_SERVICE_BREAK_RATE,
            comparison="above",
            target=0.0,
            channel_name=channel_name,
            group_by=["caller_service", "callee_service"],
        ),
    }
    rule_ids: dict[str, str] = {}
    for key, payload in payloads.items():
        result = await client.call_tool("signoz_create_alert", payload)
        rule_ids[key] = result.get("ruleId") if isinstance(result, dict) else None
    return rule_ids
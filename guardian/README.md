# Guardian

The Telemetry Health Guardian service: an MCP-powered auditor that reads
telemetry back out of SigNoz, runs it through a fixed rule engine, scores
it, and explains the findings in plain, cited language.

This is a component of the [Telemetry Health Guardian](../README.md)
project — see the root README for the system-level architecture, the
health-score formula, the write-back mechanism, and how this service fits
together with [`otel-griptape`](../otel-griptape/README.md) and
[`demo-agent-app`](../demo-agent-app/README.md). This document only
covers `guardian/` itself: its modules, its HTTP API, and how to run it.

---

## What it does, module by module

| Module | Role |
|---|---|
| `mcp_client.py` | `SignozMCPClient` — the *only* code in this project that opens an MCP session against SigNoz. Selects cloud vs. self-hosted via `SIGNOZ_MCP_MODE` (`cloud` default, or `selfhost`); every rule module calls its typed methods (`aggregate_traces`, `get_field_keys`, `execute_builder_query`, …) rather than touching MCP directly. |
| `rules/r1_missing_fields.py` | R1 — required `gen_ai.*` attribute presence on `gen_ai.operation.name == "chat"` spans, plus exact `"{operation} {model}"` span-naming conformance. |
| `rules/r2_cardinality.py` | R2 — flags an attribute key only when `distinct_values/total_spans > 0.8` **and** `avg_value_bytes > 200` both hold. |
| `rules/r3_orphaned_spans.py` | R3 — a span whose `parent_span_id` is set but doesn't resolve within the same trace, cross-checked against the audit window's edge. |
| `rules/r6_silent_truncation.py` | R6 — compares `payload.raw_bytes` vs. `payload.captured_bytes` (custom attributes `otel-griptape` and `demo-agent-app/ollama_r6.py` set); flags when `captured_bytes < raw_bytes * 0.95`. |
| `rules/r7_cross_service_breaks.py` | R7 — for an outbound call from one service to another, checks whether the closest span in the callee within a 2-second (`HANDOFF_WINDOW_MS`) window shares the caller's `trace_id`. A different `trace_id` = a broken handoff; no candidate span in-window at all is left unevaluated, not flagged. |
| `rules/types.py` | `AuditWindow` — the shared time-window type every rule module's `fetch_*`/`run` takes. |
| `health_score.py` | `compute_health_score` — the weighted 0–100 formula (R1 0.30 / R2 0.25 / R3 0.20 / R6 0.20 / R7 0.05, R7's term included only when an `r7` result is supplied). Clamps the final score to `[0, 100]`; keeps unclamped per-rule terms for debugging. |
| `llm_client.py` | `resolve_model` + `generate` — a LiteLLM wrapper switching between OpenAI and Ollama via `LLM_PROVIDER`, so the same rule-engine output can be run through either provider. |
| `narrative.py` | `combine_results` assembles the rule engine's per-rule `Result` objects into one JSON structure (`AuditFindings`); `generate_report`/`answer_question` turn that into a cited natural-language report and free-form chat answers; `validate_citations` is a best-effort post-hoc check for whether an LLM answer actually names the rule IDs that fired. |
| `scheduler.py` | `run_audit_cycle` — the single orchestration path: MCP fetch → rule engine → `compute_health_score` → `narrative.generate_report` → (optional) write-back, cached in `AuditStore`. `build_scheduler` wraps this in an `AsyncIOScheduler` job firing every `AUDIT_INTERVAL_MINUTES`. |
| `writeback.py` | `HealthWriteback` — direct OTLP export of the health-score metric and issue logs (there is no MCP tool that writes raw metrics/logs). `ensure_notification_channel` / `ensure_alerts` — the four alert rules, provisioned via real `signoz_create_notification_channel` / `signoz_create_alert` MCP calls. |
| `main.py` | FastAPI app: routes below, plus process lifecycle (constructs `HealthWriteback`, provisions alerts once at startup if configured, kicks off an immediate audit cycle per service, then starts the scheduler). |
| `scripts/provision_dashboards.py` | One-time SigNoz dashboard provisioning via `signoz_create_dashboard`. No upsert-by-title — check `signoz_list_dashboards` before re-running, or delete the existing "Telemetry Health Guardian" dashboard first. |

There is no `rules/r5_*.py` or `rules/r4_*.py` module — R5 is out of scope
by design, and R4 (span-naming) is merged into R1. See the root README's
["What it catches"](../README.md#what-it-catches) table for the full
rationale.

## HTTP API (`main.py`)

| Route | Method | Behavior |
|---|---|---|
| `/health` | GET | Liveness check — `{"status": "ok"}`. |
| `/audit/run` | POST | Body: `{"service": "<name>" \| null}`. Runs `scheduler.run_audit_cycle` immediately for that service (or all services combined, if omitted/`null`) and returns the serialized result. Maps a `SignozMCPError` to `502`, any other failure to `500`. |
| `/audit/report/{service}` | GET | Returns whatever `AuditStore` has cached for `service`. `service` also accepts the literal aliases `_all_` or `all` for the no-scope (all-services-combined) audit. `404` if nothing has completed yet for that service. |
| `/chat` | POST | Body: `{"question": "...", "service": "<name>" \| null}`. Reads the cached findings for `service` (running one fresh audit cycle only if nothing has ever been cached for it yet — it does not re-audit on every message) and answers via `narrative.answer_question`. Response includes `rules_fired_but_uncited`, `validate_citations`'s best-effort list of rule IDs that fired but weren't named in the LLM's answer. |

`POST /audit/run` and the scheduler's periodic tick call the exact same
`run_audit_cycle` function — there is deliberately no second,
HTTP-specific audit implementation.

## Running it

```bash
# from the repo root, after the Setup steps in ../README.md
uvicorn guardian.main:app --reload --port 8000
```

Startup, per `main.py`'s `lifespan`:
1. Constructs one `HealthWriteback` (owns this process's OTLP meter/logger providers).
2. If `GUARDIAN_ALERT_WEBHOOK_URL` is set, provisions the notification channel and the four alerts via MCP once. If unset, this step is skipped with a log line — you can provision them out-of-band instead with `python experiments/test_stage6.py --provision-alerts` (run from the repo root).
3. Fires one immediate audit cycle per `AUDIT_SERVICES` entry in the background, then starts the recurring `AsyncIOScheduler` loop on top of it.

Provision the SigNoz dashboards separately (also one-time):

```bash
python guardian/scripts/provision_dashboards.py --dry-run   # print the payload, no MCP call
python guardian/scripts/provision_dashboards.py              # actually create it
```

## Configuration

All of `guardian/`'s env vars are documented inline in
[`../env.example`](../env.example): `SIGNOZ_INSTANCE_URL` /
`SIGNOZ_MCP_URL` / `SIGNOZ_API_KEY` / `SIGNOZ_MCP_MODE` /
`SIGNOZ_CLOUD_REGION` (MCP connection — see `mcp_client.py`'s module
docstring for the cloud-vs-self-hosted split), `LLM_PROVIDER` /
`OPENAI_API_KEY` / `OPENAI_MODEL` / `OLLAMA_BASE_URL` / `OLLAMA_MODEL`
(reasoning layer), and `AUDIT_INTERVAL_MINUTES` / `FASTAPI_PORT` /
`AUDIT_SERVICES` / `AUDIT_WINDOW` / `GUARDIAN_CORS_ALLOW_ORIGINS` /
`GUARDIAN_CORS_ALLOW_ORIGIN_REGEX` / `GUARDIAN_ALERT_WEBHOOK_URL`
(service behavior). `scheduler.py` defaults `AUDIT_WINDOW` to `5m` if
unset.

## Tests

```bash
pip install -e guardian[test]
pytest guardian/tests
```

`guardian/tests/` covers each rule module's pure `evaluate()` logic in
isolation (`test_r1_missing_fields.py` … `test_r7_cross_service_breaks.py`),
plus `test_health_score.py`, `test_llm_client.py` (mocked `litellm`),
`test_narrative.py`, `test_scheduler.py`, and `test_main.py` (the FastAPI
routes above, via `httpx`/`TestClient` with the MCP and LLM layers
mocked). These are unit tests against mocked adapters, not a live SigNoz
or LLM round trip — see the root README's
[Verifying against a live audit](../README.md#verifying-against-a-live-audit)
section for the `experiments/test_stage3.py` … `test_stage8.py` drivers
that were actually run against a live `SigNoz` MCP session and a real LLM
provider.

## Honesty notes worth knowing

Several rule and client modules carry an explicit disclaimer in their own
docstrings: they were written against the SigNoz MCP tool/response
reference (`mcp_client.py`, `signoz-mcp-server` README, v0.8.0) rather than
independently confirmed live from this environment, since this
environment has no network access to a real SigNoz instance. Where a live
run *did* happen and surfaced a bug, the fix and the date are recorded
directly in that module's docstring (e.g. `r7_cross_service_breaks.py`'s
"Bug found and fixed here (live run, 2026-07-24)" note about
`signoz_search_traces`'s `filter` not reliably filtering, and the fix of
rebuilding the query on `signoz_execute_builder_query` with explicit
`selectFields` — the same fix R1 and R6 each independently had to apply).
Read the module docstring before trusting a rule's live behavior blindly.

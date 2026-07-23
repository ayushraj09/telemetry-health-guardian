# Stage 8 — Final Rehearsal of the Section 8 Verification Flow

This is the pre-submission run-through of
[Section 8](../telemetry-health-guardian-BUILD-SPEC.md#8-end-to-end-verification-flow)
of the build spec, using the same drivers each earlier stage already built
(`experiments/test_stage7.py` plus the provisioning/alert scripts from
Stage 6). It's written as an operator checklist rather than a new script —
every command it calls already exists and was verified individually at its
own stage; this pass is about running them back-to-back, once, in the exact
order a live demo would, and confirming nothing regressed at the seams.

Run this **immediately before any live demo or submission recording**, on a
freshly-provisioned SigNoz Cloud project if possible, so what's on screen
matches what a grader/judge would see cold.

## Pre-flight

```bash
cp env.example .env        # fill in real SigNoz Cloud MCP creds + LLM_PROVIDER
pip install -r requirements.txt
pip install -e otel-griptape
pip install -e guardian[test]

python verify_mcp_connection.py        # Stage 0 gate, re-confirmed
```
Expect: a successful response from at least one `signoz_*` MCP call. If this
fails, stop — do not proceed to the rest of the flow on a broken MCP
connection (fall back to the self-hosted Docker path per Stage 0 first).

## Step 1 — Healthy baseline

```bash
cd demo-agent-app
python fixtures/generate_fixture_pdf.py     # once, if fixtures/long_climate_report.pdf isn't present
python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf
cd ..

python guardian/scripts/provision_dashboards.py --dry-run
python guardian/scripts/provision_dashboards.py     # skip if already provisioned this project

uvicorn guardian.main:app --reload --port 8000 &
streamlit run frontend/app.py &

cp experiments/test_stage7.py .
python test_stage7.py --step baseline
```
**Check (Section 8, item 1):** the SigNoz dashboard's health-score panel
shows a healthy baseline, ~95+, across services. Read this off the live
panel, not just the JSON `test_stage7.py` prints — the gate is about what's
on screen in SigNoz.

## Step 2 — Trigger chaos

```bash
cd demo-agent-app
CHAOS_MODE=1 CHAOS_R1_RATE=1.0 CHAOS_R2_RATE=1.0 CHAOS_R3_RATE=1.0 CHAOS_R6_RATE=1.0 CHAOS_SEED=42 \
    python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf
cd ..
```
**Check (item 2):** four distinct faults were injected in this run — a
missing token field (R1), a spiked-cardinality attribute (R2), a severed
span-parent link (R3), and a truncated tool payload (R6). (R7 is not built
in this submission, so its handoff-severing step is intentionally skipped —
see the README/write-up for why.)

## Step 3 — Confirm the live drop

```bash
python test_stage7.py --step chaos
```
**Check (item 3):** the health-score panel visibly drops in SigNoz within
one audit cycle of the chaos run landing — watch the panel itself during
this step, since "within one audit cycle" is a live-timing check that a
printed API response can't substitute for.

## Step 4 — Chat narrative

Open the Streamlit chat tab and ask:

> why did the research pipeline's score drop?

**Check (item 4):** the response names each rule that fired — R1, R2, R3,
and R6 by ID, each tied to its specific offending span/attribute (for R6
specifically: that the PDF's extracted text was truncated before reaching
context) — and keeps all four distinct from each other rather than
collapsing them into one generic "something's wrong" answer. `test_stage7.py`'s
`rules_fired_but_uncited` field on the `/chat` response is a mechanical
proxy for "every fired rule was named" — confirm it's empty, then still read
the printed answer itself, since the mechanical check can't confirm the
rules are correctly *distinguished* from each other, only that all their
IDs appear somewhere in the text.

## Step 5 — Alerts

**Check (item 5):** the threshold alert (health score < 70) and the
anomaly-detection alert (cardinality spike) both fire in SigNoz off the
same chaos run, and the alert notification carries the same rule-level
diagnosis as the chat answer above — not a bare "threshold breached" with
no context. If `GUARDIAN_ALERT_WEBHOOK_URL` is set in `.env`, a
[webhook.site](https://webhook.site) URL is the fastest way to observe this
live during rehearsal without needing a real notification channel wired up.

## Step 6 — Dashboard panels

**Check (item 6):** the PromQL-vs-Builder-Query comparison panel renders
the same health-score series both ways, side by side, and the
cardinality-risk table lists the R2 chaos attribute with its distinct-value
ratio and average byte length — the two conditions Section 4.3.1 requires
R2 to check jointly.

## Step 7 — Standalone library

```bash
cd /tmp && python -m venv otel-griptape-standalone-check
source otel-griptape-standalone-check/bin/activate
pip install -e /path/to/telemetry-health-guardian/otel-griptape
python -c "import otel_griptape; print(otel_griptape.__version__ if hasattr(otel_griptape, '__version__') else 'imported OK')"
deactivate
```
**Check (item 7):** `otel-griptape` installs and imports cleanly in an
environment with no `guardian/` package present at all — confirming it
really is independently installable, not accidentally coupled to the
Guardian service's dependencies.

## Sign-off

Record the outcome of this pass the same way every earlier stage's gate was
recorded, in `experiments/stage_wise_guidance.txt` — date, SigNoz project,
chaos seed, and which of the seven checks above passed cleanly on the first
attempt versus needed a rerun. A rehearsal that surfaces a rough edge here
(a slow-to-refresh panel, a webhook that needs re-arming between runs) is
exactly what this stage is for — better to hit it now than live.

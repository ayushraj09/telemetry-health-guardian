# Telemetry Health Guardian — Write-up

## The one-sentence version

We don't observe the agent's behavior. We observe whether the observability
itself is trustworthy — then fix the instrumentation that produces it.

## The problem this solves

Most agent observability tooling assumes the telemetry it's looking at is
correct, and answers "is my agent behaving well?" But telemetry is code,
and code has bugs: a span missing its token-usage fields, raw prompt text
leaking into an indexed attribute and blowing up cardinality, a thread-pool
tool call losing its parent span, a 50-page PDF silently truncated to a few
hundred bytes before it ever reaches the model. None of these show up as an
error anywhere — the agent runs "successfully," the trace looks complete
enough to skim past, and the dashboard stays green while the underlying
data quietly stops being trustworthy. Telemetry Health Guardian audits for
exactly that class of problem, on top of a framework (Griptape) that had no
existing OpenTelemetry support to audit in the first place.

## What was built (MVP, fully wired end-to-end)

- **`otel-griptape`** — hooks into Griptape's own `@observable` extension
  points (no monkey-patching of `Agent`/`Structure`/`PromptDriver`) to emit
  GenAI-semconv-compliant spans, patches
  `concurrent.futures.ThreadPoolExecutor.submit` for correct context
  propagation across the demo app's concurrent claim-checking, and records
  `payload.raw_bytes` / `payload.captured_bytes` for R6. One narrow,
  documented exception: `gen_ai.response.finish_reasons` requires patching
  `OpenAiChatPromptDriver._to_message` directly, because Griptape's own
  `Message` object discards `finish_reason` before the observability hook
  ever sees it.
- **`demo-agent-app`** — the four-stage document research pipeline
  (Planner → Fetch & Read → Fact-Check & Cite → Report Writer), built so
  each required rule has a natural, non-contrived trigger in the domain
  itself rather than a synthetic stand-in: concurrent claim-checking for
  R3, unbounded extracted PDF text for R2, and a long PDF whose extracted
  text is bounded before reaching context for R6. `chaos.py` provides
  seeded, on-demand fault injection for all four required rules so the
  failure modes are reproducible on command rather than waited-for.
- **`guardian/rules/`** — R1, R2, R3, and R6 implemented exactly per the
  build spec's detection logic (Section 4.3.1), each against real SigNoz
  data via the MCP client, each rule's findings kept structurally distinct
  from the others (R3 vs. R6 in particular — a broken span parent and a
  truncated payload are never merged into one generic finding).
- **`guardian/llm_client.py` + `narrative.py`** — LiteLLM abstraction
  switchable between OpenAI and Ollama via `LLM_PROVIDER`, and a reasoning
  layer that turns the rule engine's combined JSON into a cited
  natural-language report — every finding sentence names the rule ID and
  the offending span/attribute, checked mechanically by
  `narrative.py::validate_citations`.
- **`guardian/scheduler.py` + `main.py`** — an APScheduler audit loop and a
  FastAPI backend (`/audit/run`, `/audit/report/{service}`, `/chat`,
  `/health`) sharing one orchestration path (`run_audit_cycle`) so the
  scheduled loop and a manual trigger can never drift apart.
- **`guardian/writeback.py` + `scripts/provision_dashboards.py`** — writes
  the health-score metric and issue logs back into SigNoz, and provisions
  the dashboard panels from Section 4.4 (health score time series,
  missing-field rate, cardinality risk table, orphaned-span trend, silent
  truncation rate, and a PromQL-vs-Builder-Query comparison panel) plus the
  Section 4.5 alerts (health score < 70, cardinality spike anomaly,
  truncation rate > 5%) programmatically via MCP.
- **`frontend/app.py`** — a deliberately thin two-view Streamlit app (health
  report tab, chat tab) — SigNoz remains the primary dashboard.

## What was not built, and why that's a boundary rather than a gap

- **R7** (cross-service trace breaks) is explicit stretch scope in the
  build spec, gated behind the Stage 7 verification flow passing *and* an
  explicit instruction to continue building it. This submission stopped at
  the required MVP rather than starting `citation_service.py` or
  `rules/r7_cross_service_breaks.py` without that go-ahead. Nothing in the
  health-score formula, the dashboards, or the alerts references an R7 term
  as a placeholder — per spec, an unbuilt R7 means the term is omitted
  entirely, not zeroed out.
- **R5** (raw-content/PII leakage heuristic) is out of scope by explicit
  design decision, with no stub, placeholder function, or partial logic
  anywhere in the repository.
- **Ollama-path verification** for Stage 5's dual-provider gate was
  exercised against OpenAI; the guidance notes in
  `experiments/stage_wise_guidance.txt` flag Ollama as still needing a live
  end-to-end pass, which is also the natural way to trigger R6 for real (a
  small `num_ctx` default silently truncating context) rather than only via
  `chaos.py`.

## Framework choice: why Griptape

Section 7's due-diligence checklist was run against six candidate agent
frameworks. Agno, Google ADK, Mastra, AWS Strands Agents, and Letta all
already have maintained or native OpenTelemetry/GenAI-semconv
instrumentation, which would have made `otel-griptape`'s equivalent for any
of them redundant. Griptape had no maintained instrumentor anywhere — a
genuine gap, not a manufactured one — which is what makes the library half
of this project a real contribution rather than a re-implementation of
something that already exists. This check is meant to be re-run at the
start of any future stage, since the instrumentation-coverage landscape for
agent frameworks changes month to month; nothing in this submission's
build history found that Griptape had gained a maintained instrumentor in
the interim.

## Known limitations

- `otel-griptape` and Guardian's R6 rely on two custom span attributes
  (`payload.raw_bytes`, `payload.captured_bytes`) that are not part of any
  OTel standard. Pointed at a target app that doesn't set them, R6 simply
  never fires — see the README's Portability section for the full
  agnostic/non-agnostic boundary.
- The Guardian's rule engine (R1/R2/R3) is genuinely framework-agnostic
  against any OTel `gen_ai.*`-emitting service; `otel-griptape` itself is
  not, and isn't meant to be — it fills a Griptape-specific gap by design.
- Two small path deviations from the spec's canonical repo layout
  (`verify_mcp_connection.py` at root instead of under `scripts/`,
  `provision_dashboards.py` under `guardian/scripts/` instead of a
  top-level `scripts/`) were left as-is at this polish stage rather than
  moved, to avoid touching every earlier stage's recorded verification
  commands for a cosmetic-only change. See the README's Repository Layout
  section.

## Verification

The full Section 8 end-to-end flow, and how it was rehearsed for this
submission, is in [`docs/REHEARSAL.md`](REHEARSAL.md). Per-stage build
notes and gate-check results are in
[`experiments/stage_wise_guidance.txt`](../experiments/stage_wise_guidance.txt).

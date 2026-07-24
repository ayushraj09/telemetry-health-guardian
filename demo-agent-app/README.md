# demo-agent-app

A four-stage document research pipeline (Planner → Fetch & Read →
Fact-Check & Cite → Report Writer), built on [Griptape](https://github.com/griptape-ai/griptape)
and instrumented via [`otel-griptape`](../otel-griptape/README.md). It
exists as the concrete workload the [Guardian service](../guardian/README.md)
audits — not a demo of Griptape or of document research on its own merit.

This is a component of the [Telemetry Health Guardian](../README.md)
project — see the root README for the system-level architecture and
`chaos.py`'s trigger table. This document covers only `demo-agent-app/`
itself: what each stage does, why its failure modes line up with what
the Guardian's rule engine (R1/R2/R3/R6/R7) detects, and how to run it.

---

## Pipeline stages

| Stage | Module | What it does |
|---|---|---|
| 1 — Planner | `planner.py` | One Griptape `Agent()` call: given the research question and available source filenames, returns which sources to use and 3–6 specific factual claims to check. |
| 2 — Fetch & Read | `fetch_and_read.py` | Extracts full text from each source PDF via `pypdf`. Never truncates, regardless of `CHAOS_MODE` — `payload.raw_bytes == payload.captured_bytes` unconditionally on this stage's span. Calls `otel_griptape.payload_tracking.record_payload_sizes()` itself (this stage does its own PDF extraction outside Griptape's `Tool` abstraction, so it isn't auto-tracked). Also where R2's chaos trigger tags the span with raw extracted text, if fired. |
| 3 — Fact-Check & Cite | `fact_check_and_cite.py` | Checks each claim from the plan against the source text, as **parallel** calls (a thread pool, not sequential) — both a real design choice for latency and R3's natural trigger, since concurrent `Agent().run()` calls are exactly where OTel context propagation can silently break if a library doesn't handle it. Each claim's citation is then fetched over real HTTP from `citation_service.py`, a separate process — that hop is R7's site. Per-claim, `chaos.py` may (independently) sever context (R3), drop the outbound `traceparent` header (R7), or reroute the verdict check itself through `ollama_r6.py` instead of OpenAI (R6). |
| 4 — Report Writer | `report_writer.py` | One ordinary Griptape `Agent()` call synthesizing the plan, source text, and fact-checked claims into a final report. No special telemetry concerns — the point is that this stage, like every other, is just a normal `gen_ai` call and should conform to R1 like any other. |

`app.py` orchestrates all four stages under one root span
(`research_pipeline.run`) and is the CLI entry point.

## Supporting modules

| Module | Role |
|---|---|
| `telemetry.py` | `init_telemetry()` — bootstraps a bare OTel `TracerProvider` exporting to SigNoz's OTLP endpoint, and configures Griptape's default driver (`gpt-4.1-nano` via `OpenAiChatPromptDriver`). Returns the provider so callers pass it into `otel_griptape.instrument(tracer_provider=...)`. Registers an `atexit` flush so a short CLI run doesn't exit before its batched spans are exported. |
| `chaos.py` | Seeded fault injection (`CHAOS_MODE=1` required for any trigger to fire) — see the trigger table in the [root README](../README.md#running-it). Works by monkeypatching `opentelemetry.sdk.trace.Span.set_attribute` for the process lifetime, deliberately kept outside both `otel-griptape` and this app's real business logic, so it mimics a real telemetry regression rather than editing the reference-correct instrumentation. |
| `ollama_r6.py` | R6's real-truncation path: when `chaos.py` reroutes a claim-check to Ollama, this module calls Ollama's raw `/api/chat` HTTP endpoint directly (not Griptape's `OllamaPromptDriver`, which never populates token usage), reading real `prompt_eval_count`/`eval_count` for token counts and deriving `payload.captured_bytes` from `prompt_eval_count` as a chars-per-token approximation. Sets its own `gen_ai.*` attributes by hand since this call bypasses Griptape's instrumentation entirely. |
| `citation_service.py` | A separate FastAPI process (its own OTel `service.name`, `"citation-service"`) providing `/verify_citation`. Its middleware calls `opentelemetry.propagate.extract` on incoming request headers before starting its own span — if the caller's `traceparent` survived, this becomes a correctly-parented child span in the same trace; if `chaos.py`'s R7 trigger dropped it, this becomes a disconnected new root span in a new trace, R7's exact failure mode, produced for real rather than simulated. |
| `fixtures/generate_fixture_pdf.py` | One-time generator for a synthetic 35-page PDF (`fixtures/long_climate_report.pdf`) with one specific, checkable numbered fact per page — long and fact-dense enough that R6's "only the first N pages reach context" failure mode provably loses identifiable facts rather than producing a coincidentally-fine answer. |

## Running it

```bash
# from the repo root, after the Setup steps in ../README.md
python demo-agent-app/fixtures/generate_fixture_pdf.py   # one-time

cd demo-agent-app
python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" \
    --pdf fixtures/long_climate_report.pdf

# with fault injection
CHAOS_MODE=1 python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" \
    --pdf fixtures/long_climate_report.pdf

# R7's citation service — a separate process, own port
uvicorn citation_service:app --port 8100
```

R6's Ollama-routed path additionally needs a local Ollama instance with
the configured model pulled (`ollama serve` / `ollama pull llama3.2`, or
whatever `CHAOS_R6_OLLAMA_MODEL` is set to).

## Configuration

`demo-agent-app`'s env vars, documented in [`../env.example`](../env.example):
`OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` /
`OTEL_SERVICE_NAME` (telemetry export target), `OPENAI_API_KEY` (Griptape's
default driver), `CITATION_SERVICE_URL` / `CITATION_SERVICE_PORT` /
`CITATION_SERVICE_TIMEOUT_SECONDS` (the R7 service boundary),
`OLLAMA_R6_TIMEOUT_SECONDS` (`ollama_r6.py`'s own HTTP call timeout,
separate from Guardian's `OLLAMA_BASE_URL`/`OLLAMA_MODEL` — same Ollama
instance, different call site), and every `CHAOS_*` var — see the root
README's [chaos trigger table](../README.md#running-it) for the full
list of triggers, env vars, and default rates. `CHAOS_SEED` makes a run
reproducible.

## Why these particular failure modes

Each stage's design was chosen to make a specific rule's target failure
real rather than staged:

- **R1** (missing fields): any stage's `gen_ai` chat span can have a
  usage field dropped by `chaos.py` — no stage is treated as exempt.
- **R2** (cardinality): Fetch & Read is the one stage handling large raw
  text outside a structured attribute, making it the natural site for an
  accidental full-text-as-indexed-attribute mistake.
- **R3** (orphaned spans): Fact-Check & Cite's concurrent claim checks are
  the only place in this app where `ThreadPoolExecutor`-based concurrency
  meets OTel context — exactly where a naive integration would silently
  drop the parent span.
- **R6** (silent truncation): the fixture PDF and `ollama_r6.py`'s small
  `num_ctx` together produce a genuine content-window truncation with a
  confident, wrong answer and no error anywhere — the failure mode
  Section 2 of the build spec describes as R6's canonical case.
- **R7** (cross-service breaks): `citation_service.py` is a real second
  process with its own `service.name`, so a dropped `traceparent` header
  produces an actually-disconnected second trace, not a simulated one.

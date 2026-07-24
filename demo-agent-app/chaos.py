"""
Seeded fault injection for Telemetry Health Guardian (Section 4.1).

Chaos works by monkeypatching `opentelemetry.sdk.trace.Span.set_attribute`
for the lifetime of the process when enabled -- deliberately, not as a
shortcut. `otel-griptape`'s instrumentor.py is the reference "do it right"
implementation (see its own module docstring); chaos.py must never edit it
to inject bugs. Patching at the Span.set_attribute boundary keeps chaos
strictly outside both otel-griptape and demo-agent-app's real business
logic -- it mimics how a real telemetry regression would actually show up
(something between the SDK and the exporter misbehaving), not a rewrite of
the instrumentation code itself.

Implements the Stage 3, Stage 4, and Stage 8 rules, per the build spec's
own stage order (Section 6):
  - R1: skip a token-usage field on some spans
  - R2: tag a span with raw extracted document text as an INDEXED attribute
        (instead of otel-griptape's correct event-based capture, Section 4.2)
  - R3: sever one Fact-Check & Cite parallel claim-check call's parent link
        by fabricating a bogus (same-trace, non-existent-span-id) parent
        context for it -- see `maybe_break_context_for_claim_check`'s own
        docstring for why this, rather than clearing context outright, is
        the correct chaos trigger for R3's literal detection logic.
  - R6: route one Fact-Check & Cite claim-check call through a real local
        Ollama model with a small `num_ctx`, instead of the normal OpenAI
        path -- Ollama's own context-window limit silently drops whatever
        doesn't fit, a REAL truncation, not a simulated one. See
        `maybe_use_ollama_for_claim_check`.
  - R7: call `citation_service.py` without forwarding the W3C `traceparent`
        header on one fetch_check_and_cite.py claim's citation-fetch call --
        see `maybe_drop_traceparent_for_citation_call`'s own docstring.

Usage:
    CHAOS_MODE=1 python app.py --question "..." --pdf fixtures/long_climate_report.pdf

Env vars:
    CHAOS_MODE            "1"/"true" to enable. Default off -- untouched
                          runs behave exactly as Stage 1/2 already verified.
    CHAOS_R1_RATE         float 0-1, probability a given chat span has one
                          token-usage field dropped. Default 0.3.
    CHAOS_R2_RATE         float 0-1, probability a given fetch_and_read call
                          gets tagged with raw text as an indexed attribute.
                          Default 1.0 -- see the R2 denominator note below.
    CHAOS_R3_RATE         float 0-1, probability a given parallel claim-check
                          call gets its parent context corrupted. Default
                          0.3 -- deliberately not 1.0: R3's own value as a
                          demo case depends on some claim-check calls in the
                          same run staying correctly parented, so the
                          orphan is visibly one broken branch in an
                          otherwise-healthy trace tree, not every branch.
    CHAOS_R6_RATE         float 0-1, probability a given Fact-Check & Cite
                          claim-check call gets routed through Ollama
                          (with real context-window truncation) instead of
                          the normal OpenAI path. Default 1.0 -- the demo
                          fixture set is small (Section 4.1: "at least one
                          long PDF"), so a low default rate risks the R6
                          gate check simply not firing on a given run.
    CHAOS_R6_OLLAMA_MODEL    Ollama model name used for the R6-routed
                          call. Default "llama3.2" -- must already be
                          pulled locally (`ollama pull llama3.2`).
    CHAOS_R6_NUM_CTX      int, the `num_ctx` (token context window) passed
                          to Ollama for the R6-routed call. Default 2048 --
                          Ollama's own real default (see ollama_r6.py's
                          module docstring for why this specific value is
                          a genuine, reproducible truncation cause and not
                          an arbitrary chaos knob).
    CHAOS_R7_RATE         float 0-1, probability a given Fact-Check & Cite
                          claim's outbound HTTP call to citation_service.py
                          gets sent with NO traceparent header. Default
                          0.5 -- same reasoning as CHAOS_R3_RATE: not 1.0,
                          so some handoffs in the same run stay correctly
                          connected, making the break visibly one severed
                          branch rather than every call looking broken.
    CHAOS_SEED            optional int, for a reproducible run.

IMPORTANT -- R2 verification caveat (tell the user, don't silently work
around it): R2's rule (guardian/rules/r2_cardinality.py) computes
distinct_ratio against `total_spans` = the count of ALL spans in the
audit window (signoz_aggregate_traces count), not just spans that carry
this new attribute key. If the audit window is wide, a few chaos-tagged
spans will never push distinct_ratio > 0.8 even with CHAOS_R2_RATE=1.0.
To actually see R2 fire, run the audit with a narrow time_range covering
only the chaos run (e.g. "5m") right after triggering it.
"""

from __future__ import annotations

import os
import random

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import Span as SDKSpan

from otel_griptape.semconv import GEN_AI_USAGE_INPUT_TOKENS, GEN_AI_USAGE_OUTPUT_TOKENS

# Deliberately an indexed span attribute, not an event -- that's the bug
# this simulates. Distinguishable at a glance from otel-griptape's own
# event-based field (`gen_ai.content` / `gen_ai.tool.content`).
R2_RAW_CONTENT_ATTRIBUTE_KEY = "document.raw_extracted_text"

_installed = False
_original_set_attribute: object | None = None
_rng: random.Random | None = None
_r1_rate = 0.0
_r2_rate = 0.0
_r3_rate = 0.0
_r6_rate = 0.0
_r6_ollama_model = "llama3.2"
_r6_num_ctx = 2048
_r7_rate = 0.0
_r1_decisions: dict[int, str | None] = {}


def _env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def is_enabled() -> bool:
    return _env_flag("CHAOS_MODE", False)


def install_chaos() -> None:
    """No-op unless CHAOS_MODE is truthy. Call this once, as early as
    possible in app.py -- before otel_griptape.instrument() and before
    run_pipeline() -- since this patches the SDK Span class globally, not
    a specific tracer/provider instance."""
    global _installed, _original_set_attribute, _rng, _r1_rate, _r2_rate, _r3_rate, _r6_rate, _r6_ollama_model, _r6_num_ctx, _r7_rate

    if not is_enabled():
        return
    if _installed:
        return

    seed = os.getenv("CHAOS_SEED")
    _rng = random.Random(int(seed)) if seed else random.Random()
    _r1_rate = _env_float("CHAOS_R1_RATE", 0.3)
    _r2_rate = _env_float("CHAOS_R2_RATE", 1.0)
    _r3_rate = _env_float("CHAOS_R3_RATE", 0.3)
    _r6_rate = _env_float("CHAOS_R6_RATE", 1.0)
    _r6_ollama_model = os.getenv("CHAOS_R6_OLLAMA_MODEL", "llama3.2")
    _r6_num_ctx = int(_env_float("CHAOS_R6_NUM_CTX", 2048))
    _r7_rate = _env_float("CHAOS_R7_RATE", 0.5)

    _original_set_attribute = SDKSpan.set_attribute
    SDKSpan.set_attribute = _chaos_set_attribute  # type: ignore[method-assign]
    _installed = True

    print(
        f"[chaos] installed -- R1 rate={_r1_rate}, R2 rate={_r2_rate}, R3 rate={_r3_rate}, "
        f"R6 rate={_r6_rate} (model={_r6_ollama_model}, num_ctx={_r6_num_ctx}), R7 rate={_r7_rate}, "
        f"seed={seed or 'random'}"
    )


def uninstall_chaos() -> None:
    """Restore the real Span.set_attribute. Mainly for test harnesses that
    run multiple pipeline invocations in one process -- a single CLI run
    doesn't need this since the process exits anyway."""
    global _installed
    if not _installed or _original_set_attribute is None:
        return
    SDKSpan.set_attribute = _original_set_attribute  # type: ignore[method-assign]
    _installed = False


def reset_state() -> None:
    """Clear the per-span R1 decision cache between runs in the same
    process. Not needed for a single `python app.py ...` invocation."""
    _r1_decisions.clear()


# --- R1: skip a token-usage field on some spans -----------------------------

def _chaos_set_attribute(self: SDKSpan, key: str, value: object) -> None:
    if key in (GEN_AI_USAGE_INPUT_TOKENS, GEN_AI_USAGE_OUTPUT_TOKENS) and _r1_should_skip(self, key):
        return  # silently drop -- the span ends up missing this required field
    _original_set_attribute(self, key, value)  # type: ignore[misc]


def _r1_should_skip(span: SDKSpan, key: str) -> bool:
    """Decide once per span (on whichever of the two usage keys arrives
    first -- instrumentor.py always sets input_tokens then output_tokens)
    whether this span is chaos-triggered, and if so, which single field to
    drop. Keyed by id(span): safe within one process run since a span
    object can't be garbage-collected and reused for a different span
    while `with tracer.start_as_current_span(...)` still holds a live
    reference to it."""
    span_key = id(span)
    if span_key not in _r1_decisions:
        triggered = _rng.random() < _r1_rate  # type: ignore[union-attr]
        _r1_decisions[span_key] = (
            _rng.choice([GEN_AI_USAGE_INPUT_TOKENS, GEN_AI_USAGE_OUTPUT_TOKENS]) if triggered else None  # type: ignore[union-attr]
        )
    return _r1_decisions[span_key] == key


# --- R2: raw content as an indexed attribute --------------------------------

def tag_r2_raw_content(text: str, span: trace.Span | None = None) -> None:
    """Call this from demo-agent-app code right after extracting raw
    document/tool content, on whichever span is current at that point
    (e.g. fetch_and_read.py's `fetch_and_read.read_pdf` span). Only
    actually tags the span when chaos is installed and this call wins the
    R2 dice roll -- callers invoke this unconditionally and let chaos
    decide, so no call site needs to know chaos's rates or even whether
    chaos is enabled at all.
    """
    if not _installed or _rng is None:
        return
    if _rng.random() >= _r2_rate:
        return
    target = span if span is not None else trace.get_current_span()
    target.set_attribute(R2_RAW_CONTENT_ATTRIBUTE_KEY, text)


# --- R3: sever one parallel claim-check call's parent link -----------------

def maybe_break_context_for_claim_check() -> otel_context.Context | None:
    """Call this from fact_check_and_cite.py's `_check_one_claim`,
    immediately before scheduling that one claim's LLM call onto the
    thread pool (i.e. right before `loop.run_in_executor(...)`). If chaos
    fires for this call, returns a context the caller should `attach()` in
    place of the real ambient one for the duration of that submission; if
    it doesn't fire (chaos off, or this call didn't win the dice roll),
    returns None and the caller should change nothing, leaving
    otel-griptape's correct context-propagation path (context_propagation.py)
    fully in control, exactly as Stage 2 built it.

    Design note (see r3_orphaned_spans.py's module docstring for the full
    reasoning): this fabricates a bogus parent SpanContext that shares the
    REAL current trace_id but references a span_id that was never actually
    created, rather than clearing context outright. Clearing context
    entirely would make the next span a brand-new trace ROOT (no
    parent_span_id set at all) -- a real bug, but not the one R3's
    detection logic (Section 4.3.1) checks for, which is specifically
    "parent_span_id is set but does not resolve to any span within the
    same trace." This produces exactly that case, deterministically, still
    within a single service's single trace -- matching R3's scope as
    distinct from R7's (Section 4.3.1: "R3 = broken within one service's
    trace tree").
    """
    if not _installed or _rng is None:
        return None
    if _rng.random() >= _r3_rate:
        return None

    current_span_context = trace.get_current_span().get_span_context()
    if not current_span_context.is_valid:
        return None  # nothing to corrupt -- no real trace context to fabricate a bogus parent within

    fake_parent_span_id = _rng.getrandbits(63) or 1  # never 0 -- that's OTel's own "invalid" sentinel
    fake_parent_context = trace.SpanContext(
        trace_id=current_span_context.trace_id,
        span_id=fake_parent_span_id,
        is_remote=True,
        trace_flags=current_span_context.trace_flags,
    )
    return trace.set_span_in_context(trace.NonRecordingSpan(fake_parent_context))


# --- R6: route a claim-check call through real Ollama context-window ------
# truncation instead of the normal OpenAI path -----------------------------

def maybe_use_ollama_for_claim_check() -> bool:
    """Call this from fact_check_and_cite.py's `_run_claim_check_llm`,
    once per claim, BEFORE deciding which backend checks it. No-op
    (returns False) unless chaos is installed and wins the R6 dice roll --
    same pattern as every other chaos gate in this module.

    When this returns True, the caller must route that one claim's check
    through `ollama_r6.run_claim_check_via_ollama` instead of the normal
    griptape/OpenAI `Agent()` path. That function sends the FULL combined
    source text to a real local Ollama model with `num_ctx` set to
    `ollama_num_ctx()` -- Ollama's own context-window limit then silently
    drops whatever doesn't fit, a real truncation this module doesn't fake
    or need to fake. See ollama_r6.py's module docstring for the full
    mechanism and why it replaced the earlier fixed-character-slice
    approach.
    """
    if not _installed or _rng is None:
        return False
    return _rng.random() < _r6_rate


def ollama_model() -> str:
    """The Ollama model name configured for R6-routed calls (`CHAOS_R6_OLLAMA_MODEL`,
    default 'llama3.2'). Valid to call whether or not chaos is installed --
    returns the module-level default if `install_chaos()` hasn't run."""
    return _r6_ollama_model


def ollama_num_ctx() -> int:
    """The `num_ctx` configured for R6-routed Ollama calls (`CHAOS_R6_NUM_CTX`,
    default 2048 -- Ollama's own real default). Valid to call whether or
    not chaos is installed."""
    return _r6_num_ctx



# --- R7: drop the traceparent header on an outbound service-to-service call -

def maybe_drop_traceparent_for_citation_call() -> bool:
    """Call this from fact_check_and_cite.py's `_fetch_citation`,
    immediately before injecting the W3C `traceparent` header into the
    outbound HTTP request to `citation_service.py`. Returns `True` when
    chaos fires for this call -- the caller must then send the request
    WITHOUT a traceparent header, so the receiving citation_service.py
    span has nothing to extract and starts as a new, disconnected trace
    root instead of a continuation of the caller's trace. This is R7's
    exact failure mode (Section 2: "the W3C traceparent header isn't
    propagated, one trace silently becomes two disconnected ones").
    Returns `False` when chaos is off or this call didn't win the R7 dice
    roll -- the caller injects the header normally via
    `otel_griptape.context_propagation.inject_traceparent_header`, the
    already-correct "do it right" mechanism (Section 4.2).

    Design note, same shape as `maybe_break_context_for_claim_check`'s:
    this only ever affects the OUTBOUND REQUEST's headers, never
    otel-griptape's own context-propagation code -- chaos stays strictly
    outside both otel-griptape's and demo-agent-app's real instrumentation
    logic (see module docstring), it just chooses, per call, whether the
    correct mechanism gets used or skipped.
    """
    if not _installed or _rng is None:
        return False
    return _rng.random() < _r7_rate
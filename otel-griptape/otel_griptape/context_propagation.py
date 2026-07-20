"""Sync/async parent-span handling + W3C traceparent forwarding.

Two independent problems live here:

1. Context propagation across thread-pool execution (needed for R3).
   `griptape.mixins.FuturesExecutorMixin` (griptape's own subtask fan-out)
   and app code that calls `asyncio`'s default executor (as this project's
   `fact_check_and_cite.py` does via `loop.run_in_executor(None, ...)`) both
   ultimately go through `concurrent.futures.ThreadPoolExecutor`.

   `ThreadPoolExecutor` reuses a small pool of persistent worker threads
   across many submitted work items. A common but *incorrect* fix is
   `opentelemetry-instrumentation-threading`, which captures the OTel
   context at `Thread.start()` time -- but for a persistent pool, that
   start() happens once per worker thread, not once per submitted task.
   Every task that later reuses that worker thread would incorrectly
   inherit whatever context was live when the worker was first spun up,
   not the context of whoever actually submitted that task. That's exactly
   the kind of broken parent link R3 exists to catch, so this library must
   not rely on it.

   The correct fix, implemented below: patch `ThreadPoolExecutor.submit` to
   snapshot the caller's OTel context *at submission time*, then explicitly
   `attach`/`detach` that snapshot inside the worker thread for the
   duration of that one task -- per Section 4.2's instruction to use
   `opentelemetry.context` attach/detach directly rather than a generic
   thread-instrumentation package.

2. W3C `traceparent` propagation across outbound HTTP calls to another
   service (needed later for R7, when Agent A calls Agent B over HTTP).
   `inject_traceparent_header` below wraps a headers dict for outbound
   requests; not wired into a specific HTTP client yet since the demo app
   doesn't make agent-to-agent calls until Stage 7's `citation_service.py`
   exists, but the mechanism is built and tested now per Stage 2's
   instructions.
"""

from __future__ import annotations

import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import propagate

T = TypeVar("T")

_original_submit: Callable[..., Any] | None = None
_patched = False


def _context_preserving_submit(self: ThreadPoolExecutor, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Any:
    """Replacement for ThreadPoolExecutor.submit that snapshots the calling
    thread's OTel context and re-attaches it inside the worker thread for
    the duration of `fn`, regardless of whether the worker thread is new or
    reused from a previous, unrelated submission.
    """
    ctx = otel_context.get_current()

    @functools.wraps(fn)
    def _wrapped(*inner_args: Any, **inner_kwargs: Any) -> T:
        token = otel_context.attach(ctx)
        try:
            return fn(*inner_args, **inner_kwargs)
        finally:
            otel_context.detach(token)

    assert _original_submit is not None  # noqa: S101 -- set by instrument_context_propagation()
    return _original_submit(self, _wrapped, *args, **kwargs)


def instrument_context_propagation() -> None:
    """Monkey-patch `concurrent.futures.ThreadPoolExecutor.submit` process-wide
    so any thread-pool-based concurrency -- griptape's own subtask fan-out,
    or app-level `asyncio.get_running_loop().run_in_executor(None, ...)` --
    correctly propagates trace context per submitted task instead of per
    worker thread. Safe to call more than once; only the first call patches.
    """
    global _original_submit, _patched  # noqa: PLW0603
    if _patched:
        return
    _original_submit = ThreadPoolExecutor.submit
    ThreadPoolExecutor.submit = _context_preserving_submit  # type: ignore[method-assign]
    _patched = True


def uninstrument_context_propagation() -> None:
    """Restore the original ThreadPoolExecutor.submit. Mainly useful for tests."""
    global _patched  # noqa: PLW0603
    if not _patched or _original_submit is None:
        return
    ThreadPoolExecutor.submit = _original_submit  # type: ignore[method-assign]
    _patched = False


def inject_traceparent_header(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Return a headers dict with the current span's W3C `traceparent` (and
    `tracestate`, if any) injected, for outbound HTTP calls to another
    service. Merges into `headers` if given rather than replacing it.

    Usage (e.g. inside a custom HTTP tool or `citation_service.py` client):

        headers = inject_traceparent_header({"Content-Type": "application/json"})
        requests.post(url, headers=headers, json=payload)

    Without this, an outbound call to another service starts a brand new,
    disconnected trace on the receiving end -- exactly the failure R7 is
    built to detect.
    """
    result = dict(headers) if headers else {}
    propagate.inject(result)
    return result

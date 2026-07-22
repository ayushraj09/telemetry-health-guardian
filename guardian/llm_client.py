"""LiteLLM OpenAI/Ollama provider abstraction (Section 4.3.3).

Same pure/adapter split as `guardian/rules/*.py`:
  - `resolve_model`: pure. Implements the exact `provider -> model` mapping
    given verbatim in the spec's code sample, as its own testable function
    rather than inlined into `generate()`, so the provider-switch logic
    (what Stage 5's gate check is actually about -- "does the same
    rule-engine output produce a content-equivalent report under both
    LLM_PROVIDER=openai and LLM_PROVIDER=ollama?") can be unit-tested
    without a real network call to either provider.
  - `generate`: the actual `litellm.completion` adapter. Mirrors the
    spec's `llm_client.py` sample almost exactly, plus: (1) explicit
    `provider` override kwarg, so callers (`guardian/narrative.py`) can
    force a specific provider for the dual-provider gate check without
    mutating `os.environ`, and (2) errors are caught and re-raised as
    `LLMClientError` rather than propagating whatever `litellm` raises
    internally, so a caller doesn't need to know litellm's own exception
    hierarchy to handle a failed generation.

IMPORTANT / honesty note: this module cannot be exercised against a real
OpenAI or Ollama endpoint from this environment -- no API key is
configured here, and this sandbox's network egress allowlist doesn't
include api.openai.com or a local Ollama port anyway (see the mcp_client.py
module docstring for the same class of limitation on the SigNoz MCP side).
`resolve_model` and `generate`'s request-shaping are unit-tested against a
mocked `litellm.completion` (guardian/tests/test_llm_client.py) instead --
that confirms the model/provider selection and message shape are correct,
but the actual "is the OpenAI output content-equivalent to the Ollama
output" question in Stage 5's gate check needs a live run with both
providers configured. See experiments/test_stage5.py and
experiments/stage_wise_guidance.txt for how to run that.
"""

from __future__ import annotations

import os
from typing import Any

import litellm


class LLMClientError(RuntimeError):
    """Raised when the underlying `litellm.completion` call fails, wrapping
    whatever litellm/the provider SDK itself raised."""


def resolve_model(provider: str | None = None) -> tuple[str, str]:
    """Pure: resolves the `(provider, model)` pair `generate()` needs.

    Mirrors the spec's Section 4.3.3 code sample exactly:
        model = {
            "openai": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "ollama": f"ollama/{os.getenv('OLLAMA_MODEL', 'llama3.1')}",
        }[provider]

    `provider` defaults to `os.getenv("LLM_PROVIDER", "openai")` when not
    passed explicitly -- explicit argument wins over the env var, which is
    what lets a caller (e.g. the Stage 5 dual-provider gate-check script)
    request both providers in the same process without touching
    `os.environ`.
    """
    resolved_provider = (provider or os.getenv("LLM_PROVIDER", "openai")).lower()

    if resolved_provider == "openai":
        return resolved_provider, os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if resolved_provider == "ollama":
        return resolved_provider, f"ollama/{os.getenv('OLLAMA_MODEL', 'llama3.1')}"

    raise ValueError(f"Unknown LLM_PROVIDER {resolved_provider!r} -- expected 'openai' or 'ollama'")


def _api_base(provider: str) -> str | None:
    """Ollama needs `api_base` pointed at the local server; OpenAI must
    NOT get one (passing an unrelated api_base would silently break
    OpenAI calls), so this stays a provider-conditional, per the spec
    sample's own `if provider == "ollama" else None`."""
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") if provider == "ollama" else None


def generate(
    prompt: str,
    system: str | None = None,
    provider: str | None = None,
    **litellm_kwargs: Any,
) -> str:
    """Impure adapter: calls `litellm.completion` and returns the message
    text. `**litellm_kwargs` passes through extra completion params
    (e.g. `temperature`) without this function needing to enumerate every
    one litellm supports.
    """
    resolved_provider, model = resolve_model(provider)
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]

    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            api_base=_api_base(resolved_provider),
            **litellm_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, re-raised typed below
        raise LLMClientError(
            f"litellm.completion failed for provider={resolved_provider!r} model={model!r}: {exc}"
        ) from exc

    return response.choices[0].message.content
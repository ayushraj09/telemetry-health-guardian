"""Unit tests for llm_client's pure provider/model resolution (Section
4.3.3) and the `generate()` adapter with `litellm.completion` mocked out --
no real OpenAI/Ollama network call, per this module's own honesty note
about why that can't happen from this environment.
"""

import pytest

from guardian import llm_client


# -- resolve_model: pure -----------------------------------------------


def test_resolve_model_openai_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    provider, model = llm_client.resolve_model()

    assert provider == "openai"
    assert model == "gpt-4o-mini"


def test_resolve_model_openai_custom_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")

    provider, model = llm_client.resolve_model()

    assert provider == "openai"
    assert model == "gpt-4o"


def test_resolve_model_ollama_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)

    provider, model = llm_client.resolve_model()

    assert provider == "ollama"
    assert model == "ollama/llama3.1"


def test_resolve_model_ollama_custom_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "mistral")

    provider, model = llm_client.resolve_model()

    assert model == "ollama/mistral"


def test_resolve_model_explicit_provider_overrides_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    provider, _model = llm_client.resolve_model(provider="ollama")

    assert provider == "ollama"


def test_resolve_model_unknown_provider_raises():
    with pytest.raises(ValueError):
        llm_client.resolve_model(provider="anthropic")


# -- generate: adapter, litellm.completion mocked ------------------------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


def test_generate_calls_litellm_with_resolved_model_and_messages(monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _FakeResponse("hello from the fake model")

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)

    result = llm_client.generate("say hi", system="be brief")

    assert result == "hello from the fake model"
    assert captured["model"] == "gpt-4o-mini"
    assert captured["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "say hi"},
    ]
    assert captured["api_base"] is None


def test_generate_without_system_omits_system_message(monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _FakeResponse("ok")

    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)

    llm_client.generate("say hi")

    assert captured["messages"] == [{"role": "user", "content": "say hi"}]


def test_generate_ollama_passes_api_base(monkeypatch):
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _FakeResponse("ok")

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1")
    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)

    llm_client.generate("say hi", provider="ollama")

    assert captured["model"] == "ollama/llama3.1"
    assert captured["api_base"] == "http://localhost:11434"


def test_generate_same_prompt_both_providers_hit_correct_models(monkeypatch):
    """Directly exercises the shape Stage 5's gate check cares about: the
    same prompt, routed to each provider via the `provider` override,
    reaches litellm with the right model each time (the actual
    content-equivalence of the two real completions still needs a live
    run -- see experiments/test_stage5.py)."""
    seen_models = []

    def fake_completion(**kwargs):
        seen_models.append(kwargs["model"])
        return _FakeResponse(f"report from {kwargs['model']}")

    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1")
    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)

    openai_result = llm_client.generate("audit this", provider="openai")
    ollama_result = llm_client.generate("audit this", provider="ollama")

    assert seen_models == ["gpt-4o-mini", "ollama/llama3.1"]
    assert openai_result != ollama_result  # sanity: fake distinguishes calls


def test_generate_wraps_litellm_errors(monkeypatch):
    def fake_completion(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_client.litellm, "completion", fake_completion)

    with pytest.raises(llm_client.LLMClientError):
        llm_client.generate("say hi")

from argus.llm.config import LLMConfig
from argus.llm.factory import build_chat_model


def _cfg(**overrides):
    base = dict(provider="ollama", model="openai/qwen3.6-35b-a3b",
               api_base="http://x/v1", api_key="k")
    base.update(overrides)
    return LLMConfig(**base)


def test_build_chat_model_sends_reasoning_budget_for_ollama_provider():
    cfg = _cfg(reasoning_budget_tokens=8192)
    model = build_chat_model(cfg)
    assert model.model_kwargs == {
        "reasoning_budget_tokens": 8192,
        "chat_template_kwargs": {"enable_thinking": True},
    }


def test_build_chat_model_omits_reasoning_budget_when_unset():
    cfg = _cfg(reasoning_budget_tokens=None)
    model = build_chat_model(cfg)
    assert model.model_kwargs == {}


def test_build_chat_model_omits_reasoning_budget_for_non_ollama_provider():
    """A budget value set on a non-ollama config (shouldn't happen since
    runner.py only sets it for provider=="ollama", but the factory itself
    must not blindly forward it to providers that don't understand this
    llama.cpp-specific param)."""
    cfg = _cfg(provider="anthropic", reasoning_budget_tokens=8192)
    model = build_chat_model(cfg)
    assert model.model_kwargs == {}

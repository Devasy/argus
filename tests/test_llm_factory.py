from langchain_core.messages import AIMessage
import pytest

from argus.llm.config import LLMConfig
from argus.llm.factory import _GroqChatLiteLLM, build_chat_model


def _cfg(**overrides):
    base = dict(provider="ollama", model="openai/qwen3.6-35b-a3b",
               api_base="http://x/v1", api_key="k")
    base.update(overrides)
    return LLMConfig(**base)


def _prior_ai_turn_with_reasoning():
    return [AIMessage(content="done thinking", additional_kwargs={
        "reasoning_content": "step by step reasoning that Groq rejects"})]


def test_fallback_keeps_callbacks_without_duplicating_langfuse():
    from langchain_core.callbacks import BaseCallbackHandler
    tracer, langfuse = BaseCallbackHandler(), BaseCallbackHandler()
    model = build_chat_model(_cfg(langfuse_handler=langfuse,
        fallback={"provider": "groq", "model": "groq/fallback"}), callbacks=[tracer])
    assert model.callbacks == model.fallback_model.callbacks == [tracer, langfuse]


@pytest.mark.parametrize("stream", [False, True])
def test_sync_context_overflow_reaches_caller_without_fallback(monkeypatch, stream):
    from langchain_litellm import ChatLiteLLM
    from litellm import ContextWindowExceededError
    error = ContextWindowExceededError(message="context exceeded", model="primary", llm_provider="openai")
    calls = []

    def fail(self, *args, **kwargs):
        calls.append(self.model)
        raise error
        yield  # The streaming path must be a generator.

    def fail_generate(self, *args, **kwargs):
        calls.append(self.model)
        raise error

    monkeypatch.setattr(ChatLiteLLM, "_stream" if stream else "_generate", fail if stream else fail_generate)
    model = build_chat_model(_cfg(fallback={"provider": "groq", "model": "groq/fallback"}))
    with pytest.raises(ContextWindowExceededError) as caught:
        if stream:
            list(model._stream(_prior_ai_turn_with_reasoning()))
        else:
            model._generate(_prior_ai_turn_with_reasoning())
    assert caught.value is error
    assert calls == [model.model]


@pytest.mark.parametrize("stream", [False, True])
async def test_async_context_overflow_reaches_caller_without_fallback(monkeypatch, stream):
    from langchain_litellm import ChatLiteLLM
    from litellm import ContextWindowExceededError
    error = ContextWindowExceededError(message="context exceeded", model="primary", llm_provider="openai")
    calls = []

    async def fail(self, *args, **kwargs):
        calls.append(self.model)
        raise error
        yield

    async def fail_generate(self, *args, **kwargs):
        calls.append(self.model)
        raise error

    monkeypatch.setattr(ChatLiteLLM, "_astream" if stream else "_agenerate", fail if stream else fail_generate)
    model = build_chat_model(_cfg(fallback={"provider": "groq", "model": "groq/fallback"}))
    with pytest.raises(ContextWindowExceededError) as caught:
        if stream:
            [chunk async for chunk in model._astream(_prior_ai_turn_with_reasoning())]
        else:
            await model._agenerate(_prior_ai_turn_with_reasoning())
    assert caught.value is error
    assert calls == [model.model]


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


def test_build_chat_model_passes_num_retries_as_max_retries():
    """ChatLiteLLM has no `num_retries` field -- it silently drops unknown
    kwargs, so passing num_retries=N was a no-op that quietly disabled all
    retry behaviour. The field it actually reads is `max_retries`."""
    cfg = _cfg(num_retries=5)
    model = build_chat_model(cfg)
    assert model.max_retries == 5


def test_build_chat_model_constructs_a_separate_fallback_endpoint():
    cfg = _cfg(fallback={"model": "openai/qwen3.8-27b",
                         "api_base": "http://llama:8080/v1", "api_key": None})
    model = build_chat_model(cfg)
    assert "fallbacks" not in model.model_kwargs
    assert model.fallback_model.model == "openai/qwen3.8-27b"
    assert model.fallback_model.api_base == "http://llama:8080/v1"


def test_build_chat_model_omits_fallback_when_unset():
    cfg = _cfg()
    model = build_chat_model(cfg)
    assert "fallbacks" not in model.model_kwargs


def test_build_chat_model_combines_fallback_and_ollama_reasoning_budget():
    """Both branches write into the same model_kwargs dict -- neither may
    clobber the other."""
    cfg = _cfg(reasoning_budget_tokens=8192,
               fallback={"model": "groq/x", "api_base": None, "api_key": "k"})
    model = build_chat_model(cfg)
    assert model.model_kwargs["reasoning_budget_tokens"] == 8192
    assert isinstance(model.fallback_model, _GroqChatLiteLLM)
    assert model.fallback_model.model_kwargs == {}


def test_build_chat_model_uses_groq_subclass_for_groq_provider():
    cfg = _cfg(provider="groq", model="groq/qwen/qwen3.8-27b", api_base=None)
    model = build_chat_model(cfg)
    assert isinstance(model, _GroqChatLiteLLM)


def test_build_chat_model_does_not_use_groq_subclass_for_ollama_provider():
    cfg = _cfg()
    model = build_chat_model(cfg)
    assert type(model) is not _GroqChatLiteLLM


def test_groq_chat_model_strips_reasoning_content_before_the_request():
    """The actual bug: Groq's API 400s on an assistant message carrying
    reasoning_content, which langchain_litellm re-attaches unconditionally
    once any thinking-capable model (e.g. the local fallback) has produced
    one -- every round after the first silently fell back to the local
    endpoint instead of using Groq."""
    cfg = _cfg(provider="groq", model="groq/qwen/qwen3.8-27b", api_base=None)
    model = build_chat_model(cfg)
    message_dicts, _ = model._create_message_dicts(
        _prior_ai_turn_with_reasoning(), stop=None)
    assert "reasoning_content" not in message_dicts[0]


def test_plain_chat_model_keeps_reasoning_content_for_local_provider():
    """Regression guard: the local-only path (no Groq involved) must keep
    behaving exactly as it did before this fix."""
    cfg = _cfg()
    model = build_chat_model(cfg)
    message_dicts, _ = model._create_message_dicts(
        _prior_ai_turn_with_reasoning(), stop=None)
    assert message_dicts[0]["reasoning_content"] == \
        "step by step reasoning that Groq rejects"


@pytest.mark.parametrize("override", [
    {"model": "openai/cohere/north-mini-code"},
    {"api_base": "https://example.com/v1"},
    {"fallback": {"model": "openai/paid"}},
])
def test_free_openrouter_rejects_paid_routes(override):
    config = _cfg(provider="openai", model="openai/cohere/north-mini-code:free",
                  api_base="https://openrouter.ai/api/v1", free_only=True)
    config = config.model_copy(update=override)
    with pytest.raises(ValueError):
        build_chat_model(config)


def test_free_openrouter_uses_durable_governor():
    from argus.llm.factory import _QuotaChatLiteLLM
    config = _cfg(provider="openai", model="openai/cohere/north-mini-code:free",
                  api_base="https://openrouter.ai/api/v1", free_only=True,
                  quota_database_url="postgresql+asyncpg://test", quota_limits=(5, 16000, 50))
    model = build_chat_model(config)
    assert isinstance(model, _QuotaChatLiteLLM)
    assert model.quota_database_url == config.quota_database_url
    assert model.quota_limits == (5, 16000, 50)
    assert model.max_retries == 0
    assert "quota_database_url" not in model.model_dump()


async def test_free_openrouter_reserves_before_request(monkeypatch):
    from argus.llm.factory import _FallbackChatLiteLLM
    from argus.llm import quota
    from langchain_core.messages import HumanMessage
    events = []

    async def reserve(*args):
        events.append(("reserve", args[1], args[4]))

    async def generate(self, messages, **kwargs):
        events.append(("request",))
        return "ok"

    monkeypatch.setattr(quota, "reserve", reserve)
    monkeypatch.setattr(_FallbackChatLiteLLM, "_agenerate", generate)
    model = build_chat_model(_cfg(provider="openai", model="openai/cohere/north-mini-code:free",
        api_base="https://openrouter.ai/api/v1", free_only=True,
        quota_database_url="postgresql+asyncpg://test", quota_limits=(5, 16000, 50)))
    assert await model._agenerate([HumanMessage(content="hello")]) == "ok"
    assert [event[0] for event in events] == ["reserve", "request"]
    assert events[0][2] == (5, 16000, 50)

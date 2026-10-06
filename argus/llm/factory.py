from typing import Any
import os

from langchain_litellm import ChatLiteLLM
from langchain_core.messages import BaseMessage
from litellm import ContextWindowExceededError
from pydantic import Field

from argus.llm.config import LLMConfig


class _FallbackChatLiteLLM(ChatLiteLLM):
    """Convert the original history separately for each provider."""

    fallback_model: ChatLiteLLM | None = Field(default=None, exclude=True)

    def _generate(self, messages, stop=None, run_manager=None, stream=None, **kwargs):
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager,
                                     stream=stream, **kwargs)
        except ContextWindowExceededError:
            raise
        except Exception:
            if self.fallback_model is None or (self.streaming if stream is None else stream):
                raise
            return self.fallback_model._generate(messages, stop=stop,
                run_manager=run_manager, stream=False, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, stream=None, **kwargs):
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager,
                                            stream=stream, **kwargs)
        except ContextWindowExceededError:
            raise
        except Exception:
            if self.fallback_model is None or (self.streaming if stream is None else stream):
                raise
            return await self.fallback_model._agenerate(messages, stop=stop,
                run_manager=run_manager, stream=False, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        started = False
        try:
            for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs):
                started = True
                yield chunk
        except ContextWindowExceededError:
            raise
        except Exception:
            if started or self.fallback_model is None:
                raise
            yield from self.fallback_model._stream(messages, stop=stop,
                run_manager=run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        started = False
        try:
            async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
                started = True
                yield chunk
        except ContextWindowExceededError:
            raise
        except Exception:
            if started or self.fallback_model is None:
                raise
            async for chunk in self.fallback_model._astream(messages, stop=stop,
                    run_manager=run_manager, **kwargs):
                yield chunk


class _GroqChatLiteLLM(_FallbackChatLiteLLM):
    """Groq's API rejects an assistant message that carries
    `reasoning_content` -- 400: "property 'reasoning_content' is
    unsupported". langchain_litellm always re-attaches that field from
    AIMessage.additional_kwargs once a thinking-capable model (e.g. the
    local llama.cpp endpoint) has returned it once; its own comment claims
    this is only meant "for Anthropic... leaving OpenAI-bound messages
    clean", but the code doesn't actually branch on provider, so every
    round after the first fails against Groq and falls back to the local
    endpoint -- a Groq-primary review that looks "stuck on the GPU" is
    usually this, not a rate limit. Stripping the field here (after the
    base class builds the request, so tool_calls/content are untouched)
    only affects the groq/... model path -- the plain ChatLiteLLM used for
    the local-only endpoint is never touched by this class."""

    def _create_message_dicts(
        self, messages: list[BaseMessage], stop: list[str] | None
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        message_dicts, params = super()._create_message_dicts(messages, stop)
        for d in message_dicts:
            d.pop("reasoning_content", None)
        return message_dicts, params


class _QuotaChatLiteLLM(_FallbackChatLiteLLM):
    """Apply durable request budgets to free cloud endpoints."""
    quota_database_url: str | None = Field(default=None, exclude=True)
    quota_reference: str = "GEMINI_API_KEY"
    quota_limits: tuple[int, int, int] = (5, 16000, 100)

    async def _agenerate(self, messages, **kwargs):
        from litellm import RateLimitError, token_counter
        from argus.llm.quota import reserve, pause
        if self.quota_database_url:
            wire, _ = self._create_message_dicts(messages, None)
            await reserve(self.quota_database_url, self.quota_reference, self.model,
                          token_counter(model=self.model, messages=wire, tools=kwargs.get("tools")), self.quota_limits)
        try:
            return await super()._agenerate(messages, **kwargs)
        except RateLimitError as error:
            if not self.quota_database_url:
                raise
            import re
            delay = re.search(r"retry(?:Delay|_delay| after)?[^0-9]*(\d+)", str(error), re.I)
            raise await pause(self.quota_database_url, self.quota_reference, self.model,
                              int(delay[1]) if delay else 60) from None


class _GeminiChatLiteLLM(_QuotaChatLiteLLM):
    """Retain Gemini thought signatures in tool histories."""
    def _create_message_dicts(self, messages, stop):
        dictionaries, params = super()._create_message_dicts(messages, stop)
        for message, wire in zip(messages, dictionaries):
            fields = message.additional_kwargs.get("provider_specific_fields")
            if fields:
                wire["provider_specific_fields"] = fields
            original = message.additional_kwargs.get("tool_calls") or []
            by_id = {tc.get("id"): tc for tc in original}
            for tc in wire.get("tool_calls") or []:
                raw = by_id.get(tc.get("id"), {})
                if raw.get("provider_specific_fields"):
                    tc["provider_specific_fields"] = raw["provider_specific_fields"]
        return dictionaries, params


def build_chat_model(cfg: LLMConfig, callbacks: list | None = None) -> ChatLiteLLM:
    """Per-instance construction — NEVER mutate litellm module globals."""
    kwargs: dict = dict(model=cfg.model, temperature=cfg.temperature,
                        callbacks=list(callbacks or []),
                        # ChatLiteLLM has no `num_retries` field -- passing
                        # that name is silently dropped, which is why this
                        # was a no-op for a while despite the comment below.
                        # `max_retries` is the field it actually reads.
                        #
                        # Without this litellm performs NO retries at all: it
                        # only enters its retry path when given an explicit
                        # count (or a Router, which we don't use). Transient
                        # connection errors were the single largest source of
                        # failed reviews before this was set.
                        max_retries=cfg.num_retries)
    if cfg.timeout is not None:
        kwargs["request_timeout"] = cfg.timeout
    if cfg.api_base:
        kwargs["api_base"] = cfg.api_base
    if cfg.provider == "vertex_ai":
        from argus.llm.vertex import transport_kwargs
        kwargs.update(transport_kwargs(cfg))
    api_key = os.environ.get(cfg.api_key_ref) if cfg.api_key_ref else cfg.api_key
    if cfg.provider == "claude_cli_proxy" and not api_key:
        api_key = "claude-cli-proxy"
    if cfg.api_key_ref and not api_key:
        raise ValueError(f"missing model credential environment variable: {cfg.api_key_ref}")
    if api_key:
        kwargs["api_key"] = api_key
    if getattr(cfg, "langfuse_handler", None):
        kwargs["callbacks"].append(cfg.langfuse_handler)

    model_kwargs: dict = {}
    if cfg.provider == "ollama" and cfg.reasoning_budget_tokens is not None:
        # ChatLiteLLM.model_kwargs is spread directly into the dict passed to
        # litellm.completion/acompletion -- litellm forwards any key it
        # doesn't recognize as an OpenAI-standard param straight through to
        # the provider's HTTP request body. reasoning_budget_tokens /
        # chat_template_kwargs are llama.cpp server params (not OpenAI-
        # standard), so this reaches the model as a per-request override
        # rather than depending solely on the server's own launch-time
        # --reasoning-budget flag (which may be absent, stale, or not
        # recognizing this model's <think> tag sequences).
        model_kwargs["reasoning_budget_tokens"] = cfg.reasoning_budget_tokens
        model_kwargs["chat_template_kwargs"] = {"enable_thinking": True}
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs
    if cfg.fallback:
        if cfg.free_only:
            raise ValueError("free-only endpoints do not permit fallback")
        fallback = cfg.fallback
        provider = fallback.get("provider") or fallback["model"].split("/", 1)[0]
        # Legacy local endpoints use the OpenAI wire protocol.
        if provider not in LLMConfig.model_fields["provider"].annotation.__args__:
            provider = "openai"
        fallback_cfg = cfg.model_copy(update={
            "provider": provider, "model": fallback["model"],
            "api_base": fallback.get("api_base"), "api_key": fallback.get("api_key"),
            "api_key_ref": fallback.get("api_key_ref"),
            "fallback": None, "langfuse_handler": None})
        kwargs["fallback_model"] = build_chat_model(fallback_cfg, callbacks=kwargs["callbacks"])
    model_cls = {"groq": _GroqChatLiteLLM, "gemini": _GeminiChatLiteLLM, "vertex_ai": _GeminiChatLiteLLM}.get(cfg.provider, _FallbackChatLiteLLM)
    if cfg.free_only:
        from argus.providers.settings import require_pilot_endpoint
        require_pilot_endpoint(cfg)
        kwargs["max_retries"] = 0
        if cfg.provider != "gemini":
            model_cls = _QuotaChatLiteLLM
        kwargs.update(quota_database_url=cfg.quota_database_url,
                      quota_reference=cfg.api_key_ref or "GEMINI_API_KEY", quota_limits=cfg.quota_limits)
    if cfg.provider == "vertex_ai":
        kwargs.update(quota_database_url=cfg.quota_database_url,
                      quota_reference=cfg.api_key_ref or "VERTEX_API_KEY", quota_limits=cfg.quota_limits)
    return model_cls(**kwargs)

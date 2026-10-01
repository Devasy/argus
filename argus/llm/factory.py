from typing import Any

from langchain_litellm import ChatLiteLLM
from langchain_core.messages import BaseMessage

from argus.llm.config import LLMConfig


class _GroqChatLiteLLM(ChatLiteLLM):
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


def build_chat_model(cfg: LLMConfig, callbacks: list | None = None) -> ChatLiteLLM:
    """Per-instance construction — NEVER mutate litellm module globals."""
    kwargs: dict = dict(model=cfg.model, temperature=cfg.temperature,
                        callbacks=callbacks or [],
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
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
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
    if cfg.fallback:
        # Same passthrough: litellm.completion's own `fallbacks=` kwarg is
        # read from the top level of the request and, on ANY exception from
        # the primary model (rate limit, timeout, outage), retries against
        # the next entry in the list -- no custom retry code needed.
        model_kwargs["fallbacks"] = [cfg.fallback]
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs
    model_cls = _GroqChatLiteLLM if (cfg.provider == "groq" or (cfg.fallback and cfg.fallback.get("provider") == "groq")) else ChatLiteLLM
    return model_cls(**kwargs)

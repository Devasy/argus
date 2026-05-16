from langchain_litellm import ChatLiteLLM

from argus.llm.config import LLMConfig


def build_chat_model(cfg: LLMConfig, callbacks: list | None = None) -> ChatLiteLLM:
    """Per-instance construction — NEVER mutate litellm module globals."""
    kwargs: dict = dict(model=cfg.model, temperature=cfg.temperature,
                        callbacks=callbacks or [],
                        # Without this litellm performs NO retries at all: it
                        # only enters its retry path when given an explicit
                        # count (or a Router, which we don't use). Transient
                        # connection errors were the single largest source of
                        # failed reviews before this was set.
                        num_retries=cfg.num_retries)
    if cfg.timeout is not None:
        kwargs["request_timeout"] = cfg.timeout
    if cfg.api_base:
        kwargs["api_base"] = cfg.api_base
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if getattr(cfg, "langfuse_handler", None):
        kwargs["callbacks"].append(cfg.langfuse_handler)
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
        kwargs["model_kwargs"] = {
            "reasoning_budget_tokens": cfg.reasoning_budget_tokens,
            "chat_template_kwargs": {"enable_thinking": True},
        }
    return ChatLiteLLM(**kwargs)

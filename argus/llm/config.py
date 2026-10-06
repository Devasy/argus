import os
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import LLMEndpoint

PROXY_MODEL = "openai/claude-sonnet-4-6"


class LLMConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: Literal["anthropic", "openai", "gemini", "ollama",
                     "claude_cli_proxy", "groq", "vertex_ai"]
    model: str
    api_base: str | None = None
    api_key: str | None = Field(default=None, exclude=True)
    api_key_ref: str | None = None
    free_only: bool = False
    quota_database_url: str | None = Field(default=None, exclude=True)
    quota_limits: tuple[int, int, int] = (5, 16000, 100)
    temperature: float = 0.1
    timeout: float | None = None
    supports_prompt_cache: bool = False
    langfuse_handler: object | None = Field(default=None, exclude=True)
    # litellm-shaped {model, api_base, api_key} for a second endpoint to try
    # if this one errors (rate limit, outage, ...) -- see llm/factory.py.
    fallback: dict | None = None
    # Set by the caller (runner.py) from Settings.reasoning_budget_tokens
    # when provider == "ollama" -- see llm/factory.py for how this reaches
    # the actual request.
    reasoning_budget_tokens: int | None = None
    # Retries for TRANSIENT provider failures (connection reset, 5xx, timeout)
    # -- litellm retries only when it is told a count, and we previously never
    # passed one, so a single blip killed a whole review. One bad afternoon on
    # 2026-07-30 produced 181 failed reviews from this alone. litellm does not
    # retry deterministic 4xx (context-window, bad request), so this cannot
    # mask a real error into a slow one.
    num_retries: int = 3

    @field_serializer("fallback")
    def serialize_fallback(self, value):
        return {k: v for k, v in value.items() if k != "api_key"} if value else None


async def resolve_llm_config(session: AsyncSession,
                             endpoint_id: uuid.UUID | None,
                             proxy_url: str | None) -> LLMConfig:
    if proxy_url:
        return LLMConfig(provider="claude_cli_proxy", model=PROXY_MODEL,
                         api_base=proxy_url.rstrip("/") + "/v1",
                         api_key="claude-cli-proxy")
    if endpoint_id is not None:
        ep = await session.get(LLMEndpoint, endpoint_id)
    else:
        ep = (await session.execute(select(LLMEndpoint).where(
            LLMEndpoint.is_default == True))).scalar_one_or_none()  # noqa: E712
    if ep is None:
        raise ValueError("no LLM endpoint configured and no proxy_url given")
    api_key = os.environ.get(ep.api_key_ref) if ep.api_key_ref else None
    fallback = None
    if ep.fallback_endpoint_id is not None:
        fb = await session.get(LLMEndpoint, ep.fallback_endpoint_id)
        if fb is not None:
            fallback = {
                "provider": fb.provider,
                "api_key_ref": fb.api_key_ref,
                "model": fb.model, "api_base": fb.base_url,
                "api_key": os.environ.get(fb.api_key_ref) if fb.api_key_ref else None,
            }
    return LLMConfig(provider=ep.provider, model=ep.model, api_base=ep.base_url,
                     temperature=1.0 if ep.provider == "gemini" else 0.1,
                     api_key_ref=ep.api_key_ref,
                     api_key=api_key, fallback=fallback,
                     supports_prompt_cache=(ep.provider == "anthropic"))


async def resolve_llm_config_by_name(session: AsyncSession, name: str) -> LLMConfig | None:
    """Config for the llm_endpoints row with this name, or None when no such row exists."""
    ep_id = (await session.execute(select(LLMEndpoint.id).where(
        LLMEndpoint.name == name))).scalar_one_or_none()
    if ep_id is None:
        return None
    return await resolve_llm_config(session, ep_id, None)

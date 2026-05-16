import os
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import LLMEndpoint

PROXY_MODEL = "openai/claude-sonnet-4-6"


class LLMConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: Literal["anthropic", "openai", "gemini", "ollama", "claude_cli_proxy"]
    model: str
    api_base: str | None = None
    api_key: str | None = None
    temperature: float = 0.1
    timeout: float | None = None
    supports_prompt_cache: bool = False
    langfuse_handler: object | None = Field(default=None, exclude=True)
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
    return LLMConfig(provider=ep.provider, model=ep.model, api_base=ep.base_url,
                     api_key=api_key,
                     supports_prompt_cache=(ep.provider == "anthropic"))

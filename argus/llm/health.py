"""Lightweight liveness check for self-hosted/proxied LLM endpoints (e.g. a
local llama.cpp server). Hosted providers without a custom api_base (direct
anthropic/openai/gemini) have no free liveness call, so they're assumed
healthy -- this only guards the case a self-hosted backend is unreachable."""
import logging

import httpx

from argus.llm.config import LLMConfig

logger = logging.getLogger("argus.llm.health")

PING_TIMEOUT_S = 5.0


def _server_root(api_base: str) -> str:
    """api_base is stored OpenAI-compat style (e.g. 'http://host:port/v1');
    llama.cpp's own /health lives at the server root, not under /v1."""
    root = api_base.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root


async def resolve_served_model(cfg: LLMConfig) -> str | None:
    """What the endpoint is actually serving, or None if unknowable. cfg.model
    is only what we asked for; llama.cpp's /models returns the loaded GGUF
    path, whose filename carries the model and its quantization. Best-effort:
    telemetry must never fail a review."""
    if not cfg.api_base:
        return None  # hosted provider serves exactly what we named
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_S) as client:
            r = await client.get(cfg.api_base.rstrip("/") + "/models",
                                 headers=headers)
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("served-model probe failed for %s: %s", cfg.api_base, e)
        return None
    if not isinstance(payload, dict):
        return None
    for key, field in (("data", "id"), ("models", "name")):
        entries = payload.get(key)
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            value = entries[0].get(field)
            if isinstance(value, str) and value:
                return value
    logger.warning("served-model probe: unrecognised payload from %s",
                   cfg.api_base)
    return None


async def check_llm_health(cfg: LLMConfig) -> bool:
    """True if cfg has no custom api_base (nothing to check) or the llama.cpp
    server responds to a GET {server_root}/health within PING_TIMEOUT_S."""
    if not cfg.api_base:
        return True
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_S) as client:
            r = await client.get(_server_root(cfg.api_base) + "/health", headers=headers)
        return r.status_code < 500
    except httpx.HTTPError as e:
        logger.warning("LLM health check failed for %s: %s", cfg.api_base, e)
        return False

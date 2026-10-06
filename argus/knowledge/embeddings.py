import logging
import os
import hashlib
import json
import math
import httpx

import litellm

from argus.config import Settings

logger = logging.getLogger("argus.embeddings")
MAX_EMBED_CHARS = 24000


class EmbeddingError(RuntimeError):
    pass


def embedding_fingerprint(settings):
    payload = [settings.embedding_model, settings.embedding_dim,
               "gemini-retrieval-v1" if "gemini-embedding" in settings.embedding_model else "legacy-v1"]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _route(settings: Settings) -> tuple[str, dict]:
    model = settings.embedding_model
    if model.startswith("ollama/"):
        return ("openai/" + model[len("ollama/"):],
                {"api_base": settings.ollama_base_url.rstrip("/") + "/v1",
                 "api_key": "ollama"})
    return model, {}


def _prefix(text: str, settings: Settings, is_query: bool) -> str:
    if "nomic-embed-text" in settings.embedding_model:
        p = "search_query: " if is_query else "search_document: "
        if not text.startswith(p):
            return p + text
    return text


async def embed_text(text: str, settings: Settings,
                     is_query: bool = False) -> list[float] | None:
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > MAX_EMBED_CHARS:
        logger.warning("truncating embed input %d -> %d chars",
                       len(text), MAX_EMBED_CHARS)
        text = text[:MAX_EMBED_CHARS]
    model, extra = _route(settings)
    if "gemini-embedding" in model:
        key = os.environ.get(settings.embedding_api_key_ref)
        if not key:
            raise EmbeddingError(f"missing embedding credential: {settings.embedding_api_key_ref}")
        model = model.removeprefix("gemini/")
        if model not in ("gemini-embedding-2", "gemini-embedding-001"):
            raise EmbeddingError("unsupported Gemini embedding model")
        content = (f"task: search result | query: {text}" if is_query else f"title: none | text: {text}")
        payload = {"content": {"parts": [{"text": content}]},
                   "outputDimensionality": settings.embedding_dim}
        if model == "gemini-embedding-001":
            payload["content"]["parts"] = [{"text": text}]
            payload["taskType"] = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        try:
            from argus.llm.quota import reserve, pause
            await reserve(settings.database_url, settings.embedding_api_key_ref, model,
                          len(content) // 3 + 1, (settings.gemini_free_rpm,
                          settings.gemini_free_tpm, settings.gemini_free_rpd))
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent",
                    headers={"x-goog-api-key": key}, json=payload)
            if r.status_code == 429:
                delay = r.headers.get("retry-after", "60")
                raise await pause(settings.database_url, settings.embedding_api_key_ref, model,
                                  int(delay) if delay.isdigit() else 60)
            r.raise_for_status()
            vector = r.json()["embedding"]["values"]
            if len(vector) != settings.embedding_dim or not all(math.isfinite(v) for v in vector):
                raise ValueError("invalid embedding dimensions or values")
            length = math.sqrt(sum(v * v for v in vector))
            if not length:
                raise ValueError("zero embedding")
            return [v / length for v in vector]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise EmbeddingError(f"Gemini embedding request failed ({type(e).__name__})") from None
    try:
        resp = await litellm.aembedding(
            model=model, input=[_prefix(text, settings, is_query)], **extra)
    except Exception as e:
        logger.warning("embedding failed: %s", e)
        raise EmbeddingError(str(e)) from e
    return resp.data[0]["embedding"]


async def validate_embedding_dimension(settings: Settings) -> None:
    vec = await embed_text("dimension probe", settings, is_query=True)
    if vec is None or len(vec) != settings.embedding_dim:
        raise EmbeddingError(
            f"model {settings.embedding_model!r} returned "
            f"{len(vec or [])}-d vectors; configured dim is "
            f"{settings.embedding_dim}. Refusing to run.")
    logger.info("embedding validated: %s -> %d-d",
                settings.embedding_model, len(vec))

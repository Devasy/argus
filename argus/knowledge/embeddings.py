import logging

import litellm

from argus.config import Settings

logger = logging.getLogger("argus.embeddings")
MAX_EMBED_CHARS = 24000


class EmbeddingError(RuntimeError):
    pass


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

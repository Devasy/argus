import pytest

import argus.knowledge.embeddings as emb
from argus.knowledge.embeddings import (EmbeddingError, embed_text,
                                            validate_embedding_dimension)


class FakeResp:
    def __init__(self, vec):
        self.data = [{"embedding": vec, "index": 0}]


async def test_embed_prefixes_and_calls(settings, monkeypatch):
    captured = {}

    async def fake_aembedding(**kw):
        captured.update(kw)
        return FakeResp([0.1] * 768)

    monkeypatch.setattr(emb.litellm, "aembedding", fake_aembedding)
    vec = await embed_text("hello", settings, is_query=True)
    assert len(vec) == 768
    assert captured["input"][0].startswith("search_query: ")
    assert captured["model"] == "openai/nomic-embed-text"
    assert captured["api_base"].endswith("/v1")


async def test_empty_returns_none(settings):
    assert await embed_text("   ", settings) is None


async def test_backend_failure_raises(settings, monkeypatch):
    async def boom(**kw):
        raise ConnectionError("down")
    monkeypatch.setattr(emb.litellm, "aembedding", boom)
    with pytest.raises(EmbeddingError):
        await embed_text("hello", settings)


async def test_dim_validation(settings, monkeypatch):
    async def wrong(**kw):
        return FakeResp([0.1] * 1536)
    monkeypatch.setattr(emb.litellm, "aembedding", wrong)
    with pytest.raises(EmbeddingError, match="1536"):
        await validate_embedding_dimension(settings)

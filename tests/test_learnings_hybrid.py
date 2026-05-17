"""Hybrid retrieval: the lexical arm must widen the candidate set, not rerank it."""
import pytest

import argus.knowledge.learnings as L
from argus.domain.models import Repository


def _vec(i: int) -> list[float]:
    v = [0.0] * 768
    v[i % 768] = 1.0
    return v


async def _repo(db):
    r = Repository(provider="gitlab", project_path="g/hybrid", gitlab_project_id=91)
    db.add(r)
    await db.flush()
    return r


async def _seed(db, settings, monkeypatch, repo):
    """Five learnings on distinct one-hot vectors. The `far` one is lexically
    distinctive but semantically nowhere near the query."""
    made = {}
    specs = [
        ("near", "logging", "prefer lazy formatting in log calls", 1),
        ("mid1", "caching", "invalidate the cache on write", 2),
        ("mid2", "testing", "assert behaviour rather than wording", 3),
        ("mid3", "typing", "annotate public functions", 4),
        ("far", "widgets", "always pass zzUniqueSentinel to renderWidget", 5),
    ]
    for name, topic, hint, idx in specs:
        async def fake(text, settings, is_query=False, _i=idx):
            return _vec(_i)
        monkeypatch.setattr(L, "embed_text", fake)
        made[name] = await L.upsert_learning(db, settings, repo_id=repo.id,
                                             topic=topic, hint_text=hint)
    return made


@pytest.fixture
def query_at_near(monkeypatch):
    async def fake(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", fake)


async def test_vector_only_cannot_reach_beyond_its_pool(db, settings, monkeypatch):
    repo = await _repo(db)
    made = await _seed(db, settings, monkeypatch, repo)

    async def fake(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", fake)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="logging", file_paths=[],
                                     pool=1, top=4, explore=False)
    assert made["far"].id not in {l.id for l in got}


async def test_lexical_arm_recovers_a_candidate_outside_the_vector_pool(
        db, settings, monkeypatch):
    repo = await _repo(db)
    made = await _seed(db, settings, monkeypatch, repo)

    async def fake(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", fake)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="logging", file_paths=[],
                                     pool=1, top=4, explore=False,
                                     lexical_query="zzUniqueSentinel")
    assert made["far"].id in {l.id for l in got}


async def test_lexical_arm_survives_a_failed_query_embedding(
        db, settings, monkeypatch):
    repo = await _repo(db)
    made = await _seed(db, settings, monkeypatch, repo)

    async def no_vector(text, settings, is_query=False):
        return None
    monkeypatch.setattr(L, "embed_text", no_vector)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="logging", file_paths=[],
                                     top=4, explore=False,
                                     lexical_query="zzUniqueSentinel")
    assert made["far"].id in {l.id for l in got}


async def test_no_lexical_query_keeps_vector_only_behaviour(
        db, settings, monkeypatch, query_at_near):
    repo = await _repo(db)
    made = await _seed(db, settings, monkeypatch, repo)

    async def fake(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", fake)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="logging", file_paths=[],
                                     top=1, explore=False)
    assert [l.id for l in got] == [made["near"].id]


async def test_excluded_kinds_stay_excluded_in_the_hybrid_path(
        db, settings, monkeypatch):
    repo = await _repo(db)
    made = await _seed(db, settings, monkeypatch, repo)

    async def fake(text, settings, is_query=False):
        return _vec(9)
    monkeypatch.setattr(L, "embed_text", fake)
    dns = await L.upsert_learning(db, settings, repo_id=repo.id,
                                  topic="noise", hint_text="never flag zzUniqueSentinel",
                                  kind="do_not_suggest")

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="logging", file_paths=[],
                                     top=8, explore=False,
                                     lexical_query="zzUniqueSentinel",
                                     exclude_kinds=("do_not_suggest",))
    assert dns.id not in {l.id for l in got}


async def test_trace_records_both_arm_ranks_and_the_outcome(
        db, settings, monkeypatch):
    repo = await _repo(db)
    await _seed(db, settings, monkeypatch, repo)

    async def fake(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", fake)

    trace: list = []
    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="logging", file_paths=[],
                                     top=2, explore=False,
                                     lexical_query="zzUniqueSentinel",
                                     trace=trace)
    assert len(trace) >= len(got)
    row = trace[0]
    assert {"learning_id", "vector_rank", "lexical_rank", "score", "chosen"} <= set(row)
    assert sum(1 for r in trace if r["chosen"]) == len(got)
    assert any(r["lexical_rank"] is not None for r in trace)


async def test_lexical_arm_finds_a_learning_with_no_embedding(
        db, settings, monkeypatch):
    """A learning saved while the embedding backend was down (upsert_learning
    tolerates vec=None) must still be reachable by pure lexical matching --
    the whole point of a lexical arm is not depending on an embedding."""
    repo = await _repo(db)
    await _seed(db, settings, monkeypatch, repo)

    async def no_embed(text, settings, is_query=False):
        return None
    monkeypatch.setattr(L, "embed_text", no_embed)
    unembedded = await L.upsert_learning(
        db, settings, repo_id=repo.id, topic="widgets",
        hint_text="always pass zzRareUnembeddedToken to renderWidget")
    assert unembedded.embedding is None

    async def fake(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", fake)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="logging", file_paths=[],
                                     top=8, explore=False,
                                     lexical_query="zzRareUnembeddedToken")
    assert unembedded.id in {l.id for l in got}


async def test_query_embedding_failure_degrades_instead_of_crashing(
        db, settings, monkeypatch):
    """embed_text raises EmbeddingError on a real backend rejection (e.g. an
    oversized query) -- unlike returning None for empty text, this used to
    propagate straight out of relevant_learnings and crash the whole review
    before the pipeline even started. It must degrade like qvec=None instead."""
    from argus.knowledge.embeddings import EmbeddingError
    repo = await _repo(db)
    made = await _seed(db, settings, monkeypatch, repo)

    async def raises(text, settings, is_query=False):
        raise EmbeddingError("the input length exceeds the context length")
    monkeypatch.setattr(L, "embed_text", raises)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[])
    assert got == []


async def test_query_embedding_failure_still_allows_lexical_only(
        db, settings, monkeypatch):
    from argus.knowledge.embeddings import EmbeddingError
    repo = await _repo(db)
    made = await _seed(db, settings, monkeypatch, repo)

    async def raises(text, settings, is_query=False):
        raise EmbeddingError("the input length exceeds the context length")
    monkeypatch.setattr(L, "embed_text", raises)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[],
                                     top=4, explore=False,
                                     lexical_query="zzUniqueSentinel")
    assert made["far"].id in {l.id for l in got}

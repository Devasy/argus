import pytest

import argus.knowledge.learnings as L
from argus.domain.models import Repository
from argus.knowledge.clustering import cluster_learnings


def _vec(i: int) -> list[float]:
    v = [0.0] * 768
    v[i % 768] = 1.0
    return v


async def _repo(db):
    r = Repository(provider="gitlab", project_path="g/c", gitlab_project_id=7)
    db.add(r)
    await db.flush()
    return r


async def test_identical_learnings_cluster_together(db, settings, monkeypatch):
    repo = await _repo(db)

    async def same(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", same)
    # dedup_threshold=0 forces distinct rows despite identical vectors, so we
    # are testing clustering, not dedup.
    a = await L.upsert_learning(db, settings, repo_id=repo.id, topic="a",
                                hint_text="x", dedup_threshold=0.0)
    b = await L.upsert_learning(db, settings, repo_id=repo.id, topic="b",
                                hint_text="y", dedup_threshold=0.0)

    clusters = await cluster_learnings(db, repo_id=repo.id)

    assert any({a.id, b.id} <= {l.id for l in c} for c in clusters)


async def test_distant_learnings_are_separate_clusters(db, settings, monkeypatch):
    repo = await _repo(db)
    state = {"i": 0}

    async def far(text, settings, is_query=False):
        state["i"] += 100
        return _vec(state["i"])
    monkeypatch.setattr(L, "embed_text", far)
    a = await L.upsert_learning(db, settings, repo_id=repo.id, topic="a",
                                hint_text="x")
    b = await L.upsert_learning(db, settings, repo_id=repo.id, topic="b",
                                hint_text="y")

    clusters = await cluster_learnings(db, repo_id=repo.id)

    for c in clusters:
        ids = {l.id for l in c}
        assert not ({a.id, b.id} <= ids)


async def test_every_active_learning_appears_exactly_once(db, settings, monkeypatch):
    repo = await _repo(db)
    state = {"i": 0}

    async def mixed(text, settings, is_query=False):
        state["i"] += 50
        return _vec(state["i"])
    monkeypatch.setattr(L, "embed_text", mixed)
    made = [await L.upsert_learning(db, settings, repo_id=repo.id,
                                    topic=f"t{i}", hint_text=f"h{i}")
            for i in range(5)]

    clusters = await cluster_learnings(db, repo_id=repo.id)

    seen = [l.id for c in clusters for l in c]
    assert sorted(seen) == sorted([m.id for m in made])
    assert len(seen) == len(set(seen))  # no learning in two clusters


async def test_cluster_size_is_capped(db, settings, monkeypatch):
    repo = await _repo(db)

    async def same(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", same)
    for i in range(10):
        await L.upsert_learning(db, settings, repo_id=repo.id, topic=f"t{i}",
                                hint_text=f"h{i}", dedup_threshold=0.0)

    clusters = await cluster_learnings(db, repo_id=repo.id, max_cluster_size=3)

    assert all(len(c) <= 3 for c in clusters)

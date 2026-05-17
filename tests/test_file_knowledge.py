import argus.knowledge.file_knowledge as FK
from argus.domain.models import Repository


async def test_remember_and_recall(db, settings, monkeypatch):
    async def fake_embed(text, settings, is_query=False):
        return [0.1] * 768
    monkeypatch.setattr(FK, "embed_text", fake_embed)

    repo = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=1)
    db.add(repo)
    await db.flush()

    row = await FK.remember(db, settings, repo_id=repo.id, path="a/b.py",
                            blob_sha="s1", summary="handles auth tokens")
    got = await FK.recall(db, repo.id, "a/b.py", "s1")
    assert got is not None and got.summary == "handles auth tokens"

    # stale blob: row still returned, caller sees sha mismatch
    stale = await FK.recall(db, repo.id, "a/b.py", "s2")
    assert stale is not None and stale.blob_sha == "s1"

    # append accumulates
    await FK.remember(db, settings, repo_id=repo.id, path="a/b.py",
                      blob_sha="s2", summary="handles auth tokens v2",
                      append_note="added refresh flow")
    got2 = await FK.recall(db, repo.id, "a/b.py", "s2")
    assert got2.blob_sha == "s2" and got2.notes and \
        got2.notes[-1]["note"] == "added refresh flow"

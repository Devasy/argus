import uuid

import httpx
import pytest
from sqlalchemy import delete

from argus.api.app import create_app
from argus.domain.models import Learning


@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    async with engine.begin() as conn:
        await conn.execute(delete(Learning).where(Learning.topic.like("api-test-%")))


async def _seed(engine, **kwargs):
    from argus.db import session_factory
    sf = session_factory(engine)
    async with sf() as s:
        l = Learning(topic=kwargs.pop("topic", "api-test-default"), hint_text="h", **kwargs)
        s.add(l)
        await s.commit()
        return l.id


async def test_list_learnings_default(api, engine):
    await _seed(engine, topic="api-test-1")
    r = await api.get("/learnings")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "total" in body and "page" in body and "per_page" in body
    assert any(l["topic"] == "api-test-1" for l in body["items"])
    matching = next(l for l in body["items"] if l["topic"] == "api-test-1")
    assert matching["hit_rate"] is None  # no hits/misses yet


async def test_list_learnings_exposes_source_mr_and_author(api, engine):
    from argus.db import session_factory
    from argus.domain.models import Actor, MergeRequest, Repository

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/api-source-trace-test",
                          gitlab_project_id=90000320)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=41, title="Adds threat_intel notifiers tasks flow",
                          state="opened", source_branch="a", target_branch="b",
                          head_sha="s", web_url="https://gitlab.example/repo/-/merge_requests/41")
        s.add(mr); await s.flush()
        actor = Actor(username="lead.dev", provider_user_id=98)
        s.add(actor); await s.flush()
        s.add(Learning(topic="api-test-traced", hint_text="h",
                       mr_id=mr.id, learned_from_actor_id=actor.id))
        await s.commit()
        repo_id, mr_id, actor_id = repo.id, mr.id, actor.id

    try:
        r = await api.get("/learnings")
        matching = next(l for l in r.json()["items"] if l["topic"] == "api-test-traced")
        assert matching["mr_iid"] == 41
        assert matching["mr_title"] == "Adds threat_intel notifiers tasks flow"
        assert matching["mr_web_url"] == "https://gitlab.example/repo/-/merge_requests/41"
        assert matching["learned_from_username"] == "lead.dev"
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(Learning).where(Learning.mr_id == mr_id))
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Actor).where(Actor.id == actor_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_list_learnings_hit_rate_computed(api, engine):
    await _seed(engine, topic="api-test-hitrate", hit_count=3, miss_count=1)
    r = await api.get("/learnings")
    matching = next(l for l in r.json()["items"] if l["topic"] == "api-test-hitrate")
    assert matching["hit_rate"] == 0.75


async def test_list_learnings_rejects_invalid_kind(api):
    r = await api.get("/learnings", params={"kind": "not-a-real-kind"})
    assert r.status_code == 422


async def test_list_learnings_rejects_invalid_status(api):
    r = await api.get("/learnings", params={"status": "not-a-real-status"})
    assert r.status_code == 422


async def test_search_learnings_rejects_empty_query(api):
    r = await api.get("/learnings/search", params={"q": "  "})
    assert r.status_code == 422


async def test_search_learnings_shape(api, engine, monkeypatch):
    from argus.api import app as app_module
    async def fake_search(session, settings, *, query_text, repo_id=None, page=1, per_page=20):
        return [], 0
    monkeypatch.setattr(app_module, "search_learnings", fake_search)
    r = await api.get("/learnings/search", params={"q": "auth"})
    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "page": 1, "per_page": 20}


async def test_file_knowledge_requires_repo_id(api):
    r = await api.get("/file-knowledge")
    assert r.status_code == 422


async def test_file_knowledge_list_shape(api, engine):
    from argus.db import session_factory
    from argus.domain.models import FileKnowledge, Repository

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/fk-api-test",
                          gitlab_project_id=90000301)
        s.add(repo); await s.flush()
        s.add(FileKnowledge(repo_id=repo.id, file_path="api/routes.py",
                            summary="handles auth", blob_sha="abc123",
                            notes=[{"note": "n1", "review_id": None, "at": "2026-01-01T00:00:00Z"}]))
        await s.commit()
        repo_id = repo.id

    r = await api.get("/file-knowledge", params={"repo_id": str(repo_id)})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    entry = body["items"][0]
    assert entry["file_path"] == "api/routes.py"
    assert entry["summary"] == "handles auth"
    assert entry["notes"] == [{"note": "n1", "review_id": None, "at": "2026-01-01T00:00:00Z"}]
    assert "purpose" not in entry and "key_symbols" not in entry and "embedding" not in entry

    async with sf() as s:
        await s.execute(delete(FileKnowledge).where(FileKnowledge.repo_id == repo_id))
        await s.execute(delete(Repository).where(Repository.id == repo_id))
        await s.commit()

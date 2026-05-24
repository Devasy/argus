import uuid

import httpx
import pytest
from sqlalchemy import delete

from argus.api.app import create_app
from argus.db import session_factory
from argus.domain.models import (Job, MergeRequest, Repository, Review,
                                     ReviewerProxy)


@pytest.fixture
async def secured_api(engine, settings):
    settings2 = settings.model_copy(update={"api_token": "sekrit"})
    app = create_app(settings=settings2, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
async def mr_fixture(engine):
    """Create a Repository + MergeRequest to trigger reviews against, and
    clean up afterwards."""
    sf = session_factory(engine)
    async with sf() as session:
        repo = Repository(provider="gitlab",
                          project_path="grp/reviewer-proxy-test",
                          gitlab_project_id=90000002)
        session.add(repo)
        await session.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        session.add(mr)
        await session.commit()
        mr_id, repo_id = mr.id, repo.id

    yield mr_id

    async with engine.begin() as conn:
        await conn.execute(delete(Review).where(Review.mr_id == mr_id))
        await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
        await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_health_open(secured_api):
    assert (await secured_api.get("/health")).status_code == 200


async def test_repositories_requires_token(secured_api):
    assert (await secured_api.get("/repositories")).status_code == 401
    ok = await secured_api.get("/repositories",
                               headers={"Authorization": "Bearer sekrit"})
    assert ok.status_code == 200


async def test_reviewer_proxy_lookup(secured_api):
    h = {"Authorization": "Bearer sekrit"}
    r = await secured_api.post("/reviewer-proxies", headers=h, json={
        "reviewer": "developer.two", "proxy_url": "http://10.0.0.5:8484"})
    assert r.status_code == 201
    listed = await secured_api.get("/reviewer-proxies", headers=h)
    assert listed.json()[0]["reviewer"] == "developer.two"


async def test_create_reviewer_proxy_duplicate_returns_409(secured_api):
    h = {"Authorization": "Bearer sekrit"}
    payload = {"reviewer": "dupe.reviewer", "proxy_url": "http://10.0.0.9:9999"}
    first = await secured_api.post("/reviewer-proxies", headers=h, json=payload)
    assert first.status_code == 201

    second = await secured_api.post("/reviewer-proxies", headers=h, json=payload)
    assert second.status_code == 409, second.text


async def test_trigger_review_unregistered_reviewer_returns_404(secured_api, mr_fixture):
    h = {"Authorization": "Bearer sekrit"}
    r = await secured_api.post(f"/merge-requests/{mr_fixture}/reviews", headers=h,
                               json={"reviewer": "nobody.registered"})
    assert r.status_code == 404, r.text
    assert "nobody.registered" in r.json()["detail"]


async def test_trigger_review_disabled_reviewer_excluded(secured_api, mr_fixture, engine):
    h = {"Authorization": "Bearer sekrit"}
    sf = session_factory(engine)
    async with sf() as session:
        proxy = ReviewerProxy(reviewer="disabled.reviewer",
                              proxy_url="http://10.0.0.6:8484", enabled=False)
        session.add(proxy)
        await session.commit()
        proxy_id = proxy.id

    try:
        r = await secured_api.post(f"/merge-requests/{mr_fixture}/reviews", headers=h,
                                   json={"reviewer": "disabled.reviewer"})
        assert r.status_code == 404, r.text
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(ReviewerProxy).where(ReviewerProxy.id == proxy_id))


async def test_trigger_review_enabled_reviewer_resolves_proxy_url(secured_api, mr_fixture, engine):
    h = {"Authorization": "Bearer sekrit"}
    sf = session_factory(engine)
    async with sf() as session:
        proxy = ReviewerProxy(reviewer="enabled.reviewer",
                              proxy_url="http://10.0.0.7:8484", enabled=True)
        session.add(proxy)
        await session.commit()
        proxy_id = proxy.id

    try:
        r = await secured_api.post(f"/merge-requests/{mr_fixture}/reviews", headers=h,
                                   json={"reviewer": "enabled.reviewer"})
        assert r.status_code == 202, r.text
        review_id = r.json()["review_id"]

        get_r = await secured_api.get(f"/reviews/{review_id}", headers=h)
        assert get_r.status_code == 200, get_r.text

        async with sf() as session:
            review = await session.get(Review, uuid.UUID(review_id))
            assert review.llm_config["provider"] == "claude_cli_proxy"
            assert review.llm_config["api_base"] == "http://10.0.0.7:8484/v1"
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(Job).where(Job.dedup_key == f"review:{mr_fixture}:full"))
            await conn.execute(delete(ReviewerProxy).where(ReviewerProxy.id == proxy_id))

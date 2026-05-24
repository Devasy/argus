import httpx
import pytest
from sqlalchemy import delete, select, update

from argus.api.app import create_app
from argus.domain.models import (ReviewerAgent, ReviewerAgentVersion,
                                     ReviewReviewerAgentVersion)


@pytest.fixture
async def api(engine, settings):
    # The "design" ReviewerAgent is normally seeded by an Alembic migration
    # (migrations/versions/c2d3e4f5a6b7_reviewer_agents.py), but the shared
    # test DB is built via Base.metadata.create_all (see conftest.py's
    # `engine` fixture), which never runs that data migration. Seed it here,
    # idempotently, so test_list_agents_includes_seeded_design has something
    # to find regardless of test/DB creation order.
    async with engine.begin() as conn:
        existing = (await conn.execute(
            select(ReviewerAgent.id).where(ReviewerAgent.name == "design")
        )).scalar_one_or_none()
        if existing is None:
            result = await conn.execute(
                ReviewerAgent.__table__.insert().returning(ReviewerAgent.id),
                {"name": "design", "description": "design reviewer", "enabled": True})
            agent_id = result.scalar_one()
            version_result = await conn.execute(
                ReviewerAgentVersion.__table__.insert().returning(ReviewerAgentVersion.id),
                {"agent_id": agent_id, "version": 1, "guidelines": "review design",
                 "max_rounds": 10})
            version_id = version_result.scalar_one()
            await conn.execute(
                update(ReviewerAgent).where(ReviewerAgent.id == agent_id)
                .values(current_version_id=version_id))

    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    async with engine.begin() as conn:
        test_agent_ids = select(ReviewerAgent.id).where(
            ReviewerAgent.name.like("test-agent%"))
        test_version_ids = select(ReviewerAgentVersion.id).where(
            ReviewerAgentVersion.agent_id.in_(test_agent_ids))
        # Delete child rows before parents to satisfy FK constraints:
        # review_reviewer_agent_versions -> reviewer_agent_versions,
        # and clear reviewer_agents.current_version_id (FK into
        # reviewer_agent_versions) before deleting the versions themselves.
        await conn.execute(delete(ReviewReviewerAgentVersion).where(
            ReviewReviewerAgentVersion.agent_version_id.in_(test_version_ids)))
        await conn.execute(update(ReviewerAgent).where(
            ReviewerAgent.name.like("test-agent%")).values(current_version_id=None))
        await conn.execute(delete(ReviewerAgentVersion).where(
            ReviewerAgentVersion.agent_id.in_(test_agent_ids)))
        await conn.execute(delete(ReviewerAgent).where(
            ReviewerAgent.name.like("test-agent%")))


async def test_list_agents_includes_seeded_design(api):
    r = await api.get("/agents", params={"per_page": 200})
    assert r.status_code == 200
    names = [a["name"] for a in r.json()["items"]]
    assert "design" in names


async def test_create_and_update_agent_versions(api):
    r = await api.post("/agents", json={
        "name": "test-agent-1", "description": "test",
        "guidelines": "v1 guidelines", "max_rounds": 5})
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert r.json()["current_version"]["guidelines"] == "v1 guidelines"

    r = await api.put(f"/agents/{agent_id}", json={"guidelines": "v2 guidelines"})
    assert r.status_code == 200, r.text
    assert r.json()["current_version"]["version"] == 2
    assert r.json()["current_version"]["guidelines"] == "v2 guidelines"

    r = await api.get(f"/agents/{agent_id}/versions")
    assert r.status_code == 200
    versions = r.json()["items"]
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # newest first
    assert "acceptance_rate" in versions[0]


async def test_toggle_agent_enabled(api):
    r = await api.post("/agents", json={
        "name": "test-agent-2", "guidelines": "g"})
    agent_id = r.json()["id"]
    r = await api.put(f"/agents/{agent_id}/enabled", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    r = await api.get("/agents", params={"per_page": 200})
    assert not any(a["name"] == "test-agent-2" and a["enabled"] for a in r.json()["items"])


async def test_set_agent_description_does_not_version(api):
    r = await api.post("/agents", json={
        "name": "test-agent-4", "description": "original description",
        "guidelines": "g", "model": "gpt-x", "max_rounds": 7})
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    version_before = r.json()["current_version"]["version"]

    r = await api.put(f"/agents/{agent_id}/description",
                       json={"description": "updated description"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description"] == "updated description"
    # Non-versioned field: version number, guidelines, model, max_rounds unchanged.
    assert body["current_version"]["version"] == version_before
    assert body["current_version"]["guidelines"] == "g"
    assert body["current_version"]["model"] == "gpt-x"
    assert body["current_version"]["max_rounds"] == 7

    r = await api.get("/agents", params={"per_page": 200})
    assert r.status_code == 200
    agent = next(a for a in r.json()["items"] if a["id"] == agent_id)
    assert agent["description"] == "updated description"
    assert agent["current_version"]["version"] == version_before


async def test_agent_usage_count(api):
    r = await api.post("/agents", json={"name": "test-agent-3", "guidelines": "g"})
    agent_id = r.json()["id"]
    r = await api.get(f"/agents/{agent_id}/usage")
    assert r.status_code == 200
    assert r.json() == {"review_count": 0}

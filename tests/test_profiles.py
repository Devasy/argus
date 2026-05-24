import httpx
import pytest

from argus.api.app import create_app


@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_profile_create_and_version_bump(api):
    r = await api.post("/profiles", json={
        "name": "security-heavy", "system_prompt": "You review for security.",
        "guidelines": "- always check authz\n- flag raw SQL",
        "tool_allowlist": ["get_hunk", "get_file_lines", "list_changed_files"]})
    assert r.status_code == 201
    pid = r.json()["id"]

    r2 = await api.put(f"/profiles/{pid}", json={
        "name": "security-heavy", "system_prompt": "You review ONLY security.",
        "guidelines": "- authz first"})
    assert r2.status_code == 200
    got = await api.get("/profiles")
    prof = next(p for p in got.json() if p["id"] == pid)
    assert prof["current_version"]["version"] == 2
    assert "ONLY security" in prof["current_version"]["system_prompt"]


async def test_update_profile_rename_to_taken_name_returns_409(api):
    a = await api.post("/profiles", json={
        "name": "profile-a", "system_prompt": "You review for security."})
    assert a.status_code == 201
    b = await api.post("/profiles", json={
        "name": "profile-b", "system_prompt": "You review for style."})
    assert b.status_code == 201
    aid = a.json()["id"]

    r = await api.put(f"/profiles/{aid}", json={
        "name": "profile-b", "system_prompt": "You review ONLY security."})
    assert r.status_code == 409, r.text

    got = await api.get("/profiles")
    prof = next(p for p in got.json() if p["id"] == aid)
    assert prof["name"] == "profile-a"
    assert prof["current_version"]["version"] == 1


async def test_update_profile_noop_rename_still_succeeds(api):
    r = await api.post("/profiles", json={
        "name": "profile-c", "system_prompt": "You review for security."})
    assert r.status_code == 201
    pid = r.json()["id"]

    r2 = await api.put(f"/profiles/{pid}", json={
        "name": "profile-c", "system_prompt": "You review ONLY security."})
    assert r2.status_code == 200, r2.text

    got = await api.get("/profiles")
    prof = next(p for p in got.json() if p["id"] == pid)
    assert prof["name"] == "profile-c"
    assert prof["current_version"]["version"] == 2

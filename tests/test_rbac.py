"""api/auth.py's role model: require_role("user"/"admin") replaces the old
require_token/require_admin_token pair. Identity is still just a static
token (see the module docstring for why -- GitLab OAuth is deliberately
deferred), but every route only ever depends on the role, so this is the
seam OAuth slots into later without touching route definitions."""
import httpx
import pytest

from argus.api.app import create_app


@pytest.fixture
async def both_tokens_api(engine, settings):
    settings2 = settings.model_copy(
        update={"api_token": "user-sekrit", "admin_token": "admin-sekrit"})
    app = create_app(settings=settings2, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_admin_token_also_satisfies_user_level_routes(both_tokens_api):
    """Admin outranks user, so the same token that unlocks /stats/users must
    also work on an ordinary user-level route -- this is what lets the
    frontend collapse to a single token per session."""
    r = await both_tokens_api.get(
        "/repositories", headers={"Authorization": "Bearer admin-sekrit"})
    assert r.status_code == 200


async def test_user_token_does_not_satisfy_admin_routes(both_tokens_api):
    r = await both_tokens_api.get(
        "/stats/users", headers={"Authorization": "Bearer user-sekrit"})
    assert r.status_code == 403


async def test_wrong_token_is_401_even_with_admin_token_configured(both_tokens_api):
    r = await both_tokens_api.get(
        "/repositories", headers={"Authorization": "Bearer neither-of-them"})
    assert r.status_code == 401


async def test_me_reports_role_and_label(both_tokens_api):
    admin = await both_tokens_api.get(
        "/me", headers={"Authorization": "Bearer admin-sekrit"})
    assert admin.status_code == 200
    assert admin.json() == {"role": "admin", "label": "admin-token"}

    user = await both_tokens_api.get(
        "/me", headers={"Authorization": "Bearer user-sekrit"})
    assert user.json() == {"role": "user", "label": "user-token"}


async def test_me_reports_anonymous_when_no_token_configured(engine, settings):
    app = create_app(settings=settings, engine=engine)  # api_token == ""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/me")
    assert r.status_code == 200
    assert r.json() == {"role": "user", "label": "anonymous"}

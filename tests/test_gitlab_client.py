import json
from pathlib import Path

import httpx
import pytest
import respx

from argus.gitlab.client import GitLabClient

FIX = Path(__file__).parent / "fixtures" / "gitlab"
BASE = "https://gitlab.test"


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.fixture
async def client():
    c = GitLabClient(BASE, "tok")
    yield c
    await c.aclose()


@respx.mock
async def test_get_merge_request(client):
    respx.get(f"{BASE}/api/v4/projects/848/merge_requests/36").mock(
        return_value=httpx.Response(200, json=fx("mr")))
    mr = await client.get_merge_request(848, 36)
    assert mr["iid"] == 36
    assert mr["author"]["username"]


@respx.mock
async def test_list_discussions_paginates(client):
    page1 = fx("discussions")[:10]
    page2 = fx("discussions")[10:]
    respx.get(f"{BASE}/api/v4/projects/848/merge_requests/36/discussions").mock(
        side_effect=[
            httpx.Response(200, json=page1, headers={"X-Next-Page": "2"}),
            httpx.Response(200, json=page2, headers={"X-Next-Page": ""}),
        ])
    discussions = await client.list_discussions(848, 36)
    assert len(discussions) == len(fx("discussions"))


@respx.mock
async def test_auth_header_sent(client):
    route = respx.get(f"{BASE}/api/v4/user").mock(
        return_value=httpx.Response(200, json={"username": "argus-bot"}))
    await client.get_current_user()
    assert route.calls[0].request.headers["PRIVATE-TOKEN"] == "tok"


@respx.mock
async def test_error_raises(client):
    respx.get(f"{BASE}/api/v4/projects/848/merge_requests/999").mock(
        return_value=httpx.Response(404, json={"message": "404 Not found"}))
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_merge_request(848, 999)

"""frontend/src/features/agents/toolAllowlist.ts used to hardcode the list of
tool names offered in the agent-editor allowlist picker. It drifted the
moment outline_file/search_code were added to build_read_tools: no chip
existed for them, so they were invisible in the UI even though the pipeline
granted them to every agent regardless of tool_allowlist.

GET /agents/available-tools replaces the hardcoded list with the pipeline's
own registered tools, so a new tool needs no frontend change to become
visible.
"""
import httpx
import pytest

from argus.api.app import create_app


@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_available_tools_reflects_the_real_pipeline(api):
    r = await api.get("/agents/available-tools")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()}
    # Tools that existed before this endpoint -- the old hardcoded list.
    for legacy in ("list_changed_files", "get_hunk", "get_file_lines",
                   "graph_impact", "graph_diff_impact", "get_learning",
                   "file_knowledge", "list_module_skills", "read_module_skill"):
        assert legacy in names, f"missing legacy tool {legacy}"
    # The exact tools that were invisible until this endpoint existed.
    assert "outline_file" in names
    assert "search_code" in names


async def test_every_tool_has_a_non_empty_description(api):
    """A blank description is precisely the bug fixed for the
    structured-output tools (ScoutOutput et al had ""): the frontend renders
    this text to help a human choose what to allow, so an empty one is a
    silent regression, not a valid state."""
    r = await api.get("/agents/available-tools")
    for t in r.json():
        assert t["description"], f"{t['name']} has an empty description"


async def test_always_available_tools_are_flagged(api):
    """The frontend needs this to render outline_file/search_code as
    informational rather than a togglable chip -- unchecking one would not
    actually revoke access, since pipeline._filtered exempts them."""
    r = await api.get("/agents/available-tools")
    by_name = {t["name"]: t for t in r.json()}
    assert by_name["outline_file"]["always_available"] is True
    assert by_name["search_code"]["always_available"] is True
    assert by_name["get_hunk"]["always_available"] is False


async def test_tool_names_are_unique(api):
    r = await api.get("/agents/available-tools")
    names = [t["name"] for t in r.json()]
    assert len(names) == len(set(names))


async def test_response_is_sorted_for_a_stable_ui(api):
    r = await api.get("/agents/available-tools")
    names = [t["name"] for t in r.json()]
    assert names == sorted(names)

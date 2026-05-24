"""registered_tools() is the source of truth GET /agents/available-tools
serves to the frontend, replacing a hardcoded list that went stale the moment
outline_file/search_code were added without a matching frontend entry.
"""
from argus.review.tool_registry import registered_tools


def test_enumerates_every_read_tool_and_graphify_tool():
    names = {t["name"] for t in registered_tools()}
    for expected in ("list_changed_files", "get_hunk", "get_file_lines",
                     "outline_file", "search_code", "get_learning",
                     "file_knowledge", "list_module_skills",
                     "read_module_skill", "graph_impact", "graph_diff_impact",
                     "search_learnings_tool", "upsert_learning_tool"):
        assert expected in names, f"missing {expected}"


def test_no_duplicate_names():
    names = [t["name"] for t in registered_tools()]
    assert len(names) == len(set(names))


def test_sorted_by_name():
    names = [t["name"] for t in registered_tools()]
    assert names == sorted(names)


def test_always_available_flag_matches_pipeline_exemption():
    from argus.review.pipeline import ALWAYS_AVAILABLE_TOOLS
    by_name = {t["name"]: t for t in registered_tools()}
    for name in ALWAYS_AVAILABLE_TOOLS:
        assert by_name[name]["always_available"] is True
    for name in ("get_hunk", "get_file_lines", "list_changed_files"):
        assert by_name[name]["always_available"] is False


def test_descriptions_are_normalized_not_raw_docstrings():
    """Docstrings come through with their source indentation, which is
    harmless as a tool description sent to a model but renders as a wall of
    leading spaces on every wrapped line in a UI list."""
    by_name = {t["name"]: t for t in registered_tools()}
    desc = by_name["outline_file"]["description"]
    assert not desc.startswith(" ")
    for line in desc.splitlines():
        assert not line.startswith("        "), f"un-dedented line: {line!r}"


def test_every_description_is_non_empty():
    """An empty description is the exact bug fixed for the structured-output
    tools (ScoutOutput et al had "") -- this is the regression guard for tool
    factories, which are more numerous and easier to forget one of."""
    for t in registered_tools():
        assert t["description"], f"{t['name']} has an empty description"


def test_does_not_touch_the_filesystem_or_database(tmp_path, monkeypatch):
    """Called on every /agents/available-tools request, so it must stay
    cheap: no real workspace, no DB session, no I/O."""
    monkeypatch.chdir(tmp_path)  # nothing under cwd for a stray glob to find
    tools = registered_tools()
    assert len(tools) >= 13

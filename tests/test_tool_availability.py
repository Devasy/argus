"""Two ways a tool can be present in code but invisible to the agent that
needs it: filtered out by a profile's allowlist, or reachable but answering
in a way that reads as "this tool is useless".

Both happened. 13 reviewer_agent_versions in production carry an explicit
tool_allowlist written before outline_file/search_code existed, and the
call-graph tools dead-ended on exactly the large MRs where blast radius
matters most (Langfuse trace 1238b9e4, round 3: "graph_impact didn't give me
much useful information" -- never called again).
"""
import pytest

from argus.review.pipeline import ALWAYS_AVAILABLE_TOOLS


class _FakeTool:
    def __init__(self, name):
        self.name = name


def _filtered(tool_list, allowlist):
    """Mirrors pipeline.build_graph's _filtered closure."""
    return [t for t in tool_list
            if allowlist is None
            or t.name in allowlist
            or t.name in ALWAYS_AVAILABLE_TOOLS]


# The allowlist actually stored on production reviewer_agent_versions rows.
PROD_ALLOWLIST = ["list_changed_files", "list_module_skills", "get_hunk",
                  "read_module_skill", "get_file_lines", "graph_impact",
                  "get_learning", "graph_diff_impact", "file_knowledge"]


def test_navigation_tools_survive_a_legacy_allowlist():
    """Otherwise the agents most in need of a cheaper way to read code are
    exactly the ones denied it."""
    tools = [_FakeTool(n) for n in ("get_hunk", "outline_file", "search_code")]
    names = {t.name for t in _filtered(tools, PROD_ALLOWLIST)}
    assert names == {"get_hunk", "outline_file", "search_code"}


def test_allowlist_still_governs_everything_else():
    """The exemption is for read-only navigation aids, not a blanket bypass:
    capability grants must keep working."""
    tools = [_FakeTool(n) for n in ("file_knowledge", "upsert_learning")]
    names = {t.name for t in _filtered(tools, PROD_ALLOWLIST)}
    assert names == {"file_knowledge"}


def test_no_allowlist_means_everything_passes():
    tools = [_FakeTool(n) for n in ("get_hunk", "outline_file", "anything")]
    assert len(_filtered(tools, None)) == 3


def test_exemption_set_is_narrow():
    """A growing exemption set would quietly make allowlists meaningless, so
    each member needs a reason. outline_file/search_code: read-only
    navigation, withheld only by accident of a profile predating them.
    report_problem: the agents carrying custom allowlists are the ones whose
    configuration is most likely to be wrong, so denying them the complaint
    channel silences the reports that matter most."""
    assert ALWAYS_AVAILABLE_TOOLS == frozenset({"outline_file", "search_code",
                                                "report_problem"})


# --- graph tools must never dead-end ---------------------------------------

@pytest.fixture
def graph_tools(tmp_path, monkeypatch):
    from argus.knowledge import graphify
    from argus.review.artifacts import FileChange

    monkeypatch.setattr(graphify, "load_graph", lambda p: object())
    monkeypatch.setattr(graphify, "impacted_by", lambda g, paths: [])
    collapsed = FileChange(file_id="f1", path="src/big.js",
                           change_kind="modified", diff_available=False)
    tools = graphify.build_graphify_tool(
        tmp_path / "graph.json", {"src/big.js"},
        hunks={}, files_by_id={"f1": collapsed})
    return {t.name: t for t in tools}


@pytest.mark.asyncio
async def test_graph_impact_offers_an_alternative_when_empty(graph_tools):
    out = await graph_tools["graph_impact"].ainvoke({})
    assert "search_code" in out
    assert "Do not repeat this call" in out


@pytest.mark.asyncio
async def test_graph_diff_impact_explains_a_collapsed_diff(graph_tools):
    """It derives changed functions from hunk text, so a collapsed MR left it
    silent. Silence read as 'useless tool' and it was abandoned."""
    out = await graph_tools["graph_diff_impact"].ainvoke({})
    assert "collapsed" in out.lower()
    assert "graph_impact" in out


@pytest.mark.asyncio
async def test_graph_diff_impact_reports_coverage(tmp_path, monkeypatch):
    """A partial answer must not look like a complete one: an agent cannot
    otherwise tell 'no callers' from 'the graph does not know this symbol'."""
    from argus.knowledge import graphify
    from argus.review.artifacts import FileChange, Hunk

    monkeypatch.setattr(graphify, "load_graph", lambda p: object())
    monkeypatch.setattr(graphify, "extract_changed_functions",
                        lambda text: ["known_fn", "unknown_fn"])
    monkeypatch.setattr(graphify, "diff_impact", lambda g, fns, **kw: [
        {"changed_function": "known_fn", "file": "a.py", "seed_found": True,
         "callers": [{"function": "caller_a"}], "callees": []},
        {"changed_function": "unknown_fn", "file": "a.py", "seed_found": False,
         "callers": [], "callees": []},
    ])
    fc = FileChange(file_id="f1", path="a.py", change_kind="modified")
    hunk = Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n+x\n")
    tools = {t.name: t for t in graphify.build_graphify_tool(
        tmp_path / "g.json", {"a.py"}, hunks={"h1": hunk},
        files_by_id={"f1": fc})}

    out = await tools["graph_diff_impact"].ainvoke({})
    assert "covered 1/2" in out
    assert "NOT in graph: unknown_fn" in out
    assert "search_code" in out          # how to resolve the unknown one
    assert "caller_a" in out             # real result still present


async def test_graph_diff_impact_flags_an_edgeless_graph(tmp_path, monkeypatch):
    """The prod symptom: 134/134 symbols "covered", every one reporting
    callers=[none] callees=[none]. A bare coverage line reads as a clean bill
    of health -- "nothing calls this changed code" -- when the truth is the
    graph has no edges at all and blast radius is simply unknown."""
    from argus.knowledge import graphify
    from argus.review.artifacts import FileChange, Hunk

    monkeypatch.setattr(graphify, "load_graph", lambda p: object())
    monkeypatch.setattr(graphify, "extract_changed_functions",
                        lambda text: ["fn_a", "fn_b"])
    monkeypatch.setattr(graphify, "diff_impact", lambda g, fns, **kw: [
        {"changed_function": n, "file": "a.py", "seed_found": True,
         "callers": [], "callees": []} for n, _ in fns])

    fc = FileChange(file_id="f1", path="a.py", change_kind="modified")
    hunk = Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n+x\n")
    tools = {t.name: t for t in graphify.build_graphify_tool(
        tmp_path / "g.json", {"a.py"}, hunks={"h1": hunk},
        files_by_id={"f1": fc})}

    out = await tools["graph_diff_impact"].ainvoke({})
    assert "NO EDGES" in out
    assert "NOT evidence that the changed code is unused" in out
    assert "search_code" in out

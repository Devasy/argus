import subprocess
from pathlib import Path

from argus.knowledge.graphify import diff_impact, extract_changed_functions, impacted_by
from argus.review.artifacts import FileChange, Hunk

GRAPH = {
    "nodes": [
        {"id": "n1", "file": "core/auth.py", "name": "check_token", "kind": "function"},
        {"id": "n2", "file": "api/routes.py", "name": "login", "kind": "function"},
        {"id": "n3", "file": "api/admin.py", "name": "admin_panel", "kind": "function"},
        {"id": "n4", "file": "cli/main.py", "name": "unrelated", "kind": "function"},
    ],
    "edges": [
        {"source": "n2", "target": "n1", "relation": "calls"},
        {"source": "n3", "target": "n2", "relation": "calls"},
    ],
}


def test_direct_and_transitive_impact():
    out = impacted_by(GRAPH, {"core/auth.py"}, depth=2)
    assert "core/auth.py :: check_token" in out
    assert "api/routes.py :: login" in out        # depth 1 (calls check_token)
    assert "api/admin.py :: admin_panel" in out   # depth 2
    assert all("unrelated" not in o for o in out)


def test_depth_limit():
    out = impacted_by(GRAPH, {"core/auth.py"}, depth=1)
    assert "api/admin.py :: admin_panel" not in out


def test_extract_changed_functions_python_new_def():
    patch = "@@ -0,0 +1,2 @@\n+def check_token(t):\n+    return True\n"
    assert extract_changed_functions(patch) == ["check_token"]


def test_extract_changed_functions_python_hunk_context():
    patch = "@@ -10,3 +10,4 @@ def login(user):\n     ok = check_token(user)\n+    log(ok)\n     return ok\n"
    assert extract_changed_functions(patch) == ["login"]


def test_extract_changed_functions_js_function():
    patch = "@@ -0,0 +1,2 @@\n+function handleLogin(user) {\n+  return true;\n+}\n"
    assert extract_changed_functions(patch) == ["handleLogin"]


def test_extract_changed_functions_dedupes_and_ignores_plusplusplus():
    patch = ("+++ b/core/auth.py\n"
             "@@ -1,2 +1,3 @@ def login(user):\n"
             "+    x = 1\n"
             "@@ -10,2 +11,3 @@ def login(user):\n"
             "+    y = 2\n")
    assert extract_changed_functions(patch) == ["login"]


def test_extract_changed_functions_no_match_returns_empty():
    patch = "@@ -1,2 +1,3 @@\n+x = 1\n+y = 2\n"
    assert extract_changed_functions(patch) == []


def test_diff_impact_finds_callers_and_callees():
    out = diff_impact(GRAPH, [("check_token", "core/auth.py")], direction="both", depth=2)
    assert len(out) == 1
    entry = out[0]
    assert entry["changed_function"] == "check_token"
    assert entry["file"] == "core/auth.py"
    assert entry["seed_found"] is True
    caller_names = {c["function"] for c in entry["callers"]}
    assert "login" in caller_names
    assert "admin_panel" in caller_names


def test_diff_impact_direction_callees_only():
    out = diff_impact(GRAPH, [("login", "api/routes.py")], direction="callees", depth=2)
    entry = out[0]
    assert entry["callers"] == []
    callee_names = {c["function"] for c in entry["callees"]}
    assert "check_token" in callee_names


def test_diff_impact_unknown_function_reports_seed_not_found():
    out = diff_impact(GRAPH, [("nonexistent_fn", "cli/main.py")], direction="both", depth=2)
    assert out[0]["seed_found"] is False
    assert out[0]["callers"] == []
    assert out[0]["callees"] == []


async def test_build_graphify_tool_returns_both_tools_when_hunks_given(tmp_path):
    from argus.knowledge.graphify import build_graphify_tool
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        '{"nodes": [{"id": "n1", "file": "core/auth.py", "name": "check_token", "kind": "function"}, '
        '{"id": "n2", "file": "api/routes.py", "name": "login", "kind": "function"}], '
        '"edges": [{"source": "n2", "target": "n1", "relation": "calls"}]}'
    )
    fc = FileChange(file_id="f1", path="core/auth.py", change_kind="modified")
    hunks = {"h1": Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                        new_start=1, new_lines=2,
                        diff_text="@@ -1,1 +1,2 @@ def check_token(t):\n+    return True\n")}
    tools = build_graphify_tool(graph_path, {"core/auth.py"}, hunks=hunks, files_by_id={"f1": fc})
    names = {t.name for t in tools}
    assert names == {"graph_impact", "graph_diff_impact"}

    diff_tool = next(t for t in tools if t.name == "graph_diff_impact")
    out = await diff_tool.ainvoke({})
    assert "check_token" in out
    assert "login" in out


async def test_build_graphify_tool_returns_only_impact_when_no_hunks(tmp_path):
    from argus.knowledge.graphify import build_graphify_tool
    graph_path = tmp_path / "graph.json"
    graph_path.write_text('{"nodes": [], "edges": []}')
    tools = build_graphify_tool(graph_path, set())
    names = {t.name for t in tools}
    assert names == {"graph_impact"}


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    (repo / "hello.py").write_text("def hello():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=repo, check=True)
    return repo


async def test_build_graph_writes_graphifyignore_when_absent(tmp_path):
    import shutil as shutil_mod

    import argus.knowledge.graphify as graphify_mod

    if shutil_mod.which("graphify") is None:
        import pytest
        pytest.skip("graphify CLI not installed in this environment")

    repo = _make_repo(tmp_path)
    ignore_path = repo / ".graphifyignore"
    assert not ignore_path.exists()

    await graphify_mod.build_graph(repo)

    assert ignore_path.exists()
    assert ignore_path.read_text() == "lib/*\n"


async def test_build_graph_does_not_overwrite_existing_graphifyignore(tmp_path):
    from argus.knowledge.graphify import _write_graphifyignore

    repo = _make_repo(tmp_path)
    ignore_path = repo / ".graphifyignore"
    ignore_path.write_text("custom/*\n")

    _write_graphifyignore(repo)

    assert ignore_path.read_text() == "custom/*\n"


async def test_build_graph_falls_back_to_update_force_when_extract_fails(tmp_path, monkeypatch):
    """When `graphify extract` fails and no graph.json exists yet, build_graph
    should retry with the code-only `graphify update . --force`, which never
    needs an LLM key."""
    import argus.knowledge.graphify as graphify_mod

    repo = _make_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_which(name):
        return "/usr/bin/graphify" if name == "graphify" else None

    def fake_run(cmd, cwd=None, capture_output=None, timeout=None, check=None):
        calls.append(cmd)
        if cmd[:2] == ["graphify", "extract"]:
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[:2] == ["graphify", "update"]:
            out_dir = Path(cwd) / "graphify-out"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "graph.json").write_text('{"nodes": [], "edges": []}')
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(graphify_mod.shutil, "which", fake_which)
    monkeypatch.setattr(graphify_mod.subprocess, "run", fake_run)

    graph_path = await graphify_mod.build_graph(repo)

    assert [c[:2] for c in calls] == [["graphify", "extract"],
                                      ["graphify", "update"]]
    # extract must exclude non-code, or it aborts for want of an LLM key on
    # any repo containing a README and falls through to the edgeless
    # update --force path (see _NON_CODE_EXCLUDES).
    assert "--exclude" in calls[0] and "*.md" in calls[0]
    assert calls[1] == ["graphify", "update", ".", "--force"]
    assert graph_path is not None
    assert graph_path.exists()


async def test_build_graph_returns_existing_graph_json_when_extract_fails(tmp_path, monkeypatch):
    """If extract fails but a graph.json already exists from a prior run,
    build_graph should return it without attempting the update fallback."""
    import argus.knowledge.graphify as graphify_mod

    repo = _make_repo(tmp_path)
    out_dir = repo / "graphify-out"
    out_dir.mkdir(parents=True)
    (out_dir / "graph.json").write_text('{"nodes": [], "edges": []}')

    calls: list[list[str]] = []

    def fake_which(name):
        return "/usr/bin/graphify" if name == "graphify" else None

    def fake_run(cmd, cwd=None, capture_output=None, timeout=None, check=None):
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(graphify_mod.shutil, "which", fake_which)
    monkeypatch.setattr(graphify_mod.subprocess, "run", fake_run)

    graph_path = await graphify_mod.build_graph(repo)

    assert len(calls) == 1 and calls[0][:2] == ["graphify", "extract"]
    assert graph_path is not None
    assert graph_path == out_dir / "graph.json"


async def test_build_graph_returns_none_when_both_extract_and_update_fail(tmp_path, monkeypatch):
    import argus.knowledge.graphify as graphify_mod

    repo = _make_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_which(name):
        return "/usr/bin/graphify" if name == "graphify" else None

    def fake_run(cmd, cwd=None, capture_output=None, timeout=None, check=None):
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(graphify_mod.shutil, "which", fake_which)
    monkeypatch.setattr(graphify_mod.subprocess, "run", fake_run)

    graph_path = await graphify_mod.build_graph(repo)

    assert [c[:2] for c in calls] == [["graphify", "extract"],
                                      ["graphify", "update"]]
    # extract must exclude non-code, or it aborts for want of an LLM key on
    # any repo containing a README and falls through to the edgeless
    # update --force path (see _NON_CODE_EXCLUDES).
    assert "--exclude" in calls[0] and "*.md" in calls[0]
    assert calls[1] == ["graphify", "update", ".", "--force"]
    assert graph_path is None


# --- edgeless-graph regression (prod bug, 2026-08-08) ----------------------
# Every graph in production had 192,920 nodes and ZERO edges. `graphify
# extract` aborts when the corpus contains docs/images (they need an LLM key
# even when the code does not), and the `update --force` fallback builds nodes
# without edges. graph_diff_impact therefore answered "callers=[none]
# callees=[none]" for all 134 changed symbols of MR !52 -- indistinguishable
# from a genuine "nothing calls this", which is how a breaking change gets
# approved.

def test_extract_excludes_non_code_so_it_needs_no_llm_key():
    """The exclusions must reach the DETECTION pass, which is what triggers
    the key check -- .graphifyignore is applied later and does not prevent
    the abort."""
    from argus.knowledge.graphify import _NON_CODE_EXCLUDES, _exclude_args

    args = _exclude_args()
    assert args[0] == "--exclude"
    for pattern in ("*.md", "*.png", "*.pdf", "*.svg"):
        assert pattern in _NON_CODE_EXCLUDES
        i = args.index(pattern)
        assert args[i - 1] == "--exclude"
    # never exclude source
    for code in ("*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.go", "*.java"):
        assert code not in _NON_CODE_EXCLUDES


async def test_build_graph_warns_when_graph_has_no_edges(tmp_path, monkeypatch,
                                                         caplog):
    """An edgeless graph must be loud. Silence is what let this survive in
    production for the life of the feature."""
    import logging
    import argus.knowledge.graphify as graphify_mod

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(graphify_mod.shutil, "which",
                        lambda n: "/usr/bin/graphify" if n == "graphify" else None)

    def fake_run(cmd, cwd=None, capture_output=None, timeout=None, check=None):
        out_dir = Path(cwd) / "graphify-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "graph.json").write_text(
            '{"nodes": [{"id": "a"}, {"id": "b"}], "edges": []}')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(graphify_mod.subprocess, "run", fake_run)
    with caplog.at_level(logging.WARNING, logger="argus.graphify"):
        await graphify_mod.build_graph(repo)
    assert any("ZERO edges" in r.message for r in caplog.records)


async def test_build_graph_reports_edge_count_on_success(tmp_path, monkeypatch,
                                                          caplog):
    import logging
    import argus.knowledge.graphify as graphify_mod

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(graphify_mod.shutil, "which",
                        lambda n: "/usr/bin/graphify" if n == "graphify" else None)

    def fake_run(cmd, cwd=None, capture_output=None, timeout=None, check=None):
        out_dir = Path(cwd) / "graphify-out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "graph.json").write_text(
            '{"nodes": [{"id": "a"}, {"id": "b"}], '
            '"edges": [{"source": "a", "target": "b"}]}')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(graphify_mod.subprocess, "run", fake_run)
    with caplog.at_level(logging.INFO, logger="argus.graphify"):
        await graphify_mod.build_graph(repo)
    assert any("2 nodes, 1 edges" in r.message for r in caplog.records)
    assert not any("ZERO edges" in r.message for r in caplog.records)

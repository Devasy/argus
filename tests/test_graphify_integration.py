"""Integration test: runs the real `graphify` CLI end-to-end against a small
fixture repo, verifying build_graph() and diff_impact() work against real
subprocess output rather than a hand-built graph dict."""
import shutil
import subprocess
from pathlib import Path

import pytest

from argus.knowledge.graphify import (build_graph, diff_impact,
                                          extract_changed_functions,
                                          impacted_by, load_graph)

pytestmark = pytest.mark.skipif(
    shutil.which("graphify") is None,
    reason="graphify CLI not installed in this environment")


def _make_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fixture_repo"
    repo.mkdir()
    (repo / "core").mkdir()
    (repo / "core" / "auth.py").write_text(
        "def check_token(t):\n"
        "    return t == 'ok'\n"
    )
    (repo / "api.py").write_text(
        "from core.auth import check_token\n\n"
        "def login(user, token):\n"
        "    return check_token(token)\n"
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=repo, check=True)
    return repo


async def test_build_graph_produces_real_graph_json(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    graph_path = await build_graph(repo)
    assert graph_path is not None
    assert graph_path.exists()
    graph = load_graph(graph_path)
    assert graph.get("nodes")


async def test_impacted_by_finds_real_caller(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    graph_path = await build_graph(repo)
    assert graph_path is not None
    graph = load_graph(graph_path)
    impacted = impacted_by(graph, {"core/auth.py"}, depth=2)
    assert any("check_token" in i for i in impacted)


async def test_diff_impact_finds_real_caller_via_extracted_function(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    graph_path = await build_graph(repo)
    assert graph_path is not None
    graph = load_graph(graph_path)

    patch = "@@ -1,2 +1,2 @@\n-def check_token(t):\n+def check_token(t):\n     return t == 'ok'\n"
    changed = extract_changed_functions(patch)
    assert changed == ["check_token"]

    results = diff_impact(graph, [(changed[0], "core/auth.py")], direction="callers", depth=2)
    assert results[0]["seed_found"] is True
    caller_names = {c["function"] for c in results[0]["callers"]}
    assert "login" in caller_names

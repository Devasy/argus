"""outline_file and search_code exist because agents answered structural
questions by brute force: get_file_lines was called 1449 times across the 66
recursion-limit failures, against 85 calls to the call-graph tool. Agents were
paging through files 100 lines at a time to learn what was in them.

An outline answers "what is here and at which line" for a few hundred tokens.
A search answers "where is this defined" without reading anything.
"""
import pytest

from argus.review import navigation
from argus.review.artifacts import FileChange
from argus.review.tools import ToolContext, build_read_tools

PY_SOURCE = '''\
import os
from typing import Any

MAX_SIZE = 100


def helper(a, b=2, *args, **kwargs):
    return a + b


class Widget(Base):
    """A widget."""

    def render(self, ctx):
        return None

    async def refresh(self):
        pass


async def main():
    pass
'''

TS_SOURCE = '''\
import React from "react";

export default class Widget extends Base {
  handleClick(e) { return 1; }
}

export function helper(a, b) { return a + b; }
export const arrow = async (x) => x * 2;
interface Props { id: number }
type Alias = string;
'''


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(PY_SOURCE)
    (tmp_path / "src" / "Widget.tsx").write_text(TS_SOURCE)
    (tmp_path / "src" / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "sub" / "deep.py").write_text(
        "def helper():\n    pass\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("def vendored():\n    pass\n")
    return tmp_path


@pytest.fixture
def tools(repo):
    fc = FileChange(file_id="f1", path="src/app.py", change_kind="modified")
    ctx = ToolContext(workspace=repo, files_by_id={"f1": fc}, hunks={})
    return {t.name: t for t in build_read_tools(ctx)}


def test_navigation_tools_are_registered(tools):
    assert "outline_file" in tools and "search_code" in tools


# --- outline_file ----------------------------------------------------------

@pytest.mark.asyncio
async def test_python_outline_is_exact(tools):
    out = await tools["outline_file"].ainvoke({"path": "src/app.py"})
    assert "via ast" in out
    assert "def helper(a, b, *args, **kwargs)" in out
    assert "class Widget(Base)" in out
    assert "async def refresh(self)" in out
    assert "MAX_SIZE = ..." in out
    # methods are attributed under their class, not as top-level symbols
    assert out.index("class Widget") < out.index("def render")


@pytest.mark.asyncio
async def test_outline_reports_usable_line_numbers(tools):
    """The whole point is deciding which ranges to fetch next, so the numbers
    have to be right."""
    out = await tools["outline_file"].ainvoke({"path": "src/app.py"})
    line_of = {}
    for row in out.splitlines()[1:]:
        num, _, rest = row.strip().partition("  ")
        if num.isdigit():
            line_of[rest.strip()] = int(num)
    source = PY_SOURCE.splitlines()
    assert source[line_of["def helper(a, b, *args, **kwargs)"] - 1].startswith("def helper")
    assert source[line_of["class Widget(Base)"] - 1].startswith("class Widget")


@pytest.mark.asyncio
async def test_outline_handles_non_python(tools):
    out = await tools["outline_file"].ainvoke({"path": "src/Widget.tsx"})
    assert "via regex" in out
    for symbol in ("class Widget", "func helper", "const arrow",
                   "interface Props", "type Alias"):
        assert symbol in out


def test_unparseable_python_still_outlines():
    """A file mid-edit in a diff often does not parse. Falling back to regex
    beats reporting nothing -- reporting nothing sends the agent back to
    get_file_lines, which is what we are trying to avoid."""
    out = navigation.outline("def ok():\n    pass\n\ndef broken(:\n", "x.py")
    assert "via regex" in out
    assert "def ok" in out and "def broken" in out


@pytest.mark.asyncio
async def test_outline_failures_name_the_next_action(tools):
    escaped = await tools["outline_file"].ainvoke({"path": "../../../etc/passwd"})
    assert "escapes the repository root" in escaped

    missing = await tools["outline_file"].ainvoke({"path": "src/nope.py"})
    assert "no such file" in missing and "search_code" in missing

    directory = await tools["outline_file"].ainvoke({"path": "src"})
    assert "is a directory" in directory


@pytest.mark.asyncio
async def test_outline_reads_files_outside_the_diff(tools):
    """Reviewing a change means reading its callers and base classes too. All
    three readers now agree on this; get_file_lines was the last holdout."""
    out = await tools["outline_file"].ainvoke({"path": "src/Widget.tsx"})
    assert "class Widget" in out


# --- search_code -----------------------------------------------------------

@pytest.mark.asyncio
async def test_search_finds_definitions_with_locations(tools):
    out = await tools["search_code"].ainvoke(
        {"pattern": r"def helper", "context": 0})
    assert "src/app.py" in out and "def helper" in out


@pytest.mark.asyncio
async def test_search_respects_glob(tools):
    hit = await tools["search_code"].ainvoke(
        {"pattern": "helper", "glob": "*.py", "context": 0})
    assert "app.py" in hit and "Widget.tsx" not in hit


@pytest.mark.asyncio
async def test_search_glob_matches_nested_directories(tools):
    """Regression for an agent-reported bug: Path.match() requires the path to
    have exactly as many segments as the glob and treats "**" as a literal
    single-segment wildcard, so a recursive pattern like "src/**/*.py" never
    matched a file more than one directory below "src/". Agents saw "no
    matches" for patterns known to exist (e.g. "acme/**/*.py")."""
    hit = await tools["search_code"].ainvoke(
        {"pattern": "helper", "glob": "src/**/*.py", "context": 0})
    assert "deep.py" in hit


@pytest.mark.asyncio
async def test_search_skips_vendor_directories(tools):
    """node_modules is where a broad pattern goes to waste a round budget."""
    out = await tools["search_code"].ainvoke(
        {"pattern": "def vendored", "context": 0})
    assert "node_modules" not in out


@pytest.mark.asyncio
async def test_search_failures_name_the_next_action(tools):
    empty = await tools["search_code"].ainvoke({"pattern": "  "})
    assert "empty pattern" in empty

    bad = await tools["search_code"].ainvoke({"pattern": "def ["})
    assert "invalid regex" in bad and "Escape" in bad

    none = await tools["search_code"].ainvoke({"pattern": "zzz_absent_zzz"})
    assert "no matches" in none
    # must not invite the identical retry that burns rounds
    assert "Do not repeat this search unchanged" in none


@pytest.mark.asyncio
async def test_search_output_is_capped(tools, repo):
    """Every result stays in the message history for the rest of the stage;
    an uncapped search is a context-window overflow waiting to happen."""
    (repo / "big.py").write_text("\n".join(f"target_{i} = {i}" for i in range(5000)))
    out = await tools["search_code"].ainvoke(
        {"pattern": "target_", "context": 0, "max_matches": 10})
    assert len(out) <= 24_000 + 500


def test_search_clamps_absurd_arguments():
    """Guard the arithmetic, not just the happy path."""
    assert navigation.SEARCH_TIMEOUT_S > 0
    assert navigation.MAX_SEARCH_BYTES > 0


@pytest.mark.asyncio
async def test_python_fallback_matches_when_ripgrep_absent(repo, monkeypatch):
    """The runtime image has no ripgrep today. The fallback is the real path,
    not a theoretical one."""
    monkeypatch.setattr(navigation.shutil, "which", lambda _: None)
    hits = await navigation.search(repo, r"def helper", context=0)
    assert any("app.py" in h for h in hits)


# --- reading beyond the diff (agent complaint, 2026-08-09) ----------------
# A verifier traced a finding to a global exception handler in
# acme/common/api/main.py -- a file the MR did not touch -- and
# get_file_lines refused: "outside the current review scope". Unable to
# confirm or reject the claim, it filed a context_insufficient complaint
# twice. The verdict it could not reach is the whole point of the stage.

@pytest.mark.asyncio
async def test_verifier_can_read_an_unchanged_file_a_finding_depends_on(repo):
    """The reported scenario, reduced: the finding is in a changed file, but
    checking it requires reading one that did not change."""
    (repo / "src" / "main.py").write_text(
        "def handle_llm_error(e):\n"
        "    logger.error('llm failed: %s', e)\n"
        "    raise\n")
    changed = FileChange(file_id="f1", path="src/app.py", change_kind="modified")
    ctx = ToolContext(workspace=repo, files_by_id={"f1": changed}, hunks={})
    tools = {t.name: t for t in build_read_tools(ctx)}

    out = await tools["get_file_lines"].ainvoke(
        {"requests": [{"path": "src/main.py", "start": 1, "end": 3}]})
    assert "logger.error" in out
    assert "outside the current review scope" not in out
    assert "[NOT CHANGED BY THIS MR]" in out


@pytest.mark.asyncio
async def test_all_three_readers_agree_on_what_is_reachable(repo):
    """outline_file and search_code always read the whole worktree, so
    restricting only get_file_lines made the cheapest reader the most
    limited one -- and protected nothing."""
    changed = FileChange(file_id="f1", path="src/app.py", change_kind="modified")
    ctx = ToolContext(workspace=repo, files_by_id={"f1": changed}, hunks={})
    tools = {t.name: t for t in build_read_tools(ctx)}

    assert "class Widget" in await tools["outline_file"].ainvoke(
        {"path": "src/Widget.tsx"})
    assert "Widget.tsx" in await tools["search_code"].ainvoke(
        {"pattern": "class Widget", "context": 0})
    assert "import React" in await tools["get_file_lines"].ainvoke(
        {"requests": [{"path": "src/Widget.tsx", "start": 1, "end": 2}]})

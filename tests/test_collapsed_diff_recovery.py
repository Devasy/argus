"""GitLab collapses diff bodies on large MRs. Every test here pins a piece of
the recovery path that failure exposed.

The original incident: on MR !52 (138 files) GitLab sent every file with an
empty `diff`, so parse_diffs produced FileChanges with no hunks. get_hunk then
answered "no hunk_ids or file_ids given" -- which reads as "you called me
wrong" -- so the scout agent spent six rounds flipping between hunk_ids and
file_ids, gave up on the diff tools entirely, and burned its whole round
budget reading files line by line (Langfuse trace 1238b9e4, 180 LLM calls,
72 minutes, no review produced).
"""
import subprocess
from pathlib import Path

import pytest

from argus.review.artifacts import FileChange, Hunk
from argus.review.diffsvc import backfill_collapsed_diffs, parse_diffs
from argus.review.tools import ToolContext, build_read_tools, summary_table

COLLAPSED = {"new_path": "src/big.js", "diff": ""}
NORMAL = {"new_path": "src/a.py", "diff": "@@ -1,2 +1,3 @@\n+import os\n ctx\n"}


def _tools(files, hunks, workspace):
    ctx = ToolContext(workspace=workspace,
                      files_by_id={f.file_id: f for f in files}, hunks=hunks)
    return {t.name: t for t in build_read_tools(ctx)}


def test_collapsed_file_is_marked_unavailable():
    files, hunks = parse_diffs([COLLAPSED, NORMAL])
    collapsed, normal = files
    assert collapsed.diff_available is False and collapsed.hunk_ids == []
    assert normal.diff_available is True and len(normal.hunk_ids) == 1


def test_summary_table_warns_before_the_agent_asks():
    """Cheapest possible fix: never let the agent request what cannot exist."""
    files, _ = parse_diffs([COLLAPSED, NORMAL])
    table = summary_table(files)
    assert "COLLAPSED-use-get_file_lines" in table
    assert "NOTE:" in table and "f1" in table


@pytest.mark.asyncio
async def test_get_hunk_on_collapsed_file_names_cause_and_recovery(tmp_path):
    """The loop-breaker. The old message was indistinguishable from a
    malformed call, so the model kept guessing argument names."""
    files, hunks = parse_diffs([COLLAPSED])
    tools = _tools(files, hunks, tmp_path)
    out = await tools["get_hunk"].ainvoke({"hunk_ids": [], "file_ids": ["f1"]})
    assert "NO fetchable hunks" in out
    assert "get_file_lines" in out and "src/big.js" in out
    assert "no hunk_ids or file_ids given" not in out


@pytest.mark.asyncio
async def test_file_id_passed_as_hunk_id_also_recovers(tmp_path):
    """The exact mistake in the trace: get_hunk(hunk_ids=["f84"]). The lenient
    fallback for this existed already but silently yielded nothing when the
    file had no hunks."""
    files, hunks = parse_diffs([COLLAPSED])
    tools = _tools(files, hunks, tmp_path)
    out = await tools["get_hunk"].ainvoke({"hunk_ids": ["f1"], "file_ids": []})
    assert "NO fetchable hunks" in out and "get_file_lines" in out


@pytest.mark.asyncio
async def test_error_messages_point_at_the_next_action(tmp_path):
    files, hunks = parse_diffs([NORMAL])
    tools = _tools(files, hunks, tmp_path)
    unknown = await tools["get_hunk"].ainvoke({"hunk_ids": [], "file_ids": ["f999"]})
    assert "list_changed_files" in unknown
    empty = await tools["get_hunk"].ainvoke({"hunk_ids": [], "file_ids": []})
    assert "pass hunk ids" in empty
    good = await tools["get_hunk"].ainvoke({"hunk_ids": [], "file_ids": ["f1"]})
    assert "import os" in good


# --- git reconstruction: the real fix, independent of GitLab's limits -------

@pytest.fixture
def repo(tmp_path):
    """A repo whose feature branch forked, then had main move on -- so a
    naive two-dot diff would wrongly include the unrelated main commit."""
    r = tmp_path / "repo"
    r.mkdir()

    def git(*a):
        return subprocess.run(["git", *a], cwd=r, capture_output=True,
                              text=True, check=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (r / "app.py").write_text("def existing():\n    return 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")

    git("checkout", "-q", "-b", "feature")
    (r / "app.py").write_text(
        "def existing():\n    return 2\n\ndef added_fn():\n    return 'new'\n")
    (r / "brand_new.py").write_text("def fresh():\n    pass\n")
    git("add", "-A")
    git("commit", "-qm", "feature")
    head = git("rev-parse", "HEAD")

    git("checkout", "-q", "main")
    (r / "unrelated.py").write_text("noise = True\n")
    git("add", "-A")
    git("commit", "-qm", "unrelated main commit")
    return r, base, head


@pytest.mark.asyncio
async def test_backfill_recovers_diffs_gitlab_refused_to_send(repo):
    r, base, head = repo
    files, hunks = parse_diffs([
        {"new_path": "app.py", "diff": ""},
        {"new_path": "brand_new.py", "diff": "", "new_file": True},
    ])
    assert not any(f.diff_available for f in files) and hunks == {}

    assert await backfill_collapsed_diffs(files, hunks, r, base, head) == 2
    assert all(f.diff_available for f in files)
    text = "\n".join(h.diff_text for h in hunks.values())
    assert "added_fn" in text and "fresh" in text


@pytest.mark.asyncio
async def test_backfill_uses_three_dot_range(repo):
    """base...head is what GitLab shows for an MR: changes on the source
    branch since it forked. A two-dot range would drag in commits that landed
    on the target afterwards and report them as part of this MR."""
    r, base, head = repo
    files, hunks = parse_diffs([{"new_path": "app.py", "diff": ""}])
    await backfill_collapsed_diffs(files, hunks, r, base, head)
    assert "noise" not in "\n".join(h.diff_text for h in hunks.values())


@pytest.mark.asyncio
async def test_backfill_does_not_collide_with_existing_hunk_ids(repo):
    r, base, head = repo
    files, hunks = parse_diffs([
        {"new_path": "app.py", "diff": "@@ -1,1 +1,1 @@\n-a\n+b\n"},
        {"new_path": "brand_new.py", "diff": ""},
    ])
    before = set(hunks)
    await backfill_collapsed_diffs(files, hunks, r, base, head)
    assert before <= set(hunks) and len(hunks) > len(before)
    assert {h.file_id for h in hunks.values()} == {"f1", "f2"}
    for f in files:
        for hid in f.hunk_ids:
            assert hunks[hid].file_id == f.file_id


@pytest.mark.asyncio
async def test_backfill_degrades_instead_of_raising(repo):
    """A review must never die because reconstruction failed -- the agent
    still has get_file_lines and the message telling it to use it."""
    r, _, head = repo
    files, hunks = parse_diffs([{"new_path": "app.py", "diff": ""}])
    assert await backfill_collapsed_diffs(files, hunks, r, "d" * 40, head) == 0
    assert files[0].diff_available is False

    files2, hunks2 = parse_diffs([{"new_path": "app.py", "diff": ""}])
    assert await backfill_collapsed_diffs(files2, hunks2, r, "", head) == 0

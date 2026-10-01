import json
from pathlib import Path

from argus.review.artifacts import FileChange, Hunk
from argus.review.diffsvc import full_diff_text, parse_diffs

FIX = Path(__file__).parent / "fixtures" / "gitlab"


def test_parse_real_diffs():
    diffs = json.loads((FIX / "diffs.json").read_text())
    files, hunks = parse_diffs(diffs)
    assert len(files) == len(diffs)
    assert files[0].file_id == "f1"
    # Real GitLab payloads can mark a file too_large/collapsed with an empty
    # diff body even when change_kind != "deleted" (no hunks to anchor then).
    assert all(
        f.hunk_ids for f, d in zip(files, diffs) if f.change_kind != "deleted" and d.get("diff")
    )
    h = hunks[files[0].hunk_ids[0]]
    assert h.diff_text.startswith("@@")
    assert h.new_start >= 1
    # every hunk id resolves
    for f in files:
        for hid in f.hunk_ids:
            assert hid in hunks


def test_change_kinds():
    diffs = [
        {"new_path": "a.py", "old_path": "a.py", "new_file": True,
         "deleted_file": False, "renamed_file": False,
         "diff": "@@ -0,0 +1,2 @@\n+x=1\n+y=2\n"},
        {"new_path": "b.py", "old_path": "old_b.py", "new_file": False,
         "deleted_file": False, "renamed_file": True,
         "diff": "@@ -1,1 +1,1 @@\n-a\n+b\n"},
    ]
    files, hunks = parse_diffs(diffs)
    assert files[0].change_kind == "added" and files[0].language == "python"
    assert files[1].change_kind == "renamed" and files[1].old_path == "old_b.py"


def test_full_diff_text_orders_by_file_then_hunk():
    files = [
        FileChange(file_id="f1", path="a.py", change_kind="modified",
                   hunk_ids=["h1", "h2"]),
        FileChange(file_id="f2", path="b.py", change_kind="modified",
                   hunk_ids=["h3"]),
    ]
    hunks = {
        "h1": Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                   new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-x\n+y\n"),
        "h2": Hunk(hunk_id="h2", file_id="f1", old_start=5, old_lines=1,
                   new_start=5, new_lines=1, diff_text="@@ -5 +5 @@\n-p\n+q\n"),
        "h3": Hunk(hunk_id="h3", file_id="f2", old_start=1, old_lines=1,
                   new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-m\n+n\n"),
    }
    text = full_diff_text(files, hunks)
    assert text.index("### a.py [h1]") < text.index("### a.py [h2]")
    assert text.index("### a.py [h2]") < text.index("### b.py [h3]")
    assert "-x\n+y" in text
    assert "-m\n+n" in text


def test_full_diff_text_skips_files_with_no_hunks():
    files = [FileChange(file_id="f1", path="deleted.py", change_kind="deleted",
                        hunk_ids=[])]
    assert full_diff_text(files, {}) == ""


import subprocess

from argus.review.diffsvc import backfill_collapsed_diffs


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _repo_with_rename(tmp_path, new_body):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    body = "".join(f"line {i}\n" for i in range(40))
    (repo / "old.py").write_text(body)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "old.py", "new.py")
    (repo / "new.py").write_text(new_body(body))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "rename")
    return repo, base, _git(repo, "rev-parse", "HEAD")


async def test_backfill_diffs_a_renamed_file_against_its_old_path(tmp_path):
    repo, base, head = _repo_with_rename(
        tmp_path, lambda b: b.replace("line 20\n", "line twenty\n"))
    fc = FileChange(file_id="f1", path="new.py", old_path="old.py",
                    change_kind="renamed", diff_available=False)
    hunks: dict = {}
    assert await backfill_collapsed_diffs([fc], hunks, repo, base, head) == 1
    h = hunks[fc.hunk_ids[0]]
    assert h.old_start > 0, "rename must diff against old.py, not as a new file"
    assert "+line twenty" in h.diff_text


async def test_backfill_pure_rename_with_no_hunks_is_left_unavailable(tmp_path):
    repo, base, head = _repo_with_rename(tmp_path, lambda b: b)
    fc = FileChange(file_id="f1", path="new.py", old_path="old.py",
                    change_kind="renamed", diff_available=False)
    assert await backfill_collapsed_diffs([fc], {}, repo, base, head) == 0
    assert fc.diff_available is False

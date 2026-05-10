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

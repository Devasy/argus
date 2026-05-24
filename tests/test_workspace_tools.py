import subprocess
from pathlib import Path

from argus.review.artifacts import FileChange, Hunk
from argus.review.tools import ToolContext, build_read_tools
from argus.review.workspace import WorkspaceManager


def _make_origin(tmp_path: Path) -> tuple[str, str]:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True)
    (origin / "hello.py").write_text("x = 1\ny = 2\nz = 3\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=origin, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin,
                         capture_output=True, text=True, check=True).stdout.strip()
    return str(origin), sha


async def test_acquire_worktree(tmp_path):
    url, sha = _make_origin(tmp_path)
    wm = WorkspaceManager(tmp_path / "root", url)
    wt = await wm.acquire(sha)
    assert (wt / "hello.py").read_text().startswith("x = 1")
    wt2 = await wm.acquire(sha)     # idempotent
    assert wt2 == wt
    await wm.release(sha)
    assert not wt.exists()


async def test_acquire_falls_back_to_merge_request_ref_after_branch_deletion(tmp_path):
    """"Delete source branch on merge" (the common setting on the ncte
    project) makes an MR's head sha unreachable via refs/heads/* the moment
    it merges. A post-merge distillation run against that sha then failed
    outright with "git worktree add ... exit 128" (2026-09-07/08, four
    merged acme-threat-intel MRs) even though GitLab still exposes
    refs/merge-requests/<iid>/head for it regardless of branch deletion."""
    url, main_sha = _make_origin(tmp_path)
    origin = Path(url)
    wm = WorkspaceManager(tmp_path / "root", url)
    await wm.acquire(main_sha)   # bare.git now exists, like an already-used repo

    subprocess.run(["git", "checkout", "-b", "feature"], cwd=origin, check=True)
    (origin / "hello.py").write_text("x = 100\ny = 2\nz = 3\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "feature work"], cwd=origin, check=True)
    feature_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin,
        capture_output=True, text=True, check=True).stdout.strip()
    # GitLab keeps this ref regardless of what happens to the source branch.
    subprocess.run(["git", "update-ref", "refs/merge-requests/42/head", feature_sha],
                   cwd=origin, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=origin, check=True)
    subprocess.run(["git", "branch", "-D", "feature"], cwd=origin, check=True)

    # The sha was never fetched (its only branch is now gone) -- without the
    # mr_iid fallback this is exactly the production failure.
    try:
        await wm.acquire(feature_sha)
    except RuntimeError as e:
        assert "worktree add" in str(e)
    else:
        raise AssertionError("expected acquire to fail without mr_iid")

    wt = await wm.acquire(feature_sha, mr_iid=42)
    assert (wt / "hello.py").read_text().startswith("x = 100")


async def test_acquire_falls_back_to_direct_sha_fetch_when_the_mr_ref_is_gone(
        tmp_path):
    """The MR-ref fallback above assumes GitLab keeps refs/merge-requests/<iid>/
    head forever. It does not: verified live against gitlab.example.internal on a
    real MR (docker-compose!12, squash-merged 2026-08-24, source branch
    force-deleted) that `git fetch +refs/merge-requests/12/head:...` fails
    with "couldn't find remote ref" two weeks after merge, while `git fetch
    origin <the original head sha>` succeeds -- the commit is still present,
    just unreachable from any ref. All 7 golden-set benchmark reviews failed
    on exactly this in ~1s each. So when the MR-ref fetch itself fails,
    fall back to fetching the sha directly rather than giving up."""
    url, main_sha = _make_origin(tmp_path)
    origin = Path(url)
    wm = WorkspaceManager(tmp_path / "root", url)
    await wm.acquire(main_sha)

    subprocess.run(["git", "checkout", "-b", "feature"], cwd=origin, check=True)
    (origin / "hello.py").write_text("squashed = True\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "feature work"], cwd=origin, check=True)
    feature_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin,
        capture_output=True, text=True, check=True).stdout.strip()
    # No refs/merge-requests/<iid>/head ever created here, simulating either
    # a project with the feature off or -- the real case -- one GitLab has
    # since pruned. The source branch is also gone, same as production.
    subprocess.run(["git", "checkout", "main"], cwd=origin, check=True)
    subprocess.run(["git", "branch", "-D", "feature"], cwd=origin, check=True)

    wt = await wm.acquire(feature_sha, mr_iid=99)
    assert (wt / "hello.py").read_text() == "squashed = True\n"


async def test_read_tools(tmp_path):
    url, sha = _make_origin(tmp_path)
    wm = WorkspaceManager(tmp_path / "root", url)
    wt = await wm.acquire(sha)
    fc = FileChange(file_id="f1", path="hello.py", change_kind="modified",
                    language="python", hunk_ids=["h1"], summary="hello module")
    hunk = Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-x = 0\n+x = 1\n")
    ctx = ToolContext(workspace=wt, files_by_id={"f1": fc}, hunks={"h1": hunk})
    _t = {t.name: t for t in build_read_tools(ctx)}
    get_hunk, get_file_lines, list_changed = (
        _t["get_hunk"], _t["get_file_lines"], _t["list_changed_files"])
    assert "+x = 1" in await get_hunk.ainvoke({"hunk_ids": ["h1"]})
    out = await get_file_lines.ainvoke(
        {"requests": [{"path": "hello.py", "start": 1, "end": 2}]})
    assert "1 | x = 1" in out and "2 | y = 2" in out
    table = await list_changed.ainvoke({})
    assert "f1" in table and "hello.py" in table


async def test_get_file_lines_rejects_workspace_escape(tmp_path):
    url, sha = _make_origin(tmp_path)
    wm = WorkspaceManager(tmp_path / "root", url)
    wt = await wm.acquire(sha)
    ctx = ToolContext(workspace=wt, files_by_id={}, hunks={})
    get_file_lines = {t.name: t for t in build_read_tools(ctx)}["get_file_lines"]
    out = await get_file_lines.ainvoke(
        {"requests": [{"path": "../../../../../../etc/passwd", "start": 1, "end": 2}]})
    assert out == "../../../../../../etc/passwd: path escapes workspace"


async def test_get_hunk_rejects_out_of_scope_hunk(tmp_path):
    url, sha = _make_origin(tmp_path)
    wm = WorkspaceManager(tmp_path / "root", url)
    wt = await wm.acquire(sha)
    fc = FileChange(file_id="f1", path="hello.py", change_kind="modified",
                    language="python", hunk_ids=["h1"], summary="hello module")
    hunk_in_scope = Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                         new_start=1, new_lines=1,
                         diff_text="@@ -1 +1 @@\n-x = 0\n+x = 1\n")
    hunk_out_of_scope = Hunk(hunk_id="h2", file_id="f2", old_start=1,
                             old_lines=1, new_start=1, new_lines=1,
                             diff_text="@@ -1 +1 @@\n-a = 0\n+a = 1\n")
    # files_by_id only contains f1; f2's hunk should be out of scope.
    ctx = ToolContext(workspace=wt, files_by_id={"f1": fc},
                      hunks={"h1": hunk_in_scope, "h2": hunk_out_of_scope})
    get_hunk = {t.name: t for t in build_read_tools(ctx)}["get_hunk"]
    out = await get_hunk.ainvoke({"hunk_ids": ["h2"]})
    assert out == "hunk h2 is outside the current review scope"
    # in-scope hunk still works normally
    in_scope_out = await get_hunk.ainvoke({"hunk_ids": ["h1"]})
    assert "+x = 1" in in_scope_out


async def test_get_file_lines_reads_unchanged_files_and_marks_them(tmp_path):
    """Reviewing a change means reading the code around it. This used to
    refuse any file outside the diff, so a verifier that correctly traced a
    finding to acme/common/api/main.py was told "outside the current
    review scope" and had to file a context_insufficient complaint instead of
    reaching a verdict. The restriction also protected nothing once
    outline_file/search_code shipped -- both read the whole worktree -- it
    only made the cheapest reader the most restricted one."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True)
    (origin / "hello.py").write_text("x = 1\ny = 2\nz = 3\n")
    (origin / "other.py").write_text("a = 1\nb = 2\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=origin, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin,
                         capture_output=True, text=True, check=True).stdout.strip()
    wm = WorkspaceManager(tmp_path / "root", str(origin))
    wt = await wm.acquire(sha)
    fc = FileChange(file_id="f1", path="hello.py", change_kind="modified",
                    language="python", hunk_ids=["h1"], summary="hello module")
    ctx = ToolContext(workspace=wt, files_by_id={"f1": fc}, hunks={})
    get_file_lines = {t.name: t for t in build_read_tools(ctx)}["get_file_lines"]
    out = await get_file_lines.ainvoke(
        {"requests": [{"path": "other.py", "start": 1, "end": 2}]})
    assert "1 | a = 1" in out
    # ...but labelled, so a finding against it is read as pre-existing rather
    # than something this MR introduced.
    assert "[NOT CHANGED BY THIS MR]" in out

    # in-scope path still works and carries no marker
    in_scope_out = await get_file_lines.ainvoke(
        {"requests": [{"path": "hello.py", "start": 1, "end": 2}]})
    assert "1 | x = 1" in in_scope_out
    assert "[NOT CHANGED BY THIS MR]" not in in_scope_out

    # workspace containment is still the real boundary
    escaped = await get_file_lines.ainvoke(
        {"requests": [{"path": "../../../../etc/passwd", "start": 1, "end": 2}]})
    assert "escapes workspace" in escaped

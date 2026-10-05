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


async def test_acquire_repoints_origin_when_clone_url_changes(tmp_path):
    """A bare repo's `origin` URL is otherwise only ever set at clone time --
    simulating a credential/token rotation (a new clone_url pointing at a
    second origin) must still be picked up on a later acquire() against the
    same already-cloned workspace, not silently keep fetching the old one."""
    url_a, sha_a = _make_origin(tmp_path)
    wm = WorkspaceManager(tmp_path / "root", url_a)
    await wm.acquire(sha_a)

    origin_b = tmp_path / "origin_b"
    origin_b.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=origin_b, check=True)
    (origin_b / "hello.py").write_text("rotated = True\n")
    subprocess.run(["git", "add", "-A"], cwd=origin_b, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=origin_b, check=True)
    sha_b = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin_b,
                           capture_output=True, text=True,
                           check=True).stdout.strip()

    wm.clone_url = str(origin_b)
    wt_b = await wm.acquire(sha_b)
    assert (wt_b / "hello.py").read_text() == "rotated = True\n"

    remote_url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=wm.bare,
        capture_output=True, text=True, check=True).stdout.strip()
    assert remote_url == str(origin_b)


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
    assert out.startswith("hunk h2 is outside the current review scope")
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


async def test_a_shared_worktree_survives_until_its_last_user_releases(tmp_path):
    url, sha = _make_origin(tmp_path)
    review_wm = WorkspaceManager(tmp_path / "root", url)
    distill_wm = WorkspaceManager(tmp_path / "root", url)
    wt = await review_wm.acquire(sha)
    assert await distill_wm.acquire(sha) == wt

    await review_wm.release(sha)
    assert (wt / "hello.py").exists()
    await distill_wm.release(sha)
    assert not wt.exists()


async def test_a_release_without_an_acquire_cannot_pull_a_worktree_from_under_a_live_job(tmp_path):
    url, sha = _make_origin(tmp_path)
    holder = WorkspaceManager(tmp_path / "root", url)
    wt = await holder.acquire(sha)

    await WorkspaceManager(tmp_path / "root", url).release(sha)
    assert (wt / "hello.py").exists()
    await holder.release(sha)
    assert not wt.exists()


async def test_git_commands_on_one_repo_never_overlap_but_different_repos_do(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time
    import argus.review.workspace as ws
    url, sha = _make_origin(tmp_path)
    real_run, guard = ws._run, threading.Lock()
    live, peak = {"all": 0}, {"all": 0}

    def slow_run(args, cwd=None):
        repo = str(Path(cwd or args[-1]).parent)  # every call runs in, or clones to, <root>/bare.git
        with guard:
            for k in (repo, "all"):
                live[k] = live.get(k, 0) + 1
                peak[k] = max(peak.get(k, 0), live[k])
        time.sleep(0.05)
        try:
            real_run(args, cwd)
        finally:
            with guard:
                live[repo] -= 1
                live["all"] -= 1

    monkeypatch.setattr(ws, "_run", slow_run)
    managers = [WorkspaceManager(tmp_path / root, url) for root in ["a"] * 4 + ["b"] * 4]
    await asyncio.gather(*(m.acquire(sha) for m in managers))
    assert peak[str(tmp_path / "a")] == 1
    assert peak[str(tmp_path / "b")] == 1
    assert peak["all"] == 2


def _commit(origin: Path, name: str, body: str) -> str:
    (origin / "hello.py").write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", name], cwd=origin, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin,
                          capture_output=True, text=True, check=True).stdout.strip()


async def test_branches_sharing_a_12_char_prefix_get_separate_worktrees(tmp_path):
    url, _ = _make_origin(tmp_path)
    origin = Path(url)
    subprocess.run(["git", "checkout", "-qb", "develop-7.0.0"], cwd=origin, check=True)
    _commit(origin, "a", "x = 'stable'\n")
    subprocess.run(["git", "checkout", "-qb", "develop-7.0.0-beta"], cwd=origin, check=True)
    _commit(origin, "b", "x = 'beta'\n")

    stable = await WorkspaceManager(tmp_path / "root", url).acquire("develop-7.0.0")
    beta = await WorkspaceManager(tmp_path / "root", url).acquire("develop-7.0.0-beta")
    assert stable != beta
    assert "stable" in (stable / "hello.py").read_text()
    assert "beta" in (beta / "hello.py").read_text()


async def test_a_leftover_branch_worktree_is_not_reused_after_the_branch_moves(tmp_path):
    url, _ = _make_origin(tmp_path)
    origin = Path(url)
    first = await WorkspaceManager(tmp_path / "root", url).acquire("main")  # never released: a crash
    _commit(origin, "moved", "x = 'new'\n")
    second = await WorkspaceManager(tmp_path / "root", url).acquire("main")
    assert second != first
    assert "new" in (second / "hello.py").read_text()


async def test_a_branch_acquire_is_released_by_the_same_name(tmp_path):
    url, _ = _make_origin(tmp_path)
    wm = WorkspaceManager(tmp_path / "root", url)
    wt = await wm.acquire("main")
    await wm.release("main")
    assert not wt.exists()


async def test_a_sha_acquire_keeps_its_worktree_path(tmp_path):
    url, sha = _make_origin(tmp_path)
    wt = await WorkspaceManager(tmp_path / "root", url).acquire(sha)
    assert wt.name == f"wt-{sha[:12]}"


def test_git_retries_a_transient_dns_failure(monkeypatch):
    import argus.review.workspace as ws
    calls = []

    def flaky(args, **kw):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(
                128, args, stderr="fatal: unable to access 'https://gitlab.example.internal/x.git/': "
                                  "Could not resolve host: gitlab.example.internal")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(ws.subprocess, "run", flaky)
    monkeypatch.setattr(ws.time, "sleep", lambda s: None)
    ws._run(["git", "fetch", "origin"])
    assert len(calls) == 2


def test_git_does_not_retry_a_real_error(monkeypatch):
    import pytest
    import argus.review.workspace as ws
    calls = []

    def missing_ref(args, **kw):
        calls.append(args)
        raise subprocess.CalledProcessError(128, args, stderr="fatal: couldn't find remote ref x")

    monkeypatch.setattr(ws.subprocess, "run", missing_ref)
    monkeypatch.setattr(ws.time, "sleep", lambda s: None, raising=False)
    with pytest.raises(RuntimeError, match="couldn't find remote ref"):
        ws._run(["git", "fetch", "origin", "x"])
    assert len(calls) == 1


def test_git_diagnostics_redact_userinfo_with_slashes():
    from argus.review.workspace import _safe_git_error
    message = "unable to access https://user:pass/word@host/repo.git: failed"
    safe = _safe_git_error(message, None)
    assert "pass" not in safe and "word" not in safe
    assert "https://[redacted]@host/repo.git" in safe


async def test_http_credentials_never_enter_argv_or_logged_traceback(tmp_path, monkeypatch):
    import traceback
    import pytest
    import argus.review.workspace as ws
    token = "dummy-test-password"
    seen = []

    def fail(args, **kw):
        seen.append((args, kw.get("env")))
        raise subprocess.CalledProcessError(128, args, stderr=f"authentication failed: {token}")

    monkeypatch.setattr(ws.subprocess, "run", fail)
    wm = WorkspaceManager(tmp_path / "root", f"https://oauth2:{token}@gitlab.test/group/repo.git")
    with pytest.raises(RuntimeError) as caught:
        await wm.acquire("a" * 40)
    args, env = seen[0]
    assert args[3] == "https://gitlab.test/group/repo.git"
    assert token not in " ".join(args)
    assert any(v.startswith("Authorization: Basic ") for v in env.values())
    assert token not in "".join(traceback.format_exception(caught.value))


async def test_http_authentication_works_without_persisting_credentials(tmp_path):
    import base64
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import quote
    url, sha = _make_origin(tmp_path)
    served = tmp_path / "served"
    served.mkdir()
    bare = served / "repo.git"
    subprocess.run(["git", "clone", "--bare", url, str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "update-server-info"], cwd=bare, check=True)
    credentials = "oauth2:dummy:p@ss/word"
    expected = "Basic " + base64.b64encode(credentials.encode()).decode()
    authenticated = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="test"')
                self.end_headers()
                return
            authenticated.append(self.path)
            super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / "workspace"
    clean_url = f"http://127.0.0.1:{server.server_port}/repo.git"
    credential_url = clean_url.replace("http://", f"http://oauth2:{quote(credentials.partition(':')[2], safe='')}@")
    wm = WorkspaceManager(root, credential_url)
    try:
        wt = await wm.acquire(sha)
        assert (wt / "hello.py").read_text().startswith("x = 1")
        # The existing-workspace path must authenticate again with runtime credentials.
        await wm.acquire(sha)
        config = (wm.bare / "config").read_text()
        assert credential_url not in config
        assert "dummy" not in config
        assert clean_url in config
        assert authenticated
        await wm.release(sha)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_windows_cleanup_recovers_only_an_empty_worktree_directory(tmp_path, monkeypatch):
    import argus.review.workspace as ws
    url, sha = _make_origin(tmp_path)
    wm = WorkspaceManager(tmp_path / "root", url)
    wt = await wm.acquire(sha)
    real_run = ws._run

    def partially_removed(args, cwd=None, **kwargs):
        real_run(args, cwd=cwd, **kwargs)
        if args[:3] == ["git", "worktree", "remove"]:
            wt.mkdir(exist_ok=True)
            raise RuntimeError("failed to delete directory: Permission denied")

    monkeypatch.setattr(ws, "_WINDOWS", True)
    monkeypatch.setattr(ws, "_run", partially_removed)
    await wm.release(sha)
    assert not wt.exists()


async def test_windows_cleanup_preserves_nonempty_worktree_on_failure(tmp_path, monkeypatch):
    import pytest
    import argus.review.workspace as ws
    url, sha = _make_origin(tmp_path)
    wm = WorkspaceManager(tmp_path / "root", url)
    wt = await wm.acquire(sha)
    real_run = ws._run

    def blocked(args, cwd=None, **kwargs):
        if args[:3] == ["git", "worktree", "remove"]:
            raise RuntimeError("failed to delete directory: Permission denied")
        real_run(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(ws, "_WINDOWS", True)
    monkeypatch.setattr(ws, "_run", blocked)
    with pytest.raises(RuntimeError, match="Permission denied"):
        await wm.release(sha)
    assert (wt / "hello.py").read_text().startswith("x = 1")

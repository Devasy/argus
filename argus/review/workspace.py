import asyncio
import base64
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit


# stderr of a git call whose connection never happened; anything else is a real answer and is not retried
_TRANSIENT = ("Could not resolve host", "Temporary failure in name resolution",
              "No address associated with hostname", "Failed to connect",
              "Connection timed out")
_RETRY_DELAYS_S = (2, 5)
_WINDOWS = os.name == "nt"


def _safe_git_error(message: str, env: dict[str, str] | None) -> str:
    """Remove URL credentials and runtime authorization from Git diagnostics."""
    message = re.sub(r"(https?://)[^\s@]+@", r"\1[redacted]@", message)
    for key, value in (env or {}).items():
        if key.startswith("GIT_CONFIG_VALUE_") and value.startswith("Authorization: Basic "):
            encoded = value.removeprefix("Authorization: Basic ")
            try:
                username, _, password = base64.b64decode(encoded, validate=True).decode().partition(":")
            except (ValueError, UnicodeError):
                continue
            for secret in (encoded, username, password):
                if secret:
                    message = message.replace(secret, "[redacted]")
    return message


def _git_auth(clone_url: str) -> tuple[str, dict[str, str] | None]:
    """Supply HTTP credentials at runtime, outside argv and persisted remotes."""
    parts = urlsplit(clone_url)
    if parts.scheme not in ("http", "https") or parts.username is None:
        return clone_url, None
    clean_url = urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[-1]))
    credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
    header = "Authorization: Basic " + base64.b64encode(credentials.encode()).decode()
    env = os.environ.copy()
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    # Empty extraHeader resets inherited headers; disable credential helpers
    # so rotated credentials cannot be replaced by a cached account.
    for key, value in ((f"http.{clean_url}.extraHeader", ""),
                       (f"http.{clean_url}.extraHeader", header),
                       ("credential.helper", "")):
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return clean_url, env


def _run(args: list[str], cwd: Path | None = None, *, env: dict[str, str] | None = None) -> None:
    """Run a git command, raising with the actual stderr on failure.

    subprocess.CalledProcessError's default str() is just "returned non-zero
    exit status 128" -- every caller here only logs str(e), so git's own
    reason (the useful part) was being silently discarded. Network failures
    (the container's DNS drops gitlab.example.internal intermittently) are retried."""
    for delay in (*_RETRY_DELAYS_S, None):
        try:
            subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=env)
            return
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or "").strip() or (e.stdout or "").strip()
            if delay is not None and any(t in detail for t in _TRANSIENT):
                time.sleep(delay)
                continue
            raise RuntimeError(
                _safe_git_error(f"{' '.join(args)} failed (exit {e.returncode}): {detail}", env)) from None


# Process-wide, keyed by repo root: every job builds its own WorkspaceManager for the same repo.
_repo_locks: dict[str, asyncio.Lock] = {}
# How many live jobs hold each worktree; a review and a distill of one sha share the same folder.
_worktree_users: dict[str, int] = {}


def _repo_lock(root: Path) -> asyncio.Lock:
    return _repo_locks.setdefault(str(root.resolve()), asyncio.Lock())


_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _commit_of(bare: Path, ref: str) -> str:
    """A branch name is not a checkout: pin it to its commit so the worktree is named, and reused, by that."""
    if _FULL_SHA.fullmatch(ref):
        return ref
    out = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=bare,
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ref


class WorkspaceManager:
    def __init__(self, root: Path, clone_url: str, *, review_ref=None):
        self.root = Path(root)
        self.clone_url = clone_url
        self.review_ref = review_ref or (lambda number: f"refs/merge-requests/{number}/head")
        self.bare = self.root / "bare.git"
        # Worktrees this instance acquired, so a release after a failed acquire can't drop another job's hold.
        self._held: set[str] = set()
        # branch/tag refs acquired by this instance -> the commit their worktree was built at
        self._resolved: dict[str, str] = {}

    async def acquire(self, sha: str, mr_iid: int | None = None) -> Path:
        def _sync() -> Path:
            clone_url, env = _git_auth(self.clone_url)

            def git(args, cwd=None):
                if env is None:
                    _run(args, cwd=cwd)
                else:
                    _run(args, cwd=cwd, env=env)

            self.root.mkdir(parents=True, exist_ok=True)
            if not self.bare.exists():
                git(["git", "clone", "--bare", clone_url, str(self.bare)])
            else:
                # Replace legacy credential-bearing remotes before fetching;
                # current credentials are supplied only through the environment.
                remote = subprocess.run(["git", "remote", "get-url", "origin"],
                                        cwd=self.bare, capture_output=True, text=True)
                if remote.returncode or remote.stdout.strip() != clone_url:
                    git(["git", "remote", "set-url", "origin", clone_url],
                        cwd=self.bare)
                git(["git", "fetch", "origin", "+refs/heads/*:refs/heads/*"],
                     cwd=self.bare)
            if mr_iid is not None:
                # `sha` is often the tip of the MR's source branch, which
                # refs/heads/* only tracks while that branch still exists --
                # "delete source branch on merge" (the common case here)
                # removes it within minutes of merging, so a post-merge
                # distillation run against that same sha then fails with a
                # bare "git worktree add ... exit 128" (the sha simply isn't
                # in the bare repo). GitLab keeps refs/merge-requests/<iid>/
                # head pointed at it regardless of branch deletion, so fetch
                # that too rather than requiring the branch to be alive.
                try:
                    review_ref = self.review_ref(mr_iid)
                    git(["git", "fetch", "origin",
                          f"+{review_ref}:{review_ref}"], cwd=self.bare)
                except RuntimeError:
                    # GitLab does not keep that ref forever: verified live
                    # against a real MR squash-merged 2+ weeks earlier with
                    # its source branch force-deleted -- the ref fetch fails
                    # with "couldn't find remote ref", yet `git fetch origin
                    # <sha>` still succeeds, because the commit itself is
                    # still present, just unreachable from any ref. All 7
                    # golden-set benchmark reviews failed on exactly this.
                    git(["git", "fetch", "origin", sha], cwd=self.bare)
            commit = _commit_of(self.bare, sha)
            self._resolved[sha] = commit
            wt = self.root / f"wt-{commit[:12]}"
            if not wt.exists():
                _run(["git", "worktree", "add", "--detach", str(wt), commit],
                     cwd=self.bare)
            return wt
        # Concurrent git fetch/worktree ops on one bare repo fail on ref locks, so serialize per repo.
        async with _repo_lock(self.root):
            wt = await asyncio.to_thread(_sync)
            key = str(wt.resolve())
            if key not in self._held:
                self._held.add(key)
                _worktree_users[key] = _worktree_users.get(key, 0) + 1
        return wt

    async def release(self, sha: str) -> None:
        commit = self._resolved.get(sha, sha)

        def _sync() -> None:
            wt = self.root / f"wt-{commit[:12]}"
            if wt.exists():
                try:
                    _run(["git", "worktree", "remove", "--force", str(wt)],
                         cwd=self.bare)
                except RuntimeError as error:
                    # Windows can hold the directory after
                    # Git has removed its files and registration. Recover only
                    # that empty directory, never recursively delete leftovers.
                    if (not _WINDOWS or "Permission denied" not in str(error)
                            or wt.resolve().parent != self.root.resolve()
                            or not wt.is_dir() or any(wt.iterdir())):
                        raise
                    for delay in (0.1, 0.5, 2, 5):
                        try:
                            wt.rmdir()
                            break
                        except FileNotFoundError:
                            break
                        except PermissionError:
                            time.sleep(delay)
                    else:
                        raise error
                    _run(["git", "worktree", "prune"], cwd=self.bare)
        async with _repo_lock(self.root):
            key = str((self.root / f"wt-{commit[:12]}").resolve())
            if key in self._held:
                self._held.discard(key)
                _worktree_users[key] = _worktree_users.get(key, 1) - 1
            if _worktree_users.get(key, 0) > 0:
                return
            _worktree_users.pop(key, None)
            await asyncio.to_thread(_sync)

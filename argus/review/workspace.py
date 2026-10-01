import asyncio
import re
import subprocess
import time
from pathlib import Path


# stderr of a git call whose connection never happened; anything else is a real answer and is not retried
_TRANSIENT = ("Could not resolve host", "Temporary failure in name resolution",
              "No address associated with hostname", "Failed to connect",
              "Connection timed out")
_RETRY_DELAYS_S = (2, 5)


def _run(args: list[str], cwd: Path | None = None) -> None:
    """Run a git command, raising with the actual stderr on failure.

    subprocess.CalledProcessError's default str() is just "returned non-zero
    exit status 128" -- every caller here only logs str(e), so git's own
    reason (the useful part) was being silently discarded. Network failures
    (the container's DNS drops gitlab.example.internal intermittently) are retried."""
    for delay in (*_RETRY_DELAYS_S, None):
        try:
            subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
            return
        except subprocess.CalledProcessError as e:
            detail = e.stderr.strip() or e.stdout.strip()
            if delay is not None and any(t in detail for t in _TRANSIENT):
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"{' '.join(args)} failed (exit {e.returncode}): {detail}") from e


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
    def __init__(self, root: Path, clone_url: str):
        self.root = Path(root)
        self.clone_url = clone_url
        self.bare = self.root / "bare.git"
        # Worktrees this instance acquired, so a release after a failed acquire can't drop another job's hold.
        self._held: set[str] = set()
        # branch/tag refs acquired by this instance -> the commit their worktree was built at
        self._resolved: dict[str, str] = {}

    async def acquire(self, sha: str, mr_iid: int | None = None) -> Path:
        def _sync() -> Path:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.bare.exists():
                _run(["git", "clone", "--bare", self.clone_url, str(self.bare)])
            else:
                # clone_url carries a freshly-built credential (see call sites),
                # but origin's URL is otherwise only ever set at clone time --
                # a bare repo cloned before a token rotation would keep
                # fetching with the stale credential baked into its own
                # config forever. Re-pointing origin here before every fetch
                # keeps existing workspaces in sync with the current token.
                _run(["git", "remote", "set-url", "origin", self.clone_url],
                     cwd=self.bare)
                _run(["git", "fetch", "origin", "+refs/heads/*:refs/heads/*"],
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
                    _run(["git", "fetch", "origin",
                          f"+refs/merge-requests/{mr_iid}/head:"
                          f"refs/merge-requests/{mr_iid}/head"], cwd=self.bare)
                except RuntimeError:
                    # GitLab does not keep that ref forever: verified live
                    # against a real MR squash-merged 2+ weeks earlier with
                    # its source branch force-deleted -- the ref fetch fails
                    # with "couldn't find remote ref", yet `git fetch origin
                    # <sha>` still succeeds, because the commit itself is
                    # still present, just unreachable from any ref. All 7
                    # golden-set benchmark reviews failed on exactly this.
                    _run(["git", "fetch", "origin", sha], cwd=self.bare)
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
                _run(["git", "worktree", "remove", "--force", str(wt)],
                     cwd=self.bare)
        async with _repo_lock(self.root):
            key = str((self.root / f"wt-{commit[:12]}").resolve())
            if key in self._held:
                self._held.discard(key)
                _worktree_users[key] = _worktree_users.get(key, 1) - 1
            if _worktree_users.get(key, 0) > 0:
                return
            _worktree_users.pop(key, None)
            await asyncio.to_thread(_sync)

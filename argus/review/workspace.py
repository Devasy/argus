import asyncio
import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path | None = None) -> None:
    """Run a git command, raising with the actual stderr on failure.

    subprocess.CalledProcessError's default str() is just "returned non-zero
    exit status 128" -- every caller here only logs str(e), so git's own
    reason (the useful part) was being silently discarded."""
    try:
        subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip() or e.stdout.strip()
        raise RuntimeError(
            f"{' '.join(args)} failed (exit {e.returncode}): {detail}") from e


class WorkspaceManager:
    def __init__(self, root: Path, clone_url: str):
        self.root = Path(root)
        self.clone_url = clone_url
        self.bare = self.root / "bare.git"

    async def acquire(self, sha: str, mr_iid: int | None = None) -> Path:
        def _sync() -> Path:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.bare.exists():
                _run(["git", "clone", "--bare", self.clone_url, str(self.bare)])
            else:
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
            wt = self.root / f"wt-{sha[:12]}"
            if not wt.exists():
                _run(["git", "worktree", "add", "--detach", str(wt), sha],
                     cwd=self.bare)
            return wt
        return await asyncio.to_thread(_sync)

    async def release(self, sha: str) -> None:
        def _sync() -> None:
            wt = self.root / f"wt-{sha[:12]}"
            if wt.exists():
                _run(["git", "worktree", "remove", "--force", str(wt)],
                     cwd=self.bare)
        await asyncio.to_thread(_sync)

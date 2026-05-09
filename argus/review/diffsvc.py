import asyncio
import logging
import re
import subprocess
from pathlib import Path, PurePosixPath

from argus.review.artifacts import FileChange, Hunk

logger = logging.getLogger(__name__)

LANG_BY_EXT = {".py": "python", ".ts": "typescript", ".tsx": "react",
               ".jsx": "react", ".js": "javascript", ".go": "go", ".java": "java"}
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


def _kind(d: dict) -> str:
    if d.get("new_file"):
        return "added"
    if d.get("deleted_file"):
        return "deleted"
    if d.get("renamed_file"):
        return "renamed"
    return "modified"


def parse_diffs(gitlab_diffs: list[dict]) -> tuple[list[FileChange], dict[str, Hunk]]:
    files: list[FileChange] = []
    hunks: dict[str, Hunk] = {}
    hseq = 0
    for i, d in enumerate(gitlab_diffs, start=1):
        fid = f"f{i}"
        path = d.get("new_path") or d.get("old_path") or ""
        fc = FileChange(
            file_id=fid, path=path,
            old_path=d.get("old_path") if d.get("old_path") != path else None,
            language=LANG_BY_EXT.get(PurePosixPath(path).suffix),
            change_kind=_kind(d))
        text = d.get("diff") or ""
        # GitLab omits the diff body for files it collapses on large MRs
        # (`collapsed`), for files over its size limit (`too_large`), and for
        # binaries. Those arrive with an empty or hunk-less `diff`, which used
        # to yield a FileChange with no hunk_ids and no way for a caller to
        # tell that apart from "this file genuinely has no changes" -- agents
        # then retried get_hunk on it until they exhausted their round budget.
        matches = list(_HUNK_RE.finditer(text))
        fc.diff_available = bool(matches)
        for j, m in enumerate(matches):
            hseq += 1
            hid = f"h{hseq}"
            end = matches[j + 1].start() if j + 1 < len(matches) else len(text)
            hunks[hid] = Hunk(
                hunk_id=hid, file_id=fid,
                old_start=int(m.group(1)), old_lines=int(m.group(2) or 1),
                new_start=int(m.group(3)), new_lines=int(m.group(4) or 1),
                diff_text=text[m.start():end])
            fc.hunk_ids.append(hid)
        files.append(fc)
    return files, hunks


def _git_diff(repo: Path, base_sha: str, head_sha: str, path: str) -> str:
    """Unified diff for ONE path straight from the local clone.

    `git diff base...head` (three dots) matches what GitLab shows for an MR:
    changes on the source branch since it forked, ignoring commits that landed
    on the target afterwards. Scoped to a single path so one enormous file
    cannot blow up the whole backfill."""
    proc = subprocess.run(
        ["git", "diff", "--no-color", f"{base_sha}...{head_sha}", "--", path],
        cwd=repo, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200] or "git diff failed")
    return proc.stdout


async def backfill_collapsed_diffs(
        files: list[FileChange], hunks: dict[str, Hunk], repo: Path,
        base_sha: str, head_sha: str) -> int:
    """Fill in hunks for files GitLab refused to send a diff for, using the
    repo we have already cloned.

    GitLab collapses diff bodies on large MRs, and those are exactly the MRs
    where review matters most. Rather than treating that as permanent, we
    reconstruct the diff locally -- the worktree's bare clone already contains
    both commits, so this needs no extra fetch and no API call, and it is not
    subject to any of GitLab's size or count limits.

    Mutates `files`/`hunks` in place and returns how many files were
    recovered. Best-effort: a file that cannot be reconstructed keeps
    diff_available=False and the agent-facing message telling it to read the
    file directly, so the caller never has to handle a failure here."""
    targets = [f for f in files if not f.diff_available and f.change_kind != "deleted"]
    if not targets or not base_sha or not head_sha:
        return 0
    hseq = max((int(h[1:]) for h in hunks), default=0)
    recovered = 0

    def _sync() -> list[tuple[FileChange, str]]:
        out = []
        for fc in targets:
            try:
                text = _git_diff(repo, base_sha, head_sha, fc.path)
            except Exception as e:  # missing sha, timeout, binary, ...
                logger.warning("could not reconstruct diff for %s: %s", fc.path, e)
                continue
            if text:
                out.append((fc, text))
        return out

    for fc, text in await asyncio.to_thread(_sync):
        matches = list(_HUNK_RE.finditer(text))
        if not matches:
            continue
        for j, m in enumerate(matches):
            hseq += 1
            hid = f"h{hseq}"
            end = matches[j + 1].start() if j + 1 < len(matches) else len(text)
            hunks[hid] = Hunk(
                hunk_id=hid, file_id=fc.file_id,
                old_start=int(m.group(1)), old_lines=int(m.group(2) or 1),
                new_start=int(m.group(3)), new_lines=int(m.group(4) or 1),
                diff_text=text[m.start():end])
            fc.hunk_ids.append(hid)
        fc.diff_available = True
        recovered += 1
    if recovered:
        logger.info("reconstructed diffs for %d/%d collapsed file(s) from git",
                    recovered, len(targets))
    return recovered


def full_diff_text(files: list[FileChange], hunks: dict[str, Hunk]) -> str:
    """Concatenate every hunk's diff text in file order, each preceded by a
    '### {path} [{hunk_id}]' header, for inlining into a single prompt."""
    parts = []
    for f in files:
        for hid in f.hunk_ids:
            h = hunks.get(hid)
            if h is None:
                continue
            parts.append(f"### {f.path} [{hid}]\n{h.diff_text}")
    return "\n".join(parts)

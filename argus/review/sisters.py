"""Opt-in, read-only access to a repo's configured sister repos during review (reference only)."""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import tool
from sqlalchemy import select

from argus.domain.models import RepoSisterLink, Repository
from argus.providers import repository_key
from argus.review import navigation
from argus.review.tools import _truncate_result
from argus.review.workspace import WorkspaceManager

logger = logging.getLogger("argus.sisters")

MAX_SISTERS = 5
MAX_LIST_ENTRIES = 500
MAX_READ_LINES = 400
SISTER_TOOL_NAMES = frozenset({"sister_list_files", "sister_read_file", "sister_search"})


@dataclass
class Sister:
    name: str
    project_path: str
    workspace: Path
    branch: str
    sha: str
    why: str

    def header(self) -> str:
        return (f"[sister repo {self.name} @ {self.branch} {self.sha[:8]} ({self.why}) -- "
                f"reference only, never report findings on this code]\n")


def short_name(project_path: str) -> str:
    return project_path.rsplit("/", 1)[-1]


async def pick_branch(gitlab, sister: Repository, link: RepoSisterLink,
                      source_branch: str | None, target_branch: str | None = None
                      ) -> tuple[str, str, str] | None:
    """(branch, sha, why): MR's source name, then its target name, then configured, then default."""
    tries = []
    if link.match_source_branch and source_branch:
        tries.append((source_branch, "same branch as this MR"))
    if link.match_source_branch and target_branch and target_branch != source_branch:
        tries.append((target_branch, "same target branch as this MR"))
    if link.branch:
        tries.append((link.branch, "configured branch"))
    tries.append((sister.default_branch or "main", "default branch"))
    for branch, why in tries:
        found = await gitlab.get_branch(repository_key(sister), branch)
        if found:
            return branch, found["commit"]["id"], why
    return None


async def acquire_sisters(sf, gitlab, repo_id, source_branch: str | None, workspace_root: Path,
                          clone_url, target_branch: str | None = None
                          ) -> tuple[list[Sister], list[tuple[WorkspaceManager, str]], list]:
    """Check out every enabled sister at its picked branch; failures are recorded, never raised."""
    async with sf() as s:
        rows = (await s.execute(
            select(RepoSisterLink, Repository)
            .join(Repository, Repository.id == RepoSisterLink.sister_repo_id)
            .where(RepoSisterLink.repo_id == repo_id, RepoSisterLink.enabled.is_(True))
            .order_by(Repository.project_path).limit(MAX_SISTERS))).all()
    if not rows:
        return [], [], []

    async def one(link, sister):
        name = short_name(sister.project_path)
        try:
            picked = await pick_branch(gitlab, sister, link, source_branch, target_branch)
            if picked is None:
                return None, None, {"name": name, "error": "no usable branch"}
            branch, sha, why = picked
            wm = WorkspaceManager(workspace_root / str(sister.id), clone_url(sister.project_path))
            path = await wm.acquire(sha)
            return (Sister(name, sister.project_path, path, branch, sha, why), (wm, sha),
                    {"name": name, "branch": branch, "sha": sha, "why": why})
        except Exception as e:
            logger.warning("sister %s unavailable: %s", sister.project_path, e)
            return None, None, {"name": name, "error": str(e)[:200]}

    results = await asyncio.gather(*(one(link, sister) for link, sister in rows))
    sisters = [r[0] for r in results if r[0]]
    held = [r[1] for r in results if r[1]]
    return sisters, held, [r[2] for r in results]


async def release_sisters(held: list[tuple[WorkspaceManager, str]]) -> None:
    for wm, sha in held:
        try:
            await wm.release(sha)
        except Exception:
            logger.warning("could not release sister worktree %s", sha[:8])


def sister_prompt_block(sisters: list[Sister], record: list) -> str:
    """One paragraph every stage sees: which sisters exist and how to use them."""
    if not record:
        return ""
    lines = ["RELATED REPOSITORIES (read-only reference; call sister_list_files / "
             "sister_read_file / sister_search with repo=<name>):"]
    lines += [f"- {s.name} @ {s.branch} ({s.why})" for s in sisters]
    lines += [f"- {r['name']}: unavailable this review" for r in record if r.get("error")]
    lines.append("Use them to check whether something is already handled, defined or intended "
                 "elsewhere (DTOs, interceptors, API contracts, shared types) BEFORE reporting it. "
                 "Never report a finding on sister-repo code; findings must be on this MR's files.")
    return "\n".join(lines)


def build_sister_tools(sisters: list[Sister]) -> list:
    if not sisters:
        return []
    by_name = {s.name: s for s in sisters} | {s.project_path: s for s in sisters}
    names = ", ".join(s.name for s in sisters)

    def _get(repo: str) -> Sister | None:
        return by_name.get(repo.strip())

    def _unknown(repo: str) -> str:
        return f"unknown sister repo {repo!r}; available: {names}"

    @tool
    async def sister_list_files(repo: str, path: str = "", glob: str = "") -> str:
        """List files in a RELATED (sister) repository, read-only. `path` is a directory
        (default: repo root); `glob` filters recursively (e.g. '**/*.dto.ts'). Use to find where
        something lives in the other repo."""
        s = _get(repo)
        if s is None:
            return _unknown(repo)
        base = navigation.resolve_in_workspace(s.workspace, path or ".")
        if base is None or not base.exists():
            return s.header() + f"no such directory {path!r}"
        if glob:
            found = [p for p in base.glob(glob) if p.is_file()]
        else:
            found = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name))
        rows, root = [], s.workspace.resolve()
        for p in found:
            try:
                rel = p.resolve().relative_to(root)
            except ValueError:
                continue  # a '..' glob or a symlink pointing outside the sister checkout
            if any(part in navigation.SKIP_DIRS or part == ".git" for part in rel.parts):
                continue
            rows.append(f"{rel}{'/' if p.is_dir() else ''}")
            if len(rows) >= MAX_LIST_ENTRIES:
                rows.append(f"... (stopped at {MAX_LIST_ENTRIES} entries; narrow path or glob)")
                break
        return s.header() + ("\n".join(rows) or "(empty)")

    @tool
    async def sister_read_file(repo: str, path: str, start: int = 1, end: int = 200) -> str:
        """Read lines start..end (1-based, at most 400 lines) of a file in a RELATED (sister)
        repository, read-only, with line numbers."""
        s = _get(repo)
        if s is None:
            return _unknown(repo)
        target = navigation.resolve_in_workspace(s.workspace, path)
        if target is None:
            return s.header() + f"{path} escapes the repository root"
        if not target.is_file():
            return s.header() + (f"no such file {path} -- find it with "
                                 f"sister_search(repo={s.name!r}, pattern=...)")
        data = target.read_bytes()
        if b"\0" in data[:8192]:
            return s.header() + f"{path} is a binary file"
        rows = data.decode(errors="replace").splitlines()
        start = max(1, start)
        end = min(len(rows), max(start, end), start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i:>6} {rows[i - 1]}" for i in range(start, end + 1))
        return _truncate_result([s.header() + f"{path} (lines {start}-{end} of {len(rows)})\n" + body],
                                "lines")

    @tool
    async def sister_search(repo: str, pattern: str, glob: str = "", context: int = 2,
                            max_matches: int = 50) -> str:
        """Regex-search a RELATED (sister) repository, read-only. Same as search_code but in the
        other repo: use it to check whether a field, DTO, interceptor or contract already exists."""
        import re
        s = _get(repo)
        if s is None:
            return _unknown(repo)
        if not pattern.strip():
            return "empty pattern -- pass a regex"
        try:
            re.compile(pattern)
        except re.error as e:
            return f"invalid regex {pattern!r}: {e}"
        hits = await navigation.search(s.workspace, pattern, glob, max(0, min(context, 10)),
                                       max(1, min(max_matches, 100)))
        if not hits:
            return s.header() + f"no matches for {pattern!r}"
        return _truncate_result([s.header()] + hits, "matching lines")

    return [sister_list_files, sister_read_file, sister_search]

import logging
import re
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Text as SAText
from sqlalchemy import cast, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.config import Settings
from argus.domain.models import Learning
from argus.review import navigation
from argus.review.artifacts import FileChange, Hunk

logger = logging.getLogger("argus.tools")

MAX_LINES = 300
# Per-call ceiling on characters returned to the model. MAX_LINES caps a single
# range, but get_hunk/get_file_lines accept LISTS, so an unbounded call could
# return hundreds of KB -- and every result stays in the message history for
# the rest of the stage. Ten uncapped ~20KB get_file_lines results are what
# walked one scout stage's prompt from 15k to 129k tokens before it overflowed
# the context window. ~24k chars is roughly 6k tokens.
MAX_TOOL_RESULT_CHARS = 24_000

# Substring unique to the truncation notice below. _ToolCallMemory
# (argus.review.stages) checks for this to decide whether a get_hunk/
# get_file_lines call's requested items were actually delivered before
# marking them "fetched" -- see the comment on _truncate_result for why that
# distinction matters. Keep this in sync with the message text below.
TRUNCATION_MARKER = "omitted to stay within the"


def _truncate_result(blocks: list[str], what: str) -> str:
    """Join blocks, stopping before MAX_TOOL_RESULT_CHARS and telling the model
    what it did not get, so it can re-request narrower ranges rather than
    silently reasoning from partial source.

    Note for callers that track what has already been fetched: a truncated
    result means some of the requested items (hunk_ids/file_ids/ranges) got
    NO content here at all, even though they were part of this call's
    arguments. Do not treat "was in the request" as "was delivered" -- see
    TRUNCATION_MARKER and its use in _ToolCallMemory, which existed for this
    exact reason: dedup bookkeeping keyed off requested ids used to mark them
    as fetched immediately, so once a call got truncated, the omitted ids
    could never be re-requested -- every retry was rejected as a
    DUPLICATE CALL pointing at a result that was never actually shown."""
    out: list[str] = []
    used = 0
    for i, b in enumerate(blocks):
        if used + len(b) > MAX_TOOL_RESULT_CHARS:
            dropped = len(blocks) - i
            out.append(
                f"[truncated: {dropped} of {len(blocks)} {what} {TRUNCATION_MARKER} "
                f"the {MAX_TOOL_RESULT_CHARS}-character tool-result "
                f"limit. Re-request the ones you still need in smaller "
                f"batches or narrower ranges.]")
            break
        out.append(b)
        used += len(b) + 1
    return "\n".join(out)


class ToolContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    workspace: Path
    files_by_id: dict[str, FileChange]
    hunks: dict[str, Hunk]


def summary_table(files: list[FileChange]) -> str:
    """The change-list the agent plans from. `diff` states up-front whether a
    file's hunks can actually be fetched, so the agent never spends rounds
    requesting hunks GitLab never sent (see FileChange.diff_available)."""
    rows = ["id | path | kind | lang | diff | risk | summary"]
    for f in files:
        diff_state = "hunks" if f.diff_available else "COLLAPSED-use-get_file_lines"
        rows.append(f"{f.file_id} | {f.path} | {f.change_kind} | "
                    f"{f.language or '-'} | {diff_state} | "
                    f"{','.join(f.risk_flags) or '-'} | {f.summary or '-'}")
    collapsed = [f.file_id for f in files if not f.diff_available]
    if collapsed:
        rows.append(
            f"\nNOTE: {len(collapsed)} file(s) have no fetchable diff "
            f"({', '.join(collapsed[:20])}"
            f"{', ...' if len(collapsed) > 20 else ''}). GitLab collapsed or "
            "omitted their diff body. get_hunk CANNOT return anything for "
            "these -- read them with get_file_lines(path) instead.")
    return "\n".join(rows)


def build_read_tools(ctx: ToolContext) -> list:
    @tool
    async def get_hunk(hunk_ids: list[str] = [], file_ids: list[str] = []) -> str:
        """Return the unified diff text of one or more hunks by id, and/or
        every hunk belonging to one or more file_ids. Pass a single-element
        list to fetch just one hunk, as before, or several ids/file_ids at
        once to reduce round trips."""
        ids: list[str] = list(hunk_ids)
        seen = set(ids)
        blocks = []
        for fid in file_ids:
            fc = ctx.files_by_id.get(fid)
            if fc is None:
                blocks.append(
                    f"unknown file_id {fid} -- call list_changed_files to see "
                    "the valid file ids for this review")
                continue
            if not fc.hunk_ids:
                # The collapsed/binary case. Previously this appended nothing
                # and the call could fall through to a generic "no ids given"
                # message, which reads as "you called me wrong" -- so agents
                # flipped between hunk_ids and file_ids for round after round.
                # Name the cause and the recovery instead.
                blocks.append(
                    f"{fid} ({fc.path}) has NO fetchable hunks -- GitLab "
                    "collapsed or omitted this file's diff. Do not retry "
                    f"get_hunk for it; read it with get_file_lines(path="
                    f"'{fc.path}').")
                continue
            for hid in fc.hunk_ids:
                if hid not in seen:
                    seen.add(hid)
                    ids.append(hid)
        for hid in ids:
            h = ctx.hunks.get(hid)
            if h is None:
                if hid in ctx.files_by_id:
                    # Model passed a file_id where a hunk_id was expected --
                    # resolve it as if it had been given via file_ids instead
                    # of failing outright (this was previously a common way
                    # for an agent to get stuck retrying the same mistake for
                    # many rounds without ever recovering).
                    fc = ctx.files_by_id[hid]
                    if not fc.hunk_ids:
                        # Same collapsed-file dead end as the file_ids branch
                        # above. Without this the lenient fallback silently
                        # yields nothing and the agent keeps guessing.
                        blocks.append(
                            f"{hid} ({fc.path}) is a file_id, and that file "
                            "has NO fetchable hunks -- GitLab collapsed or "
                            "omitted its diff. Do not retry get_hunk for it; "
                            f"read it with get_file_lines(path='{fc.path}').")
                        continue
                    for sub_hid in fc.hunk_ids:
                        sub_h = ctx.hunks.get(sub_hid)
                        if sub_h is not None:
                            blocks.append(
                                f"--- hunk {sub_hid} ({fc.path}) ---\n{sub_h.diff_text}")
                    continue
                blocks.append(
                    f"unknown hunk_id {hid} (if this is a file_id, pass it via "
                    "the file_ids argument instead)")
                continue
            if h.file_id not in ctx.files_by_id:
                blocks.append(f"hunk {hid} is outside the current review scope")
                continue
            fc = ctx.files_by_id[h.file_id]
            blocks.append(f"--- hunk {hid} ({fc.path}) ---\n{h.diff_text}")
        if not blocks:
            # Only reachable when the caller really passed two empty lists.
            # Every "I asked for something and got nothing" path above now
            # returns its own specific, actionable message instead of this.
            return ("no hunk_ids or file_ids given -- pass hunk ids (h12) via "
                    "hunk_ids, or file ids (f3) via file_ids")
        return _truncate_result(blocks, "hunks")

    @tool
    async def get_file_lines(requests: list[dict]) -> str:
        """Return numbered source lines for one or more {path, start, end}
        ranges at the reviewed revision, in one call. Max 300 lines per
        range. Pass a single-element list to fetch just one range, as
        before, or several ranges/files at once to reduce round trips.

        Reads ANY file in the repository, not just the ones this MR changed --
        use it to check a caller, a base class, or a global handler the change
        depends on. Unchanged files are marked [NOT CHANGED BY THIS MR] so you
        can tell context from the change itself."""
        blocks = []
        for req in requests:
            missing = [k for k in ("path", "start", "end")
                       if not isinstance(req, dict) or k not in req]
            if missing:
                blocks.append(f"request {req!r} is missing "
                              f"{', '.join(missing)} -- each range needs "
                              f"{{path, start, end}}")
                continue
            path, start, end = req["path"], req["start"], req["end"]
            try:
                start, end = int(start), int(end)
            except (TypeError, ValueError):
                blocks.append(f"{path}: start/end must be integers, "
                               f"got start={start!r} end={end!r}")
                continue
            target = (ctx.workspace / path).resolve()
            if not target.is_relative_to(ctx.workspace.resolve()):
                blocks.append(f"{path}: path escapes workspace")
                continue
            if not target.exists():
                blocks.append(f"no such file {path}")
                continue
            if target.is_dir():
                blocks.append(f"{path} is a directory, not a file -- pass "
                              "a path to one of the files inside it")
                continue
            # Deliberately NOT restricted to changed files. Reviewing a change
            # means reading the code around it: the caller a signature change
            # breaks, the base class it overrides, the global handler it
            # relies on. A verifier once traced a finding correctly all the
            # way to acme/common/api/main.py and was refused with "outside
            # the current review scope", so it could not confirm or reject the
            # claim and had to file a context_insufficient complaint instead.
            # The restriction also protected nothing once outline_file and
            # search_code shipped -- both read the whole worktree -- it only
            # made the cheapest reader the most restricted one. Containment to
            # the workspace is enforced above and is the real boundary.
            in_scope = (not ctx.files_by_id
                        or path in {f.path for f in ctx.files_by_id.values()})
            try:
                lines = target.read_text(errors="replace").splitlines()
            except OSError as e:
                blocks.append(f"could not read {path}: {e}")
                continue
            s = max(1, start)
            e = min(len(lines), end, s + MAX_LINES - 1)
            body = "\n".join(f"{i} | {lines[i - 1]}" for i in range(s, e + 1))
            # Mark unchanged files: this code is context, not something the MR
            # introduced, and a finding against it is pre-existing rather than
            # a regression -- a distinction the review methodology turns on.
            marker = "" if in_scope else "  [NOT CHANGED BY THIS MR]"
            blocks.append(f"--- {path}:{s}-{e}{marker} ---\n{body}")
        return _truncate_result(blocks, "ranges")

    @tool
    async def list_changed_files() -> str:
        """Return the file-change summary table with stable file ids."""
        return summary_table(list(ctx.files_by_id.values()))

    @tool
    async def outline_file(path: str) -> str:
        """Cheap map of ONE file: every class/function/constant with its exact
        line number, without the bodies. Call this BEFORE get_file_lines on
        any file you have not read -- it costs a few hundred tokens instead of
        several thousand and tells you which line ranges are worth fetching.
        Works on any file in the repository, including files this MR does not
        change (use it to inspect a caller or a base class). If a file has no
        symbols it says so; do not re-request it."""
        target = navigation.resolve_in_workspace(ctx.workspace, path)
        if target is None:
            return (f"{path} escapes the repository root -- pass a path "
                    "relative to the repo, e.g. 'src/app/main.py'")
        if not target.exists():
            return (f"no such file {path} -- check the path with "
                    "list_changed_files, or find it with "
                    f"search_code(pattern='{Path(path).name}')")
        if target.is_dir():
            return (f"{path} is a directory, not a file -- outline_file takes "
                    "one file path")
        try:
            source = target.read_text(errors="replace")
        except OSError as e:
            return f"could not read {path}: {e}"
        return navigation.outline(source, path)

    @tool
    async def search_code(pattern: str, glob: str = "", context: int = 2,
                          max_matches: int = 50) -> str:
        """Regex-search the WHOLE repository (not just changed files) and
        return matching lines with their file, line number and surrounding
        context. Use it to answer "where is this defined", "who else calls
        this", "is this pattern used anywhere else" -- questions you would
        otherwise answer by reading files one range at a time. `glob` narrows
        by filename (e.g. '*.py', 'src/**/*.ts'). Prefer a specific pattern:
        a broad one wastes your round budget on noise."""
        if not pattern.strip():
            return "empty pattern -- pass a regex, e.g. 'def handle_login'"
        try:
            re.compile(pattern)
        except re.error as e:
            return (f"invalid regex {pattern!r}: {e}. Escape regex "
                    r"metacharacters (. * + ? [ ] ( ) | \\) to match them "
                    "literally.")
        context = max(0, min(context, 10))
        max_matches = max(1, min(max_matches, 200))
        hits = await navigation.search(ctx.workspace, pattern, glob,
                                       context, max_matches)
        if not hits:
            scope = f" in files matching {glob!r}" if glob else ""
            return (f"no matches for {pattern!r}{scope}. The symbol may be "
                    "spelled differently, defined in a dependency rather than "
                    "this repo, or generated at runtime. Do not repeat this "
                    "search unchanged.")
        return _truncate_result(hits, "matching lines")

    return [get_hunk, get_file_lines, list_changed_files, outline_file,
            search_code]


def discover_module_skills(workspace: Path) -> list[dict]:
    """Glob workspace/**/.claude/skills/*/SKILL.md, parse YAML frontmatter,
    and return [{"name", "description", "skill_dir"}, ...] for each one
    found. Returns [] if the repo has no .claude/skills directory."""
    import yaml

    results = []
    for skill_md in sorted(workspace.glob("**/.claude/skills/*/SKILL.md")):
        text = skill_md.read_text(errors="replace")
        if not text.startswith("---"):
            continue
        end = text.find("---", 3)
        if end == -1:
            continue
        try:
            front = yaml.safe_load(text[3:end]) or {}
        except yaml.YAMLError:
            continue
        name = front.get("name") or skill_md.parent.name
        description = (front.get("description") or "").strip()
        results.append({"name": name, "description": description,
                        "skill_dir": skill_md.parent})
    return results


def build_skill_tools(workspace: Path, skills: list[dict]) -> list:
    """Only call when discover_module_skills() found at least one skill.
    Returns [list_module_skills, read_module_skill]."""
    by_name = {s["name"]: s for s in skills}

    @tool
    async def list_module_skills() -> str:
        """List the module-specific skill docs available for this repo, as
        'name: description' lines, so you can decide which module's workflow
        docs are relevant to the current change."""
        return "\n".join(f"{s['name']}: {s['description']}" for s in skills)

    @tool
    async def read_module_skill(name: str, doc_path: str = "") -> str:
        """Read a module skill's SKILL.md (call with just name), or a doc it
        references (call with name and doc_path, e.g.
        'docs/domain-concepts.md') to understand that module's lifecycle,
        invariants, and workflows before reviewing changes to it."""
        s = by_name.get(name)
        if s is None:
            return f"unknown skill {name}"
        skill_dir: Path = s["skill_dir"]
        if not doc_path:
            return (skill_dir / "SKILL.md").read_text(errors="replace")
        target = (skill_dir / doc_path).resolve()
        if not target.is_relative_to(skill_dir.resolve()):
            return f"{doc_path}: path escapes skill directory"
        if not target.exists():
            return f"no such doc {doc_path} in skill {name}"
        return target.read_text(errors="replace")

    return [list_module_skills, read_module_skill]


def build_learning_tool(sf: async_sessionmaker):
    @tool
    async def get_learning(learning_id: str) -> str:
        """Fetch the full text of a team learning by its short id from the index."""
        async with sf() as s:
            row = (await s.execute(select(Learning).where(
                cast(Learning.id, SAText).like(f"{learning_id}%")
            ).limit(1))).scalar_one_or_none()
        if row is None:
            return f"no learning with id {learning_id}"
        return f"{row.topic}\n\n{row.hint_text}"

    return get_learning


def build_learnings_search_tool(sf: async_sessionmaker, settings: Settings, repo_id):
    @tool
    async def search_learnings_tool(query: str, file_paths: list[str] | None = None) -> str:
        """Search existing team learnings before proposing a new one. Returns
        up to 8 candidates ranked by relevance, each with its short id, topic,
        hint text, kind, associated file paths, and hit rate. Call this FIRST
        for every distinct point you're considering distilling — if a close
        match already exists, prefer updating it (via upsert_learning_tool
        with action="update" and that learning's id) over creating a
        duplicate."""
        from argus.knowledge.learnings import relevant_learnings
        async with sf() as s:
            rows = await relevant_learnings(
                s, settings, repo_id=repo_id, query_text=query,
                file_paths=file_paths or [])
        if not rows:
            return "no existing learnings match this query"
        lines = []
        for l in rows:
            total = l.hit_count + l.miss_count
            hit_rate = f"{l.hit_count}/{total}" if total else "no verdicts yet"
            lines.append(
                f"id={str(l.id)[:8]} kind={l.kind} hit_rate={hit_rate}\n"
                f"  topic: {l.topic}\n  hint: {l.hint_text}\n"
                f"  file_paths: {l.file_paths or []}")
        return "\n\n".join(lines)

    return search_learnings_tool


def build_learnings_upsert_tool(sf: async_sessionmaker, settings: Settings, repo_id,
                                mr_id=None, note_lookup: dict | None = None):
    @tool
    async def upsert_learning_tool(action: str, topic: str, hint_text: str,
                                   kind: str = "guidance",
                                   file_paths: list[str] | None = None,
                                   metadata: dict | None = None,
                                   learning_id: str | None = None,
                                   source_note_id: str | None = None) -> str:
        """Create a new learning (action="create") or update an existing one
        in place (action="update", requires learning_id from a prior
        search_learnings_tool call). kind is "guidance" (prefer/do X) or
        "do_not_suggest" (stop suggesting X). Always call search_learnings_tool
        first for this point — only create when nothing close already exists.
        Always set source_note_id to the id of the comment (from the
        "Comment N (id=...)" header) that most directly drove this call."""
        from argus.knowledge.learnings import upsert_learning
        import uuid as _uuid

        parsed_id = None
        if action == "update":
            if not learning_id:
                return "action=\"update\" requires a learning_id from search_learnings_tool"
            try:
                parsed_id = _uuid.UUID(learning_id)
            except ValueError:
                return f"no learning with id {learning_id}"
            async with sf() as s:
                existing = await s.get(Learning, parsed_id)
            if existing is None:
                return f"no learning with id {learning_id}"

        async with sf() as s:
            learned_from_actor_id = None
            if source_note_id and note_lookup is not None:
                src_note = note_lookup.get(source_note_id)
                if src_note is not None:
                    learned_from_actor_id = src_note.author_id
            parsed_source_note_id = None
            if source_note_id:
                try:
                    parsed_source_note_id = _uuid.UUID(source_note_id)
                except ValueError:
                    parsed_source_note_id = None
            result = await upsert_learning(
                s, settings, repo_id=repo_id, topic=topic, hint_text=hint_text,
                kind=kind, file_paths=file_paths, metadata=metadata,
                learning_id=parsed_id, mr_id=mr_id,
                source_note_id=parsed_source_note_id,
                learned_from_actor_id=learned_from_actor_id)
            await s.commit()
        if parsed_id is not None:
            return f"updated learning {str(result.id)[:8]}"
        return f"created learning {str(result.id)[:8]}"

    return upsert_learning_tool


def build_file_knowledge_tool(sf: async_sessionmaker, settings: Settings,
                              repo_id, workspace: Path):
    from argus.knowledge.file_knowledge import get_blob_sha, recall, remember

    @tool
    async def file_knowledge(path: str, new_summary: str = "") -> str:
        """Recall the stored understanding of a file (call with just path), or
        store an updated one-paragraph summary after you have analyzed it
        (call with path and new_summary)."""
        sha = await get_blob_sha(workspace, path)
        async with sf() as s:
            if new_summary:
                await remember(s, settings, repo_id=repo_id, path=path,
                               blob_sha=sha, summary=new_summary)
                await s.commit()
                return "saved"
            row = await recall(s, repo_id, path, sha)
        if row is None:
            return f"no stored knowledge for {path}"
        fresh = "FRESH" if row.blob_sha == sha else "STALE (file changed since)"
        notes = "\n".join(f"- {n['note']}" for n in (row.notes or [])[-5:])
        return f"[{fresh}] {row.summary}\n{notes}"

    return file_knowledge


COMPLAINT_CATEGORIES = (
    "tool_broken",           # a tool ran but its answer is wrong or unusable
    "tool_missing",          # no tool exists for something the task requires
    "context_insufficient",  # not enough was provided to do the job
    "context_wrong",         # what was provided contradicts the actual code
    "prompt_irrelevant",     # instructions do not fit the task at hand
    "prompt_unclear",        # instructions are ambiguous
    "instructions_conflict",  # two instructions cannot both be satisfied
    "task_impossible",       # the work as framed cannot be completed
)


def build_report_problem_tool(sf: async_sessionmaker, review_id, stage_name: str):
    """A channel for an agent to report that OUR tooling, prompt, or context is
    at fault -- as opposed to a finding, which is about the code under review.

    Agents already diagnose these problems accurately; they just had nowhere to
    put them. "graph_impact didn't give me much useful information" (the call
    graph had zero edges) and "the hunk_ids don't seem to be working" (a
    misleading error message) were both correct bug reports, sitting in
    `thinking` blocks where only a human trawling Langfuse would ever see them.
    """
    from argus.domain.models import AgentComplaint

    @tool
    async def report_problem(category: str, detail: str, target: str = "",
                             blocked: bool = False) -> str:
        """Optional. Report a broken/missing tool, prompt or input of OURS --
        not the code under review. Say what you tried, expected, and got.

        category: tool_broken | tool_missing | context_insufficient |
            context_wrong | prompt_irrelevant | prompt_unclear |
            instructions_conflict | task_impossible
        target: the thing at fault, not a description (use `detail` for
            that). Tool categories -> tool name ("get_hunk"). context_* ->
            the finding_id you could not verify ("c2-a1"). Else empty.
        blocked: true only if this is why you got no real result for
            `target`. Worked around it -> false.
        """
        if category not in COMPLAINT_CATEGORIES:
            return (f"unknown category {category!r} -- use one of: "
                    f"{', '.join(COMPLAINT_CATEGORIES)}")
        if not detail.strip():
            return ("detail is required -- say what you tried, what you "
                    "expected, and what you got instead")
        async with sf() as s:
            s.add(AgentComplaint(
                review_id=review_id, stage_name=stage_name,
                category=category, target=target.strip() or None,
                detail=detail.strip()[:4000], blocked=blocked))
            await s.commit()
        # Logged at WARNING so a problem is visible the moment it happens, the
        # same way the zero-edge graph warning surfaced a bug that had been
        # silently degrading every review for the life of the feature.
        logger.warning(
            "agent complaint [%s] stage=%s target=%s blocked=%s: %s",
            category, stage_name, target or "-", blocked,
            detail.strip()[:300])
        return ("recorded -- thank you. Continue with the task; work around "
                "the problem if you can, and say so in your output if it "
                "limited what you could do.")

    return report_problem

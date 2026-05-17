"""Agentic learnings distillation — reads human review commentary on an MR,
searches existing learnings, and decides create/update/skip per lesson.

Distinct from argus.knowledge.distiller (distill_rejection): that path is
a single tool-free structured-output call, fired only on bot-rejection
notes. This path is a genuine tool-calling agent, fired on ANY qualifying
human comment (inline or summary), with code-reading and learnings-search/
write tools — used by both the one-off backfill CLI and the live
reconcile-and-distill route. Bot-authored notes are filtered out on entry
(see the author_type filter below) since only human commentary should ever
be distilled into a learning.
"""
import logging
import uuid
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.config import Settings
from argus.domain.models import DistillationRun, MergeRequest, Note, Repository
from argus.gitlab.client import GitLabClient
from argus.knowledge.graphify import build_graph, build_graphify_tool
from argus.llm.config import LLMConfig
from argus.llm.factory import build_chat_model
from argus.llm.langfuse_run import LangfuseRun
from argus.review import stages
from argus.review.diffsvc import parse_diffs
from argus.review.tools import (ToolContext, build_file_knowledge_tool,
                                    build_learnings_search_tool,
                                    build_learnings_upsert_tool,
                                    build_read_tools, build_skill_tools,
                                    discover_module_skills)
from argus.review.workspace import WorkspaceManager

logger = logging.getLogger("argus.agentic_distiller")


def _round_budget(settings: Settings, override: int | None) -> int:
    """Settings unless a caller names a budget explicitly."""
    return settings.distiller_max_rounds if override is None else override


class DistillationEntry(BaseModel):
    action: Literal["create", "update", "skip"]
    topic: str | None = None
    hint_text: str | None = None
    kind: Literal["guidance", "do_not_suggest", "missed_pattern"] | None = None
    file_paths: list[str] | None = None
    learning_id: str | None = None
    source_note_id: str | None = None
    reason: str


class DistillationResult(BaseModel):
    entries: list[DistillationEntry]


_SYSTEM = """\
You are a code-review meta-learner distilling reusable lessons from human
review commentary on a merged pull request.

Your job is NOT to review the code — that already happened. Your job is to
read the human comments below (inline comments anchored to specific lines,
and general summary-level discussion), use your tools to understand WHY each
comment matters — look at the actual code it refers to, trace call graphs if
useful — and turn genuinely reusable points into learnings.

For EVERY distinct point you're considering:
1. Call search_learnings_tool FIRST. If a close existing learning already
   captures it, call upsert_learning_tool with action="update" and that
   learning's id to sharpen/extend it (e.g. add a newly-relevant file path,
   improve the wording) — do NOT create a duplicate.
2. If the point is new and genuinely generalizable (not a one-off, purely
   conversational, or already-obvious exchange), call upsert_learning_tool
   with action="create".
3. Otherwise, do not call upsert_learning_tool for that point at all.

kind is "guidance" (prefer/do X) when the comment teaches a positive pattern,
or "do_not_suggest" (stop suggesting X) when it corrects something the bot
got wrong.

Use kind="missed_pattern" when the point comes from a human comment in a
discussion where the bot posted no comment at all — the bot's review never
addressed this location or topic, as distinct from do_not_suggest (bot
addressed it and was wrong) or guidance (bot addressed it but could go
further).

Every entry you produce (including skip) MUST set source_note_id to the id
shown in the "Comment N (id=...)" header of whichever comment most directly
drove your decision. (But that shall be a human's comment)

After you finish investigating and calling tools as needed, respond with the
required structured output: one DistillationEntry per distinct point you
considered (action create/update/skip), each with a `reason` explaining your
call — including for entries you skipped. It is correct to return zero
create/update entries (all skip) when nothing here is worth remembering.
"""


def _format_notes(notes: list[Note]) -> str:
    blocks = []
    for i, n in enumerate(notes, 1):
        anchor = f"{n.file_path}:{n.line}" if n.file_path else "(general discussion)"
        author = f" (author_id={n.author_id})" if n.author_id else ""
        blocks.append(
            f"--- Comment {i} (id={n.id}, {n.kind}, {anchor}{author}) ---\n{n.body}")
    return "\n\n".join(blocks)


async def run_agentic_distillation_for_mr(
    sf: async_sessionmaker, settings: Settings, gitlab: GitLabClient,
    repo: Repository, mr: MergeRequest, notes: list[Note],
    llm_cfg: LLMConfig, max_rounds: int | None = None,
    distillation_run_id: uuid.UUID | None = None,
) -> DistillationResult:
    notes = [n for n in notes if n.author_type == "human"]

    # Langfuse, exactly as reviews and audits do it: the trace id is a pure
    # function of the run id, knowable before any work starts and persisted
    # immediately. session_id=mr.id puts this run in the same Langfuse
    # session as that MR's review, so the two show up together.
    langfuse_run = LangfuseRun.start(
        settings, "distillation", distillation_run_id, session_id=str(mr.id),
        tags=[repo.project_path, "distillation", llm_cfg.provider, llm_cfg.model],
        metadata={"distillation_run_id": str(distillation_run_id),
                 "repo": repo.project_path, "mr_iid": mr.mr_iid})
    if langfuse_run.trace_id is not None:
        async with sf() as s:
            row = await s.get(DistillationRun, distillation_run_id)
            if row is not None:
                row.langfuse_trace_id = langfuse_run.trace_id
                await s.commit()

    diffs = await gitlab.list_diffs(repo.gitlab_project_id, mr.mr_iid)
    files, hunks = parse_diffs(diffs)

    workspace: Path | None = None
    graphify_tools: list = []
    skill_tools: list = []
    file_content_tools: list = []
    wm = WorkspaceManager(
        Path(settings.workspace_root).expanduser() / str(repo.id),
        f"{settings.gitlab_url.replace('://', f'://oauth2:{settings.gitlab_token}@')}/{repo.project_path}.git")
    try:
        workspace = await wm.acquire(mr.head_sha, mr.mr_iid)
    except Exception as e:
        logger.warning("workspace clone failed for mr %s (%s) — degrading to "
                       "diff-only tools: %s", mr.id, mr.head_sha, e)
        workspace = None

    tool_ctx = ToolContext(workspace=workspace or Path("."),
                          files_by_id={f.file_id: f for f in files},
                          hunks=hunks)
    read_tools = build_read_tools(tool_ctx)
    if workspace is not None:
        file_content_tools = read_tools  # get_hunk, get_file_lines, list_changed_files
        graph_path = await build_graph(workspace)
        if graph_path is not None:
            graphify_tools = build_graphify_tool(
                graph_path, {f.path for f in files}, hunks=hunks,
                files_by_id={f.file_id: f for f in files})
        skills = discover_module_skills(workspace)
        if skills:
            skill_tools = build_skill_tools(workspace, skills)
    else:
        # degraded: only the tools that don't need real file content on disk
        file_content_tools = [t for t in read_tools
                              if t.name in ("get_hunk", "list_changed_files")]

    note_lookup = {str(n.id): n for n in notes}
    learnings_tools = [
        build_learnings_search_tool(sf, settings, repo.id),
        build_learnings_upsert_tool(sf, settings, repo.id, mr_id=mr.id,
                                    note_lookup=note_lookup)]
    extra_tools = []
    if workspace is not None:
        extra_tools.append(build_file_knowledge_tool(sf, settings, repo.id, workspace))

    tools = file_content_tools + graphify_tools + skill_tools + learnings_tools + extra_tools
    from argus.llm.trace import DBTraceCallback
    callbacks = ([DBTraceCallback(sf, "distill",
                                  distillation_run_id=distillation_run_id)]
                if distillation_run_id is not None else [])
    if langfuse_run.handler is not None:
        llm_cfg = llm_cfg.model_copy(update={"langfuse_handler": langfuse_run.handler})
        callbacks = callbacks + [langfuse_run.handler]
    model = build_chat_model(llm_cfg, callbacks=callbacks)

    user_msg = (f"MR !{mr.mr_iid}: {mr.title}\n\n"
               f"HUMAN COMMENTS TO DISTIL FROM:\n{_format_notes(notes)}")

    try:
        with langfuse_run.span("distillation"):
            result: DistillationResult = await stages.run_stage_agent(
                model, tools, _SYSTEM, user_msg, DistillationResult,
                _round_budget(settings, max_rounds),
                callbacks=callbacks, metadata=langfuse_run.metadata)
    finally:
        # Close the Langfuse span before releasing the worktree (see
        # auditor.py's run_audit_for_repo), so the trace is flushed even if
        # release hangs or fails.
        if workspace is not None:
            try:
                await wm.release(mr.head_sha)
            except Exception:
                pass
    if langfuse_run.trace_id is not None:
        action_counts = Counter(entry.action for entry in result.entries)
        langfuse_run.score("distillation_entries_count", len(result.entries))
        for action, count in action_counts.items():
            langfuse_run.score(f"distillation_{action}_count", count)
    return result

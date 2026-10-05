"""Agentic learnings distillation over review THREADS (see distill_threads).

Distinct from argus.knowledge.distiller (distill_rejection): that path is
a single tool-free structured-output call. This one is a tool-calling agent
that reads each thread (bot comment + human replies, or a human's own comment)
and the code, judges the human's reply, and proposes learnings through
propose_learning. Proposals are validated as they are made but written only
by apply_decisions, and only for threads the agent finished deciding.
"""
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.config import Settings
from argus.domain.models import DistillationRun, MergeRequest, Repository
from argus.gitlab.client import GitLabClient
from argus.knowledge.graphify import build_graph, build_graphify_tool
from argus.knowledge.learnings import relevant_learnings
from argus.llm.config import LLMConfig
from argus.llm.factory import build_chat_model
from argus.llm.langfuse_run import LangfuseRun
from argus.review import stages
from argus.review.diffsvc import parse_diffs
from argus.review.tools import (ToolContext, build_file_knowledge_tool,
                                    build_learning_tool, build_learnings_search_tool,
                                    build_read_tools, build_report_problem_tool,
                                    build_skill_tools, discover_module_skills)
from argus.review.workspace import WorkspaceManager

logger = logging.getLogger("argus.agentic_distiller")


def _round_budget(settings: Settings, override: int | None) -> int:
    """Settings unless a caller names a budget explicitly."""
    return settings.distiller_max_rounds if override is None else override


class ThreadDecision(BaseModel):
    discussion_id: str
    reply_verdict: Literal["accepted", "rejected", "acknowledged", "question",
                           "unclear"] | None = None
    verdict_reason: str | None = None
    reason: str


class DistillationResult(BaseModel):
    decisions: list[ThreadDecision]


@dataclass
class StagedLearning:
    discussion_id: uuid.UUID
    action: str
    learning_id: uuid.UUID | None
    kind_hint: str | None
    topic: str
    hint_text: str
    file_paths: list[str] | None
    source_note_id: uuid.UUID


def _as_uuid(v) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def resolve_kind(thread, verdict: str | None, kind_hint: str | None) -> str:
    """do_not_suggest means "the bot said X and a human rejected it" -- nothing else can mint it."""
    if thread.thread_type == "bot_thread":
        return "do_not_suggest" if verdict == "rejected" else "guidance"
    return kind_hint if kind_hint in ("guidance", "missed_pattern") else "missed_pattern"


def build_propose_learning_tool(sf, settings, repo_id, threads: list, staged: list):
    from argus.domain.models import Learning
    from argus.knowledge.distill_threads import MAX_LEARNINGS_PER_THREAD
    from argus.review.tools import _resolve_learning_id

    by_id = {t.discussion_id: t for t in threads}

    @tool
    async def propose_learning(discussion_id: str, action: str, topic: str, hint_text: str,
                               source_note_id: str, kind: str = "", learning_id: str = "",
                               file_paths: list[str] | None = None) -> str:
        """Propose a reusable learning taught by ONE thread. Checked now, written only
        after you finish. action: "create" or "update" (update needs learning_id from
        search_learnings_tool). source_note_id: the [note <id>] of the HUMAN note that
        taught it. kind (human threads only): "guidance" or "missed_pattern"."""
        t = by_id.get(_as_uuid(discussion_id))
        if t is None:
            return (f"{discussion_id!r} is not a thread in this batch; use one of: "
                    + ", ".join(str(x) for x in by_id))
        humans = {n.id for n in t.human_notes}
        src = _as_uuid(source_note_id)
        if src not in humans:
            return ("source_note_id must be a human note in this thread; valid: "
                    + ", ".join(str(h) for h in humans))
        if not topic.strip() or not hint_text.strip():
            return "topic and hint_text are both required"
        if action not in ("create", "update"):
            return 'action must be "create" or "update"'
        if sum(1 for s in staged if s.discussion_id == t.discussion_id) >= MAX_LEARNINGS_PER_THREAD:
            return f"limit reached: at most {MAX_LEARNINGS_PER_THREAD} learnings per thread"
        target = None
        if action == "update":
            target = await _resolve_learning_id(sf, learning_id)
            if target is None:
                return f"no learning with id {learning_id!r}"
            async with sf() as s:
                if (await s.get(Learning, target)).repo_id != repo_id:
                    return "that learning belongs to another repository; propose create instead"
        hint = ""
        if action == "create":
            async with sf() as s:
                near = await relevant_learnings(s, settings, repo_id=repo_id,
                                                query_text=f"{topic} :: {hint_text}",
                                                file_paths=file_paths or [])
            # a global or foreign learning can't be updated here, so never suggest one
            near = [n for n in near if getattr(n, "repo_id", None) == repo_id]
            if near:
                n = near[0]
                hint = (f" Note: a similar learning exists, id={str(n.id)[:8]} "
                        f"({n.topic}: {n.hint_text[:160]}). If it is the same lesson, "
                        "propose action=\"update\" with that id instead.")
        staged.append(StagedLearning(
            discussion_id=t.discussion_id, action=action, learning_id=target,
            kind_hint=kind or None, topic=topic.strip(), hint_text=hint_text.strip(),
            file_paths=file_paths, source_note_id=src))
        return f"staged ({action}) -- it will be written when this run completes.{hint}"

    return propose_learning


def validate_decisions(result: DistillationResult, threads: list) -> dict:
    """One decision per known thread, the last one winning -- a repeat is the agent correcting itself."""
    by_id = {t.discussion_id: t for t in threads}
    out: dict = {}
    for d in result.decisions:
        t = by_id.get(_as_uuid(d.discussion_id))
        if t is None:
            continue
        verdict = (d.reply_verdict or "unclear") if t.thread_type == "bot_thread" else None
        out[t.discussion_id] = d.model_copy(update={
            "discussion_id": str(t.discussion_id), "reply_verdict": verdict})
    return out


_SYSTEM = """\
You distil reusable code-review lessons from finished review THREADS on a pull
request. You do not review the code; that already happened.

Each thread is either:
- bot_thread: argus posted a comment and a human replied. Read the bot
  comment, the human notes, the "CODE AFTER" record, and the current code, then
  judge WHAT THE HUMAN DID about the comment -- not whether the bot was right:
    accepted     -- the human acted on it: agreed, or changed the code it points at
    rejected     -- the human declined: argued against it and left the code, or
                    changed only a comment to explain why the code stays as it is
    acknowledged -- noted or deferred, neither acting nor declining
    question     -- the human asked something and nothing was settled
    unclear      -- you cannot tell, even after reading the code
  An explicit affirmation ("Updated", "Done", "Fixed", "Added X") is accepted,
  even when the fix differs from the bot's suggestion or the bot's reasoning was
  partly wrong. Mark it rejected only when the code plainly shows the human
  kept what the bot objected to. A bot that was wrong but still prompted a
  change is accepted; its mistake belongs in the lesson, not the verdict.
  "CODE AFTER" is GitLab's own record of the commented line changing and of
  commits pushed after the comment; the current code is what your tools read.
  "reconciler label" is a keyword heuristic's guess. Overrule it when the
  thread says otherwise ("fixed?" is a question; "not needed, the SDK retries"
  is a rejection even without a keyword).
- human_thread: a human reviewer's own comment the bot never made. Its
  "reconciler label" says, by the same keyword reading, whether the MR author
  acted on it; a reviewer point the author adopted is the strongest lesson.

Then decide whether the thread teaches something REUSABLE for future reviews of
this repository. Most threads do not; proposing nothing is the normal answer.
Propose nothing when the lesson is a one-off task, holds only under a narrow
condition unlikely to recur, is specific to this MR's feature, or restates
generic good practice. Ask: would this change a review of a different MR in
this repository? If not, skip it -- the verdict still stands on its own.
When one does:
- call search_learnings_tool first; if a close learning exists, propose
  action="update" with its id instead of a duplicate;
- call propose_learning with the thread's discussion_id and the [note <id>] of
  the human note that taught it. It checks your proposal immediately -- if it
  answers with a problem, fix the call; it writes nothing until you finish;
- make hint_text concrete and checkable (name the API, file pattern, or
  condition), one or two sentences. For a rejected bot comment, the lesson is
  what the bot should STOP suggesting.

Check what a thread refers to in the code before deciding; "the wrapper already
does this" is only a lesson if the wrapper does. If a tool of ours is broken or
misleading, say so with report_problem.

Finish with exactly one decision per thread, using its discussion_id; if you
list a thread twice, the LAST decision counts. A thread without a decision is
retried later and its proposals are discarded.
"""


async def run_thread_distillation(
    sf: async_sessionmaker, settings: Settings, gitlab: GitLabClient,
    repo: Repository, mr: MergeRequest, threads: list, llm_cfg: LLMConfig, *,
    distillation_run_id: uuid.UUID | None,
) -> tuple[dict, list]:
    """One agent over up to MAX_THREADS_PER_RUN threads; returns (validated decisions, staged learnings)."""
    from argus.knowledge.distill_threads import format_threads

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

    staged: list = []
    learnings_tools = [
        build_learnings_search_tool(sf, settings, repo.id),
        build_learning_tool(sf),
        build_propose_learning_tool(sf, settings, repo.id, threads, staged)]
    extra_tools = []
    if workspace is not None:
        extra_tools.append(build_file_knowledge_tool(sf, settings, repo.id, workspace))
    # agent_complaints needs exactly one parent run, so no run id means no channel
    if distillation_run_id is not None:
        extra_tools.append(build_report_problem_tool(
            sf, None, "distill", distillation_run_id=distillation_run_id))

    tools = file_content_tools + graphify_tools + skill_tools + learnings_tools + extra_tools
    from argus.llm.trace import DBTraceCallback
    callbacks = ([DBTraceCallback(sf, "distill",
                                  distillation_run_id=distillation_run_id)]
                if distillation_run_id is not None else [])
    if langfuse_run.handler is not None:
        llm_cfg = llm_cfg.model_copy(update={"langfuse_handler": langfuse_run.handler})
        callbacks = callbacks + [langfuse_run.handler]
    model = build_chat_model(llm_cfg, callbacks=callbacks)

    user_msg = format_threads(threads, mr)

    try:
        with langfuse_run.span("distillation"):
            result: DistillationResult = await stages.run_stage_agent(
                model, tools, _SYSTEM, user_msg, DistillationResult,
                _round_budget(settings, None) + 2 * len(threads),
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
    decisions = validate_decisions(result, threads)
    if langfuse_run.trace_id is not None:
        langfuse_run.score("distillation_threads", len(threads))
        langfuse_run.score("distillation_proposals", len(staged))
        for verdict, count in Counter(d.reply_verdict for d in decisions.values()
                                      if d.reply_verdict).items():
            langfuse_run.score(f"distillation_verdict_{verdict}", count)
    return decisions, staged


async def _apply_verdict_to_bot_notes(session, thread, verdict: str | None) -> None:
    """The reconciler may not run on this MR again (merged MRs rarely re-sync), so land the verdict now."""
    from sqlalchemy import select

    from argus.domain.models import Feedback, Note
    from argus.ingest.reconciler import _NEVER_OVERRIDDEN, _VERDICT_TO_DISPOSITION

    target = _VERDICT_TO_DISPOSITION.get(verdict or "")
    if thread.thread_type != "bot_thread" or target is None:
        return
    for note in (await session.execute(select(Note).where(
            Note.discussion_id == thread.discussion_id, Note.author_type == "bot"))).scalars():
        if note.disposition in _NEVER_OVERRIDDEN or note.disposition == target:
            continue
        note.disposition = target
        session.add(Feedback(note_id=note.id, kind="resolved",
                             payload={"disposition": target, "source": "distill"}))


async def apply_decisions(sf, settings, repo, mr, threads, decisions, staged,
                          distillation_run_id) -> dict:
    """Write staged learnings only for decided threads, then close each thread's ledger row."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from argus.domain.models import DistillThread, Learning
    from argus.knowledge.learnings import upsert_learning
    from argus.knowledge.distillation_history import save_decision

    counts = {"created": 0, "updated": 0, "skipped_learnings": 0,
              "threads_done": 0, "threads_failed": 0}
    now = datetime.now(timezone.utc)
    for t in threads:
        d = decisions.get(t.discussion_id)
        learning_ids: list[str] = []
        errored = False
        written = {"created": 0, "updated": 0}
        authors = {n.id: n.author_id for n in t.human_notes}
        staged_for_thread = [s for s in staged if s.discussion_id == t.discussion_id] if d else []
        async with sf() as s:
            for sl in staged_for_thread:
                kind = resolve_kind(t, d.reply_verdict, sl.kind_hint)
                target = sl.learning_id
                if target is not None:
                    existing = await s.get(Learning, target)
                    if existing is None or existing.kind != kind:
                        target = None
                try:
                    row = await upsert_learning(
                        s, settings, repo_id=repo.id, topic=sl.topic, hint_text=sl.hint_text,
                        kind=kind, file_paths=sl.file_paths, learning_id=target, mr_id=mr.id,
                        source_note_id=sl.source_note_id,
                        learned_from_actor_id=authors.get(sl.source_note_id))
                except ValueError:
                    counts["skipped_learnings"] += 1
                    continue
                except Exception:
                    logger.exception("writing a learning for thread %s failed", t.discussion_id)
                    counts["skipped_learnings"] += 1
                    errored = True
                    break
                learning_ids.append(str(row.id))
                written["updated" if target else "created"] += 1

            if d is None or errored:
                await s.rollback()
                async with sf() as s_fail:
                    row = (await s_fail.execute(select(DistillThread).where(
                        DistillThread.discussion_id == t.discussion_id))).scalar_one_or_none()
                    if row is None:
                        row = DistillThread(mr_id=mr.id, discussion_id=t.discussion_id,
                                            thread_type=t.thread_type, content_hash=t.content_hash,
                                            attempts=0)
                        s_fail.add(row)
                    row.content_hash, row.thread_type = t.content_hash, t.thread_type
                    row.distillation_run_id, row.updated_at = distillation_run_id, now
                    row.attempts = (row.attempts or 0) + 1
                    row.status = "failed"
                    row.reply_verdict = row.verdict_reason = None
                    row.learning_ids = []
                    row.decision_reason = "Learning write failed" if errored else "No thread decision"
                    await save_decision(s_fail, distillation_run_id, mr, t,
                        status="failed", decision_reason=row.decision_reason)
                    await s_fail.commit()
                    counts["threads_failed"] += 1
            else:
                row = (await s.execute(select(DistillThread).where(
                    DistillThread.discussion_id == t.discussion_id))).scalar_one_or_none()
                if row is None:
                    row = DistillThread(mr_id=mr.id, discussion_id=t.discussion_id,
                                        thread_type=t.thread_type, content_hash=t.content_hash,
                                        attempts=0)
                    s.add(row)
                row.content_hash, row.thread_type = t.content_hash, t.thread_type
                row.distillation_run_id, row.updated_at = distillation_run_id, now
                row.attempts = (row.attempts or 0) + 1
                row.status, row.reply_verdict = "done", d.reply_verdict
                row.verdict_reason, row.decision_reason = d.verdict_reason, d.reason
                row.learning_ids = learning_ids
                await save_decision(s, distillation_run_id, mr, t, status="done",
                    reply_verdict=d.reply_verdict, verdict_reason=d.verdict_reason,
                    learning_ids=learning_ids, decision_reason=d.reason)
                await _apply_verdict_to_bot_notes(s, t, d.reply_verdict)
                await s.commit()
                counts["threads_done"] += 1
                for key, value in written.items():
                    counts[key] += value
    return counts

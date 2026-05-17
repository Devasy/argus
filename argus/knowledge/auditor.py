"""Periodic agentic audit of learning groundedness.

Complements the outcome loop rather than duplicating it. Outcome reputation
(Phase 1) measures *revealed preference* — did humans keep the comment this
learning drove? That is ground truth but sparse and slow: a learning only earns
evidence when it is injected AND cited AND dispositioned.

This module measures something the outcome loop structurally cannot see: does
the learning still describe reality in the code it claims to govern? That is
available immediately, for every learning, and catches learnings that were never
true, learnings that rotted after a refactor, unfalsifiable platitudes, and
near-duplicates that split evidence between rows.
"""
import logging
import subprocess
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.config import Settings
from argus.knowledge.audit_apply import (ARCHIVABLE, groundedness_from_verdict,
                                             validate_verdicts)
from argus.knowledge.clustering import cluster_learnings
from argus.llm.langfuse_run import LangfuseRun

logger = logging.getLogger("argus.auditor")

VERDICTS = ("corroborated", "stale", "contradicted", "unfalsifiable",
            "duplicate_of", "conflicts_with", "ungrounded")
ACTIONS = ("none", "archive", "merge", "flag_for_rewrite", "escalate_to_human")


class Citation(BaseModel):
    file: str
    line: int = 0
    quote: str


class LearningVerdict(BaseModel):
    learning_id: str
    verdict: Literal["corroborated", "stale", "contradicted", "unfalsifiable",
                     "duplicate_of", "conflicts_with", "ungrounded"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    citations: list[Citation] = []
    related_learning_id: str | None = None
    proposed_action: Literal["none", "archive", "merge", "flag_for_rewrite",
                             "escalate_to_human"] = "none"
    # Required for flag_for_rewrite and merge -- see _SYSTEM. An action that
    # rewrites a learning is not actionable without the replacement text, and
    # approving it previously did nothing at all.
    suggested_hint_text: str | None = None


class ClusterAudit(BaseModel):
    verdicts: list[LearningVerdict] = []


_SYSTEM = """You audit a team's stored code-review "learnings" against the real
codebase. Your job is to find which learnings are still TRUE and USEFUL, and
which are noise.

For each learning in the cluster:
1. Find what it actually refers to in the code. Use the search and file-reading
   tools. Look at the file_paths/file_pattern it claims, but do not stop there.
2. Decide a verdict:
   - corroborated: the code confirms this learning still applies.
   - stale: it was true once, but the pattern it refers to is gone or refactored.
   - contradicted: the codebase consistently does the OPPOSITE, and deliberately.
   - unfalsifiable: too vague to test against any code ("write clean code").
   - duplicate_of: another learning in this cluster says the same thing. Set
     related_learning_id to the one that should SURVIVE (the clearer, better
     evidenced one).
   - conflicts_with: two learnings in this cluster give contradictory advice.
     Set related_learning_id. Propose escalate_to_human — never pick a winner
     yourself; a genuine conflict is a team disagreement, not a data error.
   - ungrounded: you could not find any code referent. Use this when the
     learning may be about process or deployment rather than code. This is NOT
     a criticism of the learning.

3. EVERY verdict except ungrounded and unfalsifiable MUST include at least one
   citation: a real file path, a line number, and a quote copied EXACTLY from
   that file. Verdicts whose quote cannot be found in the cited file are thrown
   away automatically. Do not paraphrase quotes. Do not cite a file you did not
   read.

4. Propose the action that FOLLOWS FROM your verdict. Nothing is executed on
   your say-so: every action is queued for a human to approve or reject, so
   proposing one is a recommendation, not a destructive act. The default
   mapping is:
   - corroborated  -> none (it is working; leave it alone)
   - stale         -> archive (it describes code that no longer exists)
   - contradicted  -> archive (the codebase deliberately does the opposite)
   - unfalsifiable -> flag_for_rewrite (the idea may be sound but it cannot be
                      tested as written; a human should sharpen or drop it)
   - duplicate_of  -> merge, with related_learning_id set to the SURVIVOR
   - conflicts_with-> escalate_to_human (a team disagreement, never yours to
                      settle)
   - ungrounded    -> none (you found no code referent, which is not evidence
                      the learning is wrong -- it may be about process or
                      deployment)

   Depart from this mapping only when you have a specific reason, and say what
   it is in the rationale. Do NOT answer 'none' merely because you are
   cautious: 'none' on a stale or contradicted learning leaves known-dead
   guidance in every future review, which is its own kind of harm. Express
   uncertainty through the confidence score instead -- a low-confidence
   archive is reviewed by a human and rejected cheaply, whereas 'none' is
   never reviewed at all.

5. If your action REWRITES the learning rather than removing it, you MUST
   supply suggested_hint_text: the replacement wording, written out in full.
   This applies to:
   - flag_for_rewrite: the sharpened, checkable version of the idea.
   - merge: the wording the SURVIVING learning should carry once it absorbs
     the duplicate, if that differs from what it says now.

   A rewrite proposal without replacement text is not actionable -- a human
   approving it would change nothing -- so it will be downgraded to 'none'
   and your analysis wasted. Make the replacement concrete and testable
   against code: name the function, file pattern, API, or condition it
   applies to. Turn "handle errors properly" into something like "wrap
   GitLab API calls in try/except and log the response body on failure".
   Keep it to one or two sentences, in the same voice as the original.
   Do NOT set suggested_hint_text for archive, none, or escalate_to_human:
   those do not rewrite anything.

Prefer saying "I could not verify this" over inventing a justification. A wrong
archive destroys real team knowledge."""


# The action each verdict implies. Applied when the model proposes "none" for
# a verdict that clearly calls for something: the first production audit
# returned proposed_action="none" on all five verdicts -- including an
# ungrounded one -- because the prompt said "be conservative, prefer none" and
# never said when an action WAS warranted. Every verdict being "none" makes
# the entire approve/apply pipeline dead code, since apply_verdict only acts
# on archive and merge.
#
# Only ever UPGRADES an explicit "none"; a model that proposed a real action
# keeps it, and the archive/conflicts_with guards downstream still have the
# final say. Nothing is executed without human approval either way.
_VERDICT_DEFAULT_ACTION = {
    "stale": "archive",
    "contradicted": "archive",
    "unfalsifiable": "flag_for_rewrite",
    "duplicate_of": "merge",
    "conflicts_with": "escalate_to_human",
    # corroborated and ungrounded genuinely imply no action: one is working,
    # the other simply has no code referent to judge it by.
}


def default_action_for(verdict: str, proposed: str) -> str:
    """The action to record, defaulting a bare "none" to what the verdict implies."""
    if proposed and proposed != "none":
        return proposed
    return _VERDICT_DEFAULT_ACTION.get(verdict, "none")


def initial_state_for(action: str) -> str:
    """The state a freshly written verdict starts in.

    A 'none' action asks the human for a decision that has no consequence:
    apply_verdict has no branch for it and returns False, so approving one is
    a no-op click. Landing it terminally keeps the queue to verdicts that
    actually change something -- and loses nothing, because the value of a
    'none' verdict is the groundedness it records on the learning, not its
    state. Every other action is a real decision and stays queued.
    """
    return "applied" if action == "none" else "proposed"


async def resolve_audit_ref(session, repo) -> str:
    """The branch to audit this repo's learnings against.

    NOT repo.default_branch. A learning is evidence about the branch it was
    learned from, and `default_branch` is frequently not that branch: two
    repositories here are set to `master` while every MR targets
    `develop-7.0.0`. Auditing them against master searched code years older
    than the learnings, and produced confident, wrong verdicts -- one
    correctly described `deprecated_features.permit.*` in
    data/rabbitmq/custom.conf, which exists on develop-7.0.0 and not on
    master, and was written off as "a pattern that does not exist in this
    codebase".

    So: the branch most of this repo's active learnings were actually learned
    from (via their source MR's target_branch), falling back to
    default_branch when a repo has no learning provenance at all.
    """
    from sqlalchemy import func, select

    from argus.domain.models import Learning, MergeRequest

    row = (await session.execute(
        select(MergeRequest.target_branch, func.count().label("n"))
        .join(Learning, Learning.mr_id == MergeRequest.id)
        .where(Learning.repo_id == repo.id, Learning.status == "active",
               MergeRequest.target_branch.isnot(None),
               MergeRequest.target_branch != "")
        .group_by(MergeRequest.target_branch)
        .order_by(func.count().desc())
        .limit(1))).first()
    if row is not None and row[0]:
        return row[0]
    return repo.default_branch or "HEAD"


def format_cluster_prompt(cluster) -> str:
    """Render the learnings under audit, with their outcome evidence so far."""
    lines = ["LEARNINGS TO AUDIT (same semantic cluster):", ""]
    for l in cluster:
        total = (l.hit_count + l.harmful_count + l.ignored_count
                 + l.miss_count + l.inconclusive_count)
        evidence = (f"injected {total}x — {l.hit_count} accepted, "
                    f"{l.harmful_count} rejected, {l.ignored_count} unused"
                    if total else "never injected yet")
        lines.append(
            f"--- learning_id={l.id} ---\n"
            f"topic: {l.topic}\n"
            f"hint: {l.hint_text}\n"
            f"claimed file_pattern: {l.file_pattern or '(none)'}\n"
            f"claimed file_paths: {l.file_paths or []}\n"
            f"outcome evidence: {evidence}")
    lines += ["", "Audit each one against the actual codebase. Cite real code."]
    return "\n".join(lines)


async def audit_cluster(model, tools, cluster, workspace: Path,
                        max_rounds: int = 12,
                        callbacks: list | None = None,
                        metadata: dict | None = None) -> ClusterAudit:
    """Run one agent over one cluster and validate what it returns.

    `callbacks` must be forwarded: LangChain fires on_tool_start/on_tool_end
    on the GRAPH invocation, not on the model, so a DBTraceCallback attached
    only to the model records LLMRound rows and no ToolCall rows at all. Every
    audit run in production therefore looked like an agent that read nothing --
    23 LLM rounds, zero recorded tool calls -- which is indistinguishable from
    an auditor judging learnings without ever opening the codebase. The same
    is true of the Langfuse handler -- see run_audit_for_repo for where it
    gets appended to `callbacks` for exactly this reason.
    """
    from argus.review import stages
    result: ClusterAudit = await stages.run_stage_agent(
        model, tools, _SYSTEM, format_cluster_prompt(cluster), ClusterAudit,
        max_rounds, callbacks=callbacks, metadata=metadata)
    raw = [v.model_dump() for v in result.verdicts]
    valid_ids = {str(l.id) for l in cluster}
    # Drop verdicts about learnings that were not in this cluster.
    raw = [v for v in raw if v.get("learning_id") in valid_ids]
    kept, _reasons = validate_verdicts(raw, workspace)
    return ClusterAudit(verdicts=[LearningVerdict(**v) for v in kept])


async def run_audit_for_repo(sf: async_sessionmaker, settings: Settings, gitlab,
                             repo, llm_cfg, *, max_clusters: int = 20,
                             audit_run_id: uuid.UUID | None = None) -> dict:
    """Cluster this repo's learnings and audit each cluster against HEAD.

    Writes AuditVerdict rows in state 'proposed' and updates each audited
    learning's groundedness. Applies nothing — see audit_apply.apply_verdict.
    """
    from argus.domain.models import AuditVerdict as AuditVerdictRow
    from argus.domain.models import Learning
    from argus.knowledge.graphify import build_graph, build_graphify_tool
    from argus.llm.factory import build_chat_model
    from argus.llm.trace import DBTraceCallback
    from argus.review.tools import (ToolContext, build_file_knowledge_tool,
                                        build_read_tools)
    from argus.review.workspace import WorkspaceManager

    async with sf() as s:
        ref = await resolve_audit_ref(s, repo)
    if ref != (repo.default_branch or "HEAD"):
        logger.info("audit: repo %s auditing against %r (learnings came from "
                    "there), not default_branch %r",
                    repo.id, ref, repo.default_branch)
    wm = WorkspaceManager(
        Path(settings.workspace_root).expanduser() / str(repo.id),
        f"{settings.gitlab_url.replace('://', f'://oauth2:{settings.gitlab_token}@')}"
        f"/{repo.project_path}.git")
    workspace = None
    try:
        workspace = await wm.acquire(ref)
    except Exception:
        logger.exception("audit: workspace clone failed for repo %s", repo.id)
        return {"status": "failed", "reason": "workspace"}

    # Record what was actually examined, so a "not found in the codebase"
    # verdict can be checked rather than taken on faith.
    if audit_run_id is not None:
        from argus.domain.models import AuditRun as _AuditRun
        sha = None
        try:
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace,
                                 capture_output=True, text=True,
                                 timeout=30).stdout.strip() or None
        except Exception:
            logger.warning("audit: could not resolve HEAD sha for repo %s", repo.id)
        async with sf() as s:
            row = await s.get(_AuditRun, audit_run_id)
            if row is not None:
                row.audited_ref = ref
                row.commit_sha = sha
                await s.commit()

    stats = {"clusters": 0, "verdicts": 0, "discarded": 0, "actions": Counter()}
    # Holds the Langfuse root span for the whole run, so every cluster's agent
    # nests under one trace instead of emitting orphaned generations. Closed
    # in the finally block alongside the workspace release.
    from contextlib import ExitStack
    audit_span_stack = ExitStack()
    try:
        async with sf() as s:
            # Repo-scoped only. A global learning cannot be judged against one
            # repository -- "not found here" is not evidence it is wrong, and
            # acting on it archives knowledge that belongs to a codebase the
            # auditor never looked at.
            clusters = await cluster_learnings(s, repo_id=repo.id,
                                               include_global=False)
        clusters = clusters[:max_clusters]

        callbacks = ([DBTraceCallback(sf, "audit", audit_run_id=audit_run_id)]
                     if audit_run_id is not None else [])

        # Langfuse, exactly as reviews do it (see review/runner.py): the trace
        # id is a pure function of the run id, so it is knowable before any
        # work starts and is persisted immediately -- a run that dies mid-way
        # is precisely the one whose trace someone needs.
        langfuse_run = LangfuseRun.start(
            settings, "audit", audit_run_id, session_id=str(repo.id),
            tags=["audit", repo.project_path, llm_cfg.provider, llm_cfg.model],
            metadata={"audit_run_id": str(audit_run_id),
                     "repo": repo.project_path})
        trace_id = langfuse_run.trace_id
        langfuse_metadata = langfuse_run.metadata
        if langfuse_run.handler is not None:
            llm_cfg = llm_cfg.model_copy(update={
                "langfuse_handler": langfuse_run.handler})
            # Also ride along in callbacks (not just bound to the model) --
            # same tool-call-visibility gap as DBTraceCallback above, and the
            # same fix: LangChain only fires on_tool_start/on_tool_end for
            # callbacks known to the GRAPH invocation, and dedupes by
            # identity so this does not double-count LLM generations either.
            callbacks = callbacks + [langfuse_run.handler]
            from argus.domain.models import AuditRun as _AR
            async with sf() as s:
                row = await s.get(_AR, audit_run_id)
                if row is not None:
                    row.langfuse_trace_id = trace_id
                    await s.commit()
            audit_span_stack.enter_context(langfuse_run.span("audit"))

        model = build_chat_model(llm_cfg, callbacks=callbacks)

        # Build the (optional) call-graph impact tool once per repo, shared
        # across every cluster's agent — it depends only on the checked-out
        # tree, not on the cluster being audited.
        graph_path = await build_graph(workspace)

        for cluster in clusters:
            tool_ctx = ToolContext(workspace=workspace, files_by_id={}, hunks={})
            tools = build_read_tools(tool_ctx)
            tools.append(build_file_knowledge_tool(sf, settings, repo.id, workspace))
            if graph_path is not None:
                changed_paths = {
                    p for l in cluster for p in (l.file_paths or [])
                    if isinstance(p, str)}
                tools += build_graphify_tool(graph_path, changed_paths)
            try:
                audit = await audit_cluster(
                    model, tools, cluster, workspace, callbacks=callbacks,
                    metadata={**langfuse_metadata,
                             "cluster_learning_ids": [str(l.id) for l in cluster]})
            except Exception:
                logger.exception("audit: cluster failed for repo %s", repo.id)
                continue
            stats["clusters"] += 1

            async with sf() as s:
                for v in audit.verdicts:
                    action = default_action_for(v.verdict, v.proposed_action)
                    # Safety: never let a non-archivable verdict propose an
                    # archive, whatever the model asked for.
                    if action == "archive" and v.verdict not in ARCHIVABLE:
                        action = "flag_for_rewrite"
                    if v.verdict == "conflicts_with":
                        action = "escalate_to_human"
                    suggested = (v.suggested_hint_text or "").strip() or None
                    # A rewrite the model did not actually write is not a
                    # decision a human can make: approving it would change
                    # nothing, which is exactly the no-op approval this field
                    # exists to eliminate. Downgrade to 'none' and say so.
                    if action == "flag_for_rewrite" and not suggested:
                        logger.info(
                            "audit: dropping flag_for_rewrite for learning %s -- "
                            "no suggested_hint_text supplied", v.learning_id)
                        action = "none"
                    if action not in ("flag_for_rewrite", "merge"):
                        suggested = None
                    s.add(AuditVerdictRow(
                        audit_run_id=audit_run_id, learning_id=uuid.UUID(v.learning_id),
                        verdict=v.verdict, confidence=v.confidence,
                        rationale=v.rationale,
                        citations=[c.model_dump() for c in v.citations],
                        related_learning_id=(uuid.UUID(v.related_learning_id)
                                             if v.related_learning_id else None),
                        proposed_action=action, suggested_hint_text=suggested,
                        state=initial_state_for(action)))
                    learning = await s.get(Learning, uuid.UUID(v.learning_id))
                    if learning is not None:
                        learning.groundedness = groundedness_from_verdict(
                            v.verdict, v.confidence)
                        learning.last_audited_at = datetime.now(timezone.utc)
                    stats["verdicts"] += 1
                    stats["actions"][action] += 1
                await s.commit()
    finally:
        # Close the Langfuse span before releasing the worktree, so the trace
        # is flushed even when release fails.
        try:
            audit_span_stack.close()
        except Exception:
            logger.warning("audit: closing langfuse span failed", exc_info=True)
        try:
            await wm.release(ref)
        except Exception:
            pass
        if trace_id is not None:
            langfuse_run.score(
                "audit_outcome", stats["clusters"],
                comment=f"{stats['clusters']} clusters, "
                       f"{stats['verdicts']} verdicts")
            for action, count in stats["actions"].items():
                langfuse_run.score(f"audit_action_{action}", count)
    logger.info("audit repo %s: %s", repo.id, stats)
    return {"status": "done", **stats}

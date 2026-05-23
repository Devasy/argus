"""Loads everything a review needs, then runs the pipeline."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.config import Settings
from argus.domain.models import (Finding, LLMRound, MergeRequest,
                                     MRVersion, ProfileVersion, Repository,
                                     Review)
from argus.gitlab.client import GitLabClient
from argus.knowledge.graphify import build_graph, build_graphify_tool
from argus.knowledge.learnings import (do_not_suggest, record_injections,
                                           relevant_learnings)
from argus.knowledge.memory import do_not_suggest_block, learnings_index_block
from argus.llm.config import LLMConfig
from argus.llm.health import resolve_served_model
from argus.llm.langfuse_run import LangfuseRun
from argus.review.diffsvc import backfill_collapsed_diffs, parse_diffs
from argus.review.incremental import (changed_paths_since,
                                          last_reviewed_version,
                                          restrict_files)
from argus.review.linters import linter_block, run_ruff
from argus.review.pipeline import PipelineDeps, run_review_pipeline
from argus.review.tools import (ToolContext, build_file_knowledge_tool,
                                    build_learning_tool, build_skill_tools,
                                    discover_module_skills)
from argus.review.workspace import WorkspaceManager

logger = logging.getLogger("argus.runner")


def _should_preserve_canceled_status(review: Review) -> bool:
    """True if the review's terminal status must not be overwritten.

    A review that was canceled while running must stay "canceled" even
    though the job's finally-block is about to record an outcome; only a
    review that wasn't canceled should transition to "failed"/"done".
    """
    return review.status == "canceled"


def _clone_url(settings: Settings, project_path: str) -> str:
    base = settings.gitlab_url.replace("://", f"://oauth2:{settings.gitlab_token}@")
    return f"{base}/{project_path}.git"


def _diff_signal(hunks: dict, max_chars: int = 2000) -> str:
    """Added-line content from the diff, bounded. Real code, not a path
    string: title+paths alone was measured to give near-chance retrieval
    separation for learning relevance (AUC 0.564, see the review-quality
    ledger's retrieval item)."""
    added = []
    for h in hunks.values():
        for line in h.diff_text.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            stripped = line[1:].strip()
            if stripped:
                added.append(stripped)
    return " ".join(added)[:max_chars]


def _learnings_query_text(mr_title: str, changed_paths: list[str],
                          hunks: dict, max_path_chars: int = 2000) -> str:
    """Path list bounded the same way _diff_signal bounds diff content: a
    54-renamed-file MR built a 5,390-char query from paths alone -- no diff
    content at all -- and that already exceeded the embedding model's real
    context window (nomic-embed-text's actual limit is far below the
    generous MAX_EMBED_CHARS truncation embed_text applies), crashing the
    whole review before the pipeline ever started."""
    paths = " ".join(changed_paths)[:max_path_chars]
    return f"{mr_title} {paths} {_diff_signal(hunks)}".strip()


async def execute_review_job(sf: async_sessionmaker, settings: Settings,
                             payload: dict) -> None:
    review_id = uuid.UUID(payload["review_id"])
    async with sf() as s:
        review = await s.get(Review, review_id)
        if review.status == "canceled":
            # The cancel endpoint won the race against the worker: it set
            # Review.status = "canceled" before we got here (see the
            # queued-cancel race note in argus/api/app.py's cancel
            # route). Bail out before doing any GitLab/workspace/LLM work
            # rather than clobbering the canceled status back to "running".
            logger.info("review %s already canceled before start; skipping", review_id)
            return
        mr = await s.get(MergeRequest, review.mr_id)
        repo = await s.get(Repository, mr.repo_id)
        review.status = "running"
        review.started_at = datetime.now(timezone.utc)
        llm_cfg = LLMConfig.model_validate(review.llm_config)
        llm_cfg.timeout = settings.review_llm_timeout_s
        if llm_cfg.provider == "ollama":
            llm_cfg.reasoning_budget_tokens = settings.reasoning_budget_tokens
        # llm_cfg.model is the name we ask for and never changes; this is what ran.
        review.served_model = await resolve_served_model(llm_cfg)
        # Persist the trace id NOW, in the same commit that marks the review
        # running -- not at the end. The id is a pure function of review_id,
        # so it is knowable up front, and a review that dies mid-run is
        # exactly the one whose trace you need. Leaving this column NULL (as
        # it was for every review) meant the only way to find a failed
        # review's trace was to match timestamps by hand.
        # session_id=mr.id groups every review of the same MR (incremental
        # re-reviews included, and now that MR's post-merge distillation
        # too) into one Langfuse session.
        langfuse_run = LangfuseRun.start(
            settings, "review", review_id, session_id=str(mr.id),
            user_id=review.trigger,
            tags=[repo.project_path, review.trigger, review.mode,
                 llm_cfg.provider, llm_cfg.model],
            metadata={"review_id": str(review_id), "repo": repo.project_path,
                     "mr_iid": mr.mr_iid, "provider": llm_cfg.provider,
                     "model": llm_cfg.model,
                     "served_model": review.served_model})
        llm_cfg.langfuse_handler = langfuse_run.handler
        review.langfuse_trace_id = langfuse_run.trace_id
        await s.commit()
        profile_static = "You are argus, a precise, low-noise code reviewer."
        tool_allowlist = None
        if review.profile_version_id is not None:
            pv = await s.get(ProfileVersion, review.profile_version_id)
            if pv is not None:
                profile_static = (f"{pv.system_prompt}\n\n"
                                  f"## Reviewer guidelines\n{pv.guidelines or ''}")
                if pv.tool_allowlist:
                    tool_allowlist = pv.tool_allowlist

        from argus.domain.models import (ReviewerAgent,
                                             ReviewerAgentRepoSetting,
                                             ReviewerAgentVersion)
        from argus.review.pipeline import ResolvedAgent
        agents_result = await s.execute(
            select(ReviewerAgent).where(ReviewerAgent.enabled == True))  # noqa: E712
        # Per-repo opt-outs. Absence of a row means the agent runs, so a repo
        # nobody has configured behaves exactly as before; only agents
        # explicitly switched off for THIS repo are withheld from Scout.
        disabled_here = set((await s.execute(
            select(ReviewerAgentRepoSetting.agent_id).where(
                ReviewerAgentRepoSetting.repo_id == repo.id,
                ReviewerAgentRepoSetting.enabled == False))  # noqa: E712
        ).scalars().all())
        reviewer_agent_versions: dict[str, ResolvedAgent] = {}
        for agent in agents_result.scalars():
            if agent.id in disabled_here:
                logger.info("agent %s disabled for repo %s; not offered to scout",
                            agent.name, repo.project_path)
                continue
            if agent.current_version_id is None:
                continue
            version = await s.get(ReviewerAgentVersion, agent.current_version_id)
            if version is None:
                continue
            reviewer_agent_versions[agent.name] = ResolvedAgent(
                name=agent.name, guidelines=version.guidelines,
                description=agent.description, model=version.model,
                max_rounds=version.max_rounds,
                tool_allowlist=version.tool_allowlist, version_id=version.id)
        force_agents = review.force_agents or []
        review_mode = review.mode
        review_publish = review.publish

    verify = settings.gitlab_ca_bundle or settings.gitlab_ssl_verify
    gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token, verify)
    wm = WorkspaceManager(Path(settings.workspace_root).expanduser() / str(repo.id),
                          _clone_url(settings, repo.project_path))
    error: str | None = None
    mr_payload: dict | None = None
    from contextlib import ExitStack
    review_span_stack = ExitStack()
    # Same id already stored on the Review row above, so the DB column and
    # the actual trace can never disagree.
    review_span_stack.enter_context(langfuse_run.span("review"))
    try:
        mr_payload = await gitlab.get_merge_request(repo.gitlab_project_id, mr.mr_iid)
        diffs = await gitlab.list_diffs(repo.gitlab_project_id, mr.mr_iid)
        files, hunks = parse_diffs(diffs)
        workspace = await wm.acquire(mr_payload["sha"], mr.mr_iid)
        # GitLab collapses diff bodies on large MRs, leaving those files with
        # no hunks at all -- and those are precisely the MRs most worth
        # reviewing. The worktree we just acquired already contains both
        # commits, so rebuild the missing diffs locally rather than accepting
        # GitLab's limits. Best-effort by design: anything still missing keeps
        # diff_available=False and the tools tell the agent to read the file
        # directly instead of retrying hunks that cannot exist.
        _refs = mr_payload.get("diff_refs") or {}
        await backfill_collapsed_diffs(
            files, hunks, workspace,
            _refs.get("base_sha") or "", _refs.get("head_sha") or mr_payload["sha"])
        changed_paths = [f.path for f in files]
        graph_path = await build_graph(workspace)

        linter_items = await run_ruff(
            workspace, [f.path for f in files if f.language == "python"])

        mr_context = (f"MR !{mr.mr_iid}: {mr_payload['title']}\n"
                      f"{mr_payload.get('description') or ''}\n"
                      f"branch {mr_payload['source_branch']} -> "
                      f"{mr_payload['target_branch']}")
        async with sf() as s:
            query_text = _learnings_query_text(mr_payload["title"], changed_paths,
                                               hunks)
            # Hybrid retrieval needs scout's file summaries for its lexical
            # arm, so it runs inside the graph instead (pipeline's scout node).
            # Deferring means scout's own prompt carries no learnings, which is
            # fine: it describes the diff, it does not judge it.
            relevant = [] if settings.retrieval_hybrid else await relevant_learnings(
                s, settings, repo_id=repo.id, query_text=query_text,
                file_paths=changed_paths,
                exclude_kinds=("do_not_suggest",))
            dns = await do_not_suggest(s, repo_id=repo.id)
            # Positive guidance goes to every stage (it helps FIND problems).
            # The two suppression blocks -- linter output and do-not-suggest --
            # are pure "do not report this" constraints that only bear on the
            # decision to keep or drop a candidate finding, so they go to
            # verify alone rather than being paid for in scout, every analyze
            # chunk and every reviewer agent. Keeping mr_context shorter and
            # identical across stages also lengthens the shared, cacheable
            # prompt prefix (see llm.prompts byte-order contract).
            if relevant:
                mr_context = mr_context + "\n\n" + learnings_index_block(relevant)
            verify_context = "\n\n".join(
                b for b in (linter_block(linter_items),
                            do_not_suggest_block(dns)) if b)
            await record_injections(s, review_id, relevant + dns)

            review = await s.get(Review, review_id)

            # Incremental re-review: if this MR was reviewed before at an
            # older head sha, restrict the file set to what changed since.
            prior_version = await last_reviewed_version(s, mr.id)
            if prior_version is not None and prior_version.head_commit_sha != mr_payload["sha"]:
                changed = await changed_paths_since(
                    gitlab, repo.gitlab_project_id, mr.mr_iid,
                    prior_version.head_commit_sha, mr_payload["sha"])
                if changed is not None:
                    files = restrict_files(files, changed)
                    review.incremental_files = [f.path for f in files]
                    mr_context = ("INCREMENTAL RE-REVIEW: only files changed "
                                 "since the last review are in scope.\n\n" +
                                 mr_context)

            # Attach this review to the MRVersion row for the current head
            # sha, if the normalizer sync has already created one. It may
            # not exist yet (poller runs on its own cadence), in which case
            # we simply leave mr_version_id unset rather than fabricate one.
            #
            # (mr_id, head_commit_sha) is NOT unique -- only (mr_id,
            # provider_version_id) is (see MRVersion.__table_args__). GitLab
            # creates a new diff-version row with a fresh provider_version_id
            # even when the head commit hasn't changed (e.g. a rebase that
            # replays to the same sha, or a target-branch recompute), so two
            # rows can share a head_commit_sha weeks apart -- confirmed on
            # MR !30 here. scalar_one_or_none() assumed uniqueness that was
            # never actually enforced and crashed the whole review the
            # moment a real MR hit it. Take the newest match instead.
            current_version = (await s.execute(
                select(MRVersion).where(
                    MRVersion.mr_id == mr.id,
                    MRVersion.head_commit_sha == mr_payload["sha"])
                .order_by(MRVersion.version_created_at.desc().nullslast(),
                         MRVersion.provider_version_id.desc())
                .limit(1))
            ).scalar_one_or_none()
            if current_version is not None:
                review.mr_version_id = current_version.id

            await s.commit()

        skills = discover_module_skills(workspace)
        skill_tools = build_skill_tools(workspace, skills) if skills else []
        graphify_tools = (build_graphify_tool(graph_path, {f.path for f in files},
                                              hunks=hunks,
                                              files_by_id={f.file_id: f for f in files})
                          if graph_path is not None else [])

        deps = PipelineDeps(
            sf=sf, settings=settings, llm_cfg=llm_cfg,
            tool_ctx=ToolContext(workspace=workspace,
                                 files_by_id={f.file_id: f for f in files},
                                 hunks=hunks),
            review_mode=review_mode,
            publish=review_publish,
            profile_static=profile_static,
            tool_allowlist=tool_allowlist,
            reviewer_agent_versions=reviewer_agent_versions,
            force_agents=force_agents,
            learnings_repo_id=repo.id if settings.retrieval_hybrid else None,
            learnings_query=query_text,
            learnings_paths=changed_paths,
            mr_context=mr_context,
            verify_context=verify_context,
            gitlab=gitlab, project_id=repo.gitlab_project_id, mr_iid=mr.mr_iid,
            diff_refs=mr_payload.get("diff_refs") or {}, mr_db_id=mr.id,
            extra_tools=[build_learning_tool(sf),
                        build_file_knowledge_tool(sf, settings, repo.id, workspace)],
            graphify_tools=graphify_tools,
            skill_tools=skill_tools,
            langfuse_metadata=langfuse_run.metadata)

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        pg_url = settings.database_url.replace("+asyncpg", "")
        async with AsyncPostgresSaver.from_conn_string(pg_url) as checkpointer:
            await checkpointer.setup()
            await run_review_pipeline(review_id, deps, checkpointer)

        # Injection outcomes are NOT resolved here: note dispositions are set
        # asynchronously by the reconciler long after this job ends. Resolution
        # happens in argus/knowledge/outcomes.py, driven by the poller.
    except asyncio.CancelledError:
        logger.warning("review %s interrupted (task canceled, e.g. process shutdown)",
                       review_id)
        error = "review interrupted: worker process was shutting down or restarted"
        raise
    except Exception as e:
        from argus.review.pipeline import ReviewCanceled
        if isinstance(e, ReviewCanceled):
            logger.info("review %s canceled", review_id)
        else:
            logger.exception("review %s failed", review_id)
            error = str(e)[:2000]
        raise
    finally:
        review_span_stack.close()
        try:
            await wm.release((mr_payload or {}).get("sha", ""))
        except Exception:
            pass
        await gitlab.aclose()
        findings: list = []
        async with sf() as s:
            review = await s.get(Review, review_id)
            totals = (await s.execute(
                select(func.coalesce(func.sum(LLMRound.prompt_tokens), 0),
                      func.coalesce(func.sum(LLMRound.completion_tokens), 0))
                .where(LLMRound.review_id == review_id))).one()
            review.prompt_tokens, review.completion_tokens = totals
            if not _should_preserve_canceled_status(review):
                review.status = "failed" if error else "done"
                review.error = error
            review.finished_at = datetime.now(timezone.utc)
            await s.commit()
            if langfuse_run.trace_id is not None:
                try:
                    findings = (await s.execute(
                        select(Finding.published_note_id, Finding.verdict_valid)
                        .where(Finding.review_id == review_id))).all()
                except Exception:
                    logger.warning("failed to load findings for review %s "
                                   "langfuse scoring", review_id, exc_info=True)
        if langfuse_run.trace_id is not None:
            # Out-of-band scores, not tied to the (already-closed) span
            # context -- create_score takes the trace_id directly, so these
            # work whether the review succeeded, failed, or was canceled.
            # review_outcome is the one signal that turns a pile of traces
            # into something queryable: "show me every failed review trace"
            # / "what's our failure rate this week" without cross-referencing
            # the reviews table by hand.
            langfuse_run.score("review_outcome", review.status, "CATEGORICAL",
                              comment=error[:500] if error else None)
            langfuse_run.score("review_finding_count", len(findings))
            langfuse_run.score(
                "review_published_count",
                sum(1 for published_id, _ in findings if published_id is not None))
            verified = [v for _, v in findings if v is not None]
            if verified:
                langfuse_run.score("review_verify_pass_rate",
                                   sum(verified) / len(verified))

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncEngine

from argus.api.auth import require_token
from argus.api.schemas import (AuditVerdictOut, AvailableToolOut, DashboardStatsOut,
                                   DistillationRunOut,
                                   DistillationRunSummaryOut,
                                   FileKnowledgeOut, LearningOut, MergeRequestDetail,
                                   MergeRequestOut, MergeRequestStateCounts, NoteOut,
                                   PaginatedAuditVerdicts,
                                   PaginatedFileKnowledge,
                                   AgentComplaintOut, ComplaintTargetCount,
                                   PaginatedComplaints,
                                   PaginatedLearnings,
                                   PaginatedMergeRequests,
                                   PaginatedRepositories,
                                   PaginatedReviewerAgents,
                                   PaginatedReviewerAgentVersions,
                                   PaginatedReviews,
                                   ProfileVersionIn, ProfileVersionOut,
                                   RepositoryCreate, RepositoryOut, RepositoryUpdate,
                                   ReviewerAgentCreate, ReviewerAgentDescriptionUpdate,
                                   ReviewerAgentEnabledUpdate,
                                   ReviewerAgentOut, ReviewerAgentVersionHistoryOut,
                                   ReviewerAgentVersionIn, ReviewerAgentVersionOut,
                                   ReviewerProfileOut, ReviewerProxyCreate,
                                   ReviewerProxyOut, ReviewerProxyUpdate,
                                   RepoAgentSettingOut, RepoAgentSettingUpdate,
                                   ReviewListItemOut, ReviewQueueSummaryOut,
                                   ReviewSummaryOut, SettingsUpdate)
from argus.api.stats import compute_dashboard_stats
from argus.config import Settings, get_settings
from argus.db import get_engine, session_factory
from argus.logging_setup import configure_logging

logger = logging.getLogger("argus.api")
from argus.settings_store import (apply_settings, load_effective_settings,
                                      read_settings_view)
from argus.domain.models import (Actor, AuditRun, AuditVerdict, DistillationRun,
                                     Job, Learning, LLMRound,
                                     MergeRequest,
                                     MRVersion, Note, ProfileVersion, Repository, Review,
                                     ReviewerAgent, ReviewerAgentVersion,
                                     ReviewerProfile, ReviewerProxy,
                                     ReviewReviewerAgentVersion,
                                     ReviewStage, ToolCall)
from argus.gitlab.client import GitLabClient
from argus.ingest.poller import run_poller_forever
from argus.jobs.queue import enqueue, run_worker_forever
from argus.knowledge.acceptance import ACCEPTED, REJECTED, compute_agent_version_acceptance
from argus.knowledge.audit_scheduler import run_audit_forever
from argus.ingest.reconciler import reconcile_mr
from argus.knowledge.file_knowledge import list_file_knowledge
from argus.review.tools import COMPLAINT_CATEGORIES
from argus.knowledge.learnings import (learning_reputation, list_learnings,
                                           search_learnings)
from argus.llm.config import resolve_llm_config
from argus.review.listing import (VALID_REVIEW_STATUSES,
                                      average_done_duration_seconds,
                                      list_reviews)
from argus.review.candidates import review_candidates
from argus.review.runner import execute_review_job


class ReviewTrigger(BaseModel):
    reviewer: str | None = None
    llm_endpoint_id: uuid.UUID | None = None
    profile_id: uuid.UUID | None = None
    force_agents: list[str] | None = None
    mode: Literal["full", "qa_scenarios"] = "full"
    # False runs the pipeline and records findings without posting to GitLab,
    # for re-reviewing a fixed set of MRs as a golden set.
    publish: bool = True


def _stage_counts(artifact) -> tuple[int | None, int | None]:
    """(produced, allowed) for the pipeline graph: how many candidates a stage
    emitted, and for verify how many it let through.

    Computed independently rather than "first list key wins": no real stage
    artifact carries both `findings` and `verdicts` today, but that was luck,
    not a guarantee, and the earlier first-match version stayed fragile even
    after being reordered to check verdicts first."""
    if not isinstance(artifact, dict):
        return None, None
    verdicts = artifact.get("verdicts")
    allowed = (sum(1 for v in verdicts if isinstance(v, dict) and v.get("valid"))
              if isinstance(verdicts, list) else None)
    for key in ("findings", "verdicts", "published_finding_ids"):
        items = artifact.get(key)
        if isinstance(items, list):
            return len(items), allowed
    return None, allowed


def _produced_by_chunk(artifact) -> dict[str, int]:
    """Analyze writes one aggregate stage row but tags each finding with its
    chunk, so the per-chunk nodes have to be counted from the findings."""
    if not isinstance(artifact, dict):
        return {}
    counts: dict[str, int] = {}
    for f in artifact.get("findings") or []:
        if isinstance(f, dict) and f.get("chunk_id"):
            counts[f["chunk_id"]] = counts.get(f["chunk_id"], 0) + 1
    return counts


def _stage_dict(r: ReviewStage) -> dict:
    produced, allowed = _stage_counts(r.artifact)
    return {"name": r.stage_name, "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "produced": produced, "allowed": allowed,
            "produced_by_chunk": _produced_by_chunk(r.artifact)}




async def _run_distill_mr_job(sf, settings, payload: dict) -> None:
    from argus.domain.models import (Actor, DistillationRun, MergeRequest,
                                         Note, Repository)
    from argus.gitlab.client import GitLabClient
    from argus.knowledge.agentic_distiller import run_agentic_distillation_for_mr
    from argus.llm.config import resolve_llm_config

    mr_id = uuid.UUID(payload["mr_id"])
    note_ids = [uuid.UUID(n) for n in payload["note_ids"]]

    async with sf() as session:
        mr = await session.get(MergeRequest, mr_id)
        repo = await session.get(Repository, mr.repo_id)
        notes = (await session.execute(
            select(Note).where(Note.id.in_(note_ids),
                               Note.author_type == "human"))).scalars().all()
        llm_cfg = await resolve_llm_config(session, None, None)
        run = DistillationRun(mr_id=mr_id, note_ids=[str(n) for n in note_ids],
                              status="running",
                              started_at=datetime.now(timezone.utc))
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()

    verify = settings.gitlab_ca_bundle or settings.gitlab_ssl_verify
    gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token, verify)
    error = None
    result = None
    try:
        result = await run_agentic_distillation_for_mr(
            sf, settings, gitlab, repo, mr, list(notes), llm_cfg,
            distillation_run_id=run_id)
    except Exception as e:
        error = str(e)[:2000]
    finally:
        await gitlab.aclose()

    async with sf() as session:
        run = await session.get(DistillationRun, run_id)
        run.status = "failed" if error else "done"
        run.error = error
        run.finished_at = datetime.now(timezone.utc)
        totals = (await session.execute(
            select(func.coalesce(func.sum(LLMRound.prompt_tokens), 0),
                  func.coalesce(func.sum(LLMRound.completion_tokens), 0))
            .where(LLMRound.distillation_run_id == run_id))).one()
        run.prompt_tokens, run.completion_tokens = totals
        await session.commit()


def create_app(settings: Settings | None = None,
               engine: AsyncEngine | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    engine = engine or get_engine(settings.database_url)
    sf = session_factory(engine)
    app = FastAPI(title="argus")
    app.state.settings = settings
    app.state.sf = sf
    router = APIRouter(dependencies=[Depends(require_token)])
    stop = asyncio.Event()

    def _client() -> GitLabClient:
        verify = settings.gitlab_ca_bundle or settings.gitlab_ssl_verify
        return GitLabClient(settings.gitlab_url, settings.gitlab_token, verify)

    @app.on_event("startup")
    async def _startup():
        # Decide what to start from the EFFECTIVE settings, not the static
        # ones. audit_enabled/poller_enabled are editable from the UI, which
        # writes to runtime_settings -- so gating on `settings` meant a toggle
        # turned on in the UI never started its loop. Auditing was switched on
        # 2026-07-31 and had produced zero audit runs nine days later, because
        # the DB override was only read INSIDE the `if` it was supposed to
        # satisfy.
        async with sf() as session:
            boot = await load_effective_settings(session, base=settings)
        if getattr(boot, "validate_embeddings_on_boot", False):
            from argus.knowledge.embeddings import validate_embedding_dimension
            await validate_embedding_dimension(boot)
        if getattr(boot, "poller_enabled", False) and boot.gitlab_token:
            async def _interval() -> int:
                async with sf() as session:
                    eff = await load_effective_settings(session, base=settings)
                    return eff.poll_interval_s
            app.state.poller = asyncio.create_task(
                run_poller_forever(sf, _client, stop, boot.poll_interval_s,
                                   get_interval=_interval))
        if getattr(boot, "audit_enabled", False):
            async with sf() as session:
                audit_llm_cfg = await resolve_llm_config(session, None, None)
            app.state.auditor = asyncio.create_task(
                run_audit_forever(sf, boot, _client, audit_llm_cfg,
                                  sleep_seconds=3600))
        if getattr(boot, "worker_enabled", False):
            async def _review(p):
                async with sf() as session:
                    eff = await load_effective_settings(session, base=settings)
                return await execute_review_job(sf, eff, p)
            async def _distill_mr(p):
                async with sf() as session:
                    eff = await load_effective_settings(session, base=settings)
                await _run_distill_mr_job(sf, eff, p)
            handlers = {"review": _review, "distill_mr": _distill_mr}
            app.state.worker = asyncio.create_task(
                run_worker_forever(sf, handlers, stop))

    @app.on_event("shutdown")
    async def _shutdown():
        stop.set()
        # run_audit_forever takes no stop event (unlike the poller/worker
        # loops) — its while-True spends nearly all its time inside
        # asyncio.sleep, so a direct cancel() is caught there cleanly.
        auditor_task = getattr(app.state, "auditor", None)
        if auditor_task is not None:
            auditor_task.cancel()

    @app.get("/health")
    async def health():
        from argus.llm.config import resolve_llm_config
        from argus.llm.health import check_llm_health
        llm_ok = True
        async with sf() as session:
            try:
                cfg = await resolve_llm_config(session, None, None)
                llm_ok = await check_llm_health(cfg)
            except ValueError:
                pass  # no endpoint configured -- not this check's concern
        return {"status": "ok" if llm_ok else "degraded", "llm_ok": llm_ok}

    @app.websocket("/ws/reviews/{review_id}")
    async def review_stream(ws: WebSocket, review_id: uuid.UUID):
        if settings.api_token and ws.query_params.get("token") != settings.api_token:
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            while True:
                async with sf() as session:
                    review = await session.get(Review, review_id)
                    if review is None:
                        break
                    stages_rows = (await session.execute(
                        select(ReviewStage).where(ReviewStage.review_id == review_id)
                        .order_by(ReviewStage.started_at)
                    )).scalars().all()
                    n_tools = (await session.execute(select(func.count(ToolCall.id))
                               .where(ToolCall.review_id == review_id))).scalar()
                    n_rounds = (await session.execute(select(func.count(LLMRound.id))
                                .where(LLMRound.review_id == review_id,
                                       LLMRound.status != "running"))).scalar()
                    active_round = (await session.execute(
                        select(LLMRound)
                        .where(LLMRound.review_id == review_id,
                               LLMRound.status == "running")
                        .order_by(LLMRound.seq.desc()).limit(1)
                    )).scalar_one_or_none()
                active_llm_call = None
                if active_round is not None:
                    started = active_round.started_at
                    elapsed_s = (datetime.now(timezone.utc) - started).total_seconds() \
                        if started else None
                    active_llm_call = {"stage": active_round.stage_name,
                                        "elapsed_s": elapsed_s}
                await ws.send_json({"status": review.status,
                                    "stages": [_stage_dict(r) for r in stages_rows],
                                    "tool_calls": n_tools, "llm_rounds": n_rounds,
                                    "active_llm_call": active_llm_call})
                if review.status in ("done", "failed", "canceled"):
                    break
                await asyncio.sleep(2)
        except WebSocketDisconnect:
            pass
        finally:
            try:
                await ws.close()
            except (WebSocketDisconnect, RuntimeError):
                pass

    @router.post("/repositories", status_code=201, response_model=RepositoryOut)
    async def create_repository(payload: RepositoryCreate):
        client = _client()
        try:
            project = await client.get_project(payload.project_path)
        finally:
            await client.aclose()
        async with sf() as session:
            repo = Repository(project_path=project["path_with_namespace"],
                              gitlab_project_id=project["id"],
                              default_branch=project.get("default_branch"))
            session.add(repo)
            await session.commit()
            await session.refresh(repo)
            return repo

    @router.get("/repositories", response_model=PaginatedRepositories)
    async def list_repositories(page: int = 1, per_page: int = 50):
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            total = (await session.execute(
                select(func.count(Repository.id)))).scalar_one()
            repos = (await session.execute(
                select(Repository).order_by(Repository.project_path)
                .offset((page - 1) * per_page).limit(per_page))).scalars().all()
            counts = dict((await session.execute(
                select(MergeRequest.repo_id, func.count(MergeRequest.id))
                .group_by(MergeRequest.repo_id))).all())
            items = [RepositoryOut.model_validate(r).model_copy(
                update={"mr_count": counts.get(r.id, 0)}) for r in repos]
            return PaginatedRepositories(items=items, total=total, page=page,
                                         per_page=per_page)

    @router.get("/repositories/{repo_id}", response_model=RepositoryOut)
    async def get_repository(repo_id: uuid.UUID):
        async with sf() as session:
            repo = await session.get(Repository, repo_id)
            if repo is None:
                raise HTTPException(404, "repository not found")
            count = (await session.execute(
                select(func.count(MergeRequest.id))
                .where(MergeRequest.repo_id == repo_id))).scalar_one()
            return RepositoryOut.model_validate(repo).model_copy(
                update={"mr_count": count})

    @router.put("/repositories/{repo_id}", response_model=RepositoryOut)
    async def update_repository(repo_id: uuid.UUID, payload: RepositoryUpdate):
        async with sf() as session:
            repo = await session.get(Repository, repo_id)
            if repo is None:
                raise HTTPException(404, "repository not found")
            if payload.poll_interval_s is not None and payload.poll_interval_s < 10:
                raise HTTPException(422, "poll_interval_s must be >= 10")
            if "default_profile_id" in payload.model_fields_set \
                    and payload.default_profile_id is not None:
                if await session.get(ReviewerProfile,
                                     payload.default_profile_id) is None:
                    raise HTTPException(422, "unknown profile")
            for field in payload.model_fields_set:
                setattr(repo, field, getattr(payload, field))
            await session.commit()
            await session.refresh(repo)
            return repo

    @router.get("/repositories/{repo_id}/agents",
                response_model=list[RepoAgentSettingOut])
    async def get_repo_agents(repo_id: uuid.UUID):
        """Every agent, with whether it runs on THIS repo.

        Opt-out model: no setting row means the agent runs, so `enabled_here`
        is True unless someone explicitly turned it off for this repo."""
        from argus.domain.models import (ReviewerAgent,
                                             ReviewerAgentRepoSetting)
        async with sf() as session:
            if await session.get(Repository, repo_id) is None:
                raise HTTPException(404, "repository not found")
            agents = (await session.execute(
                select(ReviewerAgent).order_by(ReviewerAgent.name))).scalars().all()
            overrides = {r.agent_id: r.enabled for r in (await session.execute(
                select(ReviewerAgentRepoSetting).where(
                    ReviewerAgentRepoSetting.repo_id == repo_id))).scalars().all()}
        return [RepoAgentSettingOut(
            agent_id=a.id, name=a.name, description=a.description,
            globally_enabled=a.enabled,
            enabled_here=overrides.get(a.id, True)) for a in agents]

    @router.put("/repositories/{repo_id}/agents/{agent_id}",
                response_model=RepoAgentSettingOut)
    async def set_repo_agent(repo_id: uuid.UUID, agent_id: uuid.UUID,
                             payload: RepoAgentSettingUpdate):
        from argus.domain.models import (ReviewerAgent,
                                             ReviewerAgentRepoSetting)
        async with sf() as session:
            if await session.get(Repository, repo_id) is None:
                raise HTTPException(404, "repository not found")
            agent = await session.get(ReviewerAgent, agent_id)
            if agent is None:
                raise HTTPException(404, "agent not found")
            row = (await session.execute(
                select(ReviewerAgentRepoSetting).where(
                    ReviewerAgentRepoSetting.repo_id == repo_id,
                    ReviewerAgentRepoSetting.agent_id == agent_id))).scalar_one_or_none()
            if row is None:
                row = ReviewerAgentRepoSetting(repo_id=repo_id, agent_id=agent_id,
                                               enabled=payload.enabled)
                session.add(row)
            else:
                row.enabled = payload.enabled
            await session.commit()
            return RepoAgentSettingOut(
                agent_id=agent.id, name=agent.name, description=agent.description,
                globally_enabled=agent.enabled, enabled_here=payload.enabled)

    @router.get("/repositories/{repo_id}/merge-requests",
               response_model=PaginatedMergeRequests)
    async def list_mrs(repo_id: uuid.UUID, state: str | None = None,
                       page: int = 1, per_page: int = 50):
        valid_states = {"opened", "merged", "closed", "locked"}
        if state is not None and state not in valid_states:
            raise HTTPException(422, f"state must be one of {sorted(valid_states)}")
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            filters = [MergeRequest.repo_id == repo_id]
            if state is not None:
                filters.append(MergeRequest.state == state)
            total = (await session.execute(
                select(func.count(MergeRequest.id)).where(*filters))).scalar_one()
            rows = (await session.execute(
                select(MergeRequest, Actor.username)
                .join(Actor, MergeRequest.author_id == Actor.id, isouter=True)
                .where(*filters)
                .order_by(MergeRequest.mr_updated_at.desc())
                .offset((page - 1) * per_page).limit(per_page))).all()
            mr_ids = [m.id for m, _ in rows]
            disposition_totals: dict[uuid.UUID, dict[str, int]] = {}
            if mr_ids:
                disp_rows = (await session.execute(
                    select(Note.mr_id, Note.disposition, func.count(Note.id))
                    .where(Note.mr_id.in_(mr_ids), Note.kind == "inline",
                           Note.author_type == "bot")
                    .group_by(Note.mr_id, Note.disposition))).all()
                for mr_id, disposition, count in disp_rows:
                    disposition_totals.setdefault(mr_id, {})[disposition] = count

            def _bucket_counts(mr_id: uuid.UUID) -> tuple[int, int, int]:
                totals = disposition_totals.get(mr_id, {})
                accepted = sum(totals.get(d, 0) for d in ACCEPTED)
                rejected = sum(totals.get(d, 0) for d in REJECTED)
                leftover = sum(count for d, count in totals.items()
                              if d not in ACCEPTED and d not in REJECTED)
                return accepted, rejected, leftover

            items = []
            for m, u in rows:
                accepted, rejected, leftover = _bucket_counts(m.id)
                items.append(MergeRequestOut(
                    id=m.id, mr_iid=m.mr_iid, title=m.title, state=m.state,
                    author_username=u, web_url=m.web_url,
                    mr_updated_at=m.mr_updated_at,
                    accepted_count=accepted, rejected_count=rejected,
                    leftover_count=leftover))

            state_totals = dict((await session.execute(
                select(MergeRequest.state, func.count(MergeRequest.id))
                .where(MergeRequest.repo_id == repo_id)
                .group_by(MergeRequest.state))).all())
            state_counts = MergeRequestStateCounts(
                all=sum(state_totals.values()),
                opened=state_totals.get("opened", 0),
                merged=state_totals.get("merged", 0),
                closed=state_totals.get("closed", 0) + state_totals.get("locked", 0))

            return PaginatedMergeRequests(items=items, total=total, page=page,
                                          per_page=per_page,
                                          state_counts=state_counts)

    @router.get("/merge-requests/{mr_id}", response_model=MergeRequestDetail)
    async def get_mr(mr_id: uuid.UUID):
        async with sf() as session:
            mr = await session.get(MergeRequest, mr_id)
            if mr is None:
                raise HTTPException(404)
            author = await session.get(Actor, mr.author_id) if mr.author_id else None
            rows = (await session.execute(
                select(Note, Actor.username)
                .join(Actor, Note.author_id == Actor.id, isouter=True)
                .where(Note.mr_id == mr_id)
                .order_by(Note.note_created_at))).all()
            review_rows = (await session.execute(
                select(Review, ProfileVersion.version, MRVersion.head_commit_sha)
                .join(ProfileVersion, Review.profile_version_id == ProfileVersion.id, isouter=True)
                .join(MRVersion, Review.mr_version_id == MRVersion.id, isouter=True)
                .where(Review.mr_id == mr_id)
                .order_by(Review.created_at.desc()))).all()

            # Sequence-number only status='done' reviews, in ascending
            # finished_at order (oldest completed review = 1), independent
            # of the display order below (which stays created_at desc).
            done_ids_in_order = [
                rev.id for rev, _, _ in sorted(
                    (r for r in review_rows if r[0].status == "done"),
                    key=lambda r: r[0].finished_at)
            ]
            sequence_by_id = {rid: i + 1 for i, rid in enumerate(done_ids_in_order)}
            distillation_run_rows = (await session.execute(
                select(DistillationRun).where(DistillationRun.mr_id == mr_id)
                .order_by(DistillationRun.created_at.desc()))).scalars().all()
            return MergeRequestDetail(
                mr=MergeRequestOut(id=mr.id, mr_iid=mr.mr_iid, title=mr.title,
                                   state=mr.state,
                                   author_username=author.username if author else None,
                                   web_url=mr.web_url, mr_updated_at=mr.mr_updated_at),
                notes=[NoteOut(id=n.id, author_username=u, author_type=n.author_type,
                               kind=n.kind, body=n.body, file_path=n.file_path,
                               line=n.line, disposition=n.disposition)
                       for n, u in rows],
                reviews=[ReviewSummaryOut(
                    id=rev.id, status=rev.status, trigger=rev.trigger,
                    profile_version=f"v{v}" if v is not None else None,
                    tokens=rev.prompt_tokens + rev.completion_tokens,
                    started_at=rev.started_at,
                    sequence_number=sequence_by_id.get(rev.id),
                    head_commit_sha=sha if rev.status == "done" else None,
                    incremental_file_count=(
                        len(rev.incremental_files)
                        if rev.incremental_files is not None else None),
                    publish=rev.publish)
                    for rev, v, sha in review_rows],
                distillation_runs=[DistillationRunSummaryOut(
                    id=run.id, status=run.status, trigger=run.trigger,
                    tokens=run.prompt_tokens + run.completion_tokens,
                    started_at=run.started_at)
                    for run in distillation_run_rows])

    @router.post("/reviewer-proxies", status_code=201, response_model=ReviewerProxyOut)
    async def create_reviewer_proxy(payload: ReviewerProxyCreate):
        async with sf() as session:
            existing = (await session.execute(select(ReviewerProxy).where(
                ReviewerProxy.reviewer == payload.reviewer))).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(
                    409, f"a proxy is already registered for reviewer {payload.reviewer!r}")
            proxy = ReviewerProxy(reviewer=payload.reviewer, proxy_url=payload.proxy_url)
            session.add(proxy)
            await session.commit()
            await session.refresh(proxy)
            return proxy

    @router.get("/reviewer-proxies", response_model=list[ReviewerProxyOut])
    async def list_reviewer_proxies():
        async with sf() as session:
            return (await session.execute(select(ReviewerProxy))).scalars().all()

    @router.put("/reviewer-proxies/{proxy_id}", response_model=ReviewerProxyOut)
    async def update_reviewer_proxy(proxy_id: uuid.UUID,
                                    payload: ReviewerProxyUpdate):
        async with sf() as session:
            proxy = await session.get(ReviewerProxy, proxy_id)
            if proxy is None:
                raise HTTPException(404, "reviewer proxy not found")
            for field in payload.model_fields_set:
                setattr(proxy, field, getattr(payload, field))
            await session.commit()
            await session.refresh(proxy)
            return proxy

    async def _profile_out(session, profile: ReviewerProfile) -> ReviewerProfileOut:
        current = None
        if profile.current_version_id is not None:
            pv = await session.get(ProfileVersion, profile.current_version_id)
            if pv is not None:
                current = ProfileVersionOut.model_validate(pv)
        return ReviewerProfileOut(id=profile.id, name=profile.name,
                                  description=profile.description,
                                  is_builtin=profile.is_builtin,
                                  current_version=current)

    @router.post("/profiles", status_code=201, response_model=ReviewerProfileOut)
    async def create_profile(payload: ProfileVersionIn):
        async with sf() as session:
            existing = (await session.execute(select(ReviewerProfile).where(
                ReviewerProfile.name == payload.name))).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(
                    409, f"a profile named {payload.name!r} already exists")
            profile = ReviewerProfile(name=payload.name)
            session.add(profile)
            await session.flush()
            pv = ProfileVersion(profile_id=profile.id, version=1,
                                system_prompt=payload.system_prompt,
                                guidelines=payload.guidelines,
                                tool_allowlist=payload.tool_allowlist,
                                model=payload.model)
            session.add(pv)
            await session.flush()
            profile.current_version_id = pv.id
            await session.commit()
            await session.refresh(profile)
            return await _profile_out(session, profile)

    @router.put("/profiles/{profile_id}", response_model=ReviewerProfileOut)
    async def update_profile(profile_id: uuid.UUID, payload: ProfileVersionIn):
        async with sf() as session:
            profile = await session.get(ReviewerProfile, profile_id)
            if profile is None:
                raise HTTPException(404)
            if payload.name and payload.name != profile.name:
                existing = (await session.execute(select(ReviewerProfile).where(
                    ReviewerProfile.name == payload.name,
                    ReviewerProfile.id != profile_id))).scalar_one_or_none()
                if existing is not None:
                    raise HTTPException(
                        409, f"a profile named {payload.name!r} already exists")
            max_version = (await session.execute(
                select(func.max(ProfileVersion.version)).where(
                    ProfileVersion.profile_id == profile_id))).scalar() or 0
            pv = ProfileVersion(profile_id=profile_id, version=max_version + 1,
                                system_prompt=payload.system_prompt,
                                guidelines=payload.guidelines,
                                tool_allowlist=payload.tool_allowlist,
                                model=payload.model)
            session.add(pv)
            await session.flush()
            profile.current_version_id = pv.id
            if payload.name:
                profile.name = payload.name
            await session.commit()
            await session.refresh(profile)
            return await _profile_out(session, profile)

    @router.get("/profiles", response_model=list[ReviewerProfileOut])
    async def list_profiles():
        async with sf() as session:
            profiles = (await session.execute(select(ReviewerProfile))).scalars().all()
            return [await _profile_out(session, p) for p in profiles]

    async def _agent_out(session, agent: ReviewerAgent) -> ReviewerAgentOut:
        current_version = None
        if agent.current_version_id is not None:
            av = await session.get(ReviewerAgentVersion, agent.current_version_id)
            if av is not None:
                current_version = ReviewerAgentVersionOut.model_validate(av)
        return ReviewerAgentOut(id=agent.id, name=agent.name,
                               description=agent.description, enabled=agent.enabled,
                               current_version=current_version)

    @router.get("/agents/available-tools", response_model=list[AvailableToolOut])
    async def list_available_tools():
        """Every tool name the review pipeline can register, so the agent
        editor's allowlist picker is generated from the actual pipeline
        instead of a hand-maintained list that drifts the moment a tool is
        added -- as happened when outline_file/search_code shipped with no
        corresponding frontend entry."""
        from argus.review.tool_registry import registered_tools
        return [AvailableToolOut(**t) for t in registered_tools()]

    @router.get("/agents", response_model=PaginatedReviewerAgents)
    async def list_agents(page: int = 1, per_page: int = 50):
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            total = (await session.execute(
                select(func.count(ReviewerAgent.id)))).scalar_one()
            agents = (await session.execute(
                select(ReviewerAgent).order_by(ReviewerAgent.name)
                .offset((page - 1) * per_page).limit(per_page))).scalars().all()
            items = [await _agent_out(session, a) for a in agents]
            return PaginatedReviewerAgents(items=items, total=total, page=page,
                                           per_page=per_page)

    @router.post("/agents", status_code=201, response_model=ReviewerAgentOut)
    async def create_agent(payload: ReviewerAgentCreate):
        async with sf() as session:
            agent = ReviewerAgent(name=payload.name, description=payload.description)
            session.add(agent)
            await session.flush()
            version = ReviewerAgentVersion(
                agent_id=agent.id, version=1, guidelines=payload.guidelines,
                model=payload.model, max_rounds=payload.max_rounds,
                tool_allowlist=payload.tool_allowlist)
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            await session.commit()
            return await _agent_out(session, agent)

    @router.put("/agents/{agent_id}", response_model=ReviewerAgentOut)
    async def update_agent(agent_id: uuid.UUID, payload: ReviewerAgentVersionIn):
        async with sf() as session:
            agent = await session.get(ReviewerAgent, agent_id)
            if agent is None:
                raise HTTPException(404, "reviewer agent not found")
            prior = None
            if agent.current_version_id is not None:
                prior = await session.get(ReviewerAgentVersion, agent.current_version_id)
            next_version_num = (prior.version + 1) if prior else 1
            version = ReviewerAgentVersion(
                agent_id=agent.id, version=next_version_num,
                guidelines=(payload.guidelines if payload.guidelines is not None
                           else (prior.guidelines if prior else "")),
                model=(payload.model if payload.model is not None
                      else (prior.model if prior else None)),
                max_rounds=(payload.max_rounds if payload.max_rounds is not None
                           else (prior.max_rounds if prior else 10)),
                tool_allowlist=(payload.tool_allowlist if payload.tool_allowlist is not None
                               else (prior.tool_allowlist if prior else None)))
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            await session.commit()
            return await _agent_out(session, agent)

    @router.put("/agents/{agent_id}/enabled", response_model=ReviewerAgentOut)
    async def set_agent_enabled(agent_id: uuid.UUID, payload: ReviewerAgentEnabledUpdate):
        async with sf() as session:
            agent = await session.get(ReviewerAgent, agent_id)
            if agent is None:
                raise HTTPException(404, "reviewer agent not found")
            agent.enabled = payload.enabled
            await session.commit()
            return await _agent_out(session, agent)

    @router.put("/agents/{agent_id}/description", response_model=ReviewerAgentOut)
    async def set_agent_description(agent_id: uuid.UUID, payload: ReviewerAgentDescriptionUpdate):
        async with sf() as session:
            agent = await session.get(ReviewerAgent, agent_id)
            if agent is None:
                raise HTTPException(404, "reviewer agent not found")
            agent.description = payload.description
            await session.commit()
            return await _agent_out(session, agent)

    @router.get("/agents/{agent_id}/versions",
               response_model=PaginatedReviewerAgentVersions)
    async def list_agent_versions(agent_id: uuid.UUID, page: int = 1,
                                  per_page: int = 50):
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            total = (await session.execute(
                select(func.count(ReviewerAgentVersion.id))
                .where(ReviewerAgentVersion.agent_id == agent_id))).scalar_one()
            versions = (await session.execute(
                select(ReviewerAgentVersion)
                .where(ReviewerAgentVersion.agent_id == agent_id)
                .order_by(ReviewerAgentVersion.version.desc())
                .offset((page - 1) * per_page).limit(per_page))).scalars().all()
            items = []
            for v in versions:
                stats = await compute_agent_version_acceptance(session, v.id)
                items.append(ReviewerAgentVersionHistoryOut(
                    id=v.id, version=v.version, guidelines=v.guidelines,
                    model=v.model, max_rounds=v.max_rounds,
                    tool_allowlist=v.tool_allowlist, created_by=v.created_by,
                    created_at=v.created_at, **stats))
            return PaginatedReviewerAgentVersions(
                items=items, total=total, page=page, per_page=per_page)

    @router.get("/agents/{agent_id}/usage")
    async def get_agent_usage(agent_id: uuid.UUID):
        async with sf() as session:
            count = (await session.execute(
                select(func.count(ReviewReviewerAgentVersion.id))
                .join(ReviewerAgentVersion,
                     ReviewerAgentVersion.id == ReviewReviewerAgentVersion.agent_version_id)
                .where(ReviewerAgentVersion.agent_id == agent_id))).scalar_one()
            return {"review_count": count}

    @router.get("/settings")
    async def get_runtime_settings():
        async with sf() as session:
            return await read_settings_view(session, settings)

    @router.put("/settings")
    async def put_runtime_settings(payload: SettingsUpdate):
        async with sf() as session:
            try:
                await apply_settings(session, payload.values)
            except ValueError as e:
                raise HTTPException(422, str(e))
            await session.commit()
            return await read_settings_view(session, settings)

    @router.post("/merge-requests/{mr_id}/reviews", status_code=202)
    async def trigger_review(mr_id: uuid.UUID, payload: ReviewTrigger):
        async with sf() as session:
            mr = await session.get(MergeRequest, mr_id)
            if mr is None:
                raise HTTPException(404)
            proxy_url = None
            if payload.reviewer is not None:
                proxy = (await session.execute(select(ReviewerProxy).where(
                    ReviewerProxy.reviewer == payload.reviewer,
                    ReviewerProxy.enabled == True))).scalar_one_or_none()  # noqa: E712
                if proxy is None:
                    raise HTTPException(
                        404, f"no enabled proxy registered for reviewer {payload.reviewer!r}")
                proxy_url = proxy.proxy_url
            try:
                cfg = await resolve_llm_config(session, payload.llm_endpoint_id,
                                               proxy_url)
            except ValueError as e:
                raise HTTPException(400, str(e))
            profile_version_id = None
            if payload.profile_id is not None:
                profile = await session.get(ReviewerProfile, payload.profile_id)
                if profile is None:
                    raise HTTPException(404, f"no profile {payload.profile_id}")
                profile_version_id = profile.current_version_id
            elif mr.repo_id is not None:
                repo = await session.get(Repository, mr.repo_id)
                if repo is not None and repo.default_profile_id is not None:
                    default_profile = await session.get(
                        ReviewerProfile, repo.default_profile_id)
                    if default_profile is not None:
                        profile_version_id = default_profile.current_version_id
            review = Review(mr_id=mr_id, status="queued",
                            llm_config=cfg.model_dump(),
                            profile_version_id=profile_version_id,
                            force_agents=payload.force_agents,
                            mode=payload.mode, publish=payload.publish)
            session.add(review)
            await session.flush()
            job = await enqueue(session, "review",
                                {"review_id": str(review.id)},
                                # Dry runs get their own key so a benchmarking
                                # pass is never blocked by (and never blocks)
                                # a real review of the same MR. The publishing
                                # key is left exactly as the poller builds it,
                                # so poller- and manually-triggered real
                                # reviews still dedup against each other.
                                dedup_key=(f"review:{mr_id}:{payload.mode}"
                                           if payload.publish
                                           else f"review:{mr_id}:{payload.mode}:dry"))
            if job is None:
                raise HTTPException(409, "a review for this MR is already in flight")
            await session.commit()
            return {"review_id": str(review.id)}

    @router.post("/merge-requests/{mr_id}/reconcile-and-distill", status_code=202)
    async def reconcile_and_distill_route(mr_id: uuid.UUID):
        async with sf() as session:
            mr = await session.get(MergeRequest, mr_id)
            if mr is None:
                raise HTTPException(404)
            repo = await session.get(Repository, mr.repo_id)
            if repo is None:
                raise HTTPException(404, "repository not found for this MR")
            client = _client()
            try:
                result = await reconcile_mr(session, client, repo, mr, settings)
            finally:
                await client.aclose()
            queued_run = False
            if result.note_ids:
                note_id_strs = sorted(str(n) for n in result.note_ids)
                batch_hash = hashlib.sha256(
                    ",".join(note_id_strs).encode()).hexdigest()[:16]
                job = await enqueue(
                    session, "distill_mr",
                    {"mr_id": str(mr_id), "note_ids": note_id_strs},
                    dedup_key=f"distill_mr:{mr_id}:{batch_hash}")
                queued_run = job is not None
            await session.commit()
            return {"queued_run": queued_run, "note_count": len(result.note_ids)}

    @router.get("/reviews", response_model=PaginatedReviews)
    async def list_reviews_route(status: str | None = None,
                                 repo_id: uuid.UUID | None = None,
                                 profile_id: uuid.UUID | None = None,
                                 page: int = 1, per_page: int = 50):
        if status is not None and status not in VALID_REVIEW_STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(VALID_REVIEW_STATUSES)}")
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            items, total = await list_reviews(
                session, status=status, repo_id=repo_id, profile_id=profile_id,
                page=page, per_page=per_page)
            return PaginatedReviews(
                items=[ReviewListItemOut(**item) for item in items],
                total=total, page=page, per_page=per_page)

    @router.get("/reviews/queue-summary", response_model=ReviewQueueSummaryOut)
    async def reviews_queue_summary_route():
        async with sf() as session:
            queued = (await session.execute(
                select(func.count(Review.id)).where(
                    Review.status == "queued"))).scalar_one()
            running = (await session.execute(
                select(func.count(Review.id)).where(
                    Review.status == "running"))).scalar_one()
            avg_duration_s = await average_done_duration_seconds(session)
            return ReviewQueueSummaryOut(
                queued=queued, running=running, avg_duration_s=avg_duration_s)

    @router.post("/reviews/{review_id}/cancel")
    async def cancel_review_route(review_id: uuid.UUID):
        async with sf() as session:
            review = await session.get(Review, review_id)
            if review is None:
                raise HTTPException(404)
            if review.status not in ("queued", "running"):
                raise HTTPException(409, f"review is already {review.status}")
            if review.status == "queued":
                # Only remove the Job row if it is STILL queued. If the
                # worker has already claimed it (flipped Job.status to
                # "running" via claim_next) between our read of the review
                # and this delete, this becomes a no-op instead of yanking a
                # live job out from under the worker mid-processing.
                await session.execute(
                    delete(Job).where(
                        Job.kind == "review",
                        Job.status == "queued",
                        Job.payload["review_id"].astext == str(review_id)))
                review.finished_at = datetime.now(timezone.utc)
            review.status = "canceled"
            await session.commit()
            return {"status": "canceled"}

    @router.get("/reviews/{review_id}")
    async def get_review(review_id: uuid.UUID):
        async with sf() as session:
            review = await session.get(Review, review_id)
            if review is None:
                raise HTTPException(404)
            stages_rows = (await session.execute(
                select(ReviewStage).where(ReviewStage.review_id == review_id)
                .order_by(ReviewStage.started_at))).scalars().all()
            return {"id": str(review.id), "mr_id": str(review.mr_id),
                    "status": review.status, "publish": review.publish,
                    "summary": review.summary, "error": review.error,
                    "started_at": review.started_at.isoformat() if review.started_at else None,
                    "finished_at": review.finished_at.isoformat() if review.finished_at else None,
                    "prompt_tokens": review.prompt_tokens,
                    "completion_tokens": review.completion_tokens,
                    "stages": [_stage_dict(r) for r in stages_rows],
                    "candidates": review_candidates(stages_rows)}

    @router.get("/reviews/{review_id}/trace")
    async def get_trace(review_id: uuid.UUID):
        async with sf() as session:
            tc = (await session.execute(select(ToolCall).where(
                ToolCall.review_id == review_id).order_by(ToolCall.seq))).scalars().all()
            lr = (await session.execute(select(LLMRound).where(
                LLMRound.review_id == review_id).order_by(LLMRound.seq))).scalars().all()
            return {"tool_calls": [{"stage": t.stage_name, "seq": t.seq,
                                    "tool": t.tool_name, "status": t.status,
                                    "duration_ms": t.duration_ms} for t in tc],
                    "llm_rounds": [{"stage": r.stage_name, "seq": r.seq,
                                    "prompt_tokens": r.prompt_tokens,
                                    "completion_tokens": r.completion_tokens,
                                    "latency_ms": r.latency_ms, "error": r.error}
                                   for r in lr]}

    @router.get("/distillation-runs/{run_id}", response_model=DistillationRunOut)
    async def get_distillation_run(run_id: uuid.UUID):
        async with sf() as session:
            run = await session.get(DistillationRun, run_id)
            if run is None:
                raise HTTPException(404)
            return DistillationRunOut(
                id=run.id, mr_id=run.mr_id, status=run.status, trigger=run.trigger,
                note_ids=run.note_ids, error=run.error,
                langfuse_trace_id=run.langfuse_trace_id,
                started_at=run.started_at, finished_at=run.finished_at,
                prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens)

    @router.get("/distillation-runs/{run_id}/trace")
    async def get_distillation_trace(run_id: uuid.UUID):
        async with sf() as session:
            tc = (await session.execute(select(ToolCall).where(
                ToolCall.distillation_run_id == run_id).order_by(ToolCall.seq))
                ).scalars().all()
            lr = (await session.execute(select(LLMRound).where(
                LLMRound.distillation_run_id == run_id).order_by(LLMRound.seq))
                ).scalars().all()
            return {"tool_calls": [{"stage": t.stage_name, "seq": t.seq,
                                    "tool": t.tool_name, "status": t.status,
                                    "duration_ms": t.duration_ms} for t in tc],
                    "llm_rounds": [{"stage": r.stage_name, "seq": r.seq,
                                    "prompt_tokens": r.prompt_tokens,
                                    "completion_tokens": r.completion_tokens,
                                    "latency_ms": r.latency_ms, "error": r.error}
                                   for r in lr]}

    @router.get("/audit-runs")
    async def list_audit_runs(repo_id: uuid.UUID | None = None,
                              page: int = 1, per_page: int = 20):
        """Audit runs, newest first. Reviews and distillation runs were both
        listable and traceable; audits were neither, so a run that stalled or
        produced a surprising verdict could only be inspected via SQL."""
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            filters = [AuditRun.repo_id == repo_id] if repo_id else []
            total = (await session.execute(
                select(func.count(AuditRun.id)).where(*filters))).scalar_one()
            rows = (await session.execute(
                select(AuditRun, Repository.project_path)
                .outerjoin(Repository, Repository.id == AuditRun.repo_id)
                .where(*filters)
                .order_by(AuditRun.created_at.desc())
                .offset((page - 1) * per_page).limit(per_page))).all()
            return {
                "items": [{
                    "id": str(r.id), "repo_id": str(r.repo_id),
                    "repo_path": path, "status": r.status,
                    "audited_ref": r.audited_ref, "commit_sha": r.commit_sha,
                    "clusters_examined": r.clusters_examined,
                    "verdicts_written": r.verdicts_written,
                    "langfuse_trace_id": r.langfuse_trace_id,
                    "error": r.error,
                    "started_at": r.started_at, "finished_at": r.finished_at,
                    "created_at": r.created_at,
                } for r, path in rows],
                "total": total, "page": page, "per_page": per_page}

    @router.get("/audit-runs/{run_id}/trace")
    async def get_audit_trace(run_id: uuid.UUID):
        async with sf() as session:
            tc = (await session.execute(select(ToolCall).where(
                ToolCall.audit_run_id == run_id).order_by(ToolCall.seq))
                ).scalars().all()
            lr = (await session.execute(select(LLMRound).where(
                LLMRound.audit_run_id == run_id).order_by(LLMRound.seq))
                ).scalars().all()
            return {"tool_calls": [{"stage": t.stage_name, "seq": t.seq,
                                    "tool": t.tool_name, "status": t.status,
                                    "duration_ms": t.duration_ms} for t in tc],
                    "llm_rounds": [{"stage": r.stage_name, "seq": r.seq,
                                    "prompt_tokens": r.prompt_tokens,
                                    "completion_tokens": r.completion_tokens,
                                    "latency_ms": r.latency_ms, "error": r.error}
                                   for r in lr]}

    _VALID_KINDS = {"guidance", "do_not_suggest", "missed_pattern"}
    _VALID_STATUSES = {"active", "archived"}

    def _learning_out(l: Learning, mr: MergeRequest | None, actor: Actor | None) -> LearningOut:
        return LearningOut(
            id=l.id, repo_id=l.repo_id, topic=l.topic, hint_text=l.hint_text,
            file_pattern=l.file_pattern, kind=l.kind, status=l.status,
            hit_count=l.hit_count, miss_count=l.miss_count,
            inconclusive_count=l.inconclusive_count,
            harmful_count=l.harmful_count, ignored_count=l.ignored_count,
            reputation=learning_reputation(l),
            injection_count=(l.hit_count + l.harmful_count + l.ignored_count
                             + l.miss_count + l.inconclusive_count),
            groundedness=l.groundedness, last_audited_at=l.last_audited_at,
            created_at=l.created_at,
            mr_iid=mr.mr_iid if mr else None,
            mr_title=mr.title if mr else None,
            mr_web_url=mr.web_url if mr else None,
            learned_from_username=actor.username if actor else None)

    @router.get("/learnings", response_model=PaginatedLearnings)
    async def get_learnings(repo_id: uuid.UUID | None = None,
                            kind: str | None = None, status: str | None = None,
                            page: int = 1, per_page: int = 50):
        if kind is not None and kind not in _VALID_KINDS:
            raise HTTPException(422, f"kind must be one of {sorted(_VALID_KINDS)}")
        if status is not None and status not in _VALID_STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(_VALID_STATUSES)}")
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            rows, total = await list_learnings(session, repo_id=repo_id, kind=kind,
                                               status=status, page=page, per_page=per_page)
            return PaginatedLearnings(
                items=[_learning_out(l, mr, actor) for l, mr, actor in rows],
                total=total, page=page, per_page=per_page)

    @router.get("/complaints", response_model=PaginatedComplaints)
    async def get_complaints(review_id: uuid.UUID | None = None,
                             category: str | None = None,
                             target: str | None = None,
                             blocked: bool | None = None,
                             page: int = 1, per_page: int = 50):
        """Problems agents reported about our tools, prompts and context.

        Read the by_target rollup first: one complaint about a tool is an
        agent having a bad day, forty is a bug worth fixing."""
        from sqlalchemy import func

        from argus.domain.models import AgentComplaint
        if category is not None and category not in COMPLAINT_CATEGORIES:
            raise HTTPException(
                422, f"category must be one of {sorted(COMPLAINT_CATEGORIES)}")
        page = max(page, 1)
        per_page = min(per_page, 200)

        def _filtered(stmt):
            if review_id is not None:
                stmt = stmt.where(AgentComplaint.review_id == review_id)
            if category is not None:
                stmt = stmt.where(AgentComplaint.category == category)
            if target is not None:
                stmt = stmt.where(AgentComplaint.target == target)
            if blocked is not None:
                stmt = stmt.where(AgentComplaint.blocked == blocked)
            return stmt

        async with sf() as session:
            total = await session.scalar(
                _filtered(select(func.count()).select_from(AgentComplaint)))
            rows = (await session.execute(
                _filtered(select(AgentComplaint))
                .order_by(AgentComplaint.created_at.desc())
                .offset((page - 1) * per_page).limit(per_page))).scalars().all()
            rollup = (await session.execute(
                _filtered(select(
                    AgentComplaint.target, AgentComplaint.category,
                    func.count().label("count"),
                    func.count().filter(AgentComplaint.blocked).label("blocked_count"),
                    func.max(AgentComplaint.created_at).label("last_seen")))
                .group_by(AgentComplaint.target, AgentComplaint.category)
                .order_by(func.count().desc()))).all()
        return PaginatedComplaints(
            items=[AgentComplaintOut.model_validate(r) for r in rows],
            total=total or 0, page=page, per_page=per_page,
            by_target=[ComplaintTargetCount(
                target=t, category=c, count=n, blocked_count=b, last_seen=ls)
                for t, c, n, b, ls in rollup])

    @router.get("/learnings/search", response_model=PaginatedLearnings)
    async def search_learnings_route(q: str, repo_id: uuid.UUID | None = None,
                                     page: int = 1, per_page: int = 20):
        if not q.strip():
            raise HTTPException(422, "q must not be empty")
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            rows, total = await search_learnings(session, settings, query_text=q,
                                                 repo_id=repo_id, page=page, per_page=per_page)
            return PaginatedLearnings(
                items=[_learning_out(l, mr, actor) for l, mr, actor in rows],
                total=total, page=page, per_page=per_page)

    @router.get("/file-knowledge", response_model=PaginatedFileKnowledge)
    async def get_file_knowledge(repo_id: uuid.UUID, page: int = 1, per_page: int = 50):
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            rows, total = await list_file_knowledge(session, repo_id=repo_id,
                                                     page=page, per_page=per_page)
            return PaginatedFileKnowledge(
                items=[FileKnowledgeOut.model_validate(r) for r in rows],
                total=total, page=page, per_page=per_page)

    @router.post("/repositories/{repo_id}/audit")
    async def trigger_audit(repo_id: uuid.UUID, max_clusters: int = 20):
        from argus.knowledge.auditor import run_audit_for_repo

        async with sf() as session:
            repo = await session.get(Repository, repo_id)
            if repo is None:
                raise HTTPException(404, "repository not found")
            in_flight = (await session.execute(select(AuditRun).where(
                AuditRun.repo_id == repo_id, AuditRun.status == "running"
            ))).scalar_one_or_none()
            if in_flight is not None:
                raise HTTPException(409, "an audit run is already in progress for this repo")
            eff = await load_effective_settings(session, base=settings)
            try:
                llm_cfg = await resolve_llm_config(session, None, None)
            except ValueError as e:
                raise HTTPException(422, f"no LLM endpoint configured: {e}")
            run = AuditRun(repo_id=repo_id, status="running",
                           started_at=datetime.now(timezone.utc))
            session.add(run)
            await session.commit()
            run_id = run.id

        client = _client()
        try:
            result = await run_audit_for_repo(
                sf, eff, client, repo, llm_cfg,
                max_clusters=max_clusters, audit_run_id=run_id)
            # run_audit_for_repo RETURNS a failure (e.g. the workspace clone
            # failed) rather than raising it, so keying purely off exceptions
            # recorded status="done" for a run that examined zero clusters and
            # wrote zero verdicts -- indistinguishable from a repo whose
            # learnings were all fine.
            if result.get("status") == "failed":
                status = "failed"
                error = f"audit could not run: {result.get('reason', 'unknown')}"
            else:
                status, error = "done", None
        except Exception as e:
            logger.exception("manual audit trigger failed for repo %s", repo_id)
            result, status, error = {}, "failed", str(e)[:2000]
        finally:
            await client.aclose()

        async with sf() as session:
            row = await session.get(AuditRun, run_id)
            row.status = status
            row.error = error
            row.clusters_examined = result.get("clusters", 0)
            row.verdicts_written = result.get("verdicts", 0)
            row.finished_at = datetime.now(timezone.utc)
            await session.commit()

        if status == "failed":
            raise HTTPException(500, f"audit run failed: {error}")
        return {"audit_run_id": str(run_id), **result}

    @router.get("/audit-verdicts", response_model=PaginatedAuditVerdicts)
    async def get_audit_verdicts(state: str = "proposed",
                                 repo_id: uuid.UUID | None = None,
                                 include_no_action: bool = False,
                                 page: int = 1, per_page: int = 50):
        page = max(page, 1)
        per_page = min(per_page, 200)
        async with sf() as session:
            filters = [AuditVerdict.state == state]
            if repo_id is not None:
                filters.append(Learning.repo_id == repo_id)
            # 'none' means "this learning is fine, leave it alone" -- there is
            # nothing for a human to decide, and approving one changes nothing.
            # They dominated the queue (31 of 40 verdicts, 14 of them approved
            # to no effect), burying the handful that actually needed a
            # decision. Hidden by default, still reachable for auditing.
            if not include_no_action:
                filters.append(AuditVerdict.proposed_action != "none")
            total = (await session.execute(
                select(func.count(AuditVerdict.id))
                .join(Learning, Learning.id == AuditVerdict.learning_id)
                .where(*filters))).scalar_one()
            # Two different repos matter here and they are easy to conflate:
            # AuditedRepo is what the auditor actually searched, LearningRepo
            # is what the learning claims to be about (NULL = global). A
            # verdict is only interpretable with both -- "no matches found in
            # the codebase" is meaningless until you know which codebase, and
            # is never conclusive for a global learning.
            AuditedRepo = aliased(Repository)
            LearningRepo = aliased(Repository)
            rows = (await session.execute(
                select(AuditVerdict, Learning, AuditedRepo.id, AuditedRepo.project_path,
                       LearningRepo.id, LearningRepo.project_path)
                .join(Learning, Learning.id == AuditVerdict.learning_id)
                .join(AuditRun, AuditRun.id == AuditVerdict.audit_run_id)
                .outerjoin(AuditedRepo, AuditedRepo.id == AuditRun.repo_id)
                .outerjoin(LearningRepo, LearningRepo.id == Learning.repo_id)
                .where(*filters)
                .order_by(AuditVerdict.created_at.desc())
                .offset((page - 1) * per_page).limit(per_page))).all()
            return PaginatedAuditVerdicts(
                items=[AuditVerdictOut(
                    id=v.id, learning_id=v.learning_id, learning_topic=l.topic,
                    verdict=v.verdict, confidence=v.confidence,
                    rationale=v.rationale, citations=v.citations or [],
                    related_learning_id=v.related_learning_id,
                    proposed_action=v.proposed_action,
                    suggested_hint_text=v.suggested_hint_text,
                    current_hint_text=l.hint_text, state=v.state,
                    created_at=v.created_at,
                    audited_repo_id=ar_id, audited_repo_path=ar_path,
                    learning_repo_id=lr_id, learning_repo_path=lr_path)
                    for v, l, ar_id, ar_path, lr_id, lr_path in rows],
                total=total, page=page, per_page=per_page)

    @router.post("/audit-verdicts/{verdict_id}/approve")
    async def approve_verdict(verdict_id: uuid.UUID):
        from argus.knowledge.audit_apply import apply_verdict
        async with sf() as session:
            v = await session.get(AuditVerdict, verdict_id)
            if v is None:
                raise HTTPException(404, "verdict not found")
            if v.state != "proposed":
                raise HTTPException(409, f"verdict already {v.state}")
            v.state = "approved"
            changed = await apply_verdict(session, v, actor="api")
            await session.commit()
        return {"ok": True, "applied": changed}

    @router.post("/audit-verdicts/{verdict_id}/reject")
    async def reject_verdict(verdict_id: uuid.UUID):
        async with sf() as session:
            v = await session.get(AuditVerdict, verdict_id)
            if v is None:
                raise HTTPException(404, "verdict not found")
            if v.state != "proposed":
                raise HTTPException(409, f"verdict already {v.state}")
            v.state = "rejected"
            await session.commit()
        return {"ok": True}

    @router.get("/stats/dashboard", response_model=DashboardStatsOut)
    async def get_dashboard_stats(days: int = 14):
        days = max(1, min(days, 90))
        async with sf() as session:
            return await compute_dashboard_stats(session, days)

    app.include_router(router)
    return app


app = create_app()

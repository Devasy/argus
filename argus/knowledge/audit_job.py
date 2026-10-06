"""An audit run is a queued audit_repo job: resumable, retried, and routed to its own lane (GPU 2)."""
import asyncio
import logging
import subprocess
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from argus.domain.models import AuditRun, Repository
from argus.jobs.queue import enqueue
from argus.providers import create_provider, workspace_for

logger = logging.getLogger("argus.audit_job")


async def enqueue_audit(session, repo_id, *, trigger: str) -> AuditRun | None:
    busy = (await session.execute(select(AuditRun.id).where(
        AuditRun.repo_id == repo_id, AuditRun.status.in_(("queued", "running"))))).first()
    if busy is not None:
        return None
    run = AuditRun(repo_id=repo_id, status="queued", trigger=trigger)
    session.add(run)
    await session.flush()
    job = await enqueue(session, "audit_repo", {"audit_run_id": str(run.id)},
                        dedup_key=f"audit_repo:{repo_id}")
    if job is None:
        await session.delete(run)
        return None
    run.job_id = job.id
    await session.flush()
    return run


async def _delete_checkpoint(pg_url: str, run_id: uuid.UUID) -> None:
    """A finished run's checkpoint thread can never resume anything again --
    it's pure disk weight from then on (~2MB/run measured), on a prod box
    that already has an unfixed checkpoint-bloat problem. Best-effort: never
    let cleanup itself turn a completed run into a failure report."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    try:
        async with AsyncPostgresSaver.from_conn_string(pg_url) as checkpointer:
            await checkpointer.adelete_thread(f"audit:{run_id}")
    except Exception:
        logger.warning("audit run %s: checkpoint cleanup failed", run_id, exc_info=True)


async def run_audit_job(sf, settings, payload: dict, endpoint_name: str | None) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from argus.jobs.workers import distill_llm_config
    from argus.knowledge.audit_pipeline import AuditDeps, run_audit_pipeline
    from argus.knowledge.auditor import resolve_audit_ref
    from argus.knowledge.graphify import build_graph
    from argus.llm.langfuse_run import LangfuseRun

    run_id = uuid.UUID(payload["audit_run_id"])
    pg_url = settings.database_url.replace("+asyncpg", "")
    error = None
    wm = None
    ref = None
    try:
        async with sf() as s:
            run = await s.get(AuditRun, run_id)
            repo = await s.get(Repository, run.repo_id)
            run.status = "running"
            run.started_at = run.started_at or datetime.now(timezone.utc)
            # A resumed checkpoint belongs to the original checkout, even if
            # its branch moved while the worker was offline.
            ref = run.commit_sha or await resolve_audit_ref(s, repo)
            # same "worker endpoint else default" rule the distill lanes use
            if repo.provider == "github":
                from argus.providers.settings import settings_for_repository, require_pilot_endpoint, configure_quota
                from argus.llm.config import resolve_llm_config
                settings = await settings_for_repository(s, settings, repo.id)
                llm_cfg = await resolve_llm_config(s, repo.default_llm_endpoint_id, None)
                require_pilot_endpoint(llm_cfg)
                configure_quota(llm_cfg, settings)
            else:
                llm_cfg = await distill_llm_config(s, endpoint_name)
            await s.commit()
        llm_cfg.timeout = settings.review_llm_timeout_s
        langfuse_run = LangfuseRun.start(
            settings, "audit", run_id, session_id=str(repo.id),
            tags=["audit", repo.project_path, llm_cfg.provider, llm_cfg.model],
            metadata={"audit_run_id": str(run_id), "repo": repo.project_path})
        if langfuse_run.handler is not None:
            llm_cfg = llm_cfg.model_copy(update={"langfuse_handler": langfuse_run.handler})
        provider = create_provider(settings, repo)
        if repo.provider == "github":
            await provider.get_project(repo.project_path)
        wm = workspace_for(settings, repo, provider)
        await provider.aclose()
        workspace = await wm.acquire(ref)
        head = await asyncio.to_thread(subprocess.run, ["git", "rev-parse", "HEAD"],
                                       cwd=workspace, capture_output=True, text=True, timeout=30)
        head.check_returncode()
        commit_sha = head.stdout.strip() or None
        async with sf() as s:
            row = await s.get(AuditRun, run_id)
            row.audited_ref = row.audited_ref or ref
            row.commit_sha = commit_sha
            row.langfuse_trace_id = langfuse_run.trace_id
            await s.commit()
        deps = AuditDeps(sf=sf, settings=settings, llm_cfg=llm_cfg, repo_id=repo.id,
                         workspace=workspace, commit_sha=commit_sha, run_id=run_id,
                         graph_path=await build_graph(workspace),
                         langfuse_metadata=langfuse_run.metadata,
                         gate=asyncio.Semaphore(max(1, settings.audit_max_parallel)))
        with langfuse_run.span("audit"):
            async with AsyncPostgresSaver.from_conn_string(pg_url) as checkpointer:
                await checkpointer.setup()
                await run_audit_pipeline(deps, checkpointer)
    except asyncio.CancelledError:
        # leave the run 'running': the requeued job resumes it from the checkpoint
        raise
    except Exception as e:
        from argus.llm.quota import QuotaDeferred
        if isinstance(e, QuotaDeferred):
            async with sf() as s:
                row = await s.get(AuditRun, run_id)
                row.status, row.error = "queued", str(e)
                await s.commit()
            raise
        logger.exception("audit run %s failed", run_id)
        error = str(e)[:2000]
    finally:
        if wm is not None and ref is not None:
            try:
                await wm.release(ref)
            except Exception:
                logger.warning("audit run %s: workspace release failed", run_id)
    # Every path that reaches here is terminal (done or failed) -- a
    # CancelledError above already returned without reaching this point, so
    # there is nothing left that could ever resume this thread.
    await _delete_checkpoint(pg_url, run_id)
    async with sf() as s:
        row = await s.get(AuditRun, run_id)
        row.status = "failed" if error else "done"
        row.error = error
        row.finished_at = datetime.now(timezone.utc)
        await s.commit()

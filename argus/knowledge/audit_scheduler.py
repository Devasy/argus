"""Periodic scheduling of learning audits.

Continuous and per-learning: each tick enqueues an audit_repo job for every
enabled repo that has due learnings, and the job system's own resume/retry/
lane routing takes it from there. Phase 2's interval-based per-repo batching
is gone -- audit_interval_days now governs when a LEARNING is re-checked, not
when a REPO gets audited.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("argus.audit_scheduler")


async def reclaim_stale_audit_runs(session, stale_after_s: int = 0) -> int:
    """Mark orphaned 'running'/'queued' AuditRun rows as failed.

    Skips a run whose job is still alive (queued or running in the job
    system) -- that run resumes from its LangGraph checkpoint when the job is
    claimed again, and marking it failed here would fight the requeue.
    AuditRun has no locked_by/attempts of its own; job.py's queue owns retry."""
    from sqlalchemy import select as _select
    from sqlalchemy import exists

    from argus.domain.models import AuditRun, Job

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
    job_alive = exists().where(Job.id == AuditRun.job_id,
                               Job.status.in_(("queued", "running")))
    rows = (await session.execute(_select(AuditRun).where(
        AuditRun.status.in_(("running", "queued")),
        (AuditRun.started_at.is_(None)) | (AuditRun.started_at < cutoff),
        ~job_alive
    ))).scalars().all()
    for run in rows:
        run.status = "failed"
        run.error = "orphaned: worker process restarted mid-run"
        run.finished_at = datetime.now(timezone.utc)
        logger.warning("audit run %s (repo %s) reclaimed as failed; orphaned "
                       "by a restart", run.id, run.repo_id)
    if rows:
        await session.flush()
    return len(rows)


async def run_audit_forever(sf, settings, sleep_seconds: int = 3600) -> None:
    """Background loop. No-op unless settings.audit_enabled is on.

    Enqueues, never runs: an audit_repo job (Task 9's audit_job.py) does the
    actual work on a configured worker lane."""
    from sqlalchemy import select
    from argus.domain.models import Repository

    while True:
        try:
            if not settings.audit_enabled:
                await asyncio.sleep(sleep_seconds)
                continue
            async with sf() as s:
                repo_ids = list((await s.execute(
                    select(Repository.id).where(Repository.enabled == True)  # noqa: E712
                )).scalars().all())
            now = datetime.now(timezone.utc)
            for repo_id in repo_ids:
                try:
                    async with sf() as s:
                        await _schedule_repo(s, settings, repo_id, now)
                        await s.commit()
                except Exception:
                    logger.exception("audit scheduling failed for repo %s", repo_id)
        except Exception:
            logger.exception("audit scheduler iteration failed")
        await asyncio.sleep(sleep_seconds)


async def _schedule_repo(s, settings, repo_id, now):
    from sqlalchemy import select
    from argus.domain.models import Learning, Repository
    from argus.knowledge.audit_job import enqueue_audit
    from argus.knowledge.audit_pick import count_due
    from argus.knowledge.auditor import resolve_audit_ref
    from argus.review.workspace import WorkspaceManager

    repo = await s.get(Repository, repo_id)
    if repo is None or not repo.enabled:
        return
    due = await count_due(s, repo.id, now=now,
                          reaudit_after_days=settings.audit_interval_days)
    if not due:
        tracked = (await s.execute(select(Learning.id).where(
            Learning.repo_id == repo.id, Learning.status == "active",
            Learning.audited_at_sha.isnot(None), Learning.file_paths.isnot(None))
            .limit(1))).first()
        if tracked:
            ref = await resolve_audit_ref(s, repo)
            wm = WorkspaceManager(
                Path(settings.workspace_root).expanduser() / str(repo.id),
                f"{settings.gitlab_url.replace('://', f'://oauth2:{settings.gitlab_token}@')}"
                f"/{repo.project_path}.git")
            try:
                workspace = await wm.acquire(ref)
                due = await count_due(s, repo.id, now=now,
                    reaudit_after_days=settings.audit_interval_days, workspace=workspace)
            finally:
                await wm.release(ref)
    if due:
        await enqueue_audit(s, repo.id, trigger="scheduled")

"""Periodic scheduling of learning audits.

Continuous and per-learning: each tick enqueues an audit_repo job for every
enabled repo that has due learnings, and the job system's own resume/retry/
lane routing takes it from there. Phase 2's interval-based per-repo batching
is gone -- audit_interval_days now governs when a LEARNING is re-checked, not
when a REPO gets audited.
"""
import asyncio
import hashlib
import json
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
    from argus.domain.models import AuditRun, Learning, Repository
    from argus.gitlab.client import GitLabClient
    from argus.knowledge.audit_job import enqueue_audit
    from argus.knowledge.audit_pick import changed_paths_since, count_due
    from argus.knowledge.auditor import resolve_audit_ref
    from argus.knowledge.maintenance_state import read_cursor, write_cursor
    from argus.review.workspace import WorkspaceManager

    repo = await s.get(Repository, repo_id)
    if repo is None or not repo.enabled:
        return
    busy = (await s.execute(select(AuditRun.id).where(
        AuditRun.repo_id == repo.id, AuditRun.status.in_(("queued", "running")))
        .limit(1))).first()
    if busy:
        return
    due = await count_due(s, repo.id, now=now,
                          reaudit_after_days=settings.audit_interval_days)
    if not due:
        tracked = (await s.execute(select(Learning.id, Learning.audited_at_sha,
            Learning.file_paths, Learning.last_audited_at).where(
            Learning.repo_id == repo.id, Learning.status == "active",
            Learning.audited_at_sha.isnot(None), Learning.file_paths.isnot(None))
            .order_by(Learning.id))).all()
        tracked = [row for row in tracked if row.file_paths]
        if tracked:
            ref = await resolve_audit_ref(s, repo)
            client = GitLabClient(settings.gitlab_url, settings.gitlab_token,
                                 settings.gitlab_ca_bundle or settings.gitlab_ssl_verify)
            try:
                if ref == "HEAD":
                    project = await client.get_project(repo.gitlab_project_id)
                    ref = project.get("default_branch")
                branch = await client.get_branch(repo.gitlab_project_id, ref) if ref else None
                if branch is None:
                    logger.warning("audit branch %r unavailable for repo %s", ref, repo.id)
                    return
                sha = (branch.get("commit") or {}).get("id")
                if not isinstance(sha, str) or len(sha) != 40 or any(
                        c not in "0123456789abcdef" for c in sha):
                    raise ValueError("audit branch response has no full commit SHA")
            finally:
                await client.aclose()
            # A same-head cache alone misses new/edited learnings or different
            # audited baselines. Bind the result to the exact selection inputs.
            inputs = [[str(r.id), r.audited_at_sha, sorted(r.file_paths),
                       r.last_audited_at.isoformat() if r.last_audited_at else None]
                      for r in tracked]
            fingerprint = hashlib.sha256(json.dumps(inputs).encode()).hexdigest()
            cursor = {"ref": ref, "sha": sha, "inputs": fingerprint,
                      "provider_url": settings.gitlab_url, "project": repo.project_path}
            if await read_cursor(s, repo.id, "audit_cursor") == cursor:
                return
            wm = WorkspaceManager(
                Path(settings.workspace_root).expanduser() / str(repo.id),
                f"{settings.gitlab_url.replace('://', f'://oauth2:{settings.gitlab_token}@')}"
                f"/{repo.project_path}.git")
            try:
                # If every learning was checked at this very tip, no checkout
                # is needed even on the first tick after restart/deployment.
                baselines = {r.audited_at_sha for r in tracked} - {sha}
                diffs = {}
                if baselines:
                    workspace = await wm.acquire(sha)
                    for baseline in baselines:
                        diffs[baseline] = await asyncio.to_thread(
                            changed_paths_since, workspace, baseline)
                for row in tracked:
                    if row.audited_at_sha == sha:
                        continue
                    paths = diffs[row.audited_at_sha]
                    # Missing history must trigger a fresh audit, never a
                    # cached assertion that no referenced file changed.
                    if paths is None or set(row.file_paths).intersection(paths):
                        due = 1
                        break
            finally:
                if baselines:
                    await wm.release(sha)
            if not due:
                await write_cursor(s, repo.id, "audit_cursor", cursor)
    if due:
        await enqueue_audit(s, repo.id, trigger="scheduled")

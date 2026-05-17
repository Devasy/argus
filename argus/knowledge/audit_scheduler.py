"""Periodic scheduling of learning audits.

Deliberately not round-robin: auditing costs an LLM call per cluster, so repos
are selected by staleness. Phase 2 keeps this simple (interval-based); the
suspicion-ranked ordering (high-injection/zero-hit first, post-refactor
triggers) is a follow-up once we can see real audit precision.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("argus.audit_scheduler")


def select_repos_to_audit(repos, now: datetime, interval_days: int) -> list:
    """Repos never audited, or last audited longer ago than the interval."""
    cutoff = now - timedelta(days=interval_days)
    out = []
    for r in repos:
        last = getattr(r, "last_audit_at", None)
        if last is None or last < cutoff:
            out.append(r)
    return out


async def run_audit_forever(sf, settings, client_factory, llm_cfg,
                            sleep_seconds: int = 3600) -> None:
    """Background loop. No-op unless settings.audit_enabled is on."""
    from sqlalchemy import func, select

    from argus.domain.models import AuditRun, Repository
    from argus.knowledge.auditor import run_audit_for_repo

    while True:
        try:
            if not settings.audit_enabled:
                await asyncio.sleep(sleep_seconds)
                continue
            async with sf() as s:
                repos = list((await s.execute(
                    select(Repository).where(Repository.enabled == True)  # noqa: E712
                )).scalars().all())
                last_by_repo = dict((await s.execute(
                    select(AuditRun.repo_id, func.max(AuditRun.finished_at))
                    .group_by(AuditRun.repo_id))).all())
            for r in repos:
                r.last_audit_at = last_by_repo.get(r.id)
            due = select_repos_to_audit(repos, datetime.now(timezone.utc),
                                        settings.audit_interval_days)
            for repo in due:
                async with sf() as s:
                    run = AuditRun(repo_id=repo.id, status="running",
                                   started_at=datetime.now(timezone.utc))
                    s.add(run)
                    await s.commit()
                    run_id = run.id
                client = client_factory()
                try:
                    result = await run_audit_for_repo(
                        sf, settings, client, repo, llm_cfg,
                        max_clusters=settings.audit_max_clusters,
                        audit_run_id=run_id)
                    status, error = "done", None
                except Exception as e:
                    logger.exception("audit run failed for repo %s", repo.id)
                    result, status, error = {}, "failed", str(e)[:2000]
                finally:
                    await client.aclose()
                async with sf() as s:
                    row = await s.get(AuditRun, run_id)
                    row.status = status
                    row.error = error
                    row.clusters_examined = result.get("clusters", 0)
                    row.verdicts_written = result.get("verdicts", 0)
                    row.finished_at = datetime.now(timezone.utc)
                    await s.commit()
        except Exception:
            logger.exception("audit scheduler iteration failed")
        await asyncio.sleep(sleep_seconds)

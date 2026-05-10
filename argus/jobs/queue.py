import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.domain.models import Job

logger = logging.getLogger("argus.jobs")

# Reviews legitimately run for hours: median 17min, p99 7h20m, observed max
# 12h34m. The lease has to clear that comfortably; stranded jobs sat 'running'
# for days, so nothing is gained by cutting it fine.
STALE_LEASE_S = 24 * 3600
MAX_JOB_ATTEMPTS = 3


async def reclaim_stale(session: AsyncSession, stale_after_s: int = STALE_LEASE_S,
                        max_attempts: int = MAX_JOB_ATTEMPTS) -> dict[str, int]:
    """Hand jobs whose worker died back to the queue.

    A worker killed mid-job cannot mark its own row: the finally block's awaits
    are cancelled too, so the job stays 'running' forever. Because `enqueue`
    dedups on 'running', that also silences every later review of the same MR.
    Reviews resume from their LangGraph checkpoint, so re-queueing loses no
    completed work.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
    rows = (await session.execute(select(Job).where(
        Job.status == "running",
        or_(Job.locked_at.is_(None), Job.locked_at < cutoff)))).scalars().all()
    stats = {"requeued": 0, "failed": 0}
    for job in rows:
        if job.attempts >= max_attempts:
            job.status = "failed"
            job.error = (f"abandoned after {job.attempts} attempt(s): worker "
                         f"died mid-job each time")
            stats["failed"] += 1
            logger.warning("job %s abandoned after %d attempts", job.id,
                           job.attempts)
            continue
        job.status, job.locked_by, job.locked_at = "queued", None, None
        stats["requeued"] += 1
        logger.info("job %s reclaimed from a dead worker; will resume", job.id)
    if rows:
        await session.flush()
    return stats


async def enqueue(session: AsyncSession, kind: str, payload: dict,
                  dedup_key: str | None) -> Job | None:
    if dedup_key:
        existing = (await session.execute(select(Job).where(
            Job.dedup_key == dedup_key,
            Job.status.in_(["queued", "running"])))).scalar_one_or_none()
        if existing is not None:
            return None
    job = Job(kind=kind, payload=payload, dedup_key=dedup_key)
    session.add(job)
    await session.flush()
    return job


async def claim_next(session: AsyncSession, worker_id: str,
                     kinds: list[str]) -> Job | None:
    now = datetime.now(timezone.utc)
    job = (await session.execute(
        select(Job).where(Job.status == "queued", Job.kind.in_(kinds),
                          or_(Job.run_after.is_(None), Job.run_after <= now))
        .order_by(Job.created_at).limit(1)
        .with_for_update(skip_locked=True))).scalar_one_or_none()
    if job is None:
        return None
    job.status, job.locked_by, job.locked_at = "running", worker_id, now
    job.attempts += 1
    await session.flush()
    return job


async def finish(session: AsyncSession, job: Job, error: str | None) -> None:
    job.status = "failed" if error else "done"
    job.error = error
    await session.flush()


async def run_worker_forever(sf: async_sessionmaker, handlers: dict[str, Callable],
                             stop: asyncio.Event, worker_id: str = "w1",
                             idle_sleep_s: float = 2.0) -> None:
    async with sf() as session:
        await reclaim_stale(session)
        await session.commit()
    while not stop.is_set():
        job_id = None
        async with sf() as session:
            job = await claim_next(session, worker_id, list(handlers))
            if job:
                job_id, kind, payload = job.id, job.kind, dict(job.payload)
            await session.commit()
        if job_id is None:
            async with sf() as session:
                await reclaim_stale(session)
                await session.commit()
            try:
                await asyncio.wait_for(stop.wait(), timeout=idle_sleep_s)
            except TimeoutError:
                pass
            continue
        error = None
        interrupted = False
        try:
            await handlers[kind](payload)
        except asyncio.CancelledError:
            logger.warning("job %s interrupted (worker shutting down); "
                           "requeued to resume", job_id)
            interrupted = True
            raise
        except Exception as e:
            logger.exception("job %s failed", job_id)
            error = str(e)[:2000]
        finally:
            async with sf() as session:
                job = await session.get(Job, job_id)
                if interrupted:
                    # Queued, not failed: a review resumes from its LangGraph
                    # checkpoint and keeps every stage it already finished.
                    job.status, job.locked_by, job.locked_at = "queued", None, None
                else:
                    await finish(session, job, error)
                await session.commit()

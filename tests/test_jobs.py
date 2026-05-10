import pytest
from sqlalchemy import delete, select

from argus.domain.models import Job
from argus.jobs.queue import claim_next, enqueue, finish


async def test_enqueue_dedup(db):
    j1 = await enqueue(db, "review", {"review_id": "x"}, dedup_key="review:m1")
    j2 = await enqueue(db, "review", {"review_id": "y"}, dedup_key="review:m1")
    assert j1 is not None and j2 is None


async def test_claim_and_finish(db):
    await enqueue(db, "review", {"review_id": "x"}, dedup_key="review:m2")
    job = await claim_next(db, "w1", ["review"])
    assert job is not None and job.status == "running" and job.locked_by == "w1"
    assert await claim_next(db, "w2", ["review"]) is None
    await finish(db, job, error=None)
    assert job.status == "done"


async def test_failed_job_records_error(db):
    await enqueue(db, "review", {"review_id": "x"}, dedup_key="review:m3")
    job = await claim_next(db, "w1", ["review"])
    await finish(db, job, error="boom")
    assert job.status == "failed" and job.error == "boom"


# --- reclaiming jobs whose worker died -------------------------------------

async def _running_job(db, dedup_key, locked_ago_s, attempts=1):
    from datetime import datetime, timedelta, timezone
    await enqueue(db, "review", {"review_id": "x"}, dedup_key=dedup_key)
    job = await claim_next(db, "w1", ["review"])
    job.locked_at = datetime.now(timezone.utc) - timedelta(seconds=locked_ago_s)
    job.attempts = attempts
    await db.flush()
    return job


async def test_a_job_stuck_past_the_lease_goes_back_to_the_queue(db):
    """A worker killed mid-job cannot mark its own row: the finally block's
    awaits are themselves cancelled. Four jobs on prod had been 'running' for
    5 to 34 days, each blocking every later review of its MR."""
    from argus.jobs.queue import STALE_LEASE_S, reclaim_stale

    job = await _running_job(db, "review:stale1", STALE_LEASE_S + 60)
    stats = await reclaim_stale(db)

    assert stats["requeued"] == 1
    assert job.status == "queued"
    assert job.locked_by is None and job.locked_at is None


async def test_a_job_still_within_its_lease_is_left_alone(db):
    """Reviews legitimately run for hours -- observed median 17min, max
    12h34m -- so a generous lease is the whole point."""
    from argus.jobs.queue import reclaim_stale

    job = await _running_job(db, "review:fresh1", 600)
    stats = await reclaim_stale(db)

    assert stats["requeued"] == 0
    assert job.status == "running" and job.locked_by == "w1"


async def test_reclaiming_unblocks_a_new_review_for_the_same_mr(db):
    """This is the user-visible symptom: enqueue dedups on 'running', so a
    stranded job silences that MR permanently."""
    from argus.jobs.queue import STALE_LEASE_S, reclaim_stale

    await _running_job(db, "review:blocked", STALE_LEASE_S + 60)
    assert await enqueue(db, "review", {"review_id": "z"},
                         dedup_key="review:blocked") is None

    await reclaim_stale(db)
    # Still deduped -- correctly, because the reclaimed job is queued and will
    # resume from its checkpoint. The point is that a worker can now claim it.
    assert await claim_next(db, "w2", ["review"]) is not None


async def test_a_job_retried_too_often_is_failed_not_requeued_forever(db):
    from argus.jobs.queue import (MAX_JOB_ATTEMPTS, STALE_LEASE_S,
                                      reclaim_stale)

    job = await _running_job(db, "review:looping", STALE_LEASE_S + 60,
                             attempts=MAX_JOB_ATTEMPTS)
    stats = await reclaim_stale(db)

    assert stats["failed"] == 1 and stats["requeued"] == 0
    assert job.status == "failed"
    assert "attempt" in (job.error or "").lower()


async def test_reclaim_ignores_jobs_that_are_not_running(db):
    from argus.jobs.queue import reclaim_stale

    await enqueue(db, "review", {"review_id": "q"}, dedup_key="review:queued1")
    done = await _running_job(db, "review:done1", 999999)
    await finish(db, done, error=None)

    stats = await reclaim_stale(db)
    assert stats == {"requeued": 0, "failed": 0}


async def test_reclaim_handles_a_running_job_with_no_lock_timestamp(db):
    """Defensive: a NULL locked_at should not make the job immortal."""
    from argus.jobs.queue import reclaim_stale

    job = await _running_job(db, "review:nolock", 10)
    job.locked_at = None
    await db.flush()

    assert (await reclaim_stale(db))["requeued"] == 1
    assert job.status == "queued"


async def test_an_interrupted_job_is_requeued_not_failed(db, engine):
    """Marking it failed would strand the LangGraph checkpoint: the review
    keeps every stage it finished, and a 12h run should not restart from zero."""
    import asyncio

    from argus.db import session_factory
    from argus.jobs.queue import run_worker_forever

    sf = session_factory(engine)
    async with sf() as s:
        await enqueue(s, "review", {"review_id": "interrupt-me"},
                      dedup_key="review:interrupted")
        await s.commit()

    async def handler(payload):
        raise asyncio.CancelledError()

    stop = asyncio.Event()
    with pytest.raises(asyncio.CancelledError):
        await run_worker_forever(sf, {"review": handler}, stop, worker_id="wx")

    async with sf() as s:
        job = (await s.execute(select(Job).where(
            Job.dedup_key == "review:interrupted"))).scalar_one()
        assert job.status == "queued", "must be resumable, not failed"
        assert job.locked_by is None
        await s.execute(delete(Job).where(Job.dedup_key == "review:interrupted"))
        await s.commit()


async def test_worker_reclaims_stale_jobs_before_claiming_work(db, engine):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from argus.db import session_factory
    from argus.jobs.queue import STALE_LEASE_S, run_worker_forever

    sf = session_factory(engine)
    async with sf() as s:
        await enqueue(s, "review", {"review_id": "stranded"},
                      dedup_key="review:stranded")
        job = await claim_next(s, "dead-worker", ["review"])
        job.locked_at = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_LEASE_S + 60)
        await s.commit()

    seen = []

    async def handler(payload):
        seen.append(payload)

    stop = asyncio.Event()

    async def stopper():
        await asyncio.sleep(0.4)
        stop.set()

    await asyncio.gather(
        run_worker_forever(sf, {"review": handler}, stop, worker_id="wy",
                           idle_sleep_s=0.05),
        stopper())

    assert {"review_id": "stranded"} in seen, "stranded job never resumed"
    async with sf() as s:
        await s.execute(delete(Job).where(Job.dedup_key == "review:stranded"))
        await s.commit()

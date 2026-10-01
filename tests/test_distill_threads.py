import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from argus.domain.models import Discussion, DistillThread
from tests.distill_helpers import _mr_disc, _note, _resolved_bot_thread, purge_repos


async def test_one_ledger_row_per_discussion(db):
    _, mr, disc = await _mr_disc(db)
    db.add(DistillThread(mr_id=mr.id, discussion_id=disc.id, thread_type="bot_thread",
                         content_hash="h1", status="queued"))
    await db.flush()
    db.add(DistillThread(mr_id=mr.id, discussion_id=disc.id, thread_type="bot_thread",
                         content_hash="h2", status="queued"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_ledger_rejects_an_unknown_verdict(db):
    _, mr, disc = await _mr_disc(db)
    db.add(DistillThread(mr_id=mr.id, discussion_id=disc.id, thread_type="bot_thread",
                         content_hash="h", status="done", reply_verdict="maybe"))
    with pytest.raises(IntegrityError):
        await db.flush()


from datetime import timedelta

from argus.domain.models import Finding, Note, Review
from argus.knowledge.distill_threads import content_hash, load_threads, thread_ready


def test_content_hash_ignores_order_and_matches_the_migration_seed():
    a, b = uuid.UUID(int=2), uuid.UUID(int=1)
    import hashlib
    expected = hashlib.md5(",".join(sorted([str(a), str(b)])).encode()).hexdigest()
    assert content_hash([a, b]) == content_hash([b, a]) == expected


def test_thread_ready_rules():
    now = datetime.now(timezone.utc)
    assert thread_ready("merged", False, now, now)
    assert thread_ready("opened", True, now, now)
    assert thread_ready("opened", False, now - timedelta(hours=25), now)
    assert not thread_ready("opened", False, now - timedelta(hours=1), now)
    assert thread_ready("opened", False, now - timedelta(hours=1), now, quiet_hours=0)
    assert not thread_ready("opened", False, now - timedelta(hours=25), now, quiet_hours=48)


async def test_load_threads_builds_a_bot_thread_with_its_finding_and_replies(db):
    _, mr, disc = await _mr_disc(db)
    bot = await _note(db, mr, disc, "bot", "Missing timeout", 0)
    reply = await _note(db, mr, disc, "human", "wrapper sets a default", 5)
    review = Review(mr_id=mr.id, status="done")
    db.add(review)
    await db.flush()
    db.add(Finding(review_id=review.id, stage="analysis", type="issue", severity="high",
                   file_path="a.py", line=3, title="Missing timeout", body="b",
                   verdict_valid=True, published_note_id=bot.id))
    await db.flush()

    [t] = await load_threads(db, mr)
    assert t.thread_type == "bot_thread"
    assert t.bot_note.id == bot.id and t.finding.title == "Missing timeout"
    assert [n.id for n in t.human_notes] == [reply.id]
    assert t.content_hash == content_hash([reply.id])


async def test_load_threads_includes_summary_discussions_and_skips_bot_only(db):
    _, mr, disc = await _mr_disc(db)
    await _note(db, mr, disc, "human", "overall: prefer dataclasses here", 0, kind="summary")
    bot_only = Discussion(mr_id=mr.id, provider_discussion_id=uuid.uuid4().hex)
    db.add(bot_only)
    await db.flush()
    await _note(db, mr, bot_only, "bot", "lonely bot comment", 0)

    threads = await load_threads(db, mr)
    assert [t.discussion_id for t in threads] == [disc.id]
    assert threads[0].thread_type == "human_thread"
    assert threads[0].anchor == "MR-level discussion"


from sqlalchemy import select

from argus.domain.models import Job
from argus.knowledge.distill_threads import (MAX_THREAD_ATTEMPTS, STALE_QUEUED_HOURS,
                                                 enqueue_ready_threads,
                                                 select_threads_to_distill)


async def test_a_ready_thread_is_queued_once_and_not_again(db):
    _, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    now = datetime.now(timezone.utc) + timedelta(hours=1)

    jobs = await enqueue_ready_threads(db, mr, now)
    assert len(jobs) == 1 and jobs[0].payload["discussion_ids"] == [str(disc.id)]
    row = (await db.execute(select(DistillThread).where(
        DistillThread.discussion_id == disc.id))).scalar_one()
    assert row.status == "queued"

    row.status = "done"
    await db.flush()
    assert await enqueue_ready_threads(db, mr, now) == []


async def test_a_new_human_reply_requeues_the_thread(db):
    _, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    await enqueue_ready_threads(db, mr, now)
    row = (await db.execute(select(DistillThread).where(
        DistillThread.discussion_id == disc.id))).scalar_one()
    row.status = "done"
    await db.flush()

    await _note(db, mr, disc, "human", "actually, fair point, will change", 30)
    assert [t.discussion_id for t in await select_threads_to_distill(db, mr, now)] == [disc.id]


async def test_a_disposition_flip_alone_does_not_requeue(db):
    _, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    await enqueue_ready_threads(db, mr, now)
    row = (await db.execute(select(DistillThread).where(
        DistillThread.discussion_id == disc.id))).scalar_one()
    row.status = "done"
    bot = (await db.execute(select(Note).where(Note.discussion_id == disc.id,
                                               Note.author_type == "bot"))).scalar_one()
    bot.disposition = "accepted_manually"
    await db.flush()
    assert await select_threads_to_distill(db, mr, now) == []


async def test_an_open_active_thread_waits(db):
    _, mr, disc = await _mr_disc(db)
    await _note(db, mr, disc, "human", "hmm, not sure about this", 0)
    assert await enqueue_ready_threads(db, mr, datetime.now(timezone.utc)) == []


async def test_a_failed_thread_retries_until_the_cap(db):
    _, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    await enqueue_ready_threads(db, mr, now)
    row = (await db.execute(select(DistillThread).where(
        DistillThread.discussion_id == disc.id))).scalar_one()
    row.status, row.attempts = "failed", 2
    await db.flush()
    assert len(await select_threads_to_distill(db, mr, now)) == 1
    row.attempts = 3
    await db.flush()
    assert await select_threads_to_distill(db, mr, now) == []


from argus.knowledge.distill_threads import format_threads


async def test_bot_thread_block_shows_the_bot_comment_before_the_reply(db):
    _, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    mr.description = "Adds retry to the uploader"
    [t] = await load_threads(db, mr)
    text = format_threads([t], mr)
    assert f"discussion_id={disc.id}" in text
    assert "type: bot_thread" in text
    assert text.index("use a context manager") < text.index("intentional, closed in finally")
    assert f"[note {t.human_notes[0].id}]" in text
    assert "Adds retry to the uploader" in text


async def _system(db, mr, disc, event_type, body, minutes):
    n = await _note(db, mr, disc, "system", body, minutes, kind="system")
    n.system_event_type = event_type
    await db.flush()
    return n


async def test_thread_carries_gitlabs_record_of_the_code_changing(db):
    # MR 159: "Updated" came 4 min after GitLab marked the commented line changed
    _, mr, disc = await _mr_disc(db)
    disc.resolved = True
    elsewhere = Discussion(mr_id=mr.id, provider_discussion_id=uuid.uuid4().hex)
    db.add(elsewhere)
    await db.flush()
    await _system(db, mr, disc, "line_outdated", "changed this line in [version 7 of the diff](/x)", -5)
    await _note(db, mr, disc, "bot", "comment misattributes the rationale", 0)
    await _system(db, mr, disc, "line_outdated", "changed this line in [version 8 of the diff](/x)", 51)
    await _system(db, mr, elsewhere, "commits_added",
                  "added 1 commit\n\n<ul><li>e2656a48 - Resolved review comments</li></ul>", 51)
    await _system(db, mr, elsewhere, "assigned", "assigned to @someone", 52)
    await _note(db, mr, disc, "human", "Updated", 55)
    [t] = await load_threads(db, mr)
    assert [(e.kind, e.text) for e in t.events] == [
        ("line_changed", "the commented line changed (version 8 of the diff)"),
        ("commit", "e2656a48 - Resolved review comments")]
    text = format_threads([t], mr)
    assert "the commented line changed (version 8 of the diff) · 4 min before the first reply" in text
    assert "commit pushed: e2656a48 - Resolved review comments" in text
    assert "version 7" not in text and "assigned" not in text


async def test_thread_says_so_when_the_commented_line_never_moved(db):
    _, mr, _ = await _mr_disc(db)
    await _resolved_bot_thread(db, mr)
    [t] = await load_threads(db, mr)
    text = format_threads([t], mr)
    assert "the commented line itself was not changed" in text
    assert "no commits pushed" in text


async def test_a_thread_opened_by_another_review_bot_is_not_a_human_lesson(db):
    _, mr, disc = await _mr_disc(db)
    await _note(db, mr, disc, "external_bot", "<!-- ai-review-bot --> 3 issues found", 0)
    await _note(db, mr, disc, "human", "Done", 5)
    assert await load_threads(db, mr) == []


async def test_a_human_thread_shows_the_reconcilers_rating_of_the_reviewer_comment(db):
    _, mr, disc = await _mr_disc(db)
    reviewer = await _note(db, mr, disc, "human", "This should be done in a transaction", 0)
    await _note(db, mr, disc, "human", "Done", 5)
    reviewer.disposition = "accepted_manually"
    [t] = await load_threads(db, mr)
    assert "reconciler label: accepted_manually" in format_threads([t], mr)


async def test_legacy_note_ids_payload_maps_to_discussions(db):
    from argus.knowledge.distill_threads import payload_discussion_ids
    _, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    bot = (await db.execute(select(Note).where(Note.discussion_id == disc.id,
                                               Note.author_type == "bot"))).scalar_one()
    assert await payload_discussion_ids(db, {"mr_id": str(mr.id),
                                             "note_ids": [str(bot.id)]}) == [disc.id]
    assert await payload_discussion_ids(db, {"mr_id": str(mr.id),
                                             "discussion_ids": [str(disc.id)]}) == [disc.id]


async def test_a_job_with_no_threads_makes_no_llm_call(db, engine, settings, monkeypatch):
    from argus.api import app as app_module
    from argus.db import session_factory
    import argus.knowledge.agentic_distiller as ad
    called = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("must not run")
    monkeypatch.setattr(ad, "run_thread_distillation", boom)
    sf = session_factory(engine)
    async with sf() as s:
        repo, mr, _ = await _mr_disc(s)
        await s.commit()
    try:
        await app_module._run_distill_mr_job(sf, settings,
                                             {"mr_id": str(mr.id), "discussion_ids": []})
        assert called == []
    finally:
        await purge_repos(engine, [repo.id])


def test_distill_settle_time_is_an_editable_setting():
    from argus.config import Settings
    from argus.settings_store import EDITABLE_KEYS, EDITABLE_MINIMUMS
    assert Settings().distill_quiet_hours == 24
    assert EDITABLE_KEYS["distill_quiet_hours"] is int
    assert EDITABLE_MINIMUMS["distill_quiet_hours"] == 0



async def test_a_stale_queued_requeue_counts_toward_the_attempt_cap(db):
    _, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    now = datetime.now(timezone.utc) + timedelta(hours=1)
    [job] = await enqueue_ready_threads(db, mr, now)
    job.status = "failed"  # the worker died; its ledger row stays queued
    row = (await db.execute(select(DistillThread).where(
        DistillThread.discussion_id == disc.id))).scalar_one()
    later = now + timedelta(hours=STALE_QUEUED_HOURS + 1)
    row.updated_at = now
    await db.flush()
    assert len(await enqueue_ready_threads(db, mr, later)) == 1
    await db.refresh(row)
    assert row.attempts == 1
    row.attempts, row.updated_at = MAX_THREAD_ATTEMPTS, now
    await db.flush()
    assert await select_threads_to_distill(db, mr, later) == []


async def test_the_sweep_enqueues_settled_threads_without_a_gitlab_sync(db):
    from argus.knowledge.distill_threads import sweep_pending_threads
    repo, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    now = datetime.now(timezone.utc) + timedelta(hours=1)

    assert await sweep_pending_threads(db, repo, now) == 1
    row = (await db.execute(select(DistillThread).where(
        DistillThread.discussion_id == disc.id))).scalar_one()
    row.status = "done"
    await db.flush()
    assert await sweep_pending_threads(db, repo, now) == 0

"""Job durability, branch-check invalidation, and bounded sweep continuation."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.domain.models import AuditRun, AuditVerdict, Job, Learning
from argus.jobs.queue import MAX_JOB_ATTEMPTS, claim_next, finish
from argus.knowledge import audit_scheduler, distill_threads, embedding_jobs
from argus.knowledge.audit_apply import apply_verdict
from argus.knowledge.maintenance_state import read_cursor, write_cursor
from tests.distill_helpers import _mr_disc, _note, _resolved_bot_thread


@pytest.fixture
async def sf(engine):
    async with engine.connect() as conn:
        transaction = await conn.begin()
        yield async_sessionmaker(conn, expire_on_commit=False,
                                 join_transaction_mode="create_savepoint")
        await transaction.rollback()


async def _rewrite(s, learning, new_text, *, survivor=None):
    run = AuditRun(repo_id=learning.repo_id, status="done")
    s.add(run)
    await s.flush()
    verdict = AuditVerdict(audit_run_id=run.id, learning_id=learning.id,
        verdict="duplicate_of" if survivor else "unfalsifiable", confidence=0.9,
        rationale="Update wording", proposed_action="merge" if survivor else "flag_for_rewrite",
        related_learning_id=survivor.id if survivor else None,
        suggested_hint_text=new_text, state="approved")
    s.add(verdict)
    await s.flush()
    await apply_verdict(s, verdict)
    return verdict


@pytest.mark.parametrize("merge", [False, True])
async def test_audit_wording_change_is_searchable_again_after_embedding_job(sf, settings, monkeypatch, merge):
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        original = Learning(repo_id=repo.id, topic="Cleanup", hint_text="Old wording",
                            embedding=[0.3] * 768)
        target = Learning(repo_id=repo.id, topic="Resources", hint_text="Survivor wording",
                          embedding=[0.4] * 768) if merge else original
        s.add_all([original, target])
        await s.flush()
        verdict = await _rewrite(s, original, "New wording", survivor=target if merge else None)
        assert target.embedding is None
        job = (await s.execute(select(Job).where(Job.kind == "embed_learning"))).scalar_one()
        assert job.payload["learning_id"] == str(target.id)
        assert not await apply_verdict(s, verdict), "reapplying cannot enqueue another repair"
        await s.commit()
    calls = []
    async def embed(text, config, **kwargs):
        calls.append(text)
        return [0.6] * 768
    monkeypatch.setattr(embedding_jobs, "embed_text", embed)
    await embedding_jobs.run_embedding_job(sf, settings, job.payload)
    await embedding_jobs.run_embedding_job(sf, settings, job.payload)
    async with sf() as s:
        repaired = await s.get(Learning, target.id)
        assert list(repaired.embedding) == pytest.approx([0.6] * 768)
        from argus.knowledge import learnings
        monkeypatch.setattr(learnings, "embed_text", embed)
        found, total = await learnings.search_learnings(s, settings, repo_id=repo.id, query_text="New wording")
        assert target.id in [row[0].id for row in found] and total >= 1
    assert calls[0] == f"{target.topic} :: New wording"
    assert len(calls) == 2, "duplicate delivery does not call the embedding provider"


async def test_rollback_keeps_old_wording_vector_and_no_repair_job(sf):
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        learning = Learning(repo_id=repo.id, topic="Cleanup", hint_text="Old", embedding=[0.3] * 768)
        s.add(learning)
        await s.commit()
        learning_id = learning.id
        await _rewrite(s, learning, "New")
        await s.rollback()
    async with sf() as s:
        learning = await s.get(Learning, learning_id)
        assert learning.hint_text == "Old" and learning.embedding is not None
        assert (await s.execute(select(Job).where(Job.kind == "embed_learning"))).all() == []


@pytest.mark.parametrize("change", ["rewrite", "archive", "delete", "topic"])
async def test_embedding_in_flight_cannot_overwrite_a_newer_learning(sf, settings, monkeypatch, change):
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        learning = Learning(repo_id=repo.id, topic="Cleanup", hint_text="First")
        s.add(learning)
        await s.flush()
        job = await embedding_jobs.enqueue_embedding(s, learning)
        await s.commit()
    async def embed(*args, **kwargs):
        async with sf() as s:
            row = await s.get(Learning, learning.id)
            if change == "rewrite":
                await _rewrite(s, row, "Second")
            elif change == "archive":
                row.status = "archived"
            elif change == "delete":
                await s.delete(row)
            else:
                row.topic = "Different topic"
            await s.commit()
        return [0.7] * 768
    monkeypatch.setattr(embedding_jobs, "embed_text", embed)
    await embedding_jobs.run_embedding_job(sf, settings, job.payload)
    async with sf() as s:
        row = await s.get(Learning, learning.id)
        assert row is None or row.embedding is None
        if change == "rewrite":
            assert len((await s.execute(select(Job).where(Job.kind == "embed_learning"))).all()) == 2


@pytest.mark.parametrize("vector", [None, [0.2], [float("nan")] * 768])
async def test_invalid_embedding_remains_repairable(sf, settings, monkeypatch, vector):
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        learning = Learning(repo_id=repo.id, topic="Cleanup", hint_text="First")
        s.add(learning)
        await s.flush()
        job = await embedding_jobs.enqueue_embedding(s, learning)
        await s.commit()
    async def embed(*args, **kwargs):
        return vector
    monkeypatch.setattr(embedding_jobs, "embed_text", embed)
    with pytest.raises(embedding_jobs.EmbeddingError):
        await embedding_jobs.run_embedding_job(sf, settings, job.payload)
    async with sf() as s:
        assert (await s.get(Learning, learning.id)).embedding is None


async def test_embedding_failures_back_off_then_recovery_resumes_after_outage(sf, settings, monkeypatch):
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        row = Learning(repo_id=repo.id, topic="Cleanup", hint_text="Retry me")
        s.add(row)
        await s.flush()
        assert await embedding_jobs.recover_missing_embeddings(s) == 1
        assert await embedding_jobs.recover_missing_embeddings(s) == 0
        await s.commit()
        for attempt in range(1, MAX_JOB_ATTEMPTS + 1):
            job = await claim_next(s, "embed", ["embed_learning"])
            assert job.attempts == attempt
            before = datetime.now(timezone.utc)
            await finish(s, job, "Provider temporarily unavailable")
            if attempt < MAX_JOB_ATTEMPTS:
                assert job.status == "queued" and job.run_after > before
                assert await claim_next(s, "embed", ["embed_learning"]) is None
                job.run_after = before - timedelta(seconds=1)
            else:
                assert job.status == "failed"
            await s.commit()
        assert await embedding_jobs.recover_missing_embeddings(s) == 0
        assert await embedding_jobs.recover_missing_embeddings(
            s, now=datetime.now(timezone.utc) + timedelta(hours=2)) == 1
        retry = (await s.execute(select(Job).where(Job.status == "queued",
            Job.kind == "embed_learning"))).scalar_one()
        await s.commit()
    async def embed(*args, **kwargs):
        return [0.2] * 768
    monkeypatch.setattr(embedding_jobs, "embed_text", embed)
    await embedding_jobs.run_embedding_job(sf, settings, retry.payload)
    async with sf() as s:
        assert (await s.get(Learning, row.id)).embedding is not None


async def test_repair_scan_is_bounded_and_skips_archived_or_already_queued_rows(db, monkeypatch):
    monkeypatch.setattr(embedding_jobs, "REPAIR_LIMIT", 2)
    repo, _, _ = await _mr_disc(db)
    for i in range(5):
        db.add(Learning(repo_id=repo.id, topic=str(i), hint_text="Missing"))
    db.add(Learning(repo_id=repo.id, topic="Archived", hint_text="Skip", status="archived"))
    await db.flush()
    assert await embedding_jobs.recover_missing_embeddings(db) == 2
    assert await embedding_jobs.recover_missing_embeddings(db) == 2
    assert await embedding_jobs.recover_missing_embeddings(db) == 1
    assert await embedding_jobs.recover_missing_embeddings(db) == 0


async def test_dead_embedding_worker_is_reclaimed_without_interrupting_a_long_review(db):
    from argus.jobs.queue import reclaim_stale
    before = datetime.now(timezone.utc) - timedelta(minutes=10)
    repair = Job(kind="embed_learning", payload={}, status="running", locked_at=before, attempts=1)
    review = Job(kind="review", payload={}, status="running", locked_at=before, attempts=1)
    db.add_all([repair, review])
    await db.flush()
    result = await reclaim_stale(db)
    assert result == {"requeued": 1, "failed": 0}
    assert repair.status == "queued" and review.status == "running"


async def test_embedding_worker_recovers_vectors_without_chat_lanes(engine, settings, monkeypatch):
    from argus.db import session_factory
    from tests.distill_helpers import purge_repos
    sf = session_factory(engine)
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        row = Learning(repo_id=repo.id, topic="Cleanup", hint_text="From before deployment")
        s.add(row)
        await s.commit()
    stop = asyncio.Event()
    async def embed(*args, **kwargs):
        stop.set()
        return [0.2] * 768
    monkeypatch.setattr(embedding_jobs, "embed_text", embed)
    try:
        await asyncio.wait_for(embedding_jobs.run_embedding_worker(
            sf, settings, stop, worker_id="dedicated-embedding"), timeout=10)
        async with sf() as s:
            assert (await s.get(Learning, row.id)).embedding is not None
    finally:
        await purge_repos(engine, [repo.id])


async def test_embedding_timeout_leaves_the_vector_pending(sf, settings, monkeypatch):
    monkeypatch.setattr(embedding_jobs, "EMBEDDING_TIMEOUT_S", 0.01)
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        row = Learning(repo_id=repo.id, topic="Cleanup", hint_text="Pending")
        s.add(row)
        await s.flush()
        job = await embedding_jobs.enqueue_embedding(s, row)
        await s.commit()
    async def embed(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(embedding_jobs, "embed_text", embed)
    with pytest.raises(TimeoutError):
        await embedding_jobs.run_embedding_job(sf, settings, job.payload)
    async with sf() as s:
        assert (await s.get(Learning, row.id)).embedding is None


async def test_custom_chat_worker_specs_still_start_and_stop_embedding_repair(engine, settings, monkeypatch):
    import importlib
    api = importlib.import_module("argus.api.app")
    started = asyncio.Event()
    stopped = asyncio.Event()
    async def embedding_worker(sf, base, stop, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async def chat_worker(sf, handlers, stop, **kwargs):
        await stop.wait()
    monkeypatch.setattr(embedding_jobs, "run_embedding_worker", embedding_worker)
    monkeypatch.setattr(api, "run_worker_forever", chat_worker)
    app = api.create_app(settings.model_copy(update={"worker_enabled": True,
        "worker_specs": '[{"id":"chat","kinds":["review"]}]'}), engine=engine)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=5)
        assert app.state.embedding_worker in app.state.workers
        assert len(app.state.workers) == 2
    await asyncio.gather(*app.state.workers, return_exceptions=True)
    assert stopped.is_set()


@pytest.fixture
def remote(monkeypatch, tmp_path):
    class Remote:
        sha = "b" * 40
        default_branch = "main"
        missing = False
        branches = []
        acquisitions = []
        releases = []
        fail_acquire = False
        paths = {"other.py"}
        def __init__(self, *args):
            pass
        async def get_project(self, project):
            return {"default_branch": self.default_branch}
        async def get_branch(self, project, ref):
            self.branches.append(ref)
            return None if self.missing else {"commit": {"id": self.sha}}
        async def aclose(self):
            pass
        async def acquire(self, sha):
            self.acquisitions.append(sha)
            if self.fail_acquire:
                raise RuntimeError("Fetch failed")
            return tmp_path
        async def release(self, sha):
            self.releases.append(sha)
    from argus.gitlab import client
    from argus.review import workspace
    from argus.knowledge import audit_pick
    monkeypatch.setattr(client, "GitLabClient", Remote)
    monkeypatch.setattr(workspace, "WorkspaceManager", Remote)
    monkeypatch.setattr(audit_pick, "changed_paths_since", lambda *args: Remote.paths)
    return Remote


async def _audited(s, now):
    repo, _, _ = await _mr_disc(s)
    repo.default_branch = "main"
    row = Learning(repo_id=repo.id, topic="Cleanup", hint_text="Use cleanup",
        audited_at_sha="a" * 40, last_audited_at=now, file_paths=["a.py"])
    s.add(row)
    await s.flush()
    return repo, row


async def test_branch_checks_skip_unchanged_worktrees_across_sessions(sf, settings, remote):
    now = datetime.now(timezone.utc)
    async with sf() as s:
        repo, row = await _audited(s, now)
        await audit_scheduler._schedule_repo(s, settings, repo.id, now)
        await s.commit()
    async with sf() as s:
        await audit_scheduler._schedule_repo(s, settings, repo.id, now)
        assert len(remote.acquisitions) == 1
        assert remote.branches == ["main", "main"]
        remote.sha = "c" * 40
        await audit_scheduler._schedule_repo(s, settings, repo.id, now)
        assert remote.acquisitions == ["b" * 40, "c" * 40]
        await s.commit()
    # Reaching the time deadline must bypass a valid same-tip negative cache.
    async with sf() as s:
        await audit_scheduler._schedule_repo(s, settings, repo.id, now + timedelta(days=8))
        assert (await s.execute(select(AuditRun).where(AuditRun.repo_id == repo.id))).scalar_one()


@pytest.mark.parametrize("change", ["paths", "baseline", "new_learning", "ref"])
async def test_unchanged_head_rechecks_when_audit_selection_inputs_change(db, settings, remote, change):
    now = datetime.now(timezone.utc)
    repo, row = await _audited(db, now)
    await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    if change == "paths":
        row.file_paths = ["other.py"]
    elif change == "baseline":
        row.audited_at_sha = "d" * 40
        remote.paths = {"a.py"}
    elif change == "new_learning":
        db.add(Learning(repo_id=repo.id, topic="New", hint_text="New learning",
            last_audited_at=now, audited_at_sha="d" * 40, file_paths=["other.py"]))
    else:
        repo.default_branch = "release"
        remote.paths = {"a.py"}
    await db.flush()
    await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    assert len(remote.acquisitions) == 2
    assert (await db.execute(select(AuditRun).where(AuditRun.repo_id == repo.id))).scalar_one()


async def test_same_audited_tip_needs_no_initial_checkout(db, settings, remote):
    now = datetime.now(timezone.utc)
    repo, row = await _audited(db, now)
    row.audited_at_sha = remote.sha
    await db.flush()
    await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    assert remote.acquisitions == []
    assert remote.releases == []
    assert await read_cursor(db, repo.id, "audit_cursor")


async def test_failed_fetch_does_not_advance_the_audit_cache(db, settings, remote):
    now = datetime.now(timezone.utc)
    repo, _ = await _audited(db, now)
    remote.fail_acquire = True
    with pytest.raises(RuntimeError, match="Fetch failed"):
        await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    assert await read_cursor(db, repo.id, "audit_cursor") is None
    remote.fail_acquire = False
    await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    assert len(remote.acquisitions) == 2


async def test_unavailable_branch_is_retried_and_head_uses_provider_default(db, settings, remote):
    now = datetime.now(timezone.utc)
    repo, _ = await _audited(db, now)
    repo.default_branch = None
    remote.missing = True
    await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    assert await read_cursor(db, repo.id, "audit_cursor") is None
    assert not remote.acquisitions
    remote.missing = False
    await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    assert remote.branches == ["main", "main"]
    assert len(remote.acquisitions) == 1


async def test_missing_diff_history_conservatively_schedules_an_audit(db, settings, remote):
    now = datetime.now(timezone.utc)
    repo, _ = await _audited(db, now)
    remote.paths = None
    await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    assert (await db.execute(select(AuditRun).where(AuditRun.repo_id == repo.id))).scalar_one()
    await audit_scheduler._schedule_repo(db, settings, repo.id, now)
    assert len(remote.acquisitions) == 1, "an existing audit suppresses repeated fetches"


async def test_sweep_is_bounded_and_resumes_past_ineligible_prefix(sf, monkeypatch):
    monkeypatch.setattr(distill_threads, "SWEEP_SCAN_LIMIT", 2)
    monkeypatch.setattr(distill_threads, "SWEEP_MR_LIMIT", 1)
    now = datetime.now(timezone.utc)
    async with sf() as s:
        repo, mr, disc = await _mr_disc(s)
        note = await _note(s, mr, disc, "human", "Still discussing", 0)
        note.note_created_at = now - timedelta(minutes=30)
        for iid in (2, 3):
            _, other, d = await _mr_disc(s)
            other.repo_id, other.mr_iid = repo.id, iid
            opener = await _note(s, other, d, "external_bot", "External review", 0)
            opener.note_created_at = now - timedelta(minutes=40)
            note = await _note(s, other, d, "human", "Reply", 0)
            note.note_created_at = now - timedelta(minutes=30)
            d.resolved = True
        _, ready, _ = await _mr_disc(s)
        ready.repo_id, ready.mr_iid = repo.id, 4
        ready_disc = await _resolved_bot_thread(s, ready)
        for note in (await s.execute(select(distill_threads.Note).where(
            distill_threads.Note.discussion_id == ready_disc.id))).scalars():
            note.note_created_at = now - timedelta(minutes=20)
        await s.commit()
    visits = []
    actual = distill_threads.enqueue_ready_threads
    async def counting(session, mr, *args, **kwargs):
        visits.append(mr.id)
        return await actual(session, mr, *args, **kwargs)
    monkeypatch.setattr(distill_threads, "enqueue_ready_threads", counting)
    async with sf() as s:
        loaded = await s.get(type(repo), repo.id)
        assert await distill_threads.sweep_pending_threads(s, loaded, now) == 0
        assert len(visits) == 2
        assert await read_cursor(s, repo.id, "thread_sweep_cursor")
        await s.commit()
    async with sf() as s:
        loaded = await s.get(type(repo), repo.id)
        assert await distill_threads.sweep_pending_threads(s, loaded, now) == 1
        assert len(visits) == 4 and len(set(visits)) == 4
        assert await read_cursor(s, repo.id, "thread_sweep_cursor") is None
        await s.commit()
        # A previously ineligible thread can become ready behind the cursor.
        old = await s.get(distill_threads.Discussion, disc.id)
        old.resolved = True
        await s.flush()
        assert await distill_threads.sweep_pending_threads(s, loaded, now) == 1


@pytest.mark.parametrize("cursor", [{"oldest": "broken", "mr_id": "broken"},
                                  {"oldest": "2026-10-01", "mr_id": str(uuid.uuid4())},
                                  {}, "bad type"])
async def test_sweep_recovers_from_bad_or_naive_cursors(db, monkeypatch, cursor):
    repo, mr, _ = await _mr_disc(db)
    await _resolved_bot_thread(db, mr)
    await write_cursor(db, repo.id, "thread_sweep_cursor", cursor)
    assert await distill_threads.sweep_pending_threads(db, repo, datetime.now(timezone.utc)) == 1
    assert await read_cursor(db, repo.id, "thread_sweep_cursor") is None


async def test_background_cursors_do_not_clobber_each_other_or_ingestion(sf):
    async with sf() as first, sf() as second:
        repo, _, _ = await _mr_disc(first)
        repo.poll_cursor = {"updated_after": "ingestion", "reconciled_at": "recent"}
        await first.commit()
        other = await second.get(type(repo), repo.id)
        await write_cursor(first, repo.id, "audit_cursor", {"sha": "first"})
        await first.commit()
        await write_cursor(second, repo.id, "thread_sweep_cursor", {"mr_id": "second"})
        other.poll_cursor = {**other.poll_cursor, "updated_after": "next"}
        await second.commit()
    async with sf() as s:
        assert await read_cursor(s, repo.id, "audit_cursor") == {"sha": "first"}
        assert await read_cursor(s, repo.id, "thread_sweep_cursor") == {"mr_id": "second"}
        assert (await s.get(type(repo), repo.id)).poll_cursor["updated_after"] == "next"

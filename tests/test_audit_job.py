import uuid

from sqlalchemy import select

from argus.domain.models import AuditRun, Job, Learning, Repository


async def _repo_with_due(db):
    repo = Repository(provider="gitlab", project_path=f"g/aj-{uuid.uuid4().hex[:6]}",
                      gitlab_project_id=int(uuid.uuid4().int % 10**8), enabled=True)
    db.add(repo)
    await db.flush()
    db.add(Learning(repo_id=repo.id, topic="t", hint_text="h", kind="guidance"))
    await db.flush()
    return repo


async def test_enqueue_audit_creates_a_queued_run_and_one_job(db):
    from argus.knowledge.audit_job import enqueue_audit
    repo = await _repo_with_due(db)
    run = await enqueue_audit(db, repo.id, trigger="manual")
    assert run.status == "queued" and run.job_id is not None
    job = await db.get(Job, run.job_id)
    assert (job.kind, job.payload["audit_run_id"]) == ("audit_repo", str(run.id))
    assert await enqueue_audit(db, repo.id, trigger="manual") is None


async def test_startup_reaper_leaves_a_run_whose_job_will_resume(db):
    from argus.knowledge.audit_job import enqueue_audit
    from argus.knowledge.audit_scheduler import reclaim_stale_audit_runs
    repo = await _repo_with_due(db)
    run = await enqueue_audit(db, repo.id, trigger="scheduled")
    run.status = "running"
    (await db.get(Job, run.job_id)).status = "queued"
    orphan = AuditRun(repo_id=repo.id, status="running")
    db.add(orphan)
    await db.flush()
    assert await reclaim_stale_audit_runs(db, stale_after_s=0) == 1
    assert run.status == "running" and orphan.status == "failed"


async def test_scheduler_enqueues_only_repos_with_due_learnings(db, engine, settings, monkeypatch):
    import asyncio
    from argus.db import session_factory
    from argus.knowledge.audit_scheduler import run_audit_forever
    from tests.distill_helpers import purge_repos
    sf = session_factory(engine)
    async with sf() as s:
        due_repo = await _repo_with_due(s)
        idle = Repository(provider="gitlab", project_path=f"g/aj-idle-{uuid.uuid4().hex[:6]}",
                          gitlab_project_id=int(uuid.uuid4().int % 10**8), enabled=True)
        s.add(idle)
        await s.commit()
    try:
        task = asyncio.create_task(run_audit_forever(
            sf, settings.model_copy(update={"audit_enabled": True}), sleep_seconds=999))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        async with sf() as s:
            runs = (await s.execute(select(AuditRun).where(
                AuditRun.repo_id.in_([due_repo.id, idle.id])))).scalars().all()
        assert [r.repo_id for r in runs] == [due_repo.id]
    finally:
        await purge_repos(engine, [due_repo.id, idle.id])


async def test_a_finished_runs_checkpoint_thread_is_deleted(settings):
    """A finished run's checkpoint thread can never resume anything again --
    it's pure disk weight from then on (~2MB/run measured), on a prod box
    that already has an unfixed checkpoint-bloat problem. run_audit_job calls
    this for every terminal outcome (done or failed)."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph
    from pydantic import BaseModel

    from argus.knowledge.audit_job import _delete_checkpoint

    class _S(BaseModel):
        x: int = 0

    run_id = uuid.uuid4()
    pg_url = settings.database_url.replace("+asyncpg", "")
    config = {"configurable": {"thread_id": f"audit:{run_id}"}}

    async with AsyncPostgresSaver.from_conn_string(pg_url) as cp:
        await cp.setup()
        g = StateGraph(_S)
        g.add_node("n", lambda s: {"x": 1})
        g.add_edge(START, "n")
        g.add_edge("n", END)
        compiled = g.compile(checkpointer=cp)
        await compiled.ainvoke(_S(), config=config)
        assert await cp.aget_tuple(config) is not None

    await _delete_checkpoint(pg_url, run_id)

    async with AsyncPostgresSaver.from_conn_string(pg_url) as cp:
        assert await cp.aget_tuple(config) is None


async def test_a_setup_failure_before_the_pipeline_starts_still_marks_the_run_failed(engine, settings):
    """resolve_audit_ref/distill_llm_config used to run before the try block,
    so a failure there (e.g. no default LLM endpoint configured) left the run
    stuck 'queued' forever -- enqueue_audit refuses a second trigger for that
    repo, and only a process restart's startup reaper would ever clear it."""
    from sqlalchemy import delete

    from argus.db import session_factory
    from argus.domain.models import LLMEndpoint
    from argus.knowledge.audit_job import run_audit_job
    from tests.distill_helpers import purge_repos

    from sqlalchemy import select, update

    sf = session_factory(engine)
    saved_default_ids = []
    async with sf() as s:
        # No default LLMEndpoint must exist, so distill_llm_config's
        # resolve_llm_config(session, None, None) raises ValueError before any
        # workspace/checkpoint work starts -- regardless of what sibling tests
        # in the shared session-scoped DB left behind.
        res = await s.execute(select(LLMEndpoint.id).where(LLMEndpoint.is_default == True))  # noqa: E712
        saved_default_ids = list(res.scalars().all())
        if saved_default_ids:
            await s.execute(
                update(LLMEndpoint)
                .where(LLMEndpoint.id.in_(saved_default_ids))
                .values(is_default=False)
            )
        repo = Repository(provider="gitlab", project_path=f"g/aj-setup-fail-{uuid.uuid4().hex[:6]}",
                          gitlab_project_id=int(uuid.uuid4().int % 10**8), enabled=True)
        s.add(repo)
        await s.flush()
        run = AuditRun(repo_id=repo.id, status="queued", trigger="manual")
        s.add(run)
        await s.commit()
        run_id, repo_id = run.id, repo.id

    try:
        await run_audit_job(sf, settings, {"audit_run_id": str(run_id)}, None)
        async with sf() as s:
            row = await s.get(AuditRun, run_id)
            assert row.status == "failed"
            assert row.error
    finally:
        if saved_default_ids:
            async with sf() as s:
                await s.execute(
                    update(LLMEndpoint)
                    .where(LLMEndpoint.id.in_(saved_default_ids))
                    .values(is_default=True)
                )
                await s.commit()
        await purge_repos(engine, [repo_id])

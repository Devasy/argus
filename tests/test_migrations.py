import asyncio
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from argus.config import get_settings
from argus.db import get_engine

MIGRATION_DB_NAME = "argus_migration_check"
ADMIN_URL = "postgresql+asyncpg://argus:argus@localhost:5433/argus"
MIGRATION_DB_URL = f"postgresql+asyncpg://argus:argus@localhost:5433/{MIGRATION_DB_NAME}"

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _drop_and_create() -> None:
    admin = get_engine(ADMIN_URL)
    async with admin.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB_NAME}"))
        await conn.execute(text(f"CREATE DATABASE {MIGRATION_DB_NAME}"))
    await admin.dispose()


async def _drop() -> None:
    admin = get_engine(ADMIN_URL)
    async with admin.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DB_NAME}"))
    await admin.dispose()


@pytest.fixture
def migration_db():
    """Create a throwaway database, dedicated to this test, and drop it afterward.

    Kept separate from the `argus_test` database (managed by the `engine`
    fixture in conftest.py) so this test can freely drop/recreate its own
    database without racing other tests that depend on `argus_test`.

    This fixture (and the test using it) run synchronously: Alembic's
    `command.upgrade` drives `migrations/env.py`, which calls
    `asyncio.run(...)` internally, and `asyncio.run` cannot be invoked from
    inside an already-running event loop (which an `async def` test/fixture
    under pytest-asyncio would have).
    """
    asyncio.run(_drop_and_create())
    try:
        yield MIGRATION_DB_URL
    finally:
        asyncio.run(_drop())


def test_alembic_upgrade_head_applies_cleanly(migration_db):
    """`alembic upgrade head` must apply the full migration chain to a fresh
    database without error. tests/conftest.py builds the test schema via
    `Base.metadata.create_all(...)` directly from the ORM models, which never
    exercises the actual migration chain -- so a green pytest run alone does
    not prove the migrations apply. This test closes that gap.
    """
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))

    # migrations/env.py resolves its URL via argus.config.get_settings(),
    # not directly from alembic.ini, and get_settings() is lru_cache'd. Point
    # it at the throwaway database via the same ARGUS_DATABASE_URL env var the
    # app normally uses, and clear the cache so the override takes effect.
    old_env = os.environ.get("ARGUS_DATABASE_URL")
    os.environ["ARGUS_DATABASE_URL"] = migration_db
    get_settings.cache_clear()
    try:
        command.upgrade(cfg, "head")
    finally:
        if old_env is None:
            os.environ.pop("ARGUS_DATABASE_URL", None)
        else:
            os.environ["ARGUS_DATABASE_URL"] = old_env
        get_settings.cache_clear()


def test_maintenance_state_upgrade_and_downgrade_preserve_ingestion_state(migration_db):
    import uuid
    repo_id = uuid.uuid4()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    old_env = os.environ.get("ARGUS_DATABASE_URL")
    os.environ["ARGUS_DATABASE_URL"] = migration_db
    get_settings.cache_clear()

    async def seed():
        engine = get_engine(migration_db)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO argus.repositories "
                    "(id, provider, project_path, gitlab_project_id, enabled, poll_interval_s, "
                    "stale_mr_after_days, learnings_cooldown_hours, auto_review_enabled, poll_cursor) "
                    "VALUES (:id, 'gitlab', 'test/maintenance', 123, true, 120, 30, 72, false, "
                    "'{\"updated_after\":\"preserve-me\"}'::jsonb)"), {"id": repo_id})
        finally:
            await engine.dispose()

    async def verify_and_write():
        engine = get_engine(migration_db)
        try:
            async with engine.begin() as conn:
                assert (await conn.execute(text("SELECT poll_cursor FROM argus.repositories "
                    "WHERE id = :id"), {"id": repo_id})).scalar_one() == {"updated_after": "preserve-me"}
                assert (await conn.execute(text("SELECT count(*) FROM "
                    "argus.repository_maintenance_state"))).scalar_one() == 0
                indexes = (await conn.execute(text("SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'argus'"))).scalars().all()
                assert {"ix_learnings_missing_embedding", "ix_jobs_embedding_learning"} <= set(indexes)
                await conn.execute(text("INSERT INTO argus.repository_maintenance_state "
                    "(repo_id, audit_cursor, thread_sweep_cursor) "
                    "VALUES (:id, '{\"sha\":\"checked\"}'::jsonb, '{\"mr_id\":\"resume\"}'::jsonb)"),
                    {"id": repo_id})
        finally:
            await engine.dispose()

    try:
        command.upgrade(cfg, "19ba6f87a231")
        asyncio.run(seed())
        command.upgrade(cfg, "head")
        asyncio.run(verify_and_write())
        command.downgrade(cfg, "19ba6f87a231")
        command.upgrade(cfg, "head")
        asyncio.run(verify_and_write())
    finally:
        if old_env is None:
            os.environ.pop("ARGUS_DATABASE_URL", None)
        else:
            os.environ["ARGUS_DATABASE_URL"] = old_env
        get_settings.cache_clear()


def test_decision_history_upgrade_preserves_existing_ledger_decisions(migration_db):
    from argus.db import session_factory
    from argus.domain.models import DistillationRun, DistillThread
    from tests.distill_helpers import _mr_disc

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    old_env = os.environ.get("ARGUS_DATABASE_URL")
    os.environ["ARGUS_DATABASE_URL"] = migration_db
    get_settings.cache_clear()

    async def seed():
        engine = get_engine(migration_db)
        try:
            async with session_factory(engine)() as s:
                _, mr, disc = await _mr_disc(s)
                run = DistillationRun(mr_id=mr.id, note_ids=[], status="done")
                s.add(run)
                await s.flush()
                s.add(DistillThread(mr_id=mr.id, discussion_id=disc.id,
                    distillation_run_id=run.id, content_hash="original-content",
                    thread_type="human_thread", status="done", reply_verdict="accepted",
                    decision_reason="Original decision", learning_ids=[]))
                for status in ("queued", "failed"):
                    _, other_mr, other_disc = await _mr_disc(s)
                    other_run = DistillationRun(mr_id=other_mr.id, note_ids=[], status="done")
                    s.add(other_run)
                    await s.flush()
                    s.add(DistillThread(mr_id=other_mr.id, discussion_id=other_disc.id,
                        distillation_run_id=other_run.id, content_hash=f"{status}-content",
                        thread_type="human_thread", status=status, learning_ids=[]))
                await s.commit()
                return str(run.id), str(disc.id)
        finally:
            await engine.dispose()

    async def verify(run_id, discussion_id):
        engine = get_engine(migration_db)
        try:
            async with engine.connect() as conn:
                row = (await conn.execute(text("SELECT distillation_run_id, discussion_id, "
                    "content_hash, decision FROM argus.distillation_decisions "
                    "WHERE distillation_run_id = :run_id"), {"run_id": run_id})).one()
                assert str(row.distillation_run_id) == run_id
                assert str(row.discussion_id) == discussion_id
                assert row.content_hash == "original-content"
                assert row.decision["reply_verdict"] == "accepted"
                assert row.decision["decision_reason"] == "Original decision"
                assert "thread" not in row.decision, "migration must not invent historical replies"
                statuses = (await conn.execute(text(
                    "SELECT decision->>'status' FROM argus.distillation_decisions"))).scalars().all()
                assert sorted(statuses) == ["done", "failed"]
        finally:
            await engine.dispose()

    try:
        command.upgrade(cfg, "b4f8d6f470df")
        identifiers = asyncio.run(seed())
        command.upgrade(cfg, "head")
        asyncio.run(verify(*identifiers))
        command.downgrade(cfg, "b4f8d6f470df")
        command.upgrade(cfg, "head")
        asyncio.run(verify(*identifiers))
    finally:
        if old_env is None:
            os.environ.pop("ARGUS_DATABASE_URL", None)
        else:
            os.environ["ARGUS_DATABASE_URL"] = old_env
        get_settings.cache_clear()

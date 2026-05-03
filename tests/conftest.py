import asyncio
import os

import pytest
from sqlalchemy import text

from argus.config import Settings
from argus.db import get_engine, session_factory

TEST_URL = "postgresql+asyncpg://argus:argus@localhost:5433/argus_test"


@pytest.fixture(autouse=True)
def _clean_secret_env_vars(monkeypatch):
    """Clear secret env vars for tests so Settings defaults to empty strings."""
    monkeypatch.setenv("ARGUS_DISTILLER_API_KEY", "")
    monkeypatch.setenv("ARGUS_API_TOKEN", "")
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "")
    monkeypatch.setenv("ARGUS_LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("ARGUS_LANGFUSE_SECRET_KEY", "")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(database_url=TEST_URL, gitlab_url="https://gitlab.test", gitlab_token="t")


@pytest.fixture(scope="session")
async def engine(settings):
    admin = get_engine(TEST_URL.rsplit("/", 1)[0] + "/argus")
    async with admin.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("DROP DATABASE IF EXISTS argus_test"))
        await conn.execute(text("CREATE DATABASE argus_test"))
    await admin.dispose()

    eng = get_engine(TEST_URL)
    from argus.domain.models import Base  # imported late: models created in Task 2
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS argus"))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine):
    async with session_factory(engine)() as session:
        yield session
        await session.rollback()

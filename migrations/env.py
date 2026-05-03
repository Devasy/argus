import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from argus.config import get_settings
from argus.domain.models import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silently disable
    # every logger not explicitly listed in alembic.ini's [loggers] section
    # (e.g. argus.* loggers) for the rest of the process -- breaking
    # caplog-based assertions in any test that runs after a real migration
    # in the same pytest session.
    fileConfig(config.config_file_name, disable_existing_loggers=False)
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    # Every application table lives in the "argus" schema. Without this
    # filter, include_schemas=True makes Alembic enumerate ALL schemas in the
    # database (e.g. "public"), which spuriously reports our schema-qualified
    # objects as removed/added (schema=None vs schema="argus" mismatches)
    # and flags the version table as "removed" because it isn't in "public".
    if type_ == "schema":
        return name in (None, "argus")
    return True


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name == "alembic_version":
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"),
                      target_metadata=target_metadata,
                      version_table_schema="argus",
                      include_schemas=True,
                      include_name=include_name,
                      include_object=include_object)
    with context.begin_transaction():
        context.execute("CREATE EXTENSION IF NOT EXISTS vector")
        context.execute("CREATE SCHEMA IF NOT EXISTS argus")
        context.run_migrations()


def _run_sync(connection) -> None:
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS argus"))
    context.configure(connection=connection, target_metadata=target_metadata,
                      version_table_schema="argus", include_schemas=True,
                      include_name=include_name, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(_run_sync)
        await connection.commit()
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

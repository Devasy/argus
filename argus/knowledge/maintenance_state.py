"""Transaction-bound cursor updates that cannot clobber another background task."""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from argus.domain.models import RepositoryMaintenanceState


async def read_cursor(session, repo_id, name):
    column = getattr(RepositoryMaintenanceState, name)
    return (await session.execute(select(column).where(
        RepositoryMaintenanceState.repo_id == repo_id))).scalar_one_or_none()


async def write_cursor(session, repo_id, name, value):
    # Update only this column, even if audit and ingestion loaded stale state.
    await session.execute(insert(RepositoryMaintenanceState)
        .values(repo_id=repo_id, **{name: value})
        .on_conflict_do_update(index_elements=[RepositoryMaintenanceState.repo_id],
                               set_={name: value}))

"""Restrict re-reviews of an MR to files changed since the last done review."""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import MRVersion, Review
from argus.review.artifacts import FileChange

logger = logging.getLogger("argus.incremental")


async def last_reviewed_version(session: AsyncSession, mr_id) -> MRVersion | None:
    review = (await session.execute(
        select(Review).where(Review.mr_id == mr_id, Review.status == "done",
                             Review.mr_version_id.isnot(None))
        .order_by(Review.finished_at.desc()).limit(1))).scalar_one_or_none()
    if review is None:
        return None
    return await session.get(MRVersion, review.mr_version_id)


async def changed_paths_since(client, project_id: int, mr_iid: int,
                              old_head_sha: str, new_head_sha: str) -> set[str] | None:
    try:
        cmp = await client.compare(project_id, old_head_sha, new_head_sha)
        return {d["new_path"] for d in cmp.get("diffs") or []}
    except Exception as e:
        logger.warning("compare failed (%s); falling back to full review", e)
        return None


def restrict_files(files: list[FileChange], changed: set[str]) -> list[FileChange]:
    kept = [f for f in files if f.path in changed]
    return kept or files

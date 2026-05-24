"""Query helpers for the reviews list page (GET /reviews and friends)."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import (Job, MergeRequest, ProfileVersion,
                                     Repository, Review, ReviewerProfile)

VALID_REVIEW_STATUSES = {"queued", "running", "done", "failed", "canceled"}


async def average_done_duration_seconds(session: AsyncSession, limit: int = 20) -> float | None:
    rows = (await session.execute(
        select(Review.started_at, Review.finished_at)
        .where(Review.status == "done", Review.started_at.isnot(None),
              Review.finished_at.isnot(None))
        .order_by(Review.finished_at.desc())
        .limit(limit))).all()
    if not rows:
        return None
    durations = [(finished - started).total_seconds() for started, finished in rows]
    return sum(durations) / len(durations)


async def queued_job_positions(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Maps review_id -> 1-based position among queued Job rows, ordered the
    same way the worker claims them (argus/jobs/queue.py claim_next:
    status='queued', order by created_at)."""
    rows = (await session.execute(
        select(Job.payload)
        .where(Job.kind == "review", Job.status == "queued")
        .order_by(Job.created_at))).scalars().all()
    positions: dict[uuid.UUID, int] = {}
    for i, payload in enumerate(rows, start=1):
        review_id = payload.get("review_id")
        if review_id:
            positions[uuid.UUID(review_id)] = i
    return positions


async def list_reviews(session: AsyncSession, *, status: str | None = None,
                       repo_id: uuid.UUID | None = None,
                       profile_id: uuid.UUID | None = None,
                       page: int = 1, per_page: int = 50
                       ) -> tuple[list[dict], int]:
    filters = []
    if status is not None:
        filters.append(Review.status == status)
    if repo_id is not None:
        filters.append(Repository.id == repo_id)
    if profile_id is not None:
        filters.append(ProfileVersion.profile_id == profile_id)

    base_query = (
        select(Review, MergeRequest, Repository, ProfileVersion, ReviewerProfile)
        .join(MergeRequest, Review.mr_id == MergeRequest.id)
        .join(Repository, MergeRequest.repo_id == Repository.id)
        .outerjoin(ProfileVersion, Review.profile_version_id == ProfileVersion.id)
        .outerjoin(ReviewerProfile, ProfileVersion.profile_id == ReviewerProfile.id)
        .where(*filters))

    total = (await session.execute(
        select(func.count()).select_from(base_query.subquery()))).scalar_one()
    rows = (await session.execute(
        base_query.order_by(Review.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page))).all()

    positions = await queued_job_positions(session)
    avg_duration = await average_done_duration_seconds(session)

    items = []
    for review, mr, repo, profile_version, profile in rows:
        queue_position = positions.get(review.id) if review.status == "queued" else None
        eta_seconds = (queue_position * avg_duration
                      if queue_position is not None and avg_duration is not None
                      else None)
        items.append({
            "id": review.id, "mr_id": mr.id, "mr_iid": mr.mr_iid,
            "mr_title": mr.title, "repo_id": repo.id,
            "repo_project_path": repo.project_path,
            "profile_name": profile.name if profile else None,
            "trigger": review.trigger, "status": review.status,
            "started_at": review.started_at, "finished_at": review.finished_at,
            "created_at": review.created_at,
            "prompt_tokens": review.prompt_tokens,
            "completion_tokens": review.completion_tokens,
            "queue_position": queue_position, "eta_seconds": eta_seconds,
            "publish": review.publish,
        })
    return items, total

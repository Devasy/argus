"""Per-agent-version acceptance rate: accepted / (accepted + rejected) over
inline bot notes from reviews that ran this specific agent version."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import MergeRequest, Note, Review, ReviewReviewerAgentVersion

ACCEPTED = ("accepted", "accepted_manually")
REJECTED = ("rejected_with_rationale",)


async def compute_agent_version_acceptance(session: AsyncSession, version_id) -> dict:
    accepted = (await session.execute(
        select(func.count(func.distinct(Note.id)))
        .join(MergeRequest, MergeRequest.id == Note.mr_id)
        .join(Review, Review.mr_id == MergeRequest.id)
        .join(ReviewReviewerAgentVersion,
              ReviewReviewerAgentVersion.review_id == Review.id)
        .where(ReviewReviewerAgentVersion.agent_version_id == version_id,
               Note.kind == "inline", Note.author_type == "bot",
               Note.disposition.in_(ACCEPTED))
    )).scalar_one()
    rejected = (await session.execute(
        select(func.count(func.distinct(Note.id)))
        .join(MergeRequest, MergeRequest.id == Note.mr_id)
        .join(Review, Review.mr_id == MergeRequest.id)
        .join(ReviewReviewerAgentVersion,
              ReviewReviewerAgentVersion.review_id == Review.id)
        .where(ReviewReviewerAgentVersion.agent_version_id == version_id,
               Note.kind == "inline", Note.author_type == "bot",
               Note.disposition.in_(REJECTED))
    )).scalar_one()
    total = accepted + rejected
    return {"accepted": accepted, "rejected": rejected,
            "acceptance_rate": (accepted / total) if total else None}

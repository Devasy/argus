"""Per-agent-version acceptance rate: accepted / (accepted + rejected) over
the inline bot notes that agent itself wrote (Finding.stage == agent name),
in reviews that ran this specific version."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import (Finding, Note, ReviewerAgent, ReviewerAgentVersion,
                                     ReviewReviewerAgentVersion)

ACCEPTED = ("accepted", "accepted_manually", "accepted_by_followup")
REJECTED = ("rejected_with_rationale",)


def agent_notes_query(agent_name: str, version_ids):
    """Distinct-count of the inline bot notes `agent_name` wrote in reviews that ran one of `version_ids`."""
    ran = select(ReviewReviewerAgentVersion.review_id).where(
        ReviewReviewerAgentVersion.agent_version_id.in_(version_ids))
    return (select(func.count(func.distinct(Note.id)))
            .join(Finding, Finding.published_note_id == Note.id)
            .where(Finding.stage == agent_name, Finding.review_id.in_(ran),
                   Note.kind == "inline", Note.author_type == "bot"))


async def compute_agent_version_acceptance(session: AsyncSession, version_id) -> dict:
    name = (await session.execute(
        select(ReviewerAgent.name).join(ReviewerAgentVersion,
                                        ReviewerAgentVersion.agent_id == ReviewerAgent.id)
        .where(ReviewerAgentVersion.id == version_id))).scalar_one()
    q = agent_notes_query(name, [version_id])
    accepted = (await session.execute(q.where(Note.disposition.in_(ACCEPTED)))).scalar_one()
    rejected = (await session.execute(q.where(Note.disposition.in_(REJECTED)))).scalar_one()
    total = accepted + rejected
    return {"accepted": accepted, "rejected": rejected,
            "acceptance_rate": (accepted / total) if total else None}

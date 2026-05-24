"""Read-only aggregates for the dashboard home page."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import (DistillationRun, Feedback, Learning,
                                     MergeRequest, Note, Repository, Review,
                                     ReviewerAgent, ReviewerAgentVersion,
                                     ReviewReviewerAgentVersion)

ACCEPTED = ("accepted", "accepted_manually")
REJECTED = ("rejected_with_rationale",)
OPENISH = ("open", "dismissed_ambiguous")


async def compute_dashboard_stats(session: AsyncSession, days: int) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    bot_notes = [Note.author_type == "bot", Note.kind == "inline",
                 Note.note_created_at >= cutoff]

    # publish=False is a dry run (e.g. a golden-set benchmark pass): it never
    # posts a comment, so it must not count as production review activity.
    mrs_reviewed = (await session.execute(
        select(func.count(func.distinct(Review.mr_id)))
        .where(Review.status == "done", Review.finished_at >= cutoff,
              Review.publish.is_(True))
    )).scalar_one()
    agent_comments = (await session.execute(
        select(func.count(Note.id)).where(*bot_notes))).scalar_one()
    bot_discussions = select(Note.discussion_id).where(
        Note.author_type == "bot", Note.discussion_id.isnot(None))
    human_replies = (await session.execute(
        select(func.count(Note.id)).where(
            Note.author_type == "human", Note.kind == "inline",
            Note.note_created_at >= cutoff,
            Note.discussion_id.in_(bot_discussions)))).scalar_one()
    accepted = (await session.execute(
        select(func.count(Note.id)).where(
            *bot_notes, Note.disposition.in_(ACCEPTED)))).scalar_one()
    rejected = (await session.execute(
        select(func.count(Note.id)).where(
            *bot_notes, Note.disposition.in_(REJECTED)))).scalar_one()
    active_learnings = (await session.execute(
        select(func.count(Learning.id)).where(
            Learning.status == "active"))).scalar_one()
    pending_distillation = (await session.execute(
        select(func.count(DistillationRun.id)).where(
            DistillationRun.status.in_(["queued", "running"])))).scalar_one()
    tok = (await session.execute(
        select(func.coalesce(func.sum(Review.prompt_tokens), 0),
               func.coalesce(func.sum(Review.completion_tokens), 0))
        .where(Review.created_at >= cutoff, Review.publish.is_(True)))).one()
    dtok = (await session.execute(
        select(func.coalesce(func.sum(DistillationRun.prompt_tokens), 0),
               func.coalesce(func.sum(DistillationRun.completion_tokens), 0))
        .where(DistillationRun.created_at >= cutoff))).one()

    per_day_rows = (await session.execute(
        select(func.date(Review.created_at).label("d"),
               func.count(Review.id))
        .where(Review.created_at >= cutoff, Review.publish.is_(True))
        .group_by("d").order_by("d"))).all()

    # ---- per-agent breakdown (direct note→review attribution) -----------
    agent_rows = (await session.execute(
        select(ReviewerAgent.id, ReviewerAgent.name,
               func.max(ReviewerAgentVersion.version))
        .join(ReviewerAgentVersion,
              ReviewerAgentVersion.agent_id == ReviewerAgent.id, isouter=True)
        .group_by(ReviewerAgent.id, ReviewerAgent.name))).all()
    agents = []
    for agent_id, name, cur_ver in agent_rows:
        version_ids = select(ReviewerAgentVersion.id).where(
            ReviewerAgentVersion.agent_id == agent_id)
        review_ids = select(ReviewReviewerAgentVersion.review_id).where(
            ReviewReviewerAgentVersion.agent_version_id.in_(version_ids))

        async def _n(dispositions=None):
            # Notes carrying review_id count only toward that review's
            # agents (direct attribution); legacy notes (review_id IS NULL,
            # predating that column) fall back to the MR-wide join used by
            # argus.knowledge.acceptance, so historical data isn't
            # silently invisible in the per-agent breakdown.
            q = (select(func.count(func.distinct(Note.id)))
                 .join(MergeRequest, MergeRequest.id == Note.mr_id)
                 .join(Review, Review.mr_id == MergeRequest.id)
                 .where(Note.author_type == "bot", Note.kind == "inline",
                        Note.note_created_at >= cutoff,
                        Review.id.in_(review_ids),
                        or_(Note.review_id.is_(None),
                            Note.review_id == Review.id)))
            if dispositions:
                q = q.where(Note.disposition.in_(dispositions))
            return (await session.execute(q)).scalar_one()

        comments = await _n()
        a = await _n(ACCEPTED)
        r = await _n(REJECTED)
        o = await _n(OPENISH)
        agents.append({"agent_id": agent_id, "name": name,
                       "current_version": cur_ver,
                       "comments": comments, "accepted": a, "rejected": r,
                       "open": o,
                       "hit_rate": (a / (a + r)) if (a + r) else None})
    agents.sort(key=lambda x: x["comments"], reverse=True)

    # ---- recent activity --------------------------------------------------
    activity: list[dict] = []
    recent_reviews = (await session.execute(
        select(Review, MergeRequest.mr_iid, Repository.project_path)
        .join(MergeRequest, MergeRequest.id == Review.mr_id)
        .join(Repository, Repository.id == MergeRequest.repo_id, isouter=True)
        .order_by(Review.created_at.desc()).limit(10))).all()
    for rev, iid, proj in recent_reviews:
        kind = {"running": "review_running", "queued": "review_running",
                "failed": "review_failed"}.get(rev.status, "review_done")
        title = f"Review {rev.status} on {proj or '?'} !{iid}"
        if not rev.publish:
            title += " (benchmark)"
        activity.append({"type": kind, "title": title,
                         "detail": rev.error, "at": rev.created_at,
                         "href_id": rev.id})
    recent_verdicts = (await session.execute(
        select(Feedback, Note.body, Note.mr_id)
        .join(Note, Note.id == Feedback.note_id)
        .where(Feedback.kind.in_(["resolved", "ui_answer"]))
        .order_by(Feedback.created_at.desc()).limit(10))).all()
    for fb, body, mr_id in recent_verdicts:
        dispo = (fb.payload or {}).get("disposition", "?")
        who = "human verdict" if fb.kind == "ui_answer" else "verdict"
        activity.append({"type": "verdict", "title": f"{who}: {dispo}",
                         "detail": body[:120], "at": fb.created_at,
                         "href_id": mr_id})
    recent_learnings = (await session.execute(
        select(Learning).order_by(Learning.created_at.desc()).limit(10)
    )).scalars().all()
    for l in recent_learnings:
        activity.append({"type": "learning",
                         "title": f"Learning: {l.topic}",
                         "detail": l.hint_text[:120], "at": l.created_at,
                         "href_id": l.mr_id})
    recent_runs = (await session.execute(
        select(DistillationRun).order_by(
            DistillationRun.created_at.desc()).limit(10))).scalars().all()
    for run in recent_runs:
        activity.append({"type": "distillation",
                         "title": f"Learning run {run.status}",
                         "detail": run.error, "at": run.created_at,
                         "href_id": run.id})
    activity.sort(key=lambda x: x["at"], reverse=True)
    activity = activity[:12]

    total = accepted + rejected
    return {
        "window_days": days,
        "tiles": {"mrs_reviewed": mrs_reviewed,
                  "agent_comments": agent_comments,
                  "human_replies": human_replies,
                  "accepted": accepted, "rejected": rejected,
                  "acceptance_rate": (accepted / total) if total else None,
                  "active_learnings": active_learnings,
                  "pending_distillation": pending_distillation,
                  "prompt_tokens": tok[0] + dtok[0],
                  "completion_tokens": tok[1] + dtok[1]},
        "reviews_per_day": [{"date": str(d), "count": c}
                            for d, c in per_day_rows],
        "agents": agents,
        "activity": activity,
    }

"""Read-only aggregates for the dashboard home page."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from argus.domain.models import (Actor, AuditRun, Discussion,
                                     DistillationRun, Feedback, Job, Learning,
                                     MergeRequest, Note, Repository, Review,
                                     ReviewerAgent, ReviewerAgentVersion)
from argus.knowledge.acceptance import agent_notes_query

ACCEPTED = ("accepted", "accepted_manually", "accepted_by_followup")
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
    accepted_by_followup = (await session.execute(
        select(func.count(Note.id)).where(
            *bot_notes, Note.disposition == "accepted_by_followup"))).scalar_one()
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

    # ---- per-agent breakdown (notes each agent wrote, via Finding.stage) --
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

        async def _n(dispositions=None):
            q = agent_notes_query(name, version_ids).where(Note.note_created_at >= cutoff)
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
                  "accepted_by_followup": accepted_by_followup,
                  "acceptance_rate": (accepted / total) if total else None,
                  "active_learnings": active_learnings,
                  "pending_distillation": pending_distillation,
                  "prompt_tokens": tok[0] + dtok[0],
                  "completion_tokens": tok[1] + dtok[1]},
        "ops": await compute_ops_stats(session, cutoff),
        "reviews_per_day": [{"date": str(d), "count": c}
                            for d, c in per_day_rows],
        "agents": agents,
        "activity": activity,
    }


async def compute_ops_stats(session: AsyncSession, cutoff: datetime) -> dict:
    """Queue depth (right now) and in-window failure counts, so stuck
    pipelines show up here instead of needing `docker logs`."""
    job_rows = (await session.execute(
        select(Job.kind, Job.status, func.count())
        .where(Job.status.in_(["queued", "running"]))
        .group_by(Job.kind, Job.status))).all()
    jobs_by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"queued": 0, "running": 0})
    for kind, status, count in job_rows:
        jobs_by_kind[kind][status] = count

    async def _count(model, **where):
        conds = [getattr(model, k) == v for k, v in where.items()]
        return (await session.execute(
            select(func.count()).select_from(model).where(*conds))).scalar_one()

    return {
        "jobs_by_kind": [{"kind": k, "queued": v["queued"], "running": v["running"]}
                        for k, v in sorted(jobs_by_kind.items())],
        "reviews_queued": await _count(Review, status="queued"),
        "reviews_running": await _count(Review, status="running"),
        "reviews_failed": (await session.execute(
            select(func.count(Review.id)).where(
                Review.status == "failed", Review.created_at >= cutoff))).scalar_one(),
        "distillation_queued": await _count(DistillationRun, status="queued"),
        "distillation_running": await _count(DistillationRun, status="running"),
        "distillation_failed": (await session.execute(
            select(func.count(DistillationRun.id)).where(
                DistillationRun.status == "failed",
                DistillationRun.created_at >= cutoff))).scalar_one(),
        "audits_running": await _count(AuditRun, status="running"),
        "audits_failed": (await session.execute(
            select(func.count(AuditRun.id)).where(
                AuditRun.status == "failed", AuditRun.created_at >= cutoff))).scalar_one(),
    }


def _duration_stats(seconds: list[float]) -> dict:
    if not seconds:
        return {"avg_s": None, "p50_s": None, "p95_s": None, "sample_size": 0}
    ordered = sorted(seconds)

    def _pct(p: float) -> float:
        idx = min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))
        return ordered[idx]

    return {"avg_s": sum(ordered) / len(ordered), "p50_s": _pct(0.5),
            "p95_s": _pct(0.95), "sample_size": len(ordered)}


async def _stage_durations(session: AsyncSession, model, limit: int = 200) -> list[float]:
    rows = (await session.execute(
        select(model.started_at, model.finished_at)
        .where(model.status == "done", model.started_at.isnot(None),
              model.finished_at.isnot(None))
        .order_by(model.finished_at.desc())
        .limit(limit))).all()
    return [(finished - started).total_seconds() for started, finished in rows]


async def compute_tat_stats(session: AsyncSession, limit: int = 200) -> dict:
    """Turnaround time: how long each pipeline stage actually takes, end to
    end, so this is answerable without reading Langfuse traces by hand."""
    review_durations = await _stage_durations(session, Review, limit)
    distillation_durations = await _stage_durations(session, DistillationRun, limit)
    audit_durations = await _stage_durations(session, AuditRun, limit)

    first_bot = (
        select(Note.mr_id, func.min(Note.note_created_at).label("first_at"))
        .where(Note.author_type == "bot", Note.kind == "inline",
              Note.note_created_at.isnot(None))
        .group_by(Note.mr_id).subquery())
    rows = (await session.execute(
        select(MergeRequest.mr_created_at, first_bot.c.first_at)
        .join(first_bot, first_bot.c.mr_id == MergeRequest.id)
        .where(MergeRequest.mr_created_at.isnot(None))
        .order_by(first_bot.c.first_at.desc())
        .limit(limit))).all()
    first_comment_durations = [(f - c).total_seconds() for c, f in rows if f >= c]

    return {
        "review": _duration_stats(review_durations),
        "distillation": _duration_stats(distillation_durations),
        "audit": _duration_stats(audit_durations),
        "time_to_first_bot_comment": _duration_stats(first_comment_durations),
    }


async def _human_reviewer_note_rows(session: AsyncSession, cutoff: datetime | None = None):
    """One row per human inline note: (reviewer_actor_id, pr_author_actor_id,
    discussion_resolved, author_reply_count_in_discussion).

    `author_reply_count_in_discussion` counts notes from the MR's own author
    in the same discussion, excluding the reviewer's own note -- used to tell
    "rejected" (author pushed back, discussion never resolved) apart from
    "ignored" (nobody engaged at all) since there is no explicit disposition
    for human comments (only bot notes get classified, in
    ingest/reconciler.py).

    `cutoff`, when given, filters which of the reviewer's own notes count
    (not the replies to them -- a reply can land after the window even
    though the comment it answers is inside it).
    """
    AuthorReply = aliased(Note)
    reply_count = (
        select(func.count())
        .select_from(AuthorReply)
        .where(AuthorReply.discussion_id == Note.discussion_id,
              AuthorReply.author_id == MergeRequest.author_id,
              AuthorReply.id != Note.id)
        .correlate(Note, MergeRequest)
        .scalar_subquery())
    conds = [Note.author_type == "human", Note.kind == "inline",
            Note.author_id.isnot(None), MergeRequest.author_id.isnot(None),
            # Excludes the PR author's own replies on their own PR: those
            # are the author's remarks, not review feedback someone else
            # gave that could be "resolved/rejected/ignored".
            Note.author_id != MergeRequest.author_id]
    if cutoff is not None:
        conds.append(Note.note_created_at >= cutoff)
    rows = (await session.execute(
        select(Note.author_id, MergeRequest.author_id, Discussion.resolved,
              reply_count)
        .join(MergeRequest, MergeRequest.id == Note.mr_id)
        .outerjoin(Discussion, Discussion.id == Note.discussion_id)
        .where(*conds)
    )).all()
    return rows


def _classify_reviewer_row(resolved: bool | None, reply_count: int) -> str:
    if resolved:
        return "resolved"
    if reply_count > 0:
        return "rejected"
    return "ignored"


async def compute_user_stats(session: AsyncSession, days: int | None = None) -> dict:
    """Per-developer stats: how their own PRs fared under bot review, and how
    their own review comments landed on other people's PRs. `days` windows
    every count by when the PR/comment itself was created; omit it for
    all-time."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None

    actor_rows = (await session.execute(
        select(Actor.id, Actor.username, Actor.display_name)
        .where(Actor.is_bot.is_(False)))).all()
    actors = {aid: {"username": u, "display_name": d} for aid, u, d in actor_rows}

    pr_count_conds = [MergeRequest.author_id.isnot(None)]
    if cutoff is not None:
        pr_count_conds.append(MergeRequest.mr_created_at >= cutoff)
    pr_counts: dict = defaultdict(int)
    for author_id, count in (await session.execute(
            select(MergeRequest.author_id, func.count(func.distinct(MergeRequest.id)))
            .where(*pr_count_conds)
            .group_by(MergeRequest.author_id))).all():
        pr_counts[author_id] = count

    author_note_conds = [Note.author_type == "bot", Note.kind == "inline",
                         MergeRequest.author_id.isnot(None)]
    if cutoff is not None:
        author_note_conds.append(Note.note_created_at >= cutoff)
    author_stats: dict = defaultdict(lambda: {"accepted": 0, "rejected": 0, "open": 0})
    for author_id, disposition, count in (await session.execute(
            select(MergeRequest.author_id, Note.disposition, func.count())
            .join(Note, Note.mr_id == MergeRequest.id)
            .where(*author_note_conds)
            .group_by(MergeRequest.author_id, Note.disposition))).all():
        bucket = ("accepted" if disposition in ACCEPTED else
                  "rejected" if disposition in REJECTED else "open")
        author_stats[author_id][bucket] += count

    reviewer_stats: dict = defaultdict(lambda: {"comments": 0, "resolved": 0,
                                                "rejected": 0, "ignored": 0})
    for reviewer_id, _author_id, resolved, reply_count in await _human_reviewer_note_rows(
            session, cutoff):
        s = reviewer_stats[reviewer_id]
        s["comments"] += 1
        s[_classify_reviewer_row(resolved, reply_count)] += 1

    involved = set(pr_counts) | set(author_stats) | set(reviewer_stats)
    items = []
    for actor_id in involved:
        info = actors.get(actor_id)
        if info is None:
            continue
        items.append({
            "actor_id": actor_id, "username": info["username"],
            "display_name": info["display_name"],
            "prs_authored": pr_counts.get(actor_id, 0),
            "author_stats": author_stats.get(
                actor_id, {"accepted": 0, "rejected": 0, "open": 0}),
            "reviewer_stats": reviewer_stats.get(
                actor_id, {"comments": 0, "resolved": 0, "rejected": 0, "ignored": 0}),
        })
    items.sort(key=lambda x: x["prs_authored"] + x["reviewer_stats"]["comments"],
              reverse=True)
    return {"window_days": days, "items": items}


async def compute_comment_source_stats(session: AsyncSession, days: int | None = None) -> dict:
    """How PR authors treat a comment depending on who wrote it: bot (MM)
    vs. human reviewer, using the same resolved/rejected/ignored buckets for
    both so the two are directly comparable. Bot notes use their own
    `disposition` (set by ingest/reconciler.py's classifier); human notes use
    the resolved/reply heuristic in _human_reviewer_note_rows, since they
    have no disposition of their own."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None

    bot_conds = [Note.author_type == "bot", Note.kind == "inline"]
    if cutoff is not None:
        bot_conds.append(Note.note_created_at >= cutoff)
    bot_counts = {"resolved": 0, "rejected": 0, "ignored": 0}
    for disposition, count in (await session.execute(
            select(Note.disposition, func.count()).where(*bot_conds)
            .group_by(Note.disposition))).all():
        bucket = ("resolved" if disposition in ACCEPTED else
                  "rejected" if disposition in REJECTED else "ignored")
        bot_counts[bucket] += count

    human_counts = {"resolved": 0, "rejected": 0, "ignored": 0}
    for _reviewer_id, _author_id, resolved, reply_count in await _human_reviewer_note_rows(
            session, cutoff):
        human_counts[_classify_reviewer_row(resolved, reply_count)] += 1

    return {"window_days": days, "bot": bot_counts, "human": human_counts}


async def compute_reviewer_graph(session: AsyncSession) -> dict:
    """Who reviews whom: one edge per (reviewer, PR author) pair with
    comment volume and the resolved/rejected/ignored breakdown."""
    edges: dict = defaultdict(lambda: {"comments": 0, "resolved": 0,
                                       "rejected": 0, "ignored": 0})
    node_ids: set = set()
    for reviewer_id, author_id, resolved, reply_count in await _human_reviewer_note_rows(session):
        if reviewer_id == author_id:
            continue
        e = edges[(reviewer_id, author_id)]
        e["comments"] += 1
        e[_classify_reviewer_row(resolved, reply_count)] += 1
        node_ids.add(reviewer_id)
        node_ids.add(author_id)

    if not node_ids:
        return {"nodes": [], "edges": []}

    actor_rows = (await session.execute(
        select(Actor.id, Actor.username, Actor.display_name)
        .where(Actor.id.in_(node_ids)))).all()
    nodes = [{"actor_id": aid, "username": u, "display_name": d}
             for aid, u, d in actor_rows]
    edge_list = [{"reviewer_id": r, "author_id": a, **counts}
                for (r, a), counts in edges.items()]
    return {"nodes": nodes, "edges": edge_list}

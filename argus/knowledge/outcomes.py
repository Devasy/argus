"""Deferred resolution of learning injection outcomes.

Why this is not done at the end of the review job: `Note.disposition` is set
asynchronously by the reconciler (`argus/ingest/reconciler.py`) whenever a
human later resolves, replies to, or applies a bot comment — typically minutes
to days after the review finished. The old
`learnings.resolve_injection_outcomes` ran inside `execute_review_job` and so
could only ever see `disposition='open'`; it assigned one review-wide verdict to
every injected learning and could never produce a `hit`. This module runs after
reconciliation instead, and attributes each verdict to the specific learning
that drove the commented finding.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import (Finding, InjectionEvent, Learning, Note,
                                     Review, ReviewStage)
from argus.knowledge.acceptance import ACCEPTED, REJECTED

logger = logging.getLogger("argus.outcomes")

# Dispositions that represent a settled human judgement. Anything else
# ('open', 'answered', ...) means the human has not decided yet, so the
# injection stays pending and is retried on a later pass.
_TERMINAL = set(ACCEPTED) | set(REJECTED) | {"dismissed_ambiguous",
                                             "resolved_no_answer",
                                             "replied_unclassified"}

_COUNTER = {"hit": "hit_count", "harmful": "harmful_count",
            "ignored": "ignored_count", "miss": "miss_count",
            "inconclusive": "inconclusive_count"}

# Bounded so a finding that never publishes cannot pin injections pending forever.
PUBLICATION_WAIT_S = 48 * 3600


def _awaiting_publication(findings: list[Finding]) -> bool:
    """True when some finding is still expected to produce an inline note."""
    return any(f.verdict_valid and not f.outside_diff
               and f.published_note_id is None for f in findings)


async def _applied_suppression_ids(session: AsyncSession,
                                   review_id: uuid.UUID) -> set[str]:
    """Short ids cited in `applied_learning_ids` of the verify stage's verdicts
    that actually rejected a finding (valid=False). Read from the persisted
    verify ReviewStage artifact, since verdicts are not stored as rows of
    their own. A verdict citing a suppression while still keeping the finding
    is not a use, so valid=True is skipped."""
    row = (await session.execute(select(ReviewStage).where(
        ReviewStage.review_id == review_id,
        ReviewStage.stage_name == "verify"))).scalar_one_or_none()
    if row is None or not isinstance(row.artifact, dict):
        return set()
    applied: set[str] = set()
    for v in row.artifact.get("verdicts") or []:
        if not isinstance(v, dict) or v.get("valid"):
            continue
        for lid in v.get("applied_learning_ids") or []:
            applied.add(str(lid)[:8])
    return applied


async def resolve_outcomes_for_review(session: AsyncSession,
                                      review_id: uuid.UUID) -> dict[str, int]:
    """Resolve every still-pending injection for one review.

    Idempotent: only rows with outcome='pending' are touched, and each is moved
    to a terminal outcome exactly once. Returns per-outcome counts.
    """
    events = (await session.execute(select(InjectionEvent).where(
        InjectionEvent.review_id == review_id,
        InjectionEvent.outcome == "pending"))).scalars().all()
    if not events:
        return {}

    findings = (await session.execute(select(Finding).where(
        Finding.review_id == review_id))).scalars().all()
    published = [f for f in findings if f.published_note_id is not None]

    review = await session.get(Review, review_id)
    # A dry run posts no notes, so every pending injection would fall through to a terminal 'inconclusive' on a REAL learning no human judged; leave them pending forever instead.
    if not review.publish:
        return {}
    # Resolving before the note lands burns the evidence: only 'pending' is revisited.
    if _awaiting_publication(findings):
        age_s = (datetime.now(timezone.utc) - review.created_at).total_seconds()
        if age_s < PUBLICATION_WAIT_S:
            return {}

    notes_by_id = {}
    if published:
        note_ids = [f.published_note_id for f in published]
        notes_by_id = {n.id: n for n in (await session.execute(
            select(Note).where(Note.id.in_(note_ids)))).scalars().all()}

    # learning_id -> best disposition seen across the findings that cited it.
    # "Best" = a single acceptance outweighs any number of rejections: the
    # learning demonstrably produced at least one comment a human kept.
    cited: dict[str, set[str]] = {}
    for f in published:
        note = notes_by_id.get(f.published_note_id)
        if note is None:
            continue
        for lid in (f.contributing_learning_ids or []):
            # The agent only ever sees an 8-char short id (see
            # argus.knowledge.memory.learnings_index_block) and cites that
            # short id in contributing_learning_ids. Truncate to 8 chars here
            # so it collides correctly with the full InjectionEvent.learning_id
            # UUID looked up below. Same collision tolerance already accepted
            # by argus/review/tools.py's get_learning prefix match.
            cited.setdefault(str(lid)[:8], set()).add(note.disposition)

    # Short ids of do_not_suggest learnings the verifier applied to REJECT a
    # candidate finding. A suppression never produces a published note, so it
    # can never appear in `cited` -- without this it was charged 'ignored' on
    # every review and could never be credited, which is why do_not_suggest
    # learnings sat at 0 hits indefinitely. A suppression that fired is a
    # settled, successful use on its own: no human judgement is pending,
    # because the whole point is that no comment was posted.
    suppressed = await _applied_suppression_ids(session, review_id)

    review_produced_findings = bool(published)
    counts: dict[str, int] = {}

    for ev in events:
        short_id = str(ev.learning_id)[:8]
        dispositions = cited.get(short_id)

        if not dispositions and short_id in suppressed:
            outcome = "hit"
        elif dispositions:
            if not (dispositions & _TERMINAL):
                continue  # human has not judged it yet — retry next pass
            if dispositions & set(ACCEPTED):
                outcome = "hit"
            elif dispositions & set(REJECTED):
                outcome = "harmful"
            else:
                outcome = "inconclusive"  # dismissed/ambiguous
        elif review_produced_findings:
            # It was injected, the review did find things, and none of them
            # cited this learning. Weak negative evidence of irrelevance.
            outcome = "ignored"
        else:
            # The review found nothing at all. That is not this learning's
            # fault — never charge it a miss for a barren review.
            outcome = "inconclusive"

        ev.outcome = outcome
        ev.resolved_at = datetime.now(timezone.utc)
        learning = await session.get(Learning, ev.learning_id)
        if learning is not None:
            attr = _COUNTER[outcome]
            setattr(learning, attr, (getattr(learning, attr) or 0) + 1)
        counts[outcome] = counts.get(outcome, 0) + 1

    await session.flush()
    if counts:
        logger.info("review %s: resolved injections %s", review_id, counts)
    return counts


async def resolve_outcomes_for_mr(session: AsyncSession,
                                  mr_id: uuid.UUID) -> dict[str, int]:
    """Resolve pending injections across every review of one MR.

    Called after reconciliation, when fresh note dispositions have just landed.
    """
    review_ids = (await session.execute(
        select(Review.id).where(Review.mr_id == mr_id))).scalars().all()
    totals: dict[str, int] = {}
    for rid in review_ids:
        for outcome, n in (await resolve_outcomes_for_review(session, rid)).items():
            totals[outcome] = totals.get(outcome, 0) + n
    return totals


def outcome_resolution_due(repo, now: datetime, cooldown_s: int = 3600) -> bool:
    """True when this repo's learning-outcome pass has not run in the last hour.

    Independent of the poller's own interval (`poll_interval_s`, which can be
    as low as 10s) — reconciliation runs every tick regardless, but resolving
    outcomes is rate-limited separately because dispositions only change on
    human timescales and every pass costs a DB round-trip per pending event.
    """
    raw = (repo.poll_cursor or {}).get("outcomes_resolved_at")
    if raw is None:
        return True
    last = datetime.fromisoformat(raw)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= cooldown_s


def mark_outcome_resolution_ran(repo, now: datetime) -> None:
    """Stamp the cooldown, preserving any other keys already in poll_cursor
    (notably the GitLab `updated_after` cursor)."""
    repo.poll_cursor = {**(repo.poll_cursor or {}),
                        "outcomes_resolved_at": now.isoformat()}

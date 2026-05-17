from datetime import datetime, timedelta, timezone

import pytest

from argus.domain.models import (Finding, InjectionEvent, Learning,
                                     MergeRequest, Note, Repository, Review)
from argus.knowledge.outcomes import (mark_outcome_resolution_ran,
                                          outcome_resolution_due,
                                          resolve_outcomes_for_mr,
                                          resolve_outcomes_for_review)


async def _scaffold(db):
    repo = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=1)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s",
                      web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="done")
    db.add(review)
    await db.flush()
    return repo, mr, review


async def _learning(db, repo, topic="t"):
    l = Learning(repo_id=repo.id, topic=topic, hint_text="h")
    db.add(l)
    await db.flush()
    return l


async def test_accepted_note_marks_cited_learning_as_hit(db):
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=1,
                author_type="bot", kind="inline", body="b",
                disposition="accepted")
    db.add(note)
    await db.flush()
    finding = Finding(review_id=review.id, stage="s", type="issue",
                      severity="low", confidence=0.5, file_path="a.py", line=1,
                      title="t", body="b", evidence_quote="q",
                      published_note_id=note.id,
                      contributing_learning_ids=[str(lrn.id)])
    db.add(finding)
    await db.flush()
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.hit_count == 1
    assert counts["hit"] == 1


async def test_rejected_note_marks_cited_learning_as_harmful(db):
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=2,
                author_type="bot", kind="inline", body="b",
                disposition="rejected_with_rationale")
    db.add(note)
    await db.flush()
    finding = Finding(review_id=review.id, stage="s", type="issue",
                      severity="low", confidence=0.5, file_path="a.py", line=1,
                      title="t", body="b", evidence_quote="q",
                      published_note_id=note.id,
                      contributing_learning_ids=[str(lrn.id)])
    db.add(finding)
    await db.flush()
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.harmful_count == 1
    assert lrn.hit_count == 0


async def test_accepted_note_marks_cited_learning_as_hit_with_short_id(db):
    """Production shape: the agent only ever sees an 8-char short id (see
    argus.knowledge.memory.learnings_index_block) and cites THAT in
    contributing_learning_ids, never the full UUID. This is the regression
    test for the id-format mismatch: without truncating both sides to 8
    chars, this lookup never matches and hit_count never increments."""
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=6,
                author_type="bot", kind="inline", body="b",
                disposition="accepted")
    db.add(note)
    await db.flush()
    finding = Finding(review_id=review.id, stage="s", type="issue",
                      severity="low", confidence=0.5, file_path="a.py", line=1,
                      title="t", body="b", evidence_quote="q",
                      published_note_id=note.id,
                      contributing_learning_ids=[str(lrn.id)[:8]])
    db.add(finding)
    await db.flush()
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.hit_count == 1
    assert counts["hit"] == 1


async def test_acceptance_outweighs_rejection_across_findings_short_id(db):
    """A single learning cited on two published findings, one accepted and one
    rejected: acceptance wins and the outcome is 'hit', not 'harmful'. Uses the
    short-id format, consistent with what production actually sends."""
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    accepted_note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=7,
                        author_type="bot", kind="inline", body="b",
                        disposition="accepted")
    rejected_note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=8,
                        author_type="bot", kind="inline", body="b",
                        disposition="rejected_with_rationale")
    db.add_all([accepted_note, rejected_note])
    await db.flush()
    db.add_all([
        Finding(review_id=review.id, stage="s", type="issue", severity="low",
               confidence=0.5, file_path="a.py", line=1, title="t", body="b",
               evidence_quote="q", published_note_id=accepted_note.id,
               contributing_learning_ids=[str(lrn.id)[:8]]),
        Finding(review_id=review.id, stage="s", type="issue", severity="low",
               confidence=0.5, file_path="b.py", line=1, title="t", body="b",
               evidence_quote="q", published_note_id=rejected_note.id,
               contributing_learning_ids=[str(lrn.id)[:8]]),
    ])
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.hit_count == 1
    assert lrn.harmful_count == 0
    assert counts["hit"] == 1


async def test_uncited_learning_in_productive_review_is_ignored(db):
    """Injected, review published findings, but this learning drove none of them."""
    repo, mr, review = await _scaffold(db)
    cited = await _learning(db, repo, topic="cited")
    uncited = await _learning(db, repo, topic="uncited")
    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=3,
                author_type="bot", kind="inline", body="b",
                disposition="accepted")
    db.add(note)
    await db.flush()
    db.add(Finding(review_id=review.id, stage="s", type="issue", severity="low",
                   confidence=0.5, file_path="a.py", line=1, title="t", body="b",
                   evidence_quote="q", published_note_id=note.id,
                   contributing_learning_ids=[str(cited.id)]))
    db.add_all([
        InjectionEvent(learning_id=cited.id, review_id=review.id, outcome="pending"),
        InjectionEvent(learning_id=uncited.id, review_id=review.id, outcome="pending"),
    ])
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)

    await db.refresh(uncited)
    assert uncited.ignored_count == 1
    assert uncited.hit_count == 0


async def test_barren_review_does_not_blame_learnings(db):
    """No findings at all -> inconclusive, NOT a miss. Absence of a finding is
    not evidence the learning was wrong."""
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.inconclusive_count == 1
    assert lrn.miss_count == 0
    assert lrn.harmful_count == 0


async def _inline_finding(db, review, **kw):
    f = Finding(review_id=review.id, stage="s", type="issue", severity="low",
                confidence=0.5, file_path="a.py", line=1, title="t", body="b",
                evidence_quote="q", verdict_valid=True, **kw)
    db.add(f)
    await db.flush()
    return f


def _outcomes(db, review):
    import sqlalchemy
    return (db.execute(sqlalchemy.select(InjectionEvent).where(
        InjectionEvent.review_id == review.id)))


async def test_finding_awaiting_publication_stays_pending(db):
    """The publication race: findings exist but their notes have not landed
    locally yet (median 55min). Resolving now would charge every injection
    'inconclusive' -- terminal -- before the human verdict can ever arrive."""
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    await _inline_finding(db, review, published_note_id=None,
                          contributing_learning_ids=[str(lrn.id)])
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)

    ev = (await _outcomes(db, review)).scalars().one()
    assert ev.outcome == "pending"
    await db.refresh(lrn)
    assert lrn.inconclusive_count == 0


async def test_race_then_acceptance_becomes_hit(db):
    """End-to-end of the bug: an early pass must not burn the evidence, so the
    later pass can still credit the learning once the human accepts."""
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    finding = await _inline_finding(db, review, published_note_id=None,
                                    contributing_learning_ids=[str(lrn.id)])
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)  # premature pass

    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=91,
                author_type="bot", kind="inline", body="b",
                disposition="accepted_manually")
    db.add(note)
    await db.flush()
    finding.published_note_id = note.id
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    assert counts.get("hit") == 1
    await db.refresh(lrn)
    assert lrn.hit_count == 1
    assert lrn.inconclusive_count == 0


async def test_summary_only_findings_do_not_block_resolution(db):
    """outside_diff findings go in the summary and never get an inline note, so
    waiting on them would leave the injection pending forever."""
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    await _inline_finding(db, review, published_note_id=None, outside_diff=True)
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)

    ev = (await _outcomes(db, review)).scalars().one()
    assert ev.outcome == "inconclusive"


async def test_unpublished_findings_resolve_once_review_is_stale(db):
    """A finding can legitimately never publish (lost inline budget). Waiting
    must be bounded or the injection is pending forever."""
    repo, mr, review = await _scaffold(db)
    review.created_at = datetime.now(timezone.utc) - timedelta(days=3)
    lrn = await _learning(db, repo)
    await _inline_finding(db, review, published_note_id=None,
                          contributing_learning_ids=[str(lrn.id)])
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)

    ev = (await _outcomes(db, review)).scalars().one()
    assert ev.outcome == "inconclusive"


async def test_open_disposition_stays_pending(db):
    """Human has not reacted yet — must remain resolvable on a later pass."""
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=4,
                author_type="bot", kind="inline", body="b", disposition="open")
    db.add(note)
    await db.flush()
    db.add(Finding(review_id=review.id, stage="s", type="issue", severity="low",
                   confidence=0.5, file_path="a.py", line=1, title="t", body="b",
                   evidence_quote="q", published_note_id=note.id,
                   contributing_learning_ids=[str(lrn.id)]))
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)

    ev = (await db.execute(
        __import__("sqlalchemy").select(InjectionEvent).where(
            InjectionEvent.review_id == review.id))).scalars().one()
    assert ev.outcome == "pending"
    await db.refresh(lrn)
    assert lrn.hit_count == 0


async def test_resolution_is_idempotent(db):
    """The poller calls this repeatedly; counters must not inflate."""
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=5,
                author_type="bot", kind="inline", body="b",
                disposition="accepted")
    db.add(note)
    await db.flush()
    db.add(Finding(review_id=review.id, stage="s", type="issue", severity="low",
                   confidence=0.5, file_path="a.py", line=1, title="t", body="b",
                   evidence_quote="q", published_note_id=note.id,
                   contributing_learning_ids=[str(lrn.id)]))
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)
    await resolve_outcomes_for_review(db, review.id)
    await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.hit_count == 1  # not 3


async def test_resolve_outcomes_for_mr_covers_all_reviews(db):
    repo, mr, review = await _scaffold(db)
    second = Review(mr_id=mr.id, status="done")
    db.add(second)
    await db.flush()
    l1 = await _learning(db, repo, topic="one")
    l2 = await _learning(db, repo, topic="two")
    db.add_all([
        InjectionEvent(learning_id=l1.id, review_id=review.id, outcome="pending"),
        InjectionEvent(learning_id=l2.id, review_id=second.id, outcome="pending"),
    ])
    await db.flush()

    counts = await resolve_outcomes_for_mr(db, mr.id)

    # Both barren reviews -> both inconclusive, neither left pending.
    assert counts.get("inconclusive") == 2


class _Repo:
    def __init__(self, poll_cursor=None):
        self.poll_cursor = poll_cursor


def test_never_resolved_repo_is_due():
    repo = _Repo(poll_cursor=None)
    assert outcome_resolution_due(repo, datetime.now(timezone.utc))


def test_repo_resolved_seconds_ago_is_not_due():
    now = datetime.now(timezone.utc)
    repo = _Repo(poll_cursor={"outcomes_resolved_at": now.isoformat()})
    assert not outcome_resolution_due(repo, now + timedelta(seconds=30))


def test_repo_resolved_over_an_hour_ago_is_due_again():
    now = datetime.now(timezone.utc)
    repo = _Repo(poll_cursor={"outcomes_resolved_at": now.isoformat()})
    assert outcome_resolution_due(repo, now + timedelta(hours=2))


def test_mark_ran_preserves_existing_poll_cursor_keys():
    now = datetime.now(timezone.utc)
    repo = _Repo(poll_cursor={"updated_after": "2026-07-01T00:00:00Z"})
    mark_outcome_resolution_ran(repo, now)
    assert repo.poll_cursor["updated_after"] == "2026-07-01T00:00:00Z"
    assert repo.poll_cursor["outcomes_resolved_at"] == now.isoformat()


async def test_backfill_resets_stale_miss_verdicts(db):
    """Old rows were marked 'miss' by review-wide logic that could never see a
    disposition. Reset them to pending so the new resolver can re-judge."""
    from argus.backfill_outcomes import reset_stale_events
    repo, mr, review = await _scaffold(db)
    lrn = await _learning(db, repo)
    lrn.miss_count = 5
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="miss"))
    await db.flush()

    n = await reset_stale_events(db)

    assert n == 1
    await db.refresh(lrn)
    assert lrn.miss_count == 0


async def test_dry_run_never_resolves_an_injection(db):
    """A dry run (benchmark/no-publish) never posts a note, so no human ever
    judges it. Earlier this resolved to 'inconclusive' immediately instead of
    waiting 48h -- which was still wrong, just faster: it charged a real
    learning for a review nobody could ever have judged. Now it stays
    'pending' forever, and the injection log survives for retrieval analysis
    without corrupting the counters."""
    repo, mr, review = await _scaffold(db)
    review.publish = False
    lrn = await _learning(db, repo)
    await _inline_finding(db, review, published_note_id=None,
                          contributing_learning_ids=[str(lrn.id)])
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    assert counts == {}
    ev = (await _outcomes(db, review)).scalars().one()
    assert ev.outcome == "pending"
    await db.refresh(lrn)
    assert lrn.inconclusive_count == 0


async def test_publishing_review_still_waits(db):
    repo, mr, review = await _scaffold(db)
    review.publish = True
    lrn = await _learning(db, repo)
    await _inline_finding(db, review, published_note_id=None,
                          contributing_learning_ids=[str(lrn.id)])
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    await resolve_outcomes_for_review(db, review.id)

    ev = (await _outcomes(db, review)).scalars().one()
    assert ev.outcome == "pending"

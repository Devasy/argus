"""do_not_suggest learnings can only ever be "used" by causing a candidate
finding to be dropped -- there is no published note to cite them from, so
they could never be scored a hit and were charged 'ignored' on every review
forever (production: 24 active do_not_suggest learnings, 0 hits, 33 ignored,
941 misses). This credits a suppression that actually fired, read from the
verify stage's persisted verdicts (Verdict.applied_learning_ids), the same way
resolve_outcomes_for_review already reads contributing_learning_ids from
published findings.
"""
from argus.domain.models import (Finding, InjectionEvent, Learning,
                                     MergeRequest, Note, Repository, Review,
                                     ReviewStage)
from argus.knowledge.memory import do_not_suggest_block
from argus.knowledge.outcomes import resolve_outcomes_for_review


async def _scaffold(db):
    repo = Repository(provider="gitlab", project_path="g/sup", gitlab_project_id=2)
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


async def _dns_learning(db, repo, topic="no console.log"):
    l = Learning(repo_id=repo.id, kind="do_not_suggest", topic=topic, hint_text="h")
    db.add(l)
    await db.flush()
    return l


def test_do_not_suggest_block_renders_short_ids():
    """Without an id in the block, the verifier has nothing to cite -- this is
    the root cause of do_not_suggest learnings being permanently unscoreable.
    Mirrors learnings_index_block's existing id + citation-instruction shape."""
    l = Learning(topic="no console.log", hint_text="use the logger")
    l.id = __import__("uuid").UUID("d37dd30e-0000-0000-0000-000000000000")
    block = do_not_suggest_block([l])
    assert "d37dd30e" in block
    assert "applied_learning_ids" in block


async def test_suppressed_finding_credits_the_learning_as_hit(db):
    """The core fix: a verdict that rejects a finding AND cites the
    do_not_suggest learning that drove the rejection must count as a hit --
    there is no note to accept, since the finding was never published."""
    repo, mr, review = await _scaffold(db)
    lrn = await _dns_learning(db, repo)
    db.add(ReviewStage(
        review_id=review.id, stage_name="verify", status="done",
        artifact={"verdicts": [
            {"finding_id": "a1", "valid": False, "reason": "matches DNS rule",
             "applied_learning_ids": [str(lrn.id)[:8]]}],
                 "ungrounded": []}))
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.hit_count == 1
    assert lrn.ignored_count == 0
    assert counts["hit"] == 1


async def test_dry_run_suppression_is_not_credited_either(db):
    """The same no-human-judged rule applies to suppression credit, not just
    published-note dispositions: a benchmark dry run must not earn a real
    learning a real hit just because verify happened to reject a finding."""
    repo, mr, review = await _scaffold(db)
    review.publish = False
    lrn = await _dns_learning(db, repo)
    db.add(ReviewStage(
        review_id=review.id, stage_name="verify", status="done",
        artifact={"verdicts": [
            {"finding_id": "a1", "valid": False, "reason": "matches DNS rule",
             "applied_learning_ids": [str(lrn.id)[:8]]}],
                 "ungrounded": []}))
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    assert counts == {}
    await db.refresh(lrn)
    assert lrn.hit_count == 0


async def test_citing_a_suppression_on_a_kept_finding_does_not_count(db):
    """A verdict that cites the learning but keeps the finding (valid=True)
    is not a suppression -- nothing was actually dropped -- so it must not be
    credited as a hit."""
    repo, mr, review = await _scaffold(db)
    lrn = await _dns_learning(db, repo)
    db.add(ReviewStage(
        review_id=review.id, stage_name="verify", status="done",
        artifact={"verdicts": [
            {"finding_id": "a1", "valid": True, "reason": "kept anyway",
             "applied_learning_ids": [str(lrn.id)[:8]]}],
                 "ungrounded": []}))
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.hit_count == 0
    assert counts.get("hit", 0) == 0


async def test_uncited_suppression_still_falls_back_to_ignored(db):
    """A do_not_suggest learning that no verdict ever mentions keeps its
    previous behaviour: 'ignored' if the review found anything, else
    inconclusive. This fix only changes what happens when it IS cited."""
    repo, mr, review = await _scaffold(db)
    lrn = await _dns_learning(db, repo)
    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=9,
               author_type="bot", kind="inline", body="b",
               disposition="accepted")
    db.add(note)
    await db.flush()
    finding = Finding(review_id=review.id, stage="s", type="issue",
                      severity="low", confidence=0.5, file_path="a.py", line=1,
                      title="t", body="b", evidence_quote="q",
                      published_note_id=note.id)
    db.add(finding)
    db.add(ReviewStage(
        review_id=review.id, stage_name="verify", status="done",
        artifact={"verdicts": [
            {"finding_id": "a1", "valid": True, "reason": "ok",
             "applied_learning_ids": []}],
                 "ungrounded": []}))
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.hit_count == 0
    assert counts["ignored"] == 1


async def test_missing_verify_stage_does_not_crash(db):
    """Reviews that failed before reaching verify (or predate this feature)
    have no verify ReviewStage row -- must degrade to the old behaviour, not
    raise."""
    repo, mr, review = await _scaffold(db)
    lrn = await _dns_learning(db, repo)
    db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                          outcome="pending"))
    await db.flush()

    counts = await resolve_outcomes_for_review(db, review.id)

    await db.refresh(lrn)
    assert lrn.hit_count == 0
    assert counts["inconclusive"] == 1

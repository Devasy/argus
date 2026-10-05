import uuid

from argus.domain.models import (Finding, MergeRequest, Note, Repository, Review,
                                     ReviewerAgent, ReviewerAgentVersion,
                                     ReviewReviewerAgentVersion)
from argus.knowledge.acceptance import compute_agent_version_acceptance


async def _repo_mr(db):
    repo = Repository(provider="gitlab", project_path=f"grp/acc-{uuid.uuid4().hex[:8]}",
                      gitlab_project_id=int.from_bytes(uuid.uuid4().bytes[:4], "big"))
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    return mr


async def _agent_version(db):
    agent = ReviewerAgent(name=f"acc-agent-{uuid.uuid4().hex[:8]}")
    db.add(agent)
    await db.flush()
    version = ReviewerAgentVersion(agent_id=agent.id, version=1, guidelines="g")
    db.add(version)
    await db.flush()
    return agent, version


_seq = iter(range(1, 10_000_000))


async def _note(db, mr, review, stage: str, disposition: str):
    note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=next(_seq),
                author_type="bot", kind="inline", disposition=disposition,
                body="x", file_path="a.py", line=1)
    db.add(note)
    await db.flush()
    db.add(Finding(review_id=review.id, stage=stage, type="issue", severity="high",
                   confidence=0.9, file_path="a.py", line=1, title="t", body="b",
                   evidence_quote="q", verdict_valid=True, published_note_id=note.id))
    await db.flush()


async def _seed(db, disposition_counts: dict[str, int], foreign_accepted: int = 0):
    mr = await _repo_mr(db)
    review = Review(mr_id=mr.id, status="done")
    db.add(review)
    agent, version = await _agent_version(db)
    db.add(ReviewReviewerAgentVersion(review_id=review.id, agent_version_id=version.id))
    for disposition, count in disposition_counts.items():
        for _ in range(count):
            await _note(db, mr, review, agent.name, disposition)
    for _ in range(foreign_accepted):
        await _note(db, mr, review, "analysis", "accepted")
    return version.id


async def test_acceptance_rate_computed_correctly(db):
    version_id = await _seed(db, {"accepted": 3, "accepted_manually": 1,
                                  "rejected_with_rationale": 2, "open": 5})
    result = await compute_agent_version_acceptance(db, version_id)
    assert result["accepted"] == 4
    assert result["rejected"] == 2
    assert result["acceptance_rate"] == 4 / 6


async def test_acceptance_rate_none_when_no_verdicts(db):
    version_id = await _seed(db, {"open": 3, "answered": 2})
    result = await compute_agent_version_acceptance(db, version_id)
    assert result["accepted"] == 0
    assert result["rejected"] == 0
    assert result["acceptance_rate"] is None


async def test_only_the_specialists_own_notes_count(db):
    version_id = await _seed(db, {"accepted": 2, "rejected_with_rationale": 1},
                             foreign_accepted=3)
    r = await compute_agent_version_acceptance(db, version_id)
    assert (r["accepted"], r["rejected"]) == (2, 1)


async def test_acceptance_rate_not_multiplied_by_repeat_reviews_on_same_mr(db):
    """A single MR can have multiple Review rows over its lifetime (e.g.
    re-triggers). If the same agent version ran on more than one of those
    reviews, a single accepted Note on that MR must still be counted once,
    not once per qualifying Review."""
    mr = await _repo_mr(db)
    agent, version = await _agent_version(db)
    review1 = Review(mr_id=mr.id, status="done")
    review2 = Review(mr_id=mr.id, status="done")
    db.add(review1)
    db.add(review2)
    await db.flush()
    db.add(ReviewReviewerAgentVersion(review_id=review1.id, agent_version_id=version.id))
    db.add(ReviewReviewerAgentVersion(review_id=review2.id, agent_version_id=version.id))
    await _note(db, mr, review1, agent.name, "accepted")

    result = await compute_agent_version_acceptance(db, version.id)
    assert result["accepted"] == 1
    assert result["rejected"] == 0

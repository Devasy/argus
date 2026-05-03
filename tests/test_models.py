import uuid

import pytest
from sqlalchemy import select

from argus.domain.models import (Actor, MergeRequest, Note, Repository)


async def test_repository_mr_note_roundtrip(db):
    repo = Repository(provider="gitlab", project_path="grp/proj", gitlab_project_id=848)
    db.add(repo)
    await db.flush()

    author = Actor(provider="gitlab", provider_user_id=1054, username="dev.reviewer")
    db.add(author)
    await db.flush()

    mr = MergeRequest(repo_id=repo.id, mr_iid=41, title="t", state="opened",
                      author_id=author.id, source_branch="f", target_branch="main",
                      head_sha="abc", web_url="https://x/41")
    db.add(mr)
    await db.flush()

    note = Note(mr_id=mr.id, provider_note_id=180500, author_id=author.id,
                author_type="human", kind="inline", body="hi", disposition="open")
    db.add(note)
    await db.flush()

    got = (await db.execute(select(Note).where(Note.mr_id == mr.id))).scalar_one()
    assert got.provider_note_id == 180500
    assert isinstance(repo.id, uuid.UUID)


async def test_disposition_check_constraint(db):
    import pytest
    from sqlalchemy.exc import IntegrityError
    repo = Repository(provider="gitlab", project_path="g/p2", gitlab_project_id=1)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    db.add(Note(mr_id=mr.id, provider_note_id=1, author_type="human",
                kind="inline", body="x", disposition="NOT_A_VALUE"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_runtime_setting_roundtrip(db):
    from argus.domain.models import RuntimeSetting
    db.add(RuntimeSetting(key="poll_interval_s", value={"v": 60}))
    await db.flush()
    row = await db.get(RuntimeSetting, "poll_interval_s")
    assert row.value == {"v": 60}
    assert row.updated_at is not None


async def test_reviewer_agent_version_roundtrip(db):
    from argus.domain.models import ReviewerAgent, ReviewerAgentVersion
    agent = ReviewerAgent(name="test-agent", description="a test agent")
    db.add(agent)
    await db.flush()
    version = ReviewerAgentVersion(agent_id=agent.id, version=1,
                                   guidelines="be thorough", max_rounds=5)
    db.add(version)
    await db.flush()
    agent.current_version_id = version.id
    await db.flush()
    loaded = await db.get(ReviewerAgent, agent.id)
    assert loaded.enabled is True
    assert loaded.current_version_id == version.id
    loaded_version = await db.get(ReviewerAgentVersion, version.id)
    assert loaded_version.guidelines == "be thorough"
    assert loaded_version.max_rounds == 5
    assert loaded_version.model is None


async def test_review_force_agents_column(db, engine):
    from argus.domain.models import MergeRequest, Repository, Review
    repo = Repository(provider="gitlab", project_path="grp/force-agents-test",
                      gitlab_project_id=90000099)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="queued", force_agents=["security-reviewer"])
    db.add(review)
    await db.flush()
    loaded = await db.get(Review, review.id)
    assert loaded.force_agents == ["security-reviewer"]


async def test_review_reviewer_agent_version_join(db):
    from argus.domain.models import (MergeRequest, Repository, Review,
                                         ReviewerAgent, ReviewerAgentVersion,
                                         ReviewReviewerAgentVersion)
    repo = Repository(provider="gitlab", project_path="grp/join-test",
                      gitlab_project_id=90000101)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="done")
    db.add(review)
    agent = ReviewerAgent(name="join-test-agent")
    db.add(agent)
    await db.flush()
    version = ReviewerAgentVersion(agent_id=agent.id, version=1, guidelines="g")
    db.add(version)
    await db.flush()
    join_row = ReviewReviewerAgentVersion(review_id=review.id, agent_version_id=version.id)
    db.add(join_row)
    await db.flush()
    loaded = await db.get(ReviewReviewerAgentVersion, join_row.id)
    assert loaded.review_id == review.id
    assert loaded.agent_version_id == version.id


async def test_review_reviewer_agent_version_unique_constraint(db):
    """A retried pipeline branch must not be able to insert a second join row
    for the same (review_id, agent_version_id) pair -- there should be a
    unique constraint enforcing this at the DB level."""
    from sqlalchemy.exc import IntegrityError

    from argus.domain.models import (MergeRequest, Repository, Review,
                                         ReviewerAgent, ReviewerAgentVersion,
                                         ReviewReviewerAgentVersion)
    repo = Repository(provider="gitlab", project_path="grp/join-unique-test",
                      gitlab_project_id=90000102)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="done")
    db.add(review)
    agent = ReviewerAgent(name="join-unique-test-agent")
    db.add(agent)
    await db.flush()
    version = ReviewerAgentVersion(agent_id=agent.id, version=1, guidelines="g")
    db.add(version)
    await db.flush()
    db.add(ReviewReviewerAgentVersion(review_id=review.id, agent_version_id=version.id))
    await db.flush()

    db.add(ReviewReviewerAgentVersion(review_id=review.id, agent_version_id=version.id))
    try:
        with pytest.raises(IntegrityError):
            await db.flush()
    finally:
        await db.rollback()


async def test_learning_has_harmful_and_ignored_counters(db):
    from argus.domain.models import Learning
    l = Learning(topic="t", hint_text="h")
    db.add(l)
    await db.flush()
    assert l.harmful_count == 0
    assert l.ignored_count == 0


async def test_injection_event_accepts_harmful_and_ignored_outcomes(db):
    from argus.domain.models import InjectionEvent, Learning, Review
    from argus.domain.models import MergeRequest, Repository
    repo = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=1)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="done")
    lrn = Learning(topic="t", hint_text="h")
    db.add_all([review, lrn])
    await db.flush()
    for outcome in ("harmful", "ignored"):
        db.add(InjectionEvent(learning_id=lrn.id, review_id=review.id,
                              outcome=outcome))
    await db.flush()  # must not violate the check constraint


async def test_finding_stores_contributing_learning_ids(db):
    from argus.domain.models import Finding, MergeRequest, Repository, Review
    repo = Repository(provider="gitlab", project_path="g/p2", gitlab_project_id=2)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=2, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="done")
    db.add(review)
    await db.flush()
    f = Finding(review_id=review.id, stage="s", type="issue", severity="low",
                confidence=0.5, file_path="a.py", line=1, title="t", body="b",
                evidence_quote="q", contributing_learning_ids=["abc", "def"])
    db.add(f)
    await db.flush()
    assert f.contributing_learning_ids == ["abc", "def"]


async def test_audit_run_and_verdict_round_trip(db):
    from argus.domain.models import (AuditRun, AuditVerdict, Learning,
                                         Repository)
    repo = Repository(provider="gitlab", project_path="g/audit",
                      gitlab_project_id=99)
    db.add(repo)
    await db.flush()
    run = AuditRun(repo_id=repo.id, status="running", commit_sha="abc123")
    lrn = Learning(repo_id=repo.id, topic="t", hint_text="h")
    db.add_all([run, lrn])
    await db.flush()
    v = AuditVerdict(audit_run_id=run.id, learning_id=lrn.id,
                     verdict="corroborated", confidence=0.9,
                     rationale="matches code",
                     citations=[{"file": "a.py", "line": 10, "quote": "x"}],
                     proposed_action="none", state="proposed")
    db.add(v)
    await db.flush()
    assert v.state == "proposed"
    assert v.citations[0]["file"] == "a.py"


async def test_learning_has_groundedness_fields(db):
    from argus.domain.models import Learning
    l = Learning(topic="t", hint_text="h")
    db.add(l)
    await db.flush()
    assert l.groundedness is None
    assert l.last_audited_at is None

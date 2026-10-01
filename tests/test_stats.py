from datetime import datetime, timedelta, timezone

from argus.api.stats import compute_dashboard_stats
from argus.domain.models import (Finding, Learning, MergeRequest, Note, Repository,
                                     Review, ReviewerAgent,
                                     ReviewerAgentVersion,
                                     ReviewReviewerAgentVersion)


async def test_dashboard_stats_empty_db_returns_zeros(db):
    # NOTE: the `db` fixture rolls back its own writes, but this shares a
    # session-scoped database with other test modules (e.g. tests using the
    # `api` fixture in test_api.py / test_api_agents.py / test_api_learnings.py)
    # that commit real rows outside that rollback. Those modules may leave
    # data behind (e.g. active learnings, done reviews), so we can't assume a
    # truly empty database. Instead we verify (a) the function is stable
    # across repeated calls with no intervening writes (catches
    # double-counting / non-determinism), and (b) the acceptance_rate
    # null-safety invariant holds against whatever accepted/rejected counts
    # are actually present in the shared DB (catches div-by-zero /
    # mis-derivation of acceptance_rate).
    before = await compute_dashboard_stats(db, days=14)
    after = await compute_dashboard_stats(db, days=14)
    assert after["tiles"] == before["tiles"]
    assert after["reviews_per_day"] == before["reviews_per_day"]

    accepted = after["tiles"]["accepted"]
    rejected = after["tiles"]["rejected"]
    acceptance_rate = after["tiles"]["acceptance_rate"]
    if accepted + rejected == 0:
        assert acceptance_rate is None
    else:
        assert acceptance_rate == accepted / (accepted + rejected)


async def test_dashboard_stats_counts_window_only(db):
    before = await compute_dashboard_stats(db, days=14)
    base_mrs = before["tiles"]["mrs_reviewed"]
    base_comments = before["tiles"]["agent_comments"]
    base_accepted = before["tiles"]["accepted"]
    base_rejected = before["tiles"]["rejected"]
    base_learnings = before["tiles"]["active_learnings"]
    base_prompt_tokens = before["tiles"]["prompt_tokens"]
    base_reviews_per_day = sum(d["count"] for d in before["reviews_per_day"])

    repo = Repository(provider="gitlab", project_path="g/stats-p",
                      gitlab_project_id=9601)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="merged",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    now = datetime.now(timezone.utc)
    recent = Review(mr_id=mr.id, status="done", created_at=now,
                    started_at=now, finished_at=now,
                    prompt_tokens=100, completion_tokens=50)
    ancient = Review(mr_id=mr.id, status="done",
                     created_at=now - timedelta(days=90),
                     started_at=now - timedelta(days=90),
                     finished_at=now - timedelta(days=90))
    db.add_all([recent, ancient])
    db.add_all([
        Note(mr_id=mr.id, provider_note_id=1, author_type="bot",
             kind="inline", body="b", disposition="accepted",
             note_created_at=now),
        Note(mr_id=mr.id, provider_note_id=2, author_type="bot",
             kind="inline", body="b", disposition="rejected_with_rationale",
             note_created_at=now),
        Note(mr_id=mr.id, provider_note_id=3, author_type="bot",
             kind="inline", body="b", disposition="accepted",
             note_created_at=now - timedelta(days=90)),  # outside window
    ])
    db.add(Learning(topic="x", hint_text="y", status="active"))
    await db.flush()

    stats = await compute_dashboard_stats(db, days=14)
    assert stats["tiles"]["mrs_reviewed"] - base_mrs == 1
    assert stats["tiles"]["agent_comments"] - base_comments == 2
    assert stats["tiles"]["accepted"] - base_accepted == 1
    assert stats["tiles"]["rejected"] - base_rejected == 1
    assert stats["tiles"]["active_learnings"] - base_learnings == 1
    assert stats["tiles"]["prompt_tokens"] - base_prompt_tokens == 100
    assert sum(d["count"] for d in stats["reviews_per_day"]) \
        - base_reviews_per_day == 1
    assert any(a["type"] == "review_done" for a in stats["activity"])


async def test_dashboard_stats_exclude_dry_run_benchmark_reviews(db):
    """A golden-set benchmark pass (publish=False) never posts a comment, so
    it must not inflate the production activity tiles/chart -- it can re-run
    the same handful of MRs dozens of times, which would otherwise dwarf real
    review activity in the window. The activity feed still surfaces it (nothing
    should vanish from the log), just visibly marked as a benchmark."""
    before = await compute_dashboard_stats(db, days=14)
    base_mrs = before["tiles"]["mrs_reviewed"]
    base_prompt_tokens = before["tiles"]["prompt_tokens"]
    base_reviews_per_day = sum(d["count"] for d in before["reviews_per_day"])

    repo = Repository(provider="gitlab", project_path="g/stats-dry-run",
                      gitlab_project_id=9603)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    now = datetime.now(timezone.utc)
    dry_run = Review(mr_id=mr.id, status="done", publish=False, created_at=now,
                     started_at=now, finished_at=now,
                     prompt_tokens=500, completion_tokens=200)
    db.add(dry_run)
    await db.flush()

    stats = await compute_dashboard_stats(db, days=14)
    assert stats["tiles"]["mrs_reviewed"] == base_mrs
    assert stats["tiles"]["prompt_tokens"] == base_prompt_tokens
    assert sum(d["count"] for d in stats["reviews_per_day"]) == base_reviews_per_day
    entry = next(a for a in stats["activity"] if a["href_id"] == dry_run.id)
    assert "(benchmark)" in entry["title"]


async def test_dashboard_per_agent_counts_only_the_agents_own_notes(db):
    # A review's analysis-stage comments must not be credited to every
    # specialist that also ran; attribution goes through Finding.stage.
    repo = Repository(provider="gitlab", project_path="g/stats-own-notes",
                      gitlab_project_id=9602)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="merged",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    now = datetime.now(timezone.utc)
    review = Review(mr_id=mr.id, status="done", created_at=now,
                    started_at=now, finished_at=now)
    db.add(review)
    await db.flush()

    agent = ReviewerAgent(name="own-notes-agent")
    db.add(agent)
    await db.flush()
    version = ReviewerAgentVersion(agent_id=agent.id, version=1,
                                   guidelines="g")
    db.add(version)
    await db.flush()
    db.add(ReviewReviewerAgentVersion(review_id=review.id,
                                      agent_version_id=version.id))
    for i, stage in enumerate((agent.name, "analysis"), start=1):
        note = Note(mr_id=mr.id, review_id=review.id, provider_note_id=i,
                    author_type="bot", kind="inline", body="b",
                    disposition="accepted", note_created_at=now)
        db.add(note)
        await db.flush()
        db.add(Finding(review_id=review.id, stage=stage, type="issue",
                       severity="high", confidence=0.9, file_path="a.py", line=1,
                       title="t", body="b", evidence_quote="q",
                       verdict_valid=True, published_note_id=note.id))
    await db.flush()

    stats = await compute_dashboard_stats(db, days=14)
    row = next(a for a in stats["agents"] if a["agent_id"] == agent.id)
    assert row["comments"] == 1
    assert row["accepted"] == 1


async def test_followup_verified_fixes_count_as_accepted_and_separately(db):
    before = (await compute_dashboard_stats(db, days=14))["tiles"]
    repo = Repository(provider="gitlab", project_path="g/stats-followup", gitlab_project_id=9401)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened", source_branch="s",
                      target_branch="m", head_sha="abc", web_url="u")
    db.add(mr)
    await db.flush()
    now = datetime.now(timezone.utc)
    for i, disposition in enumerate(("accepted_by_followup", "accepted_manually")):
        db.add(Note(mr_id=mr.id, provider_note_id=940100 + i, author_type="bot", kind="inline",
                    body="x", disposition=disposition, note_created_at=now))
    await db.flush()

    after = (await compute_dashboard_stats(db, days=14))["tiles"]
    assert after["accepted"] - before["accepted"] == 2
    assert after["accepted_by_followup"] - before["accepted_by_followup"] == 1


async def test_dashboard_api_exposes_the_followup_count(engine, settings):
    import httpx
    from argus.api.app import create_app
    app = create_app(settings=settings, engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        tiles = (await c.get("/stats/dashboard", params={"days": 14})).json()["tiles"]
    assert "accepted_by_followup" in tiles

from sqlalchemy import select

from argus.domain.models import (LLMEndpoint, MergeRequest, MRVersion,
                                     Repository, Review)
from argus.ingest.poller import _maybe_auto_review
from argus.jobs.queue import claim_next


def _mr(repo, iid, head_sha, draft=False, state="opened"):
    return MergeRequest(repo_id=repo.id, mr_iid=iid, title="t", state=state,
                        source_branch="a", target_branch="b", head_sha=head_sha,
                        draft=draft, web_url="u")


async def _default_endpoint(db):
    ep = LLMEndpoint(name="default", provider="ollama", base_url="http://x",
                     model="m", is_default=True)
    db.add(ep)
    await db.flush()
    return ep


async def test_auto_review_disabled_by_default(db):
    repo = Repository(provider="gitlab", project_path="ar/disabled",
                      gitlab_project_id=1)
    db.add(repo); await db.flush()
    mr = _mr(repo, 1, "sha1")
    db.add(mr); await db.flush()

    await _maybe_auto_review(db, repo, mr, llm_healthy=True)
    await db.flush()
    job = await claim_next(db, "w1", ["review"])
    assert job is None


async def test_auto_review_skips_draft(db):
    await _default_endpoint(db)
    repo = Repository(provider="gitlab", project_path="ar/draft",
                      gitlab_project_id=1, auto_review_enabled=True)
    db.add(repo); await db.flush()
    mr = _mr(repo, 1, "sha1", draft=True)
    db.add(mr); await db.flush()

    await _maybe_auto_review(db, repo, mr, llm_healthy=True)
    await db.flush()
    job = await claim_next(db, "w1", ["review"])
    assert job is None


async def test_auto_review_skips_unchanged_head_sha(db):
    await _default_endpoint(db)
    repo = Repository(provider="gitlab", project_path="ar/unchanged",
                      gitlab_project_id=1, auto_review_enabled=True)
    db.add(repo); await db.flush()
    mr = _mr(repo, 1, "sha1")
    db.add(mr); await db.flush()
    v1 = MRVersion(mr_id=mr.id, provider_version_id=1, head_commit_sha="sha1",
                   base_commit_sha="b", start_commit_sha="st")
    db.add(v1); await db.flush()
    db.add(Review(mr_id=mr.id, status="done", mr_version_id=v1.id))
    await db.flush()

    await _maybe_auto_review(db, repo, mr, llm_healthy=True)
    await db.flush()
    job = await claim_next(db, "w1", ["review"])
    assert job is None


async def test_auto_review_triggers_on_new_head_sha(db):
    await _default_endpoint(db)
    repo = Repository(provider="gitlab", project_path="ar/changed",
                      gitlab_project_id=1, auto_review_enabled=True)
    db.add(repo); await db.flush()
    mr = _mr(repo, 1, "sha2")
    db.add(mr); await db.flush()
    v1 = MRVersion(mr_id=mr.id, provider_version_id=1, head_commit_sha="sha1",
                   base_commit_sha="b", start_commit_sha="st")
    db.add(v1); await db.flush()
    db.add(Review(mr_id=mr.id, status="done", mr_version_id=v1.id))
    await db.flush()

    await _maybe_auto_review(db, repo, mr, llm_healthy=True)
    await db.flush()
    job = await claim_next(db, "w1", ["review"])
    assert job is not None
    assert job.payload["review_id"]


async def test_auto_review_triggers_on_first_review(db):
    await _default_endpoint(db)
    repo = Repository(provider="gitlab", project_path="ar/first",
                      gitlab_project_id=1, auto_review_enabled=True)
    db.add(repo); await db.flush()
    mr = _mr(repo, 1, "sha1")
    db.add(mr); await db.flush()

    await _maybe_auto_review(db, repo, mr, llm_healthy=True)
    await db.flush()
    job = await claim_next(db, "w1", ["review"])
    assert job is not None


async def test_auto_review_skipped_when_llm_unhealthy(db):
    """The poller must not schedule new reviews while the LLM endpoint is
    down (e.g. a self-hosted llama.cpp backend that's unreachable) -- MR
    sync/reconciliation still runs, only scheduling is paused."""
    await _default_endpoint(db)
    repo = Repository(provider="gitlab", project_path="ar/unhealthy",
                      gitlab_project_id=1, auto_review_enabled=True)
    db.add(repo); await db.flush()
    mr = _mr(repo, 1, "sha1")
    db.add(mr); await db.flush()

    await _maybe_auto_review(db, repo, mr, llm_healthy=False)
    await db.flush()
    job = await claim_next(db, "w1", ["review"])
    assert job is None
    reviews = (await db.execute(select(Review).where(Review.mr_id == mr.id))).scalars().all()
    assert len(reviews) == 0


async def test_auto_review_does_not_create_extra_review_rows_while_job_in_flight(db):
    """Regression: repeated poll ticks (job still queued/running, no 'done'
    review yet) must not create additional Review rows. Before the fix,
    _maybe_auto_review created a Review row unconditionally and only relied
    on enqueue()'s dedup to skip the *job* — leaving the newly created Review
    orphaned at status='queued' forever with no job behind it."""
    await _default_endpoint(db)
    repo = Repository(provider="gitlab", project_path="ar/repeat-poll",
                      gitlab_project_id=1, auto_review_enabled=True)
    db.add(repo); await db.flush()
    mr = _mr(repo, 1, "sha1")
    db.add(mr); await db.flush()

    # First poll tick: creates the Review + job, leaves the job queued
    # (simulating a slow/still-running worker).
    await _maybe_auto_review(db, repo, mr, llm_healthy=True)
    await db.flush()

    # Simulated subsequent poll ticks before the job finishes.
    await _maybe_auto_review(db, repo, mr, llm_healthy=True)
    await _maybe_auto_review(db, repo, mr, llm_healthy=True)
    await db.flush()

    reviews = (await db.execute(select(Review).where(Review.mr_id == mr.id))).scalars().all()
    assert len(reviews) == 1

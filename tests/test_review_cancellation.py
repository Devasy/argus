import uuid
from datetime import datetime, timezone

import pytest

from argus.db import session_factory
from argus.domain.models import MergeRequest, Repository, Review


@pytest.fixture
async def canceled_review(engine):
    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/cancel-test",
                          gitlab_project_id=90000300)
        s.add(repo)
        await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr)
        await s.flush()
        review = Review(mr_id=mr.id, status="canceled",
                        started_at=datetime.now(timezone.utc))
        s.add(review)
        await s.flush()
        review_id = review.id
        await s.commit()
    yield sf, review_id
    async with sf() as s:
        from sqlalchemy import delete
        await s.execute(delete(Review).where(Review.mr_id == mr.id))
        await s.execute(delete(MergeRequest).where(MergeRequest.id == mr.id))
        await s.execute(delete(Repository).where(Repository.id == repo.id))
        await s.commit()


async def test_raise_if_canceled_raises_when_status_is_canceled(canceled_review):
    from argus.review.pipeline import ReviewCanceled, _raise_if_canceled

    sf, review_id = canceled_review
    with pytest.raises(ReviewCanceled):
        await _raise_if_canceled(sf, str(review_id))


async def test_raise_if_canceled_noop_when_status_is_running(engine):
    from argus.db import session_factory
    from argus.review.pipeline import _raise_if_canceled

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/cancel-test-2",
                          gitlab_project_id=90000301)
        s.add(repo)
        await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr)
        await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review)
        await s.flush()
        review_id = review.id
        await s.commit()

    await _raise_if_canceled(sf, str(review_id))  # should not raise

    async with sf() as s:
        from sqlalchemy import delete
        await s.execute(delete(Review).where(Review.mr_id == mr.id))
        await s.execute(delete(MergeRequest).where(MergeRequest.id == mr.id))
        await s.execute(delete(Repository).where(Repository.id == repo.id))
        await s.commit()


async def test_execute_review_job_preserves_canceled_status(engine, settings):
    from argus.db import session_factory
    from argus.review.runner import _should_preserve_canceled_status

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/cancel-runner",
                          gitlab_project_id=90000302)
        s.add(repo)
        await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr)
        await s.flush()
        review = Review(mr_id=mr.id, status="canceled",
                        llm_config={"provider": "openai", "model": "gpt-4o-mini",
                                   "api_key": "x", "base_url": None})
        s.add(review)
        await s.flush()
        review_id = review.id
        await s.commit()

    # Exercise the exact guard used in runner.py's finally-block: a review
    # that was canceled while running must not be flipped to "done"/"failed".
    async with sf() as s:
        review = await s.get(Review, review_id)
        if not _should_preserve_canceled_status(review):
            review.status = "done"
        await s.commit()

    async with sf() as s:
        review = await s.get(Review, review_id)
        assert review.status == "canceled"

    async with sf() as s:
        from sqlalchemy import delete
        await s.execute(delete(Review).where(Review.mr_id == mr.id))
        await s.execute(delete(MergeRequest).where(MergeRequest.id == mr.id))
        await s.execute(delete(Repository).where(Repository.id == repo.id))
        await s.commit()


async def test_execute_review_job_skips_start_when_already_canceled(engine, settings):
    """Regression test for the queued-cancel race (fix #2 in runner.py).

    If the cancel endpoint wins the race and sets Review.status="canceled"
    before execute_review_job gets a chance to run, the job must bail out
    immediately: it must NOT overwrite status back to "running" and must not
    attempt any GitLab/workspace/LLM work. We don't mock GitLab/workspace/LLM
    here — the point of this test is precisely that execute_review_job
    returns before ever reaching that code, so no mocking is needed. If the
    early-return check were missing or broken, this test would hang or
    error out trying to reach a real GitLab/workspace/LLM setup, since none
    of those are configured/mocked here.
    """
    from argus.review.runner import execute_review_job

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/cancel-race",
                          gitlab_project_id=90000304)
        s.add(repo)
        await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr)
        await s.flush()
        review = Review(mr_id=mr.id, status="canceled",
                        llm_config={"provider": "openai", "model": "gpt-4o-mini",
                                   "api_key": "x", "base_url": None})
        s.add(review)
        await s.flush()
        review_id = review.id
        await s.commit()

    # Should return early without raising and without touching GitLab/LLM.
    await execute_review_job(sf, settings, {"review_id": str(review_id)})

    async with sf() as s:
        review = await s.get(Review, review_id)
        # Still "canceled" -- never got flipped to "running", and the
        # finally-block that would set "done"/"failed" was never reached
        # either since the function returned before the try/finally.
        assert review.status == "canceled"
        assert review.started_at is None

    async with sf() as s:
        from sqlalchemy import delete
        await s.execute(delete(Review).where(Review.mr_id == mr.id))
        await s.execute(delete(MergeRequest).where(MergeRequest.id == mr.id))
        await s.execute(delete(Repository).where(Repository.id == repo.id))
        await s.commit()


async def test_should_preserve_canceled_status_returns_false_for_running(engine):
    """Regression guard: a non-canceled review must be allowed to transition
    to a terminal status ("done"/"failed") in the finally-block."""
    from argus.review.runner import _should_preserve_canceled_status

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/cancel-runner-2",
                          gitlab_project_id=90000303)
        s.add(repo)
        await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr)
        await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review)
        await s.flush()
        review_id = review.id
        await s.commit()

    async with sf() as s:
        review = await s.get(Review, review_id)
        assert _should_preserve_canceled_status(review) is False

    async with sf() as s:
        from sqlalchemy import delete
        await s.execute(delete(Review).where(Review.mr_id == mr.id))
        await s.execute(delete(MergeRequest).where(MergeRequest.id == mr.id))
        await s.execute(delete(Repository).where(Repository.id == repo.id))
        await s.commit()

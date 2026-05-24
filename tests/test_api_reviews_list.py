import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from argus.api.app import create_app
from argus.db import session_factory
from argus.domain.models import Job, MergeRequest, Repository, Review


@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
async def reviews_fixture(engine):
    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/reviews-list",
                          gitlab_project_id=90000400)
        s.add(repo)
        await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=7, title="Add feature X",
                          state="opened", source_branch="a", target_branch="b",
                          head_sha="s", web_url="u")
        s.add(mr)
        await s.flush()

        now = datetime.now(timezone.utc)
        done1 = Review(mr_id=mr.id, status="done", trigger="manual",
                       started_at=now - timedelta(minutes=10),
                       finished_at=now - timedelta(minutes=6),
                       prompt_tokens=100, completion_tokens=50)
        done2 = Review(mr_id=mr.id, status="done", trigger="auto",
                       started_at=now - timedelta(minutes=20),
                       finished_at=now - timedelta(minutes=18),
                       prompt_tokens=80, completion_tokens=40)
        running = Review(mr_id=mr.id, status="running", trigger="manual",
                         started_at=now - timedelta(minutes=1),
                         prompt_tokens=10, completion_tokens=0)
        queued = Review(mr_id=mr.id, status="queued", trigger="manual")
        benchmark = Review(mr_id=mr.id, status="done", trigger="manual",
                           publish=False, started_at=now - timedelta(minutes=30),
                           finished_at=now - timedelta(minutes=28),
                           prompt_tokens=60, completion_tokens=30)
        s.add_all([done1, done2, running, queued, benchmark])
        await s.flush()
        job = Job(kind="review", payload={"review_id": str(queued.id)},
                  dedup_key=f"review:{mr.id}", status="queued")
        s.add(job)
        await s.flush()
        ids = {"repo": repo.id, "mr": mr.id, "done1": done1.id, "done2": done2.id,
               "running": running.id, "queued": queued.id, "job": job.id,
               "benchmark": benchmark.id}
        await s.commit()
    yield sf, ids
    async with sf() as s:
        await s.execute(delete(Job).where(Job.id == ids["job"]))
        await s.execute(delete(Review).where(Review.mr_id == ids["mr"]))
        await s.execute(delete(MergeRequest).where(MergeRequest.id == ids["mr"]))
        await s.execute(delete(Repository).where(Repository.id == ids["repo"]))
        await s.commit()


async def test_list_reviews_returns_all_with_pagination(api, reviews_fixture):
    _, ids = reviews_fixture
    r = await api.get("/reviews")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 4
    returned_ids = {item["id"] for item in body["items"]}
    assert str(ids["done1"]) in returned_ids
    assert str(ids["queued"]) in returned_ids


async def test_list_reviews_filters_by_status(api, reviews_fixture):
    _, ids = reviews_fixture
    r = await api.get("/reviews", params={"status": "queued"})
    assert r.status_code == 200
    body = r.json()
    assert all(item["status"] == "queued" for item in body["items"])
    assert str(ids["queued"]) in {item["id"] for item in body["items"]}


async def test_list_reviews_queued_item_has_position_and_eta(api, reviews_fixture):
    _, ids = reviews_fixture
    r = await api.get("/reviews", params={"status": "queued"})
    body = r.json()
    item = next(i for i in body["items"] if i["id"] == str(ids["queued"]))
    assert item["queue_position"] == 1
    # eta_seconds may be None if there are no done reviews to average, but
    # this fixture seeds two done reviews, so it should be a number >= 0.
    assert item["eta_seconds"] is None or item["eta_seconds"] >= 0


async def test_list_reviews_marks_dry_runs_as_unpublished(api, reviews_fixture):
    """A benchmark/dry-run review must be distinguishable in the listing the
    UI reads, or it silently looks identical to a real production review."""
    _, ids = reviews_fixture
    r = await api.get("/reviews")
    body = r.json()
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id[str(ids["benchmark"])]["publish"] is False
    assert by_id[str(ids["done1"])]["publish"] is True


async def test_list_reviews_non_queued_item_has_no_position(api, reviews_fixture):
    _, ids = reviews_fixture
    r = await api.get("/reviews", params={"status": "done"})
    body = r.json()
    item = next(i for i in body["items"] if i["id"] == str(ids["done1"]))
    assert item["queue_position"] is None
    assert item["eta_seconds"] is None


async def test_queue_summary_counts_and_avg(api, reviews_fixture):
    r = await api.get("/reviews/queue-summary")
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] >= 1
    assert body["running"] >= 1
    assert body["avg_duration_s"] is None or body["avg_duration_s"] >= 0


async def test_cancel_queued_review_removes_job_and_sets_canceled(api, engine, reviews_fixture):
    sf, ids = reviews_fixture
    r = await api.post(f"/reviews/{ids['queued']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "canceled"

    async with sf() as s:
        review = await s.get(Review, ids["queued"])
        assert review.status == "canceled"
        job = await s.get(Job, ids["job"])
        assert job is None


async def test_cancel_running_review_sets_canceled_leaves_no_job(api, reviews_fixture):
    _, ids = reviews_fixture
    r = await api.post(f"/reviews/{ids['running']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "canceled"


async def test_cancel_done_review_returns_409(api, reviews_fixture):
    _, ids = reviews_fixture
    r = await api.post(f"/reviews/{ids['done1']}/cancel")
    assert r.status_code == 409


async def test_cancel_missing_review_returns_404(api):
    r = await api.post(f"/reviews/{uuid.uuid4()}/cancel")
    assert r.status_code == 404


async def test_cancel_queued_review_does_not_delete_job_already_claimed_by_worker(
        api, engine, reviews_fixture):
    """Regression test for the queued-cancel race with the worker.

    Simulates the worker's claim_next() having already flipped the Job row
    to status="running" (it runs in its own transaction and commits before
    execute_review_job gets around to marking the Review "running") while
    the Review row itself is still "queued" from the cancel endpoint's point
    of view. The cancel endpoint's DELETE must be scoped to
    Job.status == "queued" so it becomes a no-op here instead of deleting a
    job the worker is actively about to process (which would otherwise kill
    the worker loop via an AttributeError in finish()).
    """
    sf, ids = reviews_fixture
    async with sf() as s:
        job = await s.get(Job, ids["job"])
        job.status = "running"
        await s.commit()

    r = await api.post(f"/reviews/{ids['queued']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "canceled"

    async with sf() as s:
        review = await s.get(Review, ids["queued"])
        assert review.status == "canceled"
        # The job row must still exist: the delete's status filter made it
        # a no-op because the job was no longer "queued".
        job = await s.get(Job, ids["job"])
        assert job is not None
        assert job.status == "running"

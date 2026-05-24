import uuid

import httpx
import pytest
from sqlalchemy import delete, or_, select, update

from argus.api.app import create_app
from argus.domain.models import (AuditRun, AuditVerdict, Job, Learning,
                                     LLMEndpoint, MergeRequest,
                                     ProfileVersion, Repository, Review,
                                     ReviewerProfile, ReviewerProxy, ReviewStage)


@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    # This fixture exercises the app's real commit path (unlike the `db`
    # fixture used elsewhere, which rolls back), so clean up any rows it
    # persisted to avoid leaking state into other test modules that share
    # this session-scoped database (e.g. test_normalizer.py's "grp/ncte").
    async with engine.begin() as conn:
        force_trigger_repo_ids = select(Repository.id).where(
            Repository.project_path == "grp/force-trigger")
        force_trigger_mr_ids = select(MergeRequest.id).where(
            MergeRequest.repo_id.in_(force_trigger_repo_ids))
        # trigger_review() enqueues a Job with dedup_key=f"review:{mr_id}:{mode}"
        # for each MR under this repo; clean those up too so they don't
        # leak into other test modules that claim jobs by type (e.g.
        # test_jobs.py::test_claim_and_finish).
        force_trigger_mr_id_rows = (await conn.execute(force_trigger_mr_ids)).scalars().all()
        force_trigger_dedup_prefixes = [f"review:{mr_id}:%" for mr_id in force_trigger_mr_id_rows]
        if force_trigger_dedup_prefixes:
            await conn.execute(delete(Job).where(
                or_(*(Job.dedup_key.like(p) for p in force_trigger_dedup_prefixes))))
        await conn.execute(delete(Review).where(
            Review.mr_id.in_(force_trigger_mr_ids)))
        await conn.execute(delete(MergeRequest).where(
            MergeRequest.repo_id.in_(force_trigger_repo_ids)))
        enriched_review_repo_ids = select(Repository.id).where(
            Repository.project_path == "grp/enriched-review")
        enriched_review_mr_ids = select(MergeRequest.id).where(
            MergeRequest.repo_id.in_(enriched_review_repo_ids))
        enriched_review_ids = select(Review.id).where(
            Review.mr_id.in_(enriched_review_mr_ids))
        await conn.execute(delete(ReviewStage).where(
            ReviewStage.review_id.in_(enriched_review_ids)))
        await conn.execute(delete(Review).where(
            Review.mr_id.in_(enriched_review_mr_ids)))
        await conn.execute(delete(MergeRequest).where(
            MergeRequest.repo_id.in_(enriched_review_repo_ids)))
        await conn.execute(delete(Repository).where(
            Repository.project_path.in_(["grp/ncte", "grp/upd", "grp/force-trigger",
                                          "grp/enriched-review"])))
        await conn.execute(delete(LLMEndpoint).where(LLMEndpoint.name == "test-ep"))
        # Profile cleanup: reviewer_profiles.current_version_id and
        # profile_versions.profile_id form a circular FK, so null the
        # current_version pointer before deleting versions, then the profile.
        prof_ids = select(ReviewerProfile.id).where(
            ReviewerProfile.name == "t5-prof")
        await conn.execute(update(ReviewerProfile)
                           .where(ReviewerProfile.name == "t5-prof")
                           .values(current_version_id=None))
        await conn.execute(delete(ProfileVersion).where(
            ProfileVersion.profile_id.in_(prof_ids)))
        await conn.execute(delete(ReviewerProfile).where(
            ReviewerProfile.name == "t5-prof"))
        await conn.execute(delete(ReviewerProxy).where(ReviewerProxy.reviewer == "upd-bot"))


async def test_health(api):
    r = await api.get("/health")
    # no LLMEndpoint configured in this fixture -- resolve_llm_config raises
    # ValueError, which /health treats as "not this check's concern"
    assert r.status_code == 200 and r.json() == {"status": "ok", "llm_ok": True}


async def test_health_reports_degraded_when_llm_endpoint_down(api, engine):
    import respx

    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        ep = LLMEndpoint(name="down-llama", provider="ollama",
                         base_url="http://llama-test-down:8080/v1", model="m",
                         is_default=True)
        session.add(ep)
        await session.commit()
    try:
        with respx.mock:
            respx.get("http://llama-test-down:8080/health").mock(
                side_effect=httpx.ConnectError("connection refused"))
            r = await api.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "degraded", "llm_ok": False}
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(LLMEndpoint).where(LLMEndpoint.name == "down-llama"))


async def test_repositories_crud(api, monkeypatch):
    async def fake_get_project(self, project):
        return {"id": 848, "path_with_namespace": "grp/ncte",
                "default_branch": "develop"}
    from argus.gitlab.client import GitLabClient
    monkeypatch.setattr(GitLabClient, "get_project", fake_get_project)

    r = await api.post("/repositories", json={"project_path": "grp/ncte"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["gitlab_project_id"] == 848

    r = await api.get("/repositories", params={"per_page": 200})
    assert any(x["project_path"] == "grp/ncte" for x in r.json()["items"])


async def test_repositories_include_mr_count(api, monkeypatch, engine):
    async def fake_get_project(self, project):
        return {"id": 848001, "path_with_namespace": "grp/mrcount",
                "default_branch": "develop"}
    from argus.gitlab.client import GitLabClient
    monkeypatch.setattr(GitLabClient, "get_project", fake_get_project)

    r = await api.post("/repositories", json={"project_path": "grp/mrcount"})
    assert r.status_code == 201, r.text
    repo_id = r.json()["id"]

    r = await api.get("/repositories", params={"per_page": 200})
    assert r.status_code == 200
    repo = next(x for x in r.json()["items"] if x["id"] == repo_id)
    assert repo["mr_count"] == 0

    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        for iid in (1, 2):
            mr = MergeRequest(repo_id=uuid.UUID(repo_id), mr_iid=iid, title="t",
                              state="opened", source_branch="a", target_branch="b",
                              head_sha=f"s{iid}", web_url="u")
            session.add(mr)
        await session.commit()

    r = await api.get("/repositories", params={"per_page": 200})
    assert r.status_code == 200
    repo = next(x for x in r.json()["items"] if x["id"] == repo_id)
    assert repo["mr_count"] == 2

    r = await api.get(f"/repositories/{repo_id}")
    assert r.status_code == 200
    assert r.json()["mr_count"] == 2


async def test_list_mrs_paginates_and_filters_by_state(api, monkeypatch, engine):
    async def fake_get_project(self, project):
        return {"id": 848002, "path_with_namespace": "grp/mrstate",
                "default_branch": "develop"}
    from argus.gitlab.client import GitLabClient
    monkeypatch.setattr(GitLabClient, "get_project", fake_get_project)

    r = await api.post("/repositories", json={"project_path": "grp/mrstate"})
    assert r.status_code == 201, r.text
    repo_id = r.json()["id"]

    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        for iid, state in ((1, "opened"), (2, "opened"), (3, "merged"), (4, "closed")):
            session.add(MergeRequest(
                repo_id=uuid.UUID(repo_id), mr_iid=iid, title=f"mr {iid}",
                state=state, source_branch="a", target_branch="b",
                head_sha=f"s{iid}", web_url="u"))
        await session.commit()

    r = await api.get(f"/repositories/{repo_id}/merge-requests",
                      params={"per_page": 2, "page": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 4
    assert body["page"] == 1
    assert body["per_page"] == 2
    assert len(body["items"]) == 2
    assert body["state_counts"] == {"all": 4, "opened": 2, "merged": 1, "closed": 1}

    r = await api.get(f"/repositories/{repo_id}/merge-requests",
                      params={"per_page": 2, "page": 2})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2

    r = await api.get(f"/repositories/{repo_id}/merge-requests",
                      params={"state": "opened"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert all(item["state"] == "opened" for item in body["items"])
    # state_counts reflects the full repo, not the filtered subset.
    assert body["state_counts"]["all"] == 4

    r = await api.get(f"/repositories/{repo_id}/merge-requests",
                      params={"state": "bogus"})
    assert r.status_code == 422


async def test_trigger_review_without_llm_endpoint_returns_400(api, engine):
    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        repo = Repository(provider="gitlab",
                          project_path="grp/no-llm-endpoint-test",
                          gitlab_project_id=90000001)
        session.add(repo)
        await session.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        session.add(mr)
        await session.commit()
        mr_id = mr.id

    try:
        r = await api.post(f"/merge-requests/{mr_id}/reviews", json={})
        assert r.status_code == 400, r.text
        assert r.json()["detail"]
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(Repository.id == repo.id))


async def test_repository_get_and_update(api, monkeypatch):
    async def fake_get_project(self, project):
        return {"id": 849, "path_with_namespace": "grp/upd",
                "default_branch": "main"}
    from argus.gitlab.client import GitLabClient
    monkeypatch.setattr(GitLabClient, "get_project", fake_get_project)

    r = await api.post("/repositories", json={"project_path": "grp/upd"})
    repo_id = r.json()["id"]

    r = await api.get(f"/repositories/{repo_id}")
    assert r.status_code == 200
    assert r.json()["project_path"] == "grp/upd"
    assert r.json()["default_profile_id"] is None

    r = await api.put(f"/repositories/{repo_id}",
                      json={"enabled": False, "poll_interval_s": 300})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False and body["poll_interval_s"] == 300

    # partial update leaves other fields alone
    r = await api.put(f"/repositories/{repo_id}", json={"enabled": True})
    assert r.json()["poll_interval_s"] == 300

    # 422: poll_interval_s below the minimum of 10
    r = await api.put(f"/repositories/{repo_id}", json={"poll_interval_s": 5})
    assert r.status_code == 422, r.text
    r = await api.get(f"/repositories/{repo_id}")
    assert r.json()["poll_interval_s"] == 300  # rejected update changed nothing

    # 422: default_profile_id that is not a known profile
    r = await api.put(f"/repositories/{repo_id}",
                      json={"default_profile_id": str(uuid.uuid4())})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "unknown profile"

    # set a real profile, then clear it with an explicit null
    r = await api.post("/profiles", json={
        "name": "t5-prof", "system_prompt": "x", "guidelines": "",
        "tool_allowlist": [], "model": "m"})
    assert r.status_code == 201, r.text
    profile_id = r.json()["id"]

    r = await api.put(f"/repositories/{repo_id}",
                      json={"default_profile_id": profile_id})
    assert r.status_code == 200, r.text
    assert r.json()["default_profile_id"] == profile_id

    r = await api.put(f"/repositories/{repo_id}",
                      json={"default_profile_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["default_profile_id"] is None  # cleared, not skipped

    r = await api.get("/repositories/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


async def test_reviewer_proxy_update(api, engine):
    r = await api.post("/reviewer-proxies",
                       json={"reviewer": "upd-bot", "proxy_url": "http://p:1"})
    proxy_id = r.json()["id"]
    r = await api.put(f"/reviewer-proxies/{proxy_id}",
                      json={"enabled": False, "proxy_url": "http://p:2"})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    assert r.json()["proxy_url"] == "http://p:2"
    r = await api.put("/reviewer-proxies/00000000-0000-0000-0000-000000000000",
                      json={"enabled": True})
    assert r.status_code == 404


async def test_trigger_review_with_force_agents(api, engine, monkeypatch):
    from argus.domain.models import Review
    async def fake_get_project(self, project):
        return {"id": 90000100, "path_with_namespace": "grp/force-trigger",
                "default_branch": "main"}
    from argus.gitlab.client import GitLabClient
    monkeypatch.setattr(GitLabClient, "get_project", fake_get_project)

    r = await api.post("/repositories", json={"project_path": "grp/force-trigger"})
    repo_id = r.json()["id"]
    from argus.db import session_factory
    sf = session_factory(engine)
    async with sf() as session:
        from argus.domain.models import MergeRequest
        mr = MergeRequest(repo_id=uuid.UUID(repo_id), mr_iid=1, title="t",
                          state="opened", source_branch="a", target_branch="b",
                          head_sha="s", web_url="u")
        session.add(mr)
        await session.commit()
        mr_id = mr.id

    from argus.domain.models import LLMEndpoint
    async with sf() as session:
        ep = LLMEndpoint(name="test-ep", provider="anthropic", model="m",
                         base_url="http://x", is_default=True)
        session.add(ep)
        await session.commit()

    r = await api.post(f"/merge-requests/{mr_id}/reviews",
                       json={"force_agents": ["security-reviewer"]})
    assert r.status_code == 202, r.text
    review_id = r.json()["review_id"]
    async with sf() as session:
        review = await session.get(Review, uuid.UUID(review_id))
        assert review.force_agents == ["security-reviewer"]


async def test_get_review_enriched_shape(api, engine):
    import uuid as _uuid
    from datetime import datetime, timezone
    from argus.db import session_factory
    from argus.domain.models import MergeRequest, Repository, Review, ReviewStage

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/enriched-review",
                          gitlab_project_id=90000200)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s", web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running",
                        started_at=datetime.now(timezone.utc),
                        prompt_tokens=1200, completion_tokens=300)
        s.add(review); await s.flush()
        s.add(ReviewStage(review_id=review.id, stage_name="scout", status="done",
                          finished_at=datetime.now(timezone.utc)))
        await s.commit()
        review_id, mr_id = review.id, mr.id

    r = await api.get(f"/reviews/{review_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["mr_id"] == str(mr_id)
    assert body["prompt_tokens"] == 1200 and body["completion_tokens"] == 300
    assert body["started_at"] is not None and body["finished_at"] is None
    assert body["publish"] is True
    stage = body["stages"][0]
    assert stage["name"] == "scout"
    assert "started_at" in stage and stage["finished_at"] is not None


async def test_get_review_marks_dry_run_as_unpublished(api, engine):
    from argus.db import session_factory
    from argus.domain.models import MergeRequest, Repository, Review

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/dry-run-review",
                          gitlab_project_id=90000201)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s", web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="done", publish=False)
        s.add(review); await s.commit()
        review_id = review.id

    r = await api.get(f"/reviews/{review_id}")
    assert r.json()["publish"] is False


async def test_get_mr_includes_review_history(api, engine):
    import uuid as _uuid
    from datetime import datetime, timezone
    from argus.db import session_factory
    from argus.domain.models import (DistillationRun, MergeRequest, ProfileVersion,
                                         Repository, Review, ReviewerProfile)

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/mr-reviews-test",
                          gitlab_project_id=90000400)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s", web_url="u")
        s.add(mr); await s.flush()
        profile = ReviewerProfile(name="p1", is_builtin=False)
        s.add(profile); await s.flush()
        version = ProfileVersion(profile_id=profile.id, version=3, system_prompt="x")
        s.add(version); await s.flush()
        older = Review(mr_id=mr.id, status="done", trigger="manual",
                       profile_version_id=version.id,
                       prompt_tokens=100, completion_tokens=50,
                       started_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        s.add(older)
        await s.commit()
        # Committed in a separate transaction so its `created_at` (server-side
        # now()) is guaranteed strictly later than `older`'s — Postgres freezes
        # now() for the duration of a single transaction, so inserting both
        # rows in one commit would give them identical timestamps and make
        # the DESC ordering below non-deterministic.
        newer = Review(mr_id=mr.id, status="queued", trigger="webhook",
                       prompt_tokens=0, completion_tokens=0, publish=False)
        s.add(newer)
        await s.commit()
        run = DistillationRun(mr_id=mr.id, status="done", trigger="reconcile",
                              note_ids=[], prompt_tokens=10, completion_tokens=5)
        s.add(run)
        await s.commit()
        mr_id, repo_id = mr.id, repo.id

    try:
        r = await api.get(f"/merge-requests/{mr_id}")
        assert r.status_code == 200
        body = r.json()
        assert len(body["reviews"]) == 2
        # newer (created later, no started_at) sorts first — created_at DESC
        assert body["reviews"][0]["status"] == "queued"
        assert body["reviews"][0]["started_at"] is None
        assert body["reviews"][0]["profile_version"] is None
        assert body["reviews"][0]["publish"] is False
        done_row = body["reviews"][1]
        assert done_row["status"] == "done"
        assert done_row["trigger"] == "manual"
        assert done_row["profile_version"] == "v3"
        assert done_row["tokens"] == 150
        assert done_row["started_at"] is not None
        assert done_row["publish"] is True
        assert len(body["distillation_runs"]) == 1
        assert body["distillation_runs"][0]["status"] == "done"
        assert body["distillation_runs"][0]["tokens"] == 15
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(DistillationRun).where(DistillationRun.mr_id == mr_id))
            await conn.execute(delete(Review).where(Review.mr_id == mr_id))
            await conn.execute(delete(ProfileVersion).where(ProfileVersion.id == version.id))
            await conn.execute(delete(ReviewerProfile).where(ReviewerProfile.id == profile.id))
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_review_sequence_numbers_only_done_reviews_by_finished_at(api, engine):
    from datetime import datetime, timezone
    from argus.db import session_factory
    from argus.domain.models import MergeRequest, MRVersion, Repository, Review

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/mr-seq-test",
                          gitlab_project_id=90000401)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s", web_url="u")
        s.add(mr); await s.flush()

        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        t3 = datetime(2026, 1, 3, tzinfo=timezone.utc)
        t4 = datetime(2026, 1, 4, tzinfo=timezone.utc)

        version_a = MRVersion(mr_id=mr.id, provider_version_id=1,
                              head_commit_sha="sha_a", start_commit_sha="sha_a")
        version_b = MRVersion(mr_id=mr.id, provider_version_id=2,
                              head_commit_sha="sha_b", start_commit_sha="sha_b")
        s.add_all([version_a, version_b]); await s.flush()

        review1 = Review(mr_id=mr.id, status="done", trigger="manual",
                         mr_version_id=version_a.id,
                         prompt_tokens=0, completion_tokens=0, finished_at=t1)
        review2 = Review(mr_id=mr.id, status="failed", trigger="manual",
                         prompt_tokens=0, completion_tokens=0, finished_at=t2)
        review3 = Review(mr_id=mr.id, status="done", trigger="manual",
                         mr_version_id=version_b.id,
                         incremental_files=["x.py", "y.py"],
                         prompt_tokens=0, completion_tokens=0, finished_at=t3)
        review4 = Review(mr_id=mr.id, status="running", trigger="manual",
                         prompt_tokens=0, completion_tokens=0, started_at=t4)
        s.add_all([review1, review2, review3, review4])
        await s.commit()
        mr_id, repo_id = mr.id, repo.id
        review_ids = {"review1": review1.id, "review2": review2.id,
                      "review3": review3.id, "review4": review4.id}

    try:
        r = await api.get(f"/merge-requests/{mr_id}")
        assert r.status_code == 200
        body = r.json()
        by_id = {rev["id"]: rev for rev in body["reviews"]}

        rev1 = by_id[str(review_ids["review1"])]
        assert rev1["sequence_number"] == 1
        assert rev1["head_commit_sha"] == "sha_a"
        assert rev1["incremental_file_count"] is None

        rev2 = by_id[str(review_ids["review2"])]
        assert rev2["sequence_number"] is None
        assert rev2["head_commit_sha"] is None

        rev3 = by_id[str(review_ids["review3"])]
        assert rev3["sequence_number"] == 2
        assert rev3["head_commit_sha"] == "sha_b"
        assert rev3["incremental_file_count"] == 2

        rev4 = by_id[str(review_ids["review4"])]
        assert rev4["sequence_number"] is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(Review).where(Review.mr_id == mr_id))
            await conn.execute(delete(MRVersion).where(MRVersion.mr_id == mr_id))
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_reconcile_and_distill_enqueues_one_batched_run(api, engine, monkeypatch):
    from datetime import datetime, timezone
    from argus.db import session_factory
    from argus.domain.models import Actor, MergeRequest, Note, Repository
    from argus.ingest.reconciler import ReconcileResult

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/reconcile-test",
                          gitlab_project_id=90000401)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=7, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s", web_url="u")
        s.add(mr); await s.flush()
        bot = Actor(username="argus-bot", provider_user_id=1)
        s.add(bot); await s.flush()
        note = Note(mr_id=mr.id, author_id=bot.id, author_type="bot", kind="inline",
                   body="consider renaming this", file_path="a.py", line=1,
                   disposition="open", provider_note_id=555,
                   note_created_at=datetime.now(timezone.utc))
        s.add(note); await s.flush()
        await s.commit()
        mr_id, repo_id, note_id = mr.id, repo.id, note.id

    async def fake_reconcile(session, client, repo, mr_row, settings):
        return ReconcileResult(note_ids=[note_id])

    from argus.api import app as app_module
    monkeypatch.setattr(app_module, "reconcile_mr", fake_reconcile)

    try:
        r = await api.post(f"/merge-requests/{mr_id}/reconcile-and-distill", json={})
        assert r.status_code == 202, r.text
        assert r.json() == {"queued_run": True, "note_count": 1}

        # second call: identical note-id batch -> same dedup_key -> not queued again
        r2 = await api.post(f"/merge-requests/{mr_id}/reconcile-and-distill", json={})
        assert r2.status_code == 202
        assert r2.json() == {"queued_run": False, "note_count": 1}
    finally:
        async with engine.begin() as conn:
            from argus.domain.models import Job, Note as NoteModel
            await conn.execute(delete(Job).where(Job.kind == "distill_mr"))
            await conn.execute(delete(NoteModel).where(NoteModel.mr_id == mr_id))
            await conn.execute(delete(Actor).where(Actor.id == bot.id))
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_get_distillation_run_returns_404_for_unknown_id(api):
    resp = await api.get(f"/distillation-runs/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_distillation_run_trace_returns_empty_lists_for_unknown_run(api):
    resp = await api.get(f"/distillation-runs/{uuid.uuid4()}/trace")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"tool_calls": [], "llm_rounds": []}


async def test_run_distill_mr_job_aggregates_llm_round_tokens(engine, settings, monkeypatch):
    from datetime import datetime, timezone

    from argus.api.app import _run_distill_mr_job
    from argus.db import session_factory
    from argus.domain.models import (Actor, DistillationRun, LLMEndpoint,
                                         LLMRound, MergeRequest, Note, Repository)

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/distill-agg-test",
                          gitlab_project_id=90000501)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=8, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s", web_url="u")
        s.add(mr); await s.flush()
        bot = Actor(username="argus-bot-agg", provider_user_id=2)
        s.add(bot); await s.flush()
        note = Note(mr_id=mr.id, author_id=bot.id, author_type="bot", kind="inline",
                   body="consider renaming this", file_path="a.py", line=1,
                   disposition="open", provider_note_id=556,
                   note_created_at=datetime.now(timezone.utc))
        s.add(note); await s.flush()
        ep = LLMEndpoint(name="distill-agg-ep", provider="anthropic", model="m",
                         base_url="http://x", is_default=True)
        s.add(ep); await s.flush()
        await s.commit()
        mr_id, repo_id, note_id, bot_id, ep_id = mr.id, repo.id, note.id, bot.id, ep.id

    async def fake_run_agentic_distillation_for_mr(
            sf, settings, gitlab, repo, mr, notes, llm_cfg, max_rounds=10,
            distillation_run_id=None):
        async with sf() as s:
            s.add(LLMRound(distillation_run_id=distillation_run_id, stage_name="distill",
                           seq=1, prompt_tokens=100, completion_tokens=40))
            s.add(LLMRound(distillation_run_id=distillation_run_id, stage_name="distill",
                           seq=2, prompt_tokens=25, completion_tokens=10))
            await s.commit()
        return None

    import argus.knowledge.agentic_distiller as distiller_module
    monkeypatch.setattr(distiller_module, "run_agentic_distillation_for_mr",
                       fake_run_agentic_distillation_for_mr)

    class FakeGitLabClient:
        async def aclose(self):
            pass

    monkeypatch.setattr("argus.gitlab.client.GitLabClient",
                       lambda *a, **k: FakeGitLabClient())

    try:
        await _run_distill_mr_job(sf, settings, {"mr_id": str(mr_id), "note_ids": [str(note_id)]})

        async with sf() as s:
            run = (await s.execute(select(DistillationRun).where(
                DistillationRun.mr_id == mr_id))).scalar_one()
            assert run.status == "done"
            assert run.prompt_tokens == 125
            assert run.completion_tokens == 50
    finally:
        async with engine.begin() as conn:
            run_ids = select(DistillationRun.id).where(DistillationRun.mr_id == mr_id)
            await conn.execute(delete(LLMRound).where(
                LLMRound.distillation_run_id.in_(run_ids)))
            await conn.execute(delete(DistillationRun).where(DistillationRun.mr_id == mr_id))
            await conn.execute(delete(Note).where(Note.mr_id == mr_id))
            await conn.execute(delete(Actor).where(Actor.id == bot_id))
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))
            await conn.execute(delete(LLMEndpoint).where(LLMEndpoint.id == ep_id))


async def test_learnings_endpoint_exposes_strength(api, engine):
    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        learning = Learning(topic="strength-test-topic", hint_text="h", hit_count=8,
                            harmful_count=1, ignored_count=2, miss_count=0)
        session.add(learning)
        await session.commit()
        learning_id = learning.id

    try:
        r = await api.get("/learnings")
        assert r.status_code == 200
        row = next(x for x in r.json()["items"] if x["topic"] == "strength-test-topic")
        assert row["harmful_count"] == 1
        assert row["ignored_count"] == 2
        assert 0.0 <= row["reputation"] <= 1.0
        assert row["injection_count"] == 11  # 8 + 1 + 2 + 0
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(Learning).where(Learning.id == learning_id))


async def test_approve_verdict_archives_learning(api, engine):
    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        repo = Repository(provider="gitlab", project_path="grp/audit-approve",
                          gitlab_project_id=90100001)
        session.add(repo)
        await session.flush()
        run = AuditRun(repo_id=repo.id, status="done")
        lrn = Learning(repo_id=repo.id, topic="stale thing", hint_text="h")
        session.add_all([run, lrn])
        await session.flush()
        v = AuditVerdict(audit_run_id=run.id, learning_id=lrn.id, verdict="stale",
                         confidence=0.9, rationale="gone",
                         citations=[{"file": "a.py", "line": 1, "quote": "x"}],
                         proposed_action="archive", state="proposed")
        session.add(v)
        await session.commit()
        verdict_id = v.id
        learning_id = lrn.id
        repo_id = repo.id

    try:
        r = await api.post(f"/audit-verdicts/{verdict_id}/approve")
        assert r.status_code == 200, r.text

        async with sf() as session:
            lrn = await session.get(Learning, learning_id)
            assert lrn.status == "archived"
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(AuditVerdict).where(AuditVerdict.id == verdict_id))
            await conn.execute(delete(Learning).where(Learning.id == learning_id))
            await conn.execute(delete(AuditRun).where(AuditRun.repo_id == repo_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_reject_verdict_leaves_learning_active(api, engine):
    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        repo = Repository(provider="gitlab", project_path="grp/audit-reject",
                          gitlab_project_id=90100002)
        session.add(repo)
        await session.flush()
        run = AuditRun(repo_id=repo.id, status="done")
        lrn = Learning(repo_id=repo.id, topic="good thing", hint_text="h")
        session.add_all([run, lrn])
        await session.flush()
        v = AuditVerdict(audit_run_id=run.id, learning_id=lrn.id,
                         verdict="contradicted", confidence=0.9, rationale="r",
                         citations=[{"file": "a.py", "line": 1, "quote": "x"}],
                         proposed_action="archive", state="proposed")
        session.add(v)
        await session.commit()
        verdict_id = v.id
        learning_id = lrn.id
        repo_id = repo.id

    try:
        r = await api.post(f"/audit-verdicts/{verdict_id}/reject")
        assert r.status_code == 200, r.text

        async with sf() as session:
            lrn = await session.get(Learning, learning_id)
            assert lrn.status == "active"
            v = await session.get(AuditVerdict, verdict_id)
            assert v.state == "rejected"
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(AuditVerdict).where(AuditVerdict.id == verdict_id))
            await conn.execute(delete(Learning).where(Learning.id == learning_id))
            await conn.execute(delete(AuditRun).where(AuditRun.repo_id == repo_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_verdict_queue_lists_proposed_only(api, engine):
    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        repo = Repository(provider="gitlab", project_path="grp/audit-queue",
                          gitlab_project_id=90100003)
        session.add(repo)
        await session.flush()
        run = AuditRun(repo_id=repo.id, status="done")
        lrn1 = Learning(repo_id=repo.id, topic="queue-proposed", hint_text="h")
        lrn2 = Learning(repo_id=repo.id, topic="queue-rejected", hint_text="h")
        session.add_all([run, lrn1, lrn2])
        await session.flush()
        v_proposed = AuditVerdict(audit_run_id=run.id, learning_id=lrn1.id,
                                  verdict="stale", confidence=0.9, rationale="r",
                                  citations=[{"file": "a.py", "line": 1, "quote": "x"}],
                                  proposed_action="archive", state="proposed")
        v_rejected = AuditVerdict(audit_run_id=run.id, learning_id=lrn2.id,
                                  verdict="stale", confidence=0.9, rationale="r",
                                  citations=[{"file": "a.py", "line": 1, "quote": "x"}],
                                  proposed_action="archive", state="rejected")
        session.add_all([v_proposed, v_rejected])
        await session.commit()
        proposed_id = v_proposed.id
        rejected_id = v_rejected.id
        learning_ids = [lrn1.id, lrn2.id]
        repo_id = repo.id

    try:
        r = await api.get("/audit-verdicts?state=proposed")
        assert r.status_code == 200
        body = r.json()
        assert all(x["state"] == "proposed" for x in body["items"])
        ids = {x["id"] for x in body["items"]}
        assert str(proposed_id) in ids
        assert str(rejected_id) not in ids
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(AuditVerdict).where(
                AuditVerdict.id.in_([proposed_id, rejected_id])))
            await conn.execute(delete(Learning).where(Learning.id.in_(learning_ids)))
            await conn.execute(delete(AuditRun).where(AuditRun.repo_id == repo_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_verdict_queue_hides_no_action_verdicts_by_default(api, engine):
    """'none' means the auditor checked a learning and left it alone -- there
    is nothing to approve, and approving one changes nothing. They outnumbered
    actionable verdicts 3:1 in production (31 of 40, 14 of them approved to no
    effect) and buried the handful that needed a decision."""
    from argus.db import session_factory

    sf = session_factory(engine)
    async with sf() as session:
        repo = Repository(provider="gitlab", project_path="grp/audit-noaction",
                          gitlab_project_id=90100004)
        session.add(repo)
        await session.flush()
        run = AuditRun(repo_id=repo.id, status="done")
        lrn1 = Learning(repo_id=repo.id, topic="needs-decision", hint_text="h")
        lrn2 = Learning(repo_id=repo.id, topic="left-alone", hint_text="h")
        session.add_all([run, lrn1, lrn2])
        await session.flush()
        actionable = AuditVerdict(
            audit_run_id=run.id, learning_id=lrn1.id, verdict="stale",
            confidence=0.9, rationale="r",
            citations=[{"file": "a.py", "line": 1, "quote": "x"}],
            proposed_action="archive", state="proposed")
        no_action = AuditVerdict(
            audit_run_id=run.id, learning_id=lrn2.id, verdict="corroborated",
            confidence=0.9, rationale="r",
            citations=[{"file": "a.py", "line": 1, "quote": "x"}],
            proposed_action="none", state="proposed")
        session.add_all([actionable, no_action])
        await session.commit()
        actionable_id, no_action_id = actionable.id, no_action.id
        learning_ids = [lrn1.id, lrn2.id]
        repo_id = repo.id

    try:
        r = await api.get("/audit-verdicts?state=proposed")
        ids = {x["id"] for x in r.json()["items"]}
        assert str(actionable_id) in ids
        assert str(no_action_id) not in ids

        r = await api.get("/audit-verdicts?state=proposed&include_no_action=true")
        ids = {x["id"] for x in r.json()["items"]}
        assert str(actionable_id) in ids and str(no_action_id) in ids
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(AuditVerdict).where(
                AuditVerdict.id.in_([actionable_id, no_action_id])))
            await conn.execute(delete(Learning).where(Learning.id.in_(learning_ids)))
            await conn.execute(delete(AuditRun).where(AuditRun.repo_id == repo_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_audit_runs_are_listable_and_traceable(api, engine):
    """Reviews and distillation runs were both listable and traceable; audits
    were neither, so a run that stalled at one cluster for eleven minutes
    could only be inspected via SQL."""
    from argus.db import session_factory
    from argus.domain.models import LLMRound, ToolCall

    sf = session_factory(engine)
    async with sf() as session:
        repo = Repository(provider="gitlab", project_path="grp/audit-runs",
                          gitlab_project_id=90100005)
        session.add(repo)
        await session.flush()
        run = AuditRun(repo_id=repo.id, status="done", audited_ref="develop",
                       commit_sha="abc123def456", clusters_examined=2,
                       verdicts_written=8, langfuse_trace_id="tr-abc")
        session.add(run)
        await session.flush()
        session.add_all([
            ToolCall(audit_run_id=run.id, stage_name="audit", seq=1,
                     tool_name="search_code", status="ok", duration_ms=12),
            LLMRound(audit_run_id=run.id, stage_name="audit", seq=1,
                     prompt_tokens=100, completion_tokens=20, latency_ms=900),
        ])
        await session.commit()
        run_id, repo_id = run.id, repo.id

    try:
        r = await api.get("/audit-runs")
        assert r.status_code == 200
        row = next(x for x in r.json()["items"] if x["id"] == str(run_id))
        assert row["repo_path"] == "grp/audit-runs"
        assert row["audited_ref"] == "develop"
        assert row["verdicts_written"] == 8
        assert row["langfuse_trace_id"] == "tr-abc"

        r = await api.get(f"/audit-runs/{run_id}/trace")
        assert r.status_code == 200
        body = r.json()
        assert [t["tool"] for t in body["tool_calls"]] == ["search_code"]
        assert body["llm_rounds"][0]["prompt_tokens"] == 100
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(ToolCall).where(ToolCall.audit_run_id == run_id))
            await conn.execute(delete(LLMRound).where(LLMRound.audit_run_id == run_id))
            await conn.execute(delete(AuditRun).where(AuditRun.id == run_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))


async def test_trigger_review_publish_flag_reaches_the_review_row(
        api, engine, monkeypatch):
    """The golden-set harness is driven through this flag, so it has to survive
    the trip from request body to Review row. Defaults to publishing."""
    from argus.domain.models import Review

    async def fake_get_project(self, project):
        return {"id": 90000105, "path_with_namespace": "grp/dry-trigger",
                "default_branch": "main"}
    from argus.gitlab.client import GitLabClient
    monkeypatch.setattr(GitLabClient, "get_project", fake_get_project)

    r = await api.post("/repositories", json={"project_path": "grp/dry-trigger"})
    repo_id = r.json()["id"]
    from argus.db import session_factory
    sf = session_factory(engine)
    async with sf() as session:
        from argus.domain.models import MergeRequest
        mr = MergeRequest(repo_id=uuid.UUID(repo_id), mr_iid=1, title="t",
                          state="opened", source_branch="a", target_branch="b",
                          head_sha="s", web_url="u")
        session.add(mr)
        await session.commit()
        mr_id = mr.id

    # Only create a default endpoint if this DB has none: a second one makes
    # resolve_llm_config ambiguous and breaks unrelated tests.
    from sqlalchemy import delete, select
    from argus.domain.models import LLMEndpoint
    created_endpoint = False
    async with sf() as session:
        existing = (await session.execute(
            select(LLMEndpoint).where(LLMEndpoint.is_default.is_(True))
        )).scalars().first()
        if existing is None:
            session.add(LLMEndpoint(name="dry-ep", provider="anthropic",
                                    model="m", base_url="http://x",
                                    is_default=True))
            await session.commit()
            created_endpoint = True

    try:
        r = await api.post(f"/merge-requests/{mr_id}/reviews",
                           json={"publish": False})
        assert r.status_code == 202, r.text
        async with sf() as session:
            review = await session.get(Review, uuid.UUID(r.json()["review_id"]))
            assert review.publish is False

        # A dry run must not block a real review of the same MR, and vice
        # versa: they carry different dedup keys.
        r2 = await api.post(f"/merge-requests/{mr_id}/reviews", json={})
        assert r2.status_code == 202, r2.text
        async with sf() as session:
            review2 = await session.get(Review, uuid.UUID(r2.json()["review_id"]))
            assert review2.publish is True

        # ...but a second dry run while one is in flight still dedups.
        r3 = await api.post(f"/merge-requests/{mr_id}/reviews",
                            json={"publish": False})
        assert r3.status_code == 409, r3.text
    finally:
        # Triggering leaves queued Jobs behind, and an unrelated test claims
        # "the next queued job" -- so this has to tidy up after itself.
        from argus.domain.models import Job, MergeRequest, Repository
        async with engine.begin() as conn:
            await conn.execute(delete(Job).where(
                Job.dedup_key.like(f"review:{mr_id}%")))
            await conn.execute(delete(Review).where(Review.mr_id == mr_id))
            await conn.execute(delete(MergeRequest).where(
                MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(
                Repository.id == uuid.UUID(repo_id)))
            if created_endpoint:
                await conn.execute(
                    delete(LLMEndpoint).where(LLMEndpoint.name == "dry-ep"))


def test_stage_dict_reports_candidate_and_verdict_counts():
    """The graph shows how many candidates a stage produced and how many
    verify let through, so the funnel is visible without opening the DB."""
    from argus.api.app import _stage_dict
    from argus.domain.models import ReviewStage

    analyze = ReviewStage(stage_name="analyze", status="done",
                          artifact={"findings": [{"a": 1}, {"b": 2}, {"c": 3}]})
    assert _stage_dict(analyze)["produced"] == 3
    assert _stage_dict(analyze)["allowed"] is None

    verify = ReviewStage(stage_name="verify", status="done", artifact={
        "verdicts": [{"valid": True}, {"valid": False}, {"valid": True}]})
    d = _stage_dict(verify)
    assert d["produced"] == 3 and d["allowed"] == 2

    publish = ReviewStage(stage_name="publish", status="done",
                          artifact={"published_finding_ids": ["f1", "f2"]})
    assert _stage_dict(publish)["produced"] == 2


def test_stage_dict_is_quiet_when_there_is_nothing_to_count():
    from argus.api.app import _stage_dict
    from argus.domain.models import ReviewStage

    for artifact in (None, {}, {"findings": "not-a-list"}, {"chunks": 2}):
        d = _stage_dict(ReviewStage(stage_name="scout", status="done",
                                    artifact=artifact))
        assert d["produced"] is None and d["allowed"] is None


def test_review_candidates_attach_to_their_graph_node():
    """Analyze findings carry chunk_id, so they belong to the analyze:<chunk>
    node rather than the aggregate analyze stage -- which is why the chunk
    nodes had nothing to show."""
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    stages = [
        ReviewStage(stage_name="analyze", status="done", artifact={"findings": [
            {"finding_id": "f1", "stage": "analysis", "chunk_id": "c1",
             "title": "Bad null check", "file_path": "a.py", "line": 10,
             "severity": "high"},
            {"finding_id": "f2", "stage": "analysis", "chunk_id": "c2",
             "title": "Leaky handle", "file_path": "b.py", "line": 20,
             "severity": "low"},
        ]}),
        ReviewStage(stage_name="design", status="done", artifact={"findings": [
            {"finding_id": "f3", "stage": "design", "title": "Layering",
             "file_path": "c.py", "line": 30, "severity": "medium"},
        ]}),
        ReviewStage(stage_name="verify", status="done", artifact={"verdicts": [
            {"finding_id": "f1", "valid": True, "reason": "traced"},
            {"finding_id": "f2", "valid": False, "reason": "speculative"},
        ]}),
    ]
    got = {c["finding_id"]: c for c in review_candidates(stages)}

    assert got["f1"]["node"] == "analyze:c1"
    assert got["f2"]["node"] == "analyze:c2"
    assert got["f3"]["node"] == "design", "agent findings attach to the agent node"
    assert got["f1"]["valid"] is True and got["f1"]["reason"] == "traced"
    assert got["f2"]["valid"] is False
    assert got["f3"]["valid"] is None, "never verified is not the same as rejected"


def test_chunkless_analyze_findings_land_on_verify():
    """60% of real analyze findings carry no chunk_id, and there is no bare
    "analyze" node in the graph -- without this they are unreachable."""
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    got = review_candidates([
        ReviewStage(stage_name="analyze", status="done", artifact={"findings": [
            {"finding_id": "f9", "title": "No chunk", "file_path": "a.py"}]}),
        ReviewStage(stage_name="verify", status="done", artifact={"verdicts": [
            {"finding_id": "f9", "valid": False, "reason": "speculative"}]}),
    ])
    assert got[0]["node"] == "verify"
    assert got[0]["valid"] is False


def test_review_candidates_tolerates_junk():
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    assert review_candidates([]) == []
    assert review_candidates([
        ReviewStage(stage_name="scout", status="done", artifact=None),
        ReviewStage(stage_name="analyze", status="done",
                    artifact={"findings": "nope"}),
        ReviewStage(stage_name="analyze:c9", status="done",
                    artifact={"findings": [None, {"no_id": 1}]}),
    ]) == []


def test_stage_dict_breaks_analyze_down_by_chunk():
    from argus.api.app import _stage_dict
    from argus.domain.models import ReviewStage

    d = _stage_dict(ReviewStage(stage_name="analyze", status="done", artifact={
        "findings": [{"finding_id": "a", "chunk_id": "c1"},
                     {"finding_id": "b", "chunk_id": "c1"},
                     {"finding_id": "c", "chunk_id": "c2"}]}))
    assert d["produced"] == 3
    assert d["produced_by_chunk"] == {"c1": 2, "c2": 1}


def test_a_finding_appears_once_under_its_most_specific_node():
    """The analyze artifact re-aggregates the agent stages' findings, so every
    design finding also shows up in analyze's list. Without deduping, each one
    rendered twice -- once under `design` and once under the chunkless
    fallback. The specific producer wins."""
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    stages = [
        ReviewStage(stage_name="design", status="done", artifact={"findings": [
            {"finding_id": "design1", "stage": "design", "title": "Layering",
             "file_path": "c.py"}]}),
        ReviewStage(stage_name="analyze", status="done", artifact={"findings": [
            {"finding_id": "design1", "stage": "design", "title": "Layering",
             "file_path": "c.py"},
            {"finding_id": "plain", "title": "Other", "file_path": "d.py"}]}),
        ReviewStage(stage_name="verify", status="done", artifact={"verdicts": [
            {"finding_id": "design1", "valid": True, "reason": "traced"}]}),
    ]
    got = review_candidates(stages)

    assert len(got) == 2
    by_id = {c["finding_id"]: c for c in got}
    assert by_id["design1"]["node"] == "design"
    assert by_id["design1"]["valid"] is True
    assert by_id["plain"]["node"] == "verify"


def test_specific_attribution_wins_regardless_of_stage_order():
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    analyze = ReviewStage(stage_name="analyze", status="done", artifact={
        "findings": [{"finding_id": "d1", "stage": "design", "title": "T",
                      "file_path": "c.py"}]})
    design = ReviewStage(stage_name="design", status="done", artifact={
        "findings": [{"finding_id": "d1", "stage": "design", "title": "T",
                      "file_path": "c.py"}]})

    for order in ([analyze, design], [design, analyze]):
        got = review_candidates(order)
        assert len(got) == 1 and got[0]["node"] == "design", order


def test_node_comes_from_the_findings_own_stage_field():
    """Real data: stage="analysis" findings always carry chunk_id, agent
    findings never do but always carry stage. Deriving the node from the
    artifact it was found in made attribution depend on stage ordering, since
    agent findings appear in BOTH the agent artifact and analyze's."""
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    analyze = ReviewStage(stage_name="analyze", status="done", artifact={
        "findings": [
            {"finding_id": "c1-a1", "stage": "analysis", "chunk_id": "c1",
             "title": "A", "file_path": "a.py"},
            {"finding_id": "design1", "stage": "design", "title": "D",
             "file_path": "b.py"},
        ]})
    design = ReviewStage(stage_name="design", status="done", artifact={
        "findings": [{"finding_id": "design1", "stage": "design", "title": "D",
                      "file_path": "b.py"}]})

    for order in ([analyze, design], [design, analyze]):
        by_id = {c["finding_id"]: c for c in review_candidates(order)}
        assert len(by_id) == 2, order
        assert by_id["c1-a1"]["node"] == "analyze:c1"
        assert by_id["design1"]["node"] == "design", order


def test_specialist_agent_findings_attach_to_their_chunk_scoped_stage():
    """A specialist records its ReviewStage as "{agent}:{chunk}", which is the
    graph node id, but the finding only carries the bare agent name."""
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    got = review_candidates([
        ReviewStage(stage_name="rules_v2-reviewer:c1", status="done", artifact={
            "findings": [{"finding_id": "x1", "stage": "rules_v2-reviewer",
                          "chunk_id": "c1", "title": "T", "file_path": "a.py"}]}),
    ])
    assert got[0]["node"] == "rules_v2-reviewer:c1"


def test_agent_findings_reach_their_node_even_if_that_stage_row_is_missing():
    """Agent stages do fail (20 rules_v2-reviewer rows on record), and a failed
    stage may write no artifact. The finding still exists in analyze's
    aggregate carrying stage="design", and the graph still renders a design
    node (discoverAgentNames also reads the trace) -- so it must attach there
    rather than stranding on verify."""
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    got = review_candidates([
        ReviewStage(stage_name="analyze", status="done", artifact={"findings": [
            {"finding_id": "design1", "stage": "design", "title": "D",
             "file_path": "b.py"}]}),
        ReviewStage(stage_name="verify", status="done", artifact={"verdicts": [
            {"finding_id": "design1", "valid": True, "reason": "ok"}]}),
    ])
    assert len(got) == 1
    assert got[0]["node"] == "design"
    assert got[0]["valid"] is True


def test_ungrounded_findings_are_reported_as_dropped_not_unverified():
    """Grounding runs before verify and removes findings whose evidence quote
    is not present in the source -- the anti-hallucination gate. Those never
    get a verdict, so valid stays None and they rendered as a grey "not
    verified", hiding that they were actively rejected. Real case: review
    c2e66a35 produced 5 findings, verify issued 4 verdicts and listed
    ungrounded=["design5"], so all 5 died but the UI showed 4 dropped."""
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    got = {c["finding_id"]: c for c in review_candidates([
        ReviewStage(stage_name="design", status="done", artifact={"findings": [
            {"finding_id": "design1", "stage": "design", "title": "A",
             "file_path": "a.py"},
            {"finding_id": "design5", "stage": "design", "title": "E",
             "file_path": "e.py"},
        ]}),
        ReviewStage(stage_name="verify", status="done", artifact={
            "verdicts": [{"finding_id": "design1", "valid": False,
                          "reason": "already the convention"}],
            "ungrounded": ["design5"]}),
    ])}

    assert got["design1"]["valid"] is False
    assert got["design1"]["gate"] == "verify"
    assert got["design5"]["valid"] is False, "ungrounded is a rejection"
    assert got["design5"]["gate"] == "grounding"
    assert "evidence" in (got["design5"]["reason"] or "").lower()


def test_a_finding_that_simply_never_reached_verify_stays_unjudged():
    from argus.review.candidates import review_candidates
    from argus.domain.models import ReviewStage

    got = review_candidates([
        ReviewStage(stage_name="design", status="done", artifact={"findings": [
            {"finding_id": "d1", "stage": "design", "title": "A",
             "file_path": "a.py"}]}),
    ])
    assert got[0]["valid"] is None and got[0]["gate"] is None


def test_stage_counts_reads_allowed_from_verdicts_even_without_a_findings_key():
    """The original latent bug: a "first list key wins" loop returned "0
    produced" for a verify artifact that happened to also carry an empty
    findings list, because the OLD key order checked findings first. Fixed
    twice: first by reordering (2ac9a5d, still fragile -- only reordered
    which key wins), then properly by computing both counts independently,
    per argus's follow-up review of the same MR."""
    from argus.api.app import _stage_counts

    produced, allowed = _stage_counts({
        "verdicts": [{"valid": True}, {"valid": False}]})
    assert (produced, allowed) == (2, 1)


def test_stage_counts_still_reads_analyze_and_publish():
    from argus.api.app import _stage_counts

    assert _stage_counts({"findings": [1, 2, 3]}) == (3, None)
    assert _stage_counts({"published_finding_ids": ["a"]}) == (1, None)
    assert _stage_counts({"findings": []}) == (0, None)


def test_stage_counts_computes_produced_and_allowed_independently():
    """argus flagged that the swap to verdicts-first in 2ac9a5d only
    reordered priority, it did not fix the underlying design: 'first list key
    wins' is still fragile if an artifact ever carries both findings and
    verdicts. No real stage does today (checked empirically), but the two
    counts should not depend on which key happens to be checked first."""
    from argus.api.app import _stage_counts

    produced, allowed = _stage_counts({
        "findings": [1, 2, 3, 4],
        "verdicts": [{"valid": True}, {"valid": False}]})
    assert produced == 4, "produced must come from findings when both exist"
    assert allowed == 1

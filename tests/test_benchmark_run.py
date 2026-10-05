import pytest
from sqlalchemy import delete, select

from argus.benchmark import spec
from argus.benchmark.run import trigger
from argus.db import session_factory
from argus.domain.models import (Job, LLMEndpoint, MergeRequest,
                                     Repository, Review)

# A gitlab_project_id no other test file uses (they use 1-2 digit ids or
# 90100-range) -- keeps this file's committed rows unambiguously identifiable
# for cleanup without touching other tests' committed rows in the same
# session-scoped test database.
_PROJECT_ID = 990100


@pytest.fixture
async def committing_db(db, engine):
    """trigger() opens its own session via sf(), so anything it must see (an
    LLMEndpoint, the seeded Repository/MergeRequest) has to be committed, not
    just flushed -- separate connections can't see another's uncommitted
    work. That means the `db` fixture's usual rollback-on-teardown does NOT
    clean these up (the engine fixture is session-scoped, shared by every
    test file), so this deletes exactly the rows this file creates
    afterward -- surgically, by the markers below, not a blanket table wipe,
    since other committing tests in this suite share the same database."""
    yield db
    async with session_factory(engine)() as cleanup:
        repo_ids = (await cleanup.execute(
            select(Repository.id).where(
                Repository.gitlab_project_id == _PROJECT_ID))).scalars().all()
        if repo_ids:
            mr_ids = (await cleanup.execute(
                select(MergeRequest.id).where(
                    MergeRequest.repo_id.in_(repo_ids)))).scalars().all()
            if mr_ids:
                review_ids = (await cleanup.execute(
                    select(Review.id).where(
                        Review.mr_id.in_(mr_ids)))).scalars().all()
                if review_ids:
                    # trigger() -> enqueue() leaves a row in the shared jobs
                    # table too -- not cleaning this up left a 'review' job
                    # behind that test_jobs.py::test_claim_and_finish then
                    # picked up, expecting nothing left to claim.
                    await cleanup.execute(delete(Job).where(
                        Job.kind == "review",
                        Job.payload["review_id"].astext.in_(
                            [str(i) for i in review_ids])))
                await cleanup.execute(
                    delete(Review).where(Review.mr_id.in_(mr_ids)))
                await cleanup.execute(
                    delete(MergeRequest).where(MergeRequest.id.in_(mr_ids)))
            await cleanup.execute(
                delete(Repository).where(Repository.id.in_(repo_ids)))
        await cleanup.execute(delete(LLMEndpoint).where(
            LLMEndpoint.name.in_(["the-default", "groq-qwen38"])))
        await cleanup.commit()


async def _seed_first_golden_mr(db):
    gm = spec.load().merge_requests[0]
    repo = Repository(provider="gitlab", project_path=gm.project_path,
                      gitlab_project_id=_PROJECT_ID)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=gm.mr_iid, title=gm.title,
                      state="merged", source_branch="s", target_branch="m",
                      head_sha="abc", web_url="http://x")
    db.add(mr)
    await db.flush()
    return mr


async def test_trigger_uses_is_default_when_no_endpoint_name_given(
        committing_db, engine, monkeypatch):
    monkeypatch.setenv("TEST_DEFAULT_KEY", "k-default")
    mr = await _seed_first_golden_mr(committing_db)
    default_ep = LLMEndpoint(name="the-default", provider="anthropic",
                             model="anthropic/claude-sonnet-4-5",
                             api_key_ref="TEST_DEFAULT_KEY", is_default=True)
    committing_db.add(default_ep)
    await committing_db.commit()

    sf = session_factory(engine)
    await trigger(sf, dry_run=False, scored_only=False)

    async with sf() as s:
        review = (await s.execute(
            select(Review).where(Review.mr_id == mr.id))).scalar_one()
        assert review.llm_config["model"] == "anthropic/claude-sonnet-4-5"


async def test_trigger_uses_named_endpoint_without_touching_is_default(
        committing_db, engine, monkeypatch):
    """The whole point: a benchmark run must be able to target a specific
    endpoint (e.g. Groq) without flipping is_default, because is_default is
    the same row the production poller reads for real GitLab-triggered
    reviews (resolve_llm_config: endpoint_id is None -> is_default)."""
    monkeypatch.setenv("TEST_DEFAULT_KEY", "k-default")
    monkeypatch.setenv("TEST_GROQ_KEY", "k-groq")
    mr = await _seed_first_golden_mr(committing_db)
    default_ep = LLMEndpoint(name="the-default", provider="anthropic",
                             model="anthropic/claude-sonnet-4-5",
                             api_key_ref="TEST_DEFAULT_KEY", is_default=True)
    groq_ep = LLMEndpoint(name="groq-qwen38", provider="groq",
                          model="groq/qwen/qwen3.8-27b",
                          api_key_ref="TEST_GROQ_KEY", is_default=False)
    committing_db.add_all([default_ep, groq_ep])
    await committing_db.commit()

    sf = session_factory(engine)
    await trigger(sf, dry_run=False, scored_only=False,
                 endpoint_name="groq-qwen38")

    async with sf() as s:
        review = (await s.execute(
            select(Review).where(Review.mr_id == mr.id))).scalar_one()
        assert review.llm_config["model"] == "groq/qwen/qwen3.8-27b"
        # is_default itself must be untouched by targeting a named endpoint
        rows = (await s.execute(select(LLMEndpoint).where(
            LLMEndpoint.name.in_(["the-default", "groq-qwen38"]))
        )).scalars().all()
        assert {e.name: e.is_default for e in rows} == {
            "the-default": True, "groq-qwen38": False}


async def test_trigger_unknown_endpoint_name_raises(committing_db, engine):
    await _seed_first_golden_mr(committing_db)
    await committing_db.commit()
    sf = session_factory(engine)
    with pytest.raises(SystemExit):
        await trigger(sf, dry_run=False, scored_only=False,
                     endpoint_name="does-not-exist")

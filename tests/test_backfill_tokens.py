from argus.backfill_tokens import (_backfill_distillation_runs,
                                       _backfill_reviews)
from argus.db import session_factory
from argus.domain.models import (DistillationRun, LLMRound, MergeRequest,
                                     Repository, Review)


async def test_backfill_reviews_aggregates_only_zero_token_rows(db, engine):
    sf = session_factory(engine)
    repo = Repository(provider="gitlab", project_path="grp/backfill-tok-p",
                      gitlab_project_id=90000700)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=10, title="t", state="merged",
                      source_branch="a", target_branch="b", head_sha="s",
                      web_url="u")
    db.add(mr)
    await db.flush()
    stale = Review(mr_id=mr.id, status="done", trigger="manual",
                   prompt_tokens=0, completion_tokens=0)
    already_set = Review(mr_id=mr.id, status="done", trigger="manual",
                         prompt_tokens=10, completion_tokens=5)
    db.add_all([stale, already_set])
    await db.flush()
    db.add(LLMRound(review_id=stale.id, stage_name="scout", seq=1,
                    prompt_tokens=100, completion_tokens=40))
    db.add(LLMRound(review_id=stale.id, stage_name="scout", seq=2,
                    prompt_tokens=50, completion_tokens=20))
    # An LLMRound under the already-nonzero review should be ignored since
    # that review is excluded from the zero-token candidate query entirely.
    db.add(LLMRound(review_id=already_set.id, stage_name="scout", seq=1,
                    prompt_tokens=999, completion_tokens=999))
    await db.flush()
    await db.commit()
    stale_id, already_set_id = stale.id, already_set.id

    n = await _backfill_reviews(sf, dry_run=False)
    assert n == 1

    async with sf() as s:
        updated = await s.get(Review, stale_id)
        assert updated.prompt_tokens == 150
        assert updated.completion_tokens == 60
        unchanged = await s.get(Review, already_set_id)
        assert unchanged.prompt_tokens == 10
        assert unchanged.completion_tokens == 5


async def test_backfill_reviews_dry_run_does_not_write(db, engine):
    sf = session_factory(engine)
    repo = Repository(provider="gitlab", project_path="grp/backfill-tok-dry-p",
                      gitlab_project_id=90000701)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=11, title="t", state="merged",
                      source_branch="a", target_branch="b", head_sha="s",
                      web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="done", trigger="manual",
                    prompt_tokens=0, completion_tokens=0)
    db.add(review)
    await db.flush()
    db.add(LLMRound(review_id=review.id, stage_name="scout", seq=1,
                    prompt_tokens=7, completion_tokens=3))
    await db.flush()
    await db.commit()
    review_id = review.id

    n = await _backfill_reviews(sf, dry_run=True)
    assert n == 1

    async with sf() as s:
        unchanged = await s.get(Review, review_id)
        assert unchanged.prompt_tokens == 0
        assert unchanged.completion_tokens == 0


async def test_backfill_distillation_runs_aggregates_only_zero_token_rows(db, engine):
    sf = session_factory(engine)
    repo = Repository(provider="gitlab", project_path="grp/backfill-tok-dr-p",
                      gitlab_project_id=90000702)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=12, title="t", state="merged",
                      source_branch="a", target_branch="b", head_sha="s",
                      web_url="u")
    db.add(mr)
    await db.flush()
    run = DistillationRun(mr_id=mr.id, status="done", trigger="reconcile",
                          note_ids=[], prompt_tokens=0, completion_tokens=0)
    db.add(run)
    await db.flush()
    db.add(LLMRound(distillation_run_id=run.id, stage_name="distill", seq=1,
                    prompt_tokens=30, completion_tokens=12))
    await db.flush()
    await db.commit()
    run_id = run.id

    n = await _backfill_distillation_runs(sf, dry_run=False)
    assert n == 1

    async with sf() as s:
        updated = await s.get(DistillationRun, run_id)
        assert updated.prompt_tokens == 30
        assert updated.completion_tokens == 12

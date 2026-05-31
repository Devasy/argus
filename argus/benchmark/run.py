"""Run the golden set as dry-run reviews, then score it.

    uv run python -m argus.benchmark.run trigger --apply
    uv run python -m argus.benchmark.run score

`trigger` queues one publish=False review per MR in the set: the full pipeline
runs and every finding is recorded, but nothing is posted to GitLab, so the set
can be re-run as often as needed against real merge requests.

`score` takes the newest dry run per MR and compares its kept findings with the
adjudicated verdicts in golden_set.yaml. It reports regressions (a finding we
have established to be false, raised again) separately from misses, because
they call for opposite fixes.
"""
import argparse
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.benchmark import scoring, spec
from argus.domain.models import (MergeRequest, Repository, Review,
                                     ReviewStage)
from argus.jobs.queue import enqueue
from argus.llm.config import resolve_llm_config
from argus.review.candidates import review_candidates

logger = logging.getLogger("argus.benchmark")


async def _resolve_mr(session, mr: spec.GoldenMR) -> MergeRequest | None:
    return (await session.execute(
        select(MergeRequest)
        .join(Repository, Repository.id == MergeRequest.repo_id)
        .where(Repository.project_path == mr.project_path,
               MergeRequest.mr_iid == mr.mr_iid))).scalar_one_or_none()


async def trigger(sf: async_sessionmaker, dry_run: bool = True,
                  scored_only: bool = True) -> dict:
    gs = spec.load()
    queued, missing, skipped = [], [], []
    async with sf() as s:
        try:
            cfg = await resolve_llm_config(s, None, None)
        except ValueError as e:
            raise SystemExit(f"no usable LLM endpoint: {e}")
        for gm in spec.select(gs, scored_only):
            row = await _resolve_mr(s, gm)
            if row is None:
                missing.append(f"{gm.project_path}!{gm.mr_iid}")
                continue
            if dry_run:
                skipped.append(f"{gm.project_path}!{gm.mr_iid}")
                continue
            review = Review(mr_id=row.id, status="queued",
                            llm_config=cfg.model_dump(), trigger="benchmark",
                            publish=False)
            s.add(review)
            await s.flush()
            job = await enqueue(s, "review", {"review_id": str(review.id)},
                                dedup_key=f"review:{row.id}:full:dry")
            if job is None:
                logger.warning("a dry run for %s!%d is already in flight",
                               gm.project_path, gm.mr_iid)
                continue
            queued.append(str(review.id))
        if not dry_run:
            await s.commit()
    if missing:
        logger.warning("%d MR(s) not synced locally, skipped: %s",
                       len(missing), ", ".join(missing))
    logger.info("queued %d dry run(s)%s", len(queued),
                " [dry-run: nothing queued]" if dry_run else "")
    return {"queued": queued, "missing": missing, "not_queued": skipped}


async def _newest_dry_run(session, mr_id) -> Review | None:
    return (await session.execute(
        select(Review).where(Review.mr_id == mr_id, Review.publish.is_(False),
                             Review.status == "done")
        .order_by(Review.created_at.desc()).limit(1))).scalar_one_or_none()


async def score(sf: async_sessionmaker) -> scoring.RunReport | None:
    gs = spec.load()
    per_mr: dict[tuple[str, int], scoring.MRScore] = {}
    models: set[str] = set()
    async with sf() as s:
        for gm in gs.merge_requests:
            if not gm.scored:
                continue
            row = await _resolve_mr(s, gm)
            if row is None:
                logger.warning("%s!%d not synced locally", gm.project_path,
                               gm.mr_iid)
                continue
            review = await _newest_dry_run(s, row.id)
            if review is None:
                logger.warning("%s!%d has no completed dry run yet",
                               gm.project_path, gm.mr_iid)
                continue
            if review.served_model:
                models.add(review.served_model)
            stages = (await s.execute(select(ReviewStage).where(
                ReviewStage.review_id == review.id))).scalars().all()
            per_mr[gm.key] = scoring.score_mr(
                gs.items_for(*gm.key), review_candidates(list(stages)))

    if not per_mr:
        logger.error("nothing to score: no completed dry runs found")
        return None

    report = scoring.aggregate(per_mr)
    print(f"\nmodel(s): {', '.join(sorted(models)) or 'unrecorded'}")
    print(f"MRs scored: {report.mrs_scored} of "
          f"{sum(1 for m in gs.merge_requests if m.scored)}")
    print(f"regressions (adjudicated-false raised again): {report.regressions}")
    print(f"hits: {report.hits}   misses: {report.misses}   "
          f"recall: {'n/a' if report.recall is None else f'{report.recall:.0%}'}")
    print(f"trivia raised: {report.trivia}")
    print(f"unadjudicated kept findings: {report.unadjudicated}")
    if report.worst_mr:
        print(f"worst MR: {report.worst_mr[0]}!{report.worst_mr[1]}")
    print()
    for key, sc in sorted(per_mr.items()):
        bits = [f"{sc.hits} hit"]
        if sc.misses:
            bits.append(f"missed {','.join(sc.misses)}")
        if sc.false_positives:
            bits.append(f"REGRESSED {','.join(sc.false_positives)}")
        if sc.trivia:
            bits.append(f"trivia {','.join(sc.trivia)}")
        bits.append(f"{sc.unadjudicated} unadjudicated")
        print(f"  {key[0].split('/')[-1]}!{key[1]}: {'; '.join(bits)}")
    return report


async def _main() -> None:
    from argus.config import get_settings
    from argus.db import get_engine, session_factory

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["trigger", "score", "show"])
    p.add_argument("--apply", action="store_true",
                   help="trigger only: actually queue; default is a dry run")
    p.add_argument("--all", action="store_true",
                   help="include the behavioural-only MRs; they add roughly "
                        "26h of serial worker time and cannot be scored")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.command == "show":
        gs = spec.load()
        scored = [m for m in gs.merge_requests if m.scored]
        print(f"golden set v{gs.version}: {len(gs.merge_requests)} MRs "
              f"({len(scored)} scored), {len(gs.items)} adjudicated items")
        for m in gs.merge_requests:
            n = len(gs.items_for(*m.key))
            print(f"  {'[scored]' if m.scored else '[behav.]'} "
                  f"{m.project_path.split('/')[-1]}!{m.mr_iid}"
                  f"{f' — {n} item(s)' if n else ''}")
        return

    settings = get_settings()
    sf = session_factory(get_engine(settings.database_url))
    if args.command == "trigger":
        await trigger(sf, dry_run=not args.apply,
                      scored_only=not args.all)
    else:
        await score(sf)


if __name__ == "__main__":
    asyncio.run(_main())

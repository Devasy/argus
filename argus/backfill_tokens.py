"""One-off CLI backfill: aggregate LLMRound token totals onto Review and
DistillationRun rows that predate the fix in runner.py/app.py (those rows
were left at their default 0/0 since nothing wrote to them before).

Usage:
    uv run python -m argus.backfill_tokens [--dry-run]
"""
import argparse
import asyncio
import logging

from sqlalchemy import func, select

from argus.config import get_settings
from argus.db import get_engine, session_factory
from argus.domain.models import DistillationRun, LLMRound, Review

logger = logging.getLogger("argus.backfill_tokens")


async def _backfill_reviews(sf, dry_run: bool) -> int:
    updated = 0
    async with sf() as s:
        review_ids = (await s.execute(
            select(Review.id).where(Review.prompt_tokens == 0,
                                    Review.completion_tokens == 0))).scalars().all()
    for review_id in review_ids:
        async with sf() as s:
            totals = (await s.execute(
                select(func.coalesce(func.sum(LLMRound.prompt_tokens), 0),
                      func.coalesce(func.sum(LLMRound.completion_tokens), 0))
                .where(LLMRound.review_id == review_id))).one()
            if totals[0] == 0 and totals[1] == 0:
                continue
            updated += 1
            logger.info("review %s: %d prompt / %d completion tokens",
                       review_id, totals[0], totals[1])
            if not dry_run:
                review = await s.get(Review, review_id)
                review.prompt_tokens, review.completion_tokens = totals
                await s.commit()
    return updated


async def _backfill_distillation_runs(sf, dry_run: bool) -> int:
    updated = 0
    async with sf() as s:
        run_ids = (await s.execute(
            select(DistillationRun.id).where(
                DistillationRun.prompt_tokens == 0,
                DistillationRun.completion_tokens == 0))).scalars().all()
    for run_id in run_ids:
        async with sf() as s:
            totals = (await s.execute(
                select(func.coalesce(func.sum(LLMRound.prompt_tokens), 0),
                      func.coalesce(func.sum(LLMRound.completion_tokens), 0))
                .where(LLMRound.distillation_run_id == run_id))).one()
            if totals[0] == 0 and totals[1] == 0:
                continue
            updated += 1
            logger.info("distillation_run %s: %d prompt / %d completion tokens",
                       run_id, totals[0], totals[1])
            if not dry_run:
                run = await s.get(DistillationRun, run_id)
                run.prompt_tokens, run.completion_tokens = totals
                await s.commit()
    return updated


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = get_engine(settings.database_url)
    sf = session_factory(engine)

    n_reviews = await _backfill_reviews(sf, args.dry_run)
    n_runs = await _backfill_distillation_runs(sf, args.dry_run)

    verb = "would update" if args.dry_run else "updated"
    logger.info("%s %d review(s) and %d distillation_run(s)", verb, n_reviews, n_runs)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would be updated, without writing")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

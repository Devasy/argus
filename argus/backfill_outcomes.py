"""One-shot backfill: discard verdicts produced by the pre-attribution resolver.

Every `miss`/`inconclusive` written by the old `resolve_injection_outcomes` was
a review-wide guess made before any human disposition existed. Those verdicts
are noise, and feeding them to the new Wilson scoring would penalise learnings
for reviews they had nothing to do with. Reset them to 'pending' and zero the
derived counters; the poller (Task 6) re-resolves them properly on the next
pass via `resolve_outcomes_for_review`/`resolve_outcomes_for_mr`.

Usage:
    uv run python -m argus.backfill_outcomes [--apply]
"""
import argparse
import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.domain.models import InjectionEvent, Learning

logger = logging.getLogger("argus.backfill_outcomes")

_STALE = ("miss", "inconclusive")


async def reset_stale_events(session: AsyncSession) -> int:
    """Reset stale verdicts to pending and zero the counters they fed."""
    events = (await session.execute(select(InjectionEvent).where(
        InjectionEvent.outcome.in_(_STALE)))).scalars().all()
    for ev in events:
        ev.outcome = "pending"
        ev.resolved_at = None
    await session.execute(
        update(Learning).values(miss_count=0, inconclusive_count=0))
    await session.flush()
    return len(events)


async def backfill(sf: async_sessionmaker, dry_run: bool = True) -> dict[str, int]:
    async with sf() as s:
        if dry_run:
            n = len((await s.execute(select(InjectionEvent).where(
                InjectionEvent.outcome.in_(_STALE)))).scalars().all())
            logger.info("[dry-run] would reset %d injection event(s)", n)
            return {"would_reset": n}
        n = await reset_stale_events(s)
        await s.commit()
        logger.info("reset %d injection event(s) to pending", n)
        return {"reset": n}


async def _main() -> None:
    from argus.config import get_settings
    from argus.db import get_engine, session_factory

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true",
                   help="actually write; default is a dry run")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    engine = get_engine(settings.database_url)
    sf = session_factory(engine)

    await backfill(sf, dry_run=not args.apply)


if __name__ == "__main__":
    asyncio.run(_main())

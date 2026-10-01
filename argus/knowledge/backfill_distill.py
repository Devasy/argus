"""One-off CLI backfill: enqueue distillation for settled threads on merged MRs.

Goes through the same path as the poller (enqueue_ready_threads -> distill_mr
jobs), so workers, the distill_threads ledger and validation are identical to
live. --bot-threads-only is the one-time catch-up for bot threads with human
replies that the old pipeline never showed the model.

Usage:
    uv run python -m argus.knowledge.backfill_distill [--repo-id ID]
        [--bot-threads-only] [--dry-run]
"""
import argparse
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.config import get_settings
from argus.db import get_engine, session_factory
from argus.domain.models import MergeRequest, Note

logger = logging.getLogger("argus.backfill_distill")


async def find_qualifying_mrs(session: AsyncSession, *,
                              repo_id: uuid.UUID | None = None) -> list[MergeRequest]:
    """Merged MRs with >=1 human note of kind in (inline, summary)."""
    filters = [MergeRequest.state == "merged"]
    if repo_id is not None:
        filters.append(MergeRequest.repo_id == repo_id)
    qualifying_mr_ids = select(Note.mr_id).where(
        Note.author_type == "human", Note.kind.in_(["inline", "summary"])).distinct()
    rows = (await session.execute(
        select(MergeRequest).where(*filters, MergeRequest.id.in_(qualifying_mr_ids))
    )).scalars().all()
    return list(rows)


async def enqueue_backfill(session: AsyncSession, mrs, *, bot_threads_only: bool,
                           now) -> int:
    """Number of distill_mr jobs enqueued; the caller commits."""
    from argus.knowledge.distill_threads import enqueue_ready_threads
    total = 0
    for mr in mrs:
        total += len(await enqueue_ready_threads(session, mr, now,
                                                 only_bot_threads=bot_threads_only))
    return total


async def _run(args: argparse.Namespace) -> None:
    from argus.knowledge.distill_threads import select_threads_to_distill

    settings = get_settings()
    sf = session_factory(get_engine(settings.database_url))
    repo_id = uuid.UUID(args.repo_id) if args.repo_id else None
    now = datetime.now(timezone.utc)

    async with sf() as s:
        mrs = await find_qualifying_mrs(s, repo_id=repo_id)
        logger.info("found %d qualifying merged MR(s)%s", len(mrs),
                    f" in repo {repo_id}" if repo_id else "")
        if args.dry_run:
            total = 0
            for mr in mrs:
                threads = await select_threads_to_distill(s, mr, now)
                if args.bot_threads_only:
                    threads = [t for t in threads if t.thread_type == "bot_thread"]
                if threads:
                    total += len(threads)
                    logger.info("[dry-run] mr=%s !%d threads=%d", mr.id, mr.mr_iid, len(threads))
            logger.info("[dry-run] %d thread(s) would be queued", total)
            return
        jobs = await enqueue_backfill(s, mrs, bot_threads_only=args.bot_threads_only, now=now)
        await s.commit()
    logger.info("enqueued %d distill_mr job(s)", jobs)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default=None,
                        help="restrict to one repository (uuid)")
    parser.add_argument("--bot-threads-only", action="store_true",
                        help="only threads where a human replied to a argus comment")
    parser.add_argument("--dry-run", action="store_true",
                        help="log how many threads would be queued, without enqueuing")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

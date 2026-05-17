"""One-off CLI backfill: distill learnings from human comments on merged
MRs, using the agentic per-MR runner (argus.knowledge.agentic_distiller).

Usage:
    uv run python -m argus.knowledge.backfill_distill [--repo-id ID]
        [--llm-endpoint-id ID] [--dry-run]
"""
import argparse
import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.config import get_settings
from argus.db import get_engine, session_factory
from argus.domain.models import MergeRequest, Note, Repository
from argus.gitlab.client import GitLabClient
from argus.knowledge.agentic_distiller import run_agentic_distillation_for_mr
from argus.llm.config import resolve_llm_config

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


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = get_engine(settings.database_url)
    sf = session_factory(engine)

    repo_id = uuid.UUID(args.repo_id) if args.repo_id else None
    endpoint_id = uuid.UUID(args.llm_endpoint_id) if args.llm_endpoint_id else None

    async with sf() as s:
        llm_cfg = await resolve_llm_config(s, endpoint_id, proxy_url=None)
        mrs = await find_qualifying_mrs(s, repo_id=repo_id)

    logger.info("found %d qualifying merged MR(s)%s", len(mrs),
               f" in repo {repo_id}" if repo_id else "")

    verify = settings.gitlab_ca_bundle or settings.gitlab_ssl_verify
    gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token, verify)
    try:
        for mr in mrs:
            async with sf() as s:
                repo = await s.get(Repository, mr.repo_id)
                notes = (await s.execute(select(Note).where(
                    Note.mr_id == mr.id, Note.author_type == "human",
                    Note.kind.in_(["inline", "summary"])
                ).order_by(Note.note_created_at))).scalars().all()
            if args.dry_run:
                logger.info("[dry-run] mr=%s !%d notes=%d — would distill",
                           mr.id, mr.mr_iid, len(notes))
                continue
            try:
                result = await run_agentic_distillation_for_mr(
                    sf, settings, gitlab, repo, mr, list(notes), llm_cfg)
            except Exception as e:
                logger.exception("mr %s !%d failed: %s", mr.id, mr.mr_iid, e)
                continue
            counts = {"create": 0, "update": 0, "skip": 0}
            for entry in result.entries:
                counts[entry.action] += 1
                logger.info("mr=%s !%d [%s] %s", mr.id, mr.mr_iid, entry.action,
                           entry.reason)
            logger.info("mr=%s !%d done: %d created, %d updated, %d skipped",
                       mr.id, mr.mr_iid, counts["create"], counts["update"],
                       counts["skip"])
    finally:
        await gitlab.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", default=None,
                        help="restrict to one repository (uuid)")
    parser.add_argument("--llm-endpoint-id", default=None,
                        help="LLM endpoint to use (uuid); default endpoint if omitted")
    parser.add_argument("--dry-run", action="store_true",
                        help="log which MRs/notes would be processed, without running the agent")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()

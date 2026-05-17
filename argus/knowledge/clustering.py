"""Group a repo's active learnings into semantically-similar clusters.

The auditor works on clusters rather than single learnings because the most
valuable findings are *relational*: "these three say the same thing", "these two
contradict each other". Neither is visible when auditing one learning alone.

The threshold is deliberately looser than `upsert_learning`'s dedup_threshold
(0.15): the point is to catch near-duplicates that slipped *past* dedup and are
now splitting evidence between rows, so neither accumulates enough verdicts to
earn an injection slot.
"""
import logging
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import Learning

logger = logging.getLogger("argus.clustering")


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - (dot / (na * nb))


async def cluster_learnings(session: AsyncSession, *, repo_id: uuid.UUID,
                            threshold: float = 0.35,
                            max_cluster_size: int = 8,
                            include_global: bool = True) -> list[list[Learning]]:
    """Greedy agglomeration by embedding proximity.

    Returns a partition: every active learning appears in exactly one cluster,
    singletons included (they still get audited, just without cross-comparison).
    Clusters are capped so one agent's prompt cannot blow past its context.

    `include_global` controls whether repo-less (global) learnings join the
    partition. It must be True when assembling context for a review -- a
    global learning applies everywhere by definition -- and False when
    AUDITING a single repo, because one repository cannot disprove a learning
    that was never about it. Auditing the Python backend produced a proposal
    to archive a global Ant Design learning on the grounds that "the codebase
    is a Python backend with no evidence of Ant Design usage" -- true of that
    repo, and irrelevant to a learning that belongs to the 305-file React UI.
    """
    scope = (or_(Learning.repo_id == repo_id, Learning.repo_id.is_(None))
             if include_global else Learning.repo_id == repo_id)
    rows = list((await session.execute(
        select(Learning)
        .where(scope, Learning.status == "active")
        .order_by(Learning.created_at))).scalars().all())

    clusters: list[list[Learning]] = []
    assigned: set[uuid.UUID] = set()

    for seed in rows:
        if seed.id in assigned:
            continue
        cluster = [seed]
        assigned.add(seed.id)
        if seed.embedding is not None:
            for other in rows:
                if len(cluster) >= max_cluster_size:
                    break
                if other.id in assigned or other.embedding is None:
                    continue
                if _cosine_distance(list(seed.embedding),
                                    list(other.embedding)) < threshold:
                    cluster.append(other)
                    assigned.add(other.id)
        clusters.append(cluster)

    logger.info("repo %s: %d learnings -> %d clusters", repo_id, len(rows),
                len(clusters))
    return clusters

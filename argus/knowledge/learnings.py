import fnmatch
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from argus.config import Settings
from argus.domain.models import Actor, InjectionEvent, Learning, MergeRequest
from argus.knowledge.embeddings import EmbeddingError, embed_text
from argus.knowledge.lexical import RRF_K, Bm25Index, rrf
from argus.knowledge.reputation import combined_strength, reputation

logger = logging.getLogger("argus.learnings")

PATTERN_BONUS = 0.3
STRENGTH_WEIGHT = 0.25

# One top-rank RRF slot. Fused base scores live on this scale, so the bonuses
# have to as well -- the vector path's 0.3/0.25 would swamp them entirely.
RRF_UNIT = 1.0 / (RRF_K + 1)
HYBRID_PATTERN_BONUS = RRF_UNIT
HYBRID_STRENGTH_WEIGHT = 0.5 * RRF_UNIT
LEXICAL_POOL = 40
# Safety valve, not a design choice: the lexical arm is meant to see the whole
# corpus. Hitting this means a repo outgrew in-memory BM25.
LEXICAL_CORPUS_LIMIT = 2000


async def upsert_learning(session: AsyncSession, settings: Settings, *,
                          repo_id, topic: str, hint_text: str,
                          kind: str = "guidance", file_pattern: str | None = None,
                          file_paths: list[str] | None = None,
                          metadata: dict | None = None,
                          source_review_id=None,
                          mr_id=None, source_note_id=None,
                          learned_from_actor_id=None,
                          learning_id: uuid.UUID | None = None,
                          dedup_threshold: float = 0.15) -> Learning:
    """When learning_id is given: load that row, update topic/hint_text/kind/
    file_pattern/file_paths/metadata in place, re-embed, return it — the
    dedup-search path is skipped entirely. When learning_id is None (default):
    unchanged existing behavior (dedup-search-or-insert)."""
    vec = await embed_text(f"{topic} :: {hint_text}", settings, is_query=False)
    if learning_id is not None:
        learning = await session.get(Learning, learning_id)
        learning.topic = topic
        learning.hint_text = hint_text
        learning.kind = kind
        learning.file_pattern = file_pattern
        learning.file_paths = file_paths
        learning.metadata_ = metadata
        learning.embedding = vec
        learning.mr_id = mr_id
        learning.source_note_id = source_note_id
        learning.learned_from_actor_id = learned_from_actor_id
        await session.flush()
        return learning
    if vec is not None:
        scope = (Learning.repo_id == repo_id) if repo_id else Learning.repo_id.is_(None)
        nearest = (await session.execute(
            select(Learning, Learning.embedding.cosine_distance(vec).label("d"))
            .where(scope, Learning.status == "active",
                   Learning.embedding.isnot(None))
            .order_by("d").limit(1))).first()
        if nearest and nearest[1] is not None and nearest[1] < dedup_threshold:
            logger.info("learning deduped onto %s (d=%.3f)", nearest[0].id, nearest[1])
            return nearest[0]
    learning = Learning(repo_id=repo_id, topic=topic, hint_text=hint_text,
                        kind=kind, file_pattern=file_pattern, file_paths=file_paths,
                        metadata_=metadata, embedding=vec,
                        source_review_id=source_review_id,
                        mr_id=mr_id, source_note_id=source_note_id,
                        learned_from_actor_id=learned_from_actor_id)
    session.add(learning)
    await session.flush()
    return learning


def learning_reputation(l: Learning) -> float:
    """Outcome-only strength in [0,1]; 0.5 means "no evidence yet"."""
    return reputation(hit=l.hit_count, harmful=l.harmful_count,
                      ignored=l.ignored_count, miss=l.miss_count)


def learning_strength(l: Learning) -> float:
    """Outcome reputation blended with audit groundedness."""
    age_days = 0.0
    if l.last_audited_at is not None:
        age_days = max(0.0, (datetime.now(timezone.utc)
                             - l.last_audited_at).total_seconds() / 86400.0)
    return combined_strength(learning_reputation(l), l.groundedness, age_days)


def _evidence(l: Learning) -> int:
    """inconclusive excluded: it means no judgement was possible."""
    return (l.hit_count + l.harmful_count + l.ignored_count + l.miss_count)


def rank_learnings(candidates: list[Learning], base: dict, *,
                   file_paths: list[str], top: int = 8, explore: bool = True,
                   pattern_bonus: float = PATTERN_BONUS,
                   strength_weight: float = STRENGTH_WEIGHT) -> list[Learning]:
    """Order candidates by `base` score plus a file-pattern bonus and a
    reputation term, then reserve one slot for exploration.

    `base` is whatever the retrieval arms produced -- cosine similarity for the
    vector-only path, a fused rank score for the hybrid one. The bonus weights
    are therefore expressed in the caller's units; ties keep the incoming
    order, so pass candidates in the arm's own ranking."""
    def score(l: Learning) -> float:
        bonus = 0.0
        if l.file_pattern and any(fnmatch.fnmatch(p, l.file_pattern)
                                  for p in file_paths):
            bonus = pattern_bonus
        # Map reputation [0,1] -> [-1,+1] so a bad learning is pushed BELOW a
        # semantically-weaker but untainted one. The old formula only ever added
        # a bonus, so a harmful learning was never actually demoted.
        strength = 2.0 * learning_strength(l) - 1.0
        return base.get(l.id, 0.0) + bonus + strength_weight * strength

    ranked = sorted(candidates, key=score, reverse=True)
    chosen = ranked[:top]

    if explore and top > 1:
        # Reserve one slot for the least-evidenced candidate that did not make
        # the cut. Without it, a learning that never ranks top-N is never
        # injected, never judged, and its reputation is frozen forever -- a
        # rich-get-richer trap.
        chosen_ids = {l.id for l in chosen}
        rest = [l for l in ranked[top:] if l.id not in chosen_ids]
        if rest:
            explorer = min(rest, key=_evidence)
            if _evidence(explorer) == 0:
                chosen = chosen[:top - 1] + [explorer]
    return chosen


def hybrid_base_scores(vector_ids: list, lexical_ids: list,
                       weights: tuple[float, float] = (1.0, 1.0)) -> dict:
    """Reciprocal Rank Fusion of the two arms' ranked id lists."""
    return rrf([list(vector_ids), list(lexical_ids)], weights=list(weights))


async def relevant_learnings(session: AsyncSession, settings: Settings, *,
                             repo_id, query_text: str, file_paths: list[str],
                             pool: int = 40, top: int = 8,
                             explore: bool = True,
                             exclude_kinds: tuple[str, ...] = (),
                             lexical_query: str | None = None,
                             lexical_pool: int = LEXICAL_POOL,
                             weights: tuple[float, float] = (1.0, 1.0),
                             trace: list | None = None) -> list[Learning]:
    """Review path excludes do_not_suggest: those reach verify by their own
    path, and winning slots here put "do not flag X" in front of every stage.
    Default empty so the distiller's search still sees every kind.

    Passing `lexical_query` turns on the hybrid path: a BM25 arm scores the
    whole active corpus and its ranking is fused with the vector arm's by RRF.
    The two candidate sets are UNIONED, never reranked -- a lexical winner
    outside the vector top-`pool` is exactly what this is for."""
    try:
        qvec = await embed_text(query_text, settings, is_query=True)
    except EmbeddingError:
        # A real backend rejection (e.g. this query is too long for the
        # embedding model's actual context window -- a repo with many changed
        # files can build a query well past what MAX_EMBED_CHARS assumes is
        # safe) is exactly as recoverable as qvec being None: retrieval is an
        # enhancement to the review, not something that should be allowed to
        # crash it. Logged, not raised, so the caller never has to guard this.
        logger.warning("query embedding failed (%d chars); degrading to "
                       "%s", len(query_text or ""),
                       "lexical-only" if lexical_query else "no learnings")
        qvec = None
    if qvec is None and not lexical_query:
        return []
    # embedding.isnot(None) only belongs on the two branches that compute
    # cosine_distance -- a learning saved while the embedding backend was
    # down (upsert_learning tolerates vec=None) still has real text, and the
    # lexical arm is exactly the arm that does not need a vector to use it.
    base_filters = [or_(Learning.repo_id == repo_id, Learning.repo_id.is_(None)),
                    Learning.status == "active"]
    if exclude_kinds:
        base_filters.append(Learning.kind.notin_(exclude_kinds))
    vector_filters = base_filters + [Learning.embedding.isnot(None)]

    if not lexical_query:
        rows = (await session.execute(
            select(Learning, Learning.embedding.cosine_distance(qvec).label("d"))
            .where(*vector_filters)
            .order_by("d").limit(pool))).all()
        return rank_learnings([r[0] for r in rows],
                              {r[0].id: 1.0 - r[1] for r in rows},
                              file_paths=file_paths, top=top, explore=explore)

    if qvec is not None:
        vec_rows = (await session.execute(
            select(Learning, Learning.embedding.cosine_distance(qvec).label("d"))
            .where(*vector_filters).order_by("d").limit(LEXICAL_CORPUS_LIMIT)
            .options(defer(Learning.embedding)))).all()
        by_id = {l.id: l for l, _ in vec_rows}
        vector_ids = [l.id for l, _ in vec_rows[:pool]]
        unembedded = list((await session.execute(
            select(Learning).where(*base_filters, Learning.embedding.is_(None))
            .limit(max(0, LEXICAL_CORPUS_LIMIT - len(vec_rows)))
            .options(defer(Learning.embedding)))).scalars().all())
        for l in unembedded:
            by_id[l.id] = l
        corpus = list(by_id.values())
    else:
        # The vector arm produced no query embedding (empty query text) --
        # the lexical arm alone is still better than injecting nothing.
        corpus = list((await session.execute(
            select(Learning).where(*base_filters).limit(LEXICAL_CORPUS_LIMIT)
            .options(defer(Learning.embedding)))).scalars().all())
        vector_ids = []
    if len(corpus) >= LEXICAL_CORPUS_LIMIT:
        logger.warning("lexical corpus hit the %d-row cap; the pool ceiling is "
                       "back in play for this repo", LEXICAL_CORPUS_LIMIT)

    index = Bm25Index({str(l.id): f"{l.topic} :: {l.hint_text}" for l in corpus})
    lexical_ids = [uuid.UUID(i)
                   for i, _ in index.rank(lexical_query, limit=lexical_pool)]

    base = hybrid_base_scores(vector_ids, lexical_ids, weights=weights)
    by_id = {l.id: l for l in corpus}
    candidates = [by_id[i] for i in dict.fromkeys(vector_ids + lexical_ids)]
    chosen = rank_learnings(candidates, base, file_paths=file_paths, top=top,
                            explore=explore,
                            pattern_bonus=HYBRID_PATTERN_BONUS,
                            strength_weight=HYBRID_STRENGTH_WEIGHT)

    if trace is not None:
        vrank = {i: n for n, i in enumerate(vector_ids)}
        lrank = {i: n for n, i in enumerate(lexical_ids)}
        chosen_ids = {l.id for l in chosen}
        for l in candidates:
            trace.append({
                "learning_id": str(l.id),
                "topic": (l.topic or "")[:80],
                "vector_rank": vrank.get(l.id),
                "lexical_rank": lrank.get(l.id),
                "score": round(base.get(l.id, 0.0), 6),
                "pattern_match": bool(
                    l.file_pattern and any(fnmatch.fnmatch(p, l.file_pattern)
                                           for p in file_paths)),
                "strength": round(learning_strength(l), 4),
                "chosen": l.id in chosen_ids,
            })
    return chosen


async def do_not_suggest(session: AsyncSession, *, repo_id,
                         limit: int = 20) -> list[Learning]:
    return list((await session.execute(
        select(Learning)
        .where(or_(Learning.repo_id == repo_id, Learning.repo_id.is_(None)),
               Learning.status == "active", Learning.kind == "do_not_suggest")
        .order_by(Learning.repo_id.is_(None), Learning.created_at.desc())
        .limit(limit))).scalars().all())


async def record_injections(session: AsyncSession, review_id,
                            learnings: list[Learning]) -> None:
    """Idempotent: a resumed review re-runs everything before the graph."""
    already = set((await session.execute(
        select(InjectionEvent.learning_id).where(
            InjectionEvent.review_id == review_id))).scalars().all())
    for l in learnings:
        if l.id in already:
            continue
        already.add(l.id)
        session.add(InjectionEvent(learning_id=l.id, review_id=review_id))
    await session.flush()


async def record_hit(session: AsyncSession, learning_id) -> None:
    l = await session.get(Learning, learning_id)
    l.hit_count += 1
    await session.flush()


async def list_learnings(session: AsyncSession, *, repo_id: uuid.UUID | None = None,
                         kind: str | None = None, status: str | None = None,
                         page: int = 1, per_page: int = 50
                         ) -> tuple[list[tuple[Learning, MergeRequest | None, Actor | None]], int]:
    """Returns (rows, total_count), each row a (Learning, MergeRequest|None,
    Actor|None) tuple — the latter two resolved via outerjoin on
    mr_id/learned_from_actor_id, None when either is unset (global learnings,
    or rows predating this traceability). repo_id=None means no repo filter
    (all scopes); pass a specific UUID to restrict to that repo OR global
    (repo_id IS NULL) learnings — i.e. 'what would apply to this repo'.
    kind/status None = no filter on that dimension."""
    filters = []
    if repo_id is not None:
        filters.append(or_(Learning.repo_id == repo_id, Learning.repo_id.is_(None)))
    if kind is not None:
        filters.append(Learning.kind == kind)
    if status is not None:
        filters.append(Learning.status == status)

    total = (await session.execute(
        select(func.count(Learning.id)).where(*filters))).scalar_one()
    rows = (await session.execute(
        select(Learning, MergeRequest, Actor)
        .outerjoin(MergeRequest, Learning.mr_id == MergeRequest.id)
        .outerjoin(Actor, Learning.learned_from_actor_id == Actor.id)
        .where(*filters)
        .order_by(Learning.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page))).all()
    return [(r[0], r[1], r[2]) for r in rows], total


async def search_learnings(session: AsyncSession, settings: Settings, *,
                           query_text: str, repo_id: uuid.UUID | None = None,
                           page: int = 1, per_page: int = 20
                           ) -> tuple[list[tuple[Learning, MergeRequest | None, Actor | None]], int]:
    """Pure cosine-distance top-K over ALL active learnings (optionally
    repo-scoped same as list_learnings), no injection-context reranking.
    Returns ([], 0) if embedding the query fails to produce a vector
    (empty/whitespace query_text -> embed_text returns None). Each row is a
    (Learning, MergeRequest|None, Actor|None) tuple, same shape/semantics as
    list_learnings."""
    qvec = await embed_text(query_text, settings, is_query=True)
    if qvec is None:
        return [], 0
    scope = (or_(Learning.repo_id == repo_id, Learning.repo_id.is_(None))
             if repo_id is not None else True)
    dist = Learning.embedding.cosine_distance(qvec).label("d")
    base = (select(Learning, MergeRequest, Actor, dist)
           .outerjoin(MergeRequest, Learning.mr_id == MergeRequest.id)
           .outerjoin(Actor, Learning.learned_from_actor_id == Actor.id)
           .where(scope, Learning.status == "active", Learning.embedding.isnot(None)))

    total = (await session.execute(
        select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await session.execute(
        base.order_by("d").offset((page - 1) * per_page).limit(per_page)
    )).all()
    return [(r[0], r[1], r[2]) for r in rows], total

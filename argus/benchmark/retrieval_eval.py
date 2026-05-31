"""Score learning retrieval against the manual gold standard.

    uv run python -m argus.benchmark.retrieval_eval --review-ids a,b,c
    uv run python -m argus.benchmark.retrieval_eval --newest 2

Outcome labels cannot score retrieval: 12 `hit` events exist across ~9,200
injections and their distance distribution is indistinguishable from chance
(ledger item 6, AUC 0.564). So relevance is judged by hand in
`retrieval_gold.yaml` and retrieval is scored as recall@k against it.

Scores whatever was actually injected into a given review, so any change to
the query, the ranking, or the corpus can be re-measured by running the set
again and pointing this at the new review ids. `score_ranking` takes a bare
list of ids instead, so a proposed strategy can be scored offline without
running a review at all.
"""
import argparse
import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.domain.models import (InjectionEvent, Learning, MergeRequest,
                                     Repository, Review)

logger = logging.getLogger("argus.retrieval_eval")

GOLD_PATH = Path(__file__).with_name("retrieval_gold.yaml")


@dataclass
class MRGold:
    project_path: str
    mr_iid: int
    pool_size: int
    strong: set[str]
    weak: set[str]

    @property
    def key(self) -> tuple[str, int]:
        return (self.project_path, self.mr_iid)


@dataclass
class MRResult:
    key: tuple[str, int]
    retrieved: int
    strong_total: int
    strong_hit: list[str] = field(default_factory=list)
    weak_hit: list[str] = field(default_factory=list)
    strong_missed: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        return len(self.strong_hit) / self.strong_total if self.strong_total else None


def _ids(entries) -> set[str]:
    """Accept both bare ids and {id, why} mappings."""
    out = set()
    for e in entries or []:
        out.add(e["id"] if isinstance(e, dict) else e)
    return out


def load_gold(path: Path = GOLD_PATH) -> dict[tuple[str, int], MRGold]:
    raw = yaml.safe_load(path.read_text())
    gold = {}
    for m in raw["merge_requests"]:
        g = MRGold(project_path=m["project_path"], mr_iid=m["mr_iid"],
                   pool_size=m.get("pool_size", 0),
                   strong=_ids(m.get("strong")), weak=_ids(m.get("weak")))
        gold[g.key] = g
    return gold


def score_ranking(gold: MRGold, ranked: list[str], k: int | None = None) -> MRResult:
    """Score an arbitrary ranked list of 8-char learning ids -- no DB needed,
    so a candidate query/ranking strategy can be evaluated offline."""
    top = [r[:8] for r in (ranked[:k] if k else ranked)]
    seen = set(top)
    return MRResult(
        key=gold.key, retrieved=len(top), strong_total=len(gold.strong),
        strong_hit=sorted(seen & gold.strong),
        weak_hit=sorted(seen & gold.weak),
        strong_missed=sorted(gold.strong - seen))


async def injected_guidance(session: AsyncSession, review_id: uuid.UUID) -> list[str]:
    rows = (await session.execute(
        select(Learning.id)
        .join(InjectionEvent, InjectionEvent.learning_id == Learning.id)
        .where(InjectionEvent.review_id == review_id,
               Learning.kind == "guidance"))).scalars().all()
    return [str(r)[:8] for r in rows]


async def _mr_key(session: AsyncSession, review_id: uuid.UUID) -> tuple[str, int] | None:
    row = (await session.execute(
        select(Repository.project_path, MergeRequest.mr_iid)
        .join(MergeRequest, MergeRequest.repo_id == Repository.id)
        .join(Review, Review.mr_id == MergeRequest.id)
        .where(Review.id == review_id))).first()
    return (row[0], row[1]) if row else None


async def score_reviews(sf: async_sessionmaker, review_ids: list[uuid.UUID],
                        gold: dict[tuple[str, int], MRGold]) -> list[MRResult]:
    results = []
    async with sf() as s:
        for rid in review_ids:
            key = await _mr_key(s, rid)
            if key is None or key not in gold:
                logger.warning("review %s has no gold entry; skipped", rid)
                continue
            results.append(score_ranking(gold[key], await injected_guidance(s, rid)))
    return results


def report(label: str, results: list[MRResult]) -> None:
    strong_total = sum(r.strong_total for r in results)
    strong_hit = sum(len(r.strong_hit) for r in results)
    retrieved = sum(r.retrieved for r in results)
    print(f"\n=== {label} ===")
    print(f"recall@k over strong set: {strong_hit}/{strong_total} "
          f"({100.0 * strong_hit / strong_total:.1f}%)" if strong_total else "no gold")
    print(f"precision: {strong_hit}/{retrieved} slots relevant "
          f"({100.0 * strong_hit / retrieved:.1f}%)" if retrieved else "")
    for r in sorted(results, key=lambda x: x.key):
        pct = f"{100.0 * r.recall:.0f}%" if r.recall is not None else "n/a"
        print(f"  {r.key[0].split('/')[-1]}!{r.key[1]}: "
              f"{len(r.strong_hit)}/{r.strong_total} strong ({pct}), "
              f"{len(r.weak_hit)} weak, {r.retrieved} slots used")
        if r.strong_missed:
            print(f"      missed: {', '.join(r.strong_missed)}")


async def _main() -> None:
    from argus.config import get_settings
    from argus.db import get_engine, session_factory

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--review-ids", help="comma-separated review uuids to score")
    p.add_argument("--label", default="run")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)

    if not args.review_ids:
        raise SystemExit("--review-ids is required")
    ids = [uuid.UUID(x.strip()) for x in args.review_ids.split(",") if x.strip()]

    settings = get_settings()
    sf = session_factory(get_engine(settings.database_url))
    report(args.label, await score_reviews(sf, ids, load_gold()))


if __name__ == "__main__":
    asyncio.run(_main())

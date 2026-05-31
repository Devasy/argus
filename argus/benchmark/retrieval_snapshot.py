"""Freeze everything the retrieval probe needs into one JSON file.

Runs where the database and GitLab are reachable (in practice, inside the
backend container); the probe itself then runs anywhere. Two things make this
worth doing rather than querying live:

* The corpus is pinned with `--as-of`. The gold standard judged pools of
  24/54/132 active guidance learnings at a fixed instant; ranking against a
  corpus that has grown since would move recall for reasons unrelated to the
  ranking, and the baseline would no longer be reproducible.
* GitLab is hit once per MR instead of once per experiment.

    python -m argus.benchmark.retrieval_snapshot --out /tmp/snap.json
"""
import argparse
import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from argus.benchmark.retrieval_eval import load_gold
from argus.domain.models import (Learning, MergeRequest, Repository,
                                     Review, ReviewStage)
from argus.review.diffsvc import parse_diffs

logger = logging.getLogger("argus.retrieval_snapshot")

GOLD_CUTOFF = "2026-09-09 05:39:00+00"
RUN3_PREFIXES = ["0ecea9ba", "87dab121", "adb140e1", "510a6281",
                 "6fb6558e", "1c93afaf", "9126e182"]


async def _repo_by_path(session: AsyncSession, project_path: str) -> Repository | None:
    return (await session.execute(
        select(Repository).where(Repository.project_path == project_path))).scalar_one_or_none()


async def _resolve_review(session: AsyncSession, prefix: str) -> Review | None:
    return (await session.execute(
        select(Review).where(text("reviews.id::text LIKE :p")).params(p=f"{prefix}%")
    )).scalars().first()


async def _corpus(session: AsyncSession, repo_ids: list[uuid.UUID],
                  as_of: datetime) -> list[dict[str, Any]]:
    """Active guidance learnings for the gold repos plus global ones, as they
    stood at `as_of`. Status is today's: a row retired since the judging is
    invisible here, which the pool-size gate is there to expose."""
    rows = (await session.execute(
        select(Learning).where(
            or_(Learning.repo_id.in_(repo_ids), Learning.repo_id.is_(None)),
            Learning.status == "active",
            Learning.kind == "guidance",
            Learning.embedding.isnot(None),
            Learning.created_at <= as_of,
        ))).scalars().all()
    out = []
    for l in rows:
        out.append({
            "id": str(l.id),
            "repo_id": str(l.repo_id) if l.repo_id else None,
            "topic": l.topic,
            "hint_text": l.hint_text,
            "kind": l.kind,
            "file_pattern": l.file_pattern,
            "embedding": [round(float(x), 6) for x in l.embedding],
            "hit_count": l.hit_count,
            "harmful_count": l.harmful_count,
            "ignored_count": l.ignored_count,
            "miss_count": l.miss_count,
            "groundedness": l.groundedness,
            "last_audited_at": l.last_audited_at.isoformat() if l.last_audited_at else None,
            "created_at": l.created_at.isoformat(),
        })
    return out


def _added_lines(hunks: dict) -> list[str]:
    out = []
    for h in hunks.values():
        for line in h.diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                stripped = line[1:].strip()
                if stripped:
                    out.append(stripped)
    return out


async def _mr_entry(session: AsyncSession, gitlab, repo: Repository,
                    review: Review, mr: MergeRequest) -> dict[str, Any]:
    payload = await gitlab.get_merge_request(repo.gitlab_project_id, mr.mr_iid)
    diffs = await gitlab.list_diffs(repo.gitlab_project_id, mr.mr_iid)
    files, hunks = parse_diffs(diffs)
    scout = (await session.execute(
        select(ReviewStage.artifact).where(
            ReviewStage.review_id == review.id,
            ReviewStage.stage_name == "scout"))).scalars().first()
    if scout is None:
        logger.warning("review %s has no stored scout artifact", review.id)
    return {
        "project_path": repo.project_path,
        "mr_iid": mr.mr_iid,
        "repo_id": str(repo.id),
        "review_id": str(review.id),
        "title": payload.get("title") or "",
        "changed_paths": [f.path for f in files],
        "added_lines": _added_lines(hunks),
        "scout": scout,
    }


async def build(sf, settings, as_of: str) -> dict[str, Any]:
    from argus.gitlab.client import GitLabClient

    gold = load_gold()
    verify = settings.gitlab_ca_bundle or settings.gitlab_ssl_verify
    gitlab = GitLabClient(settings.gitlab_url, settings.gitlab_token, verify)
    by_iid = {}
    async with sf() as s:
        repos: dict[str, Repository] = {}
        for key in gold:
            if key[0] not in repos:
                repo = await _repo_by_path(s, key[0])
                if repo is None:
                    raise SystemExit(f"repo {key[0]} not in the database")
                repos[key[0]] = repo
            by_iid[key[1]] = key

        mrs = []
        for prefix in RUN3_PREFIXES:
            review = await _resolve_review(s, prefix)
            if review is None:
                raise SystemExit(f"no review found for prefix {prefix}")
            mr = await s.get(MergeRequest, review.mr_id)
            key = by_iid.get(mr.mr_iid)
            if key is None:
                raise SystemExit(f"MR !{mr.mr_iid} (review {prefix}) has no gold entry")
            mrs.append(await _mr_entry(s, gitlab, repos[key[0]], review, mr))

        learnings = await _corpus(
            s, [r.id for r in repos.values()], datetime.fromisoformat(as_of))

    return {
        "as_of": as_of,
        "repos": {p: {"repo_id": str(r.id),
                      "gitlab_project_id": r.gitlab_project_id}
                  for p, r in repos.items()},
        "learnings": learnings,
        "mrs": mrs,
    }


async def _main() -> None:
    from argus.config import get_settings
    from argus.db import get_engine, session_factory

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True)
    p.add_argument("--as-of", default=GOLD_CUTOFF)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    sf = session_factory(get_engine(settings.database_url))
    snap = await build(sf, settings, args.as_of)
    with open(args.out, "w") as fh:
        json.dump(snap, fh)
    per_repo: dict[str, int] = {}
    for l in snap["learnings"]:
        per_repo[l["repo_id"] or "global"] = per_repo.get(l["repo_id"] or "global", 0) + 1
    logger.info("wrote %s: %d learnings, %d MRs",
                args.out, len(snap["learnings"]), len(snap["mrs"]))
    for path, meta in snap["repos"].items():
        logger.info("  pool %-42s %d", path.split("/")[-1],
                    per_repo.get(meta["repo_id"], 0))
    logger.info("  pool %-42s %d", "global", per_repo.get("global", 0))


if __name__ == "__main__":
    asyncio.run(_main())

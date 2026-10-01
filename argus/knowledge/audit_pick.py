"""Which learnings an audit run should check, most-needed first."""
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import Learning


def changed_paths_since(workspace: Path, sha: str | None) -> set[str] | None:
    if not sha:
        return None
    out = subprocess.run(["git", "diff", "--name-only", sha, "HEAD"], cwd=workspace,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return None
    return {p for p in out.stdout.splitlines() if p}


def _stale_or_never(now: datetime, reaudit_after_days: int):
    return or_(Learning.last_audited_at.is_(None),
               Learning.last_audited_at < now - timedelta(days=reaudit_after_days))


async def count_due(session: AsyncSession, repo_id, *, now: datetime,
                    reaudit_after_days: int, workspace: Path | None = None) -> int:
    if workspace is not None:
        due = await select_due_learnings(session, repo_id, workspace=workspace,
                                         now=now, reaudit_after_days=reaudit_after_days,
                                         limit=10000)
        return len(due)
    return (await session.execute(select(func.count(Learning.id)).where(
        Learning.repo_id == repo_id, Learning.status == "active",
        _stale_or_never(now, reaudit_after_days)))).scalar_one()


async def select_due_learnings(session: AsyncSession, repo_id, *, workspace: Path,
                               now: datetime, reaudit_after_days: int,
                               limit: int) -> list[Learning]:
    rows = (await session.execute(select(Learning).where(
        Learning.repo_id == repo_id, Learning.status == "active"))).scalars().all()
    cutoff = now - timedelta(days=reaudit_after_days)
    diffs: dict[str, set[str] | None] = {}
    ranked = []
    for l in rows:
        audited = l.last_audited_at
        if audited is not None and audited.tzinfo is None:
            audited = audited.replace(tzinfo=now.tzinfo)
        if audited is None:
            tier = 0
        else:
            if l.audited_at_sha and l.file_paths and l.audited_at_sha not in diffs:
                import asyncio
                diffs[l.audited_at_sha] = await asyncio.to_thread(
                    changed_paths_since, workspace, l.audited_at_sha)
            changed = diffs.get(l.audited_at_sha) if l.audited_at_sha else None
            if changed and any(p in changed for p in (l.file_paths or [])):
                tier = 1
            elif audited < cutoff:
                tier = 2
            else:
                continue
        ranked.append((tier, audited or datetime.min.replace(tzinfo=now.tzinfo), l))
    ranked.sort(key=lambda x: (x[0], x[1]))
    return [l for _, _, l in ranked[:limit]]

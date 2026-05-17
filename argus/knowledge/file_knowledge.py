"""Blob-SHA-keyed file knowledge: recall/store a persistent summary of what a
file does, keyed by its git blob SHA so staleness is detectable."""
import asyncio
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.config import Settings
from argus.domain.models import FileKnowledge
from argus.knowledge.embeddings import embed_text


async def get_blob_sha(workspace: Path, path: str) -> str | None:
    def _sync():
        r = subprocess.run(["git", "rev-parse", f"HEAD:{path}"], cwd=workspace,
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    return await asyncio.to_thread(_sync)


async def recall(session: AsyncSession, repo_id, path: str,
                 blob_sha: str | None) -> FileKnowledge | None:
    return (await session.execute(select(FileKnowledge).where(
        FileKnowledge.repo_id == repo_id,
        FileKnowledge.file_path == path))).scalar_one_or_none()


async def remember(session: AsyncSession, settings: Settings, *, repo_id,
                   path: str, blob_sha: str | None, summary: str,
                   review_id=None, append_note: str | None = None) -> FileKnowledge:
    row = await recall(session, repo_id, path, blob_sha)
    if row is None:
        row = FileKnowledge(repo_id=repo_id, file_path=path)
        session.add(row)
    row.blob_sha = blob_sha
    row.summary = summary
    row.updated_by_review_id = review_id
    row.updated_at = datetime.now(timezone.utc)
    if append_note:
        notes = list(row.notes or [])
        notes.append({"note": append_note, "review_id": str(review_id) if review_id else None,
                      "at": datetime.now(timezone.utc).isoformat()})
        row.notes = notes
    row.embedding = await embed_text(f"{path}\n{summary}", settings, is_query=False)
    await session.flush()
    return row


async def list_file_knowledge(session: AsyncSession, *, repo_id: uuid.UUID,
                              page: int = 1, per_page: int = 50) -> tuple[list[FileKnowledge], int]:
    total = (await session.execute(
        select(func.count(FileKnowledge.id))
        .where(FileKnowledge.repo_id == repo_id))).scalar_one()
    rows = (await session.execute(
        select(FileKnowledge).where(FileKnowledge.repo_id == repo_id)
        .order_by(FileKnowledge.updated_at.desc())
        .offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return list(rows), total

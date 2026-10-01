"""Discussion threads as a reader sees them: comments, replies and what happened to the code."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.api.schemas import CodeEventOut, ThreadNoteOut, ThreadOut
from argus.domain.models import Actor, Discussion, DistillThread, Note
from argus.knowledge.distill_threads import CODE_EVENT_TYPES, code_events

_COMMENT_KINDS = ("inline", "summary")


def _aware(t: datetime | None) -> datetime | None:
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def _anchor(note: Note) -> str:
    if not note.file_path:
        return "MR-level discussion"
    return f"{note.file_path}:{note.line}" if note.line else note.file_path


async def build_threads(session: AsyncSession, mr, discussion_ids: list[uuid.UUID] | None = None
                        ) -> list[ThreadOut]:
    """Comment threads of one MR (system notes never appear), oldest first."""
    q = (select(Note, Actor.username).join(Actor, Note.author_id == Actor.id, isouter=True)
         .where(Note.mr_id == mr.id, Note.discussion_id.isnot(None),
                Note.kind.in_(_COMMENT_KINDS)))
    if discussion_ids is not None:
        q = q.where(Note.discussion_id.in_(discussion_ids))
    rows = (await session.execute(q.order_by(Note.note_created_at, Note.provider_note_id))).all()
    by_disc: dict[uuid.UUID, list] = {}
    for note, username in rows:
        by_disc.setdefault(note.discussion_id, []).append((note, username))
    if not by_disc:
        return []
    ids = list(by_disc)
    resolved = {d.id: d.resolved for d in (await session.execute(
        select(Discussion).where(Discussion.id.in_(ids)))).scalars().all()}
    verdicts = {t.discussion_id: t for t in (await session.execute(
        select(DistillThread).where(DistillThread.discussion_id.in_(ids)))).scalars().all()}
    system_notes = (await session.execute(select(Note).where(
        Note.mr_id == mr.id, Note.kind == "system", Note.system_event_type.in_(CODE_EVENT_TYPES)
    ).order_by(Note.note_created_at))).scalars().all()

    threads = []
    for disc_id, entries in by_disc.items():
        bot = next((n for n, _ in entries if n.author_type == "bot"), None)
        first = entries[0][0]
        events = code_events(_aware((bot or first).note_created_at), disc_id, system_notes)
        verdict = verdicts.get(disc_id)
        threads.append(ThreadOut(
            discussion_id=disc_id, resolved=bool(resolved.get(disc_id)),
            anchor=_anchor(bot or first), has_bot_comment=bot is not None,
            notes=[ThreadNoteOut(
                id=n.id, author_username=u, author_type=n.author_type, kind=n.kind, body=n.body,
                created_at=n.note_created_at, depth=0 if i == 0 else 1,
                disposition=n.disposition if n.author_type == "bot" else None)
                for i, (n, u) in enumerate(entries)],
            events=[CodeEventOut(at=e.at, kind=e.kind, text=e.text) for e in events],
            disposition=bot.disposition if bot else None,
            verdict=verdict.reply_verdict if verdict else None,
            verdict_reason=verdict.verdict_reason if verdict else None,
            gitlab_url=f"{mr.web_url}#note_{first.provider_note_id}" if mr.web_url else None))
    return sorted(threads, key=lambda t: (_aware(t.notes[0].created_at) or datetime.min.replace(
        tzinfo=timezone.utc)))

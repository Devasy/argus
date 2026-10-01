"""Distillation works on discussion THREADS, not loose note ids.

A human reply means nothing without the bot comment it answers, and a thread
is re-distilled only when its human content changes (see DistillThread)."""
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import Discussion, DistillThread, Finding, Note
from argus.jobs.queue import enqueue

QUIET_HOURS = 24
MAX_THREADS_PER_RUN = 10
MAX_THREAD_ATTEMPTS = 3
STALE_QUEUED_HOURS = 6
MAX_LEARNINGS_PER_THREAD = 3
MAX_COMMIT_EVENTS = 5
_HUMAN_KINDS = ("inline", "summary")
# GitLab's own record of code moving under a comment; every other system note is noise here.
CODE_EVENT_TYPES = ("line_outdated", "commits_added")


def content_hash(human_note_ids: Iterable[uuid.UUID]) -> str:
    """md5 of sorted human note ids; the ledger migration's seed computes the identical value in SQL."""
    return hashlib.md5(",".join(sorted(str(i) for i in human_note_ids)).encode()).hexdigest()


def thread_ready(mr_state: str, resolved: bool, last_human_at: datetime | None,
                 now: datetime, quiet_hours: int = QUIET_HOURS) -> bool:
    """Distil a conversation once it has settled, not mid-argument."""
    if mr_state in ("merged", "closed") or resolved:
        return True
    return last_human_at is not None and now - last_human_at >= timedelta(hours=quiet_hours)


@dataclass
class CodeEvent:
    at: datetime
    kind: str  # "line_changed" | "commit"
    text: str


def _commit_lines(body: str | None) -> list[str]:
    """'added 1 commit <ul><li>e2656a48 - Fix x</li></ul>' -> ['e2656a48 - Fix x']."""
    items = re.findall(r"<li>(.*?)</li>", body or "", flags=re.S)
    return [re.sub(r"<[^>]+>", "", i).strip() for i in items if i.strip()]


def code_events(start: datetime | None, discussion_id, system_notes: list) -> list[CodeEvent]:
    """What GitLab recorded happening to the code after `start`: this line changing, and pushes."""
    if start is None:
        return []
    events: list[CodeEvent] = []
    commits: list[CodeEvent] = []
    for n in system_notes:
        at = _aware(n.note_created_at)
        if at is None or at <= start:
            continue
        if n.system_event_type == "line_outdated" and n.discussion_id == discussion_id:
            version = re.search(r"version \d+", n.body or "")
            events.append(CodeEvent(at, "line_changed", "the commented line changed"
                                    + (f" ({version.group(0)} of the diff)" if version else "")))
        elif n.system_event_type == "commits_added":
            commits += [CodeEvent(at, "commit", c) for c in _commit_lines(n.body)]
    return sorted(events + commits[:MAX_COMMIT_EVENTS], key=lambda e: e.at)


@dataclass
class Thread:
    discussion_id: uuid.UUID
    thread_type: str
    resolved: bool
    notes: list = field(default_factory=list)
    bot_note: Note | None = None
    finding: Finding | None = None
    events: list = field(default_factory=list)

    @property
    def human_notes(self) -> list:
        return [n for n in self.notes if n.author_type == "human"]

    @property
    def content_hash(self) -> str:
        return content_hash(n.id for n in self.human_notes)

    @property
    def last_human_at(self) -> datetime | None:
        times = [n.note_created_at for n in self.human_notes if n.note_created_at]
        return max(times) if times else None

    @property
    def anchor(self) -> str:
        first = self.bot_note or (self.human_notes[0] if self.human_notes else None)
        if first is None or not first.file_path:
            return "MR-level discussion"
        return f"{first.file_path}:{first.line}" if first.line else first.file_path


def _aware(t: datetime | None) -> datetime | None:
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


async def load_threads(session: AsyncSession, mr,
                       discussion_ids: list[uuid.UUID] | None = None) -> list[Thread]:
    """Every discussion on the MR with at least one human note, oldest note first."""
    q = select(Note).where(Note.mr_id == mr.id, Note.discussion_id.isnot(None),
                           Note.kind.in_(_HUMAN_KINDS))
    if discussion_ids is not None:
        q = q.where(Note.discussion_id.in_(discussion_ids))
    notes = (await session.execute(q.order_by(Note.note_created_at))).scalars().all()
    by_disc: dict = {}
    for n in notes:
        n.note_created_at = _aware(n.note_created_at)
        by_disc.setdefault(n.discussion_id, []).append(n)
    discs = {d.id: d for d in (await session.execute(
        select(Discussion).where(Discussion.id.in_(list(by_disc))))).scalars().all()}
    threads = []
    for disc_id, ns in by_disc.items():
        humans = [n for n in ns if n.author_type == "human"]
        # another review bot's comment is not a human lesson, even when someone replied "Done"
        if not humans or ns[0].author_type == "external_bot":
            continue
        bot = next((n for n in ns if n.author_type == "bot"), None)
        # a bot thread is one where a human answered the bot, not one the bot joined later
        replied = bot is not None and any(
            h.note_created_at and bot.note_created_at and h.note_created_at > bot.note_created_at
            for h in humans)
        threads.append(Thread(
            discussion_id=disc_id, thread_type="bot_thread" if replied else "human_thread",
            resolved=bool(discs.get(disc_id) and discs[disc_id].resolved),
            notes=ns, bot_note=bot if replied else None))
    if threads:
        system_notes = (await session.execute(select(Note).where(
            Note.mr_id == mr.id, Note.kind == "system",
            Note.system_event_type.in_(CODE_EVENT_TYPES)).order_by(Note.note_created_at))).scalars().all()
        for t in threads:
            first = t.bot_note or t.human_notes[0]
            t.events = code_events(first.note_created_at, t.discussion_id, system_notes)
    bot_ids = [t.bot_note.id for t in threads if t.bot_note]
    if bot_ids:
        findings = {f.published_note_id: f for f in (await session.execute(
            select(Finding).where(Finding.published_note_id.in_(bot_ids)))).scalars().all()}
        for t in threads:
            if t.bot_note:
                t.finding = findings.get(t.bot_note.id)
    return threads


def _needs_distill(t: Thread, row: DistillThread | None, now: datetime) -> bool:
    if row is None or row.content_hash != t.content_hash:
        return True
    if row.status == "done":
        return False
    if row.status == "queued":
        # a queued row whose job died never finishes; retry it after a while, within the cap
        return (_aware(row.updated_at) < now - timedelta(hours=STALE_QUEUED_HOURS)
                and row.attempts < MAX_THREAD_ATTEMPTS)
    return row.attempts < MAX_THREAD_ATTEMPTS


async def _ready_with_rows(session: AsyncSession, mr, now: datetime,
                           quiet_hours: int) -> tuple[list, dict]:
    threads = [t for t in await load_threads(session, mr)
               if thread_ready(mr.state, t.resolved, t.last_human_at, now, quiet_hours)]
    if not threads:
        return [], {}
    rows = {r.discussion_id: r for r in (await session.execute(select(DistillThread).where(
        DistillThread.discussion_id.in_([t.discussion_id for t in threads])))).scalars().all()}
    return [t for t in threads if _needs_distill(t, rows.get(t.discussion_id), now)], rows


async def select_threads_to_distill(session: AsyncSession, mr, now: datetime,
                                   quiet_hours: int = QUIET_HOURS) -> list[Thread]:
    return (await _ready_with_rows(session, mr, now, quiet_hours))[0]


async def enqueue_ready_threads(session: AsyncSession, mr, now: datetime, *,
                                only_bot_threads: bool = False,
                                quiet_hours: int = QUIET_HOURS) -> list:
    threads, rows = await _ready_with_rows(session, mr, now, quiet_hours)
    if only_bot_threads:
        threads = [t for t in threads if t.thread_type == "bot_thread"]
    jobs = []
    for i in range(0, len(threads), MAX_THREADS_PER_RUN):
        batch = threads[i:i + MAX_THREADS_PER_RUN]
        for t in batch:
            old = rows.get(t.discussion_id)
            # attempts count failures of THIS content (a stale requeue is one); new content starts over
            same = old is not None and old.content_hash == t.content_hash
            attempts = (old.attempts + (old.status == "queued")) if same else 0
            await session.execute(pg_insert(DistillThread).values(
                mr_id=mr.id, discussion_id=t.discussion_id, thread_type=t.thread_type,
                content_hash=t.content_hash, status="queued", attempts=attempts, updated_at=now,
            ).on_conflict_do_update(
                index_elements=["discussion_id"],
                set_={"thread_type": t.thread_type, "status": "queued", "updated_at": now,
                      "attempts": attempts, "content_hash": t.content_hash}))
        key = hashlib.md5(",".join(sorted(
            f"{t.discussion_id}:{t.content_hash}" for t in batch)).encode()).hexdigest()[:16]
        job = await enqueue(session, "distill_mr",
                            {"mr_id": str(mr.id),
                             "discussion_ids": [str(t.discussion_id) for t in batch]},
                            dedup_key=f"distill_threads:{mr.id}:{key}")
        if job is not None:
            jobs.append(job)
    await session.flush()
    return jobs


def _clip(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …[truncated]"


def format_threads(threads: list[Thread], mr) -> str:
    head = [f"MR !{mr.mr_iid}: {mr.title}"]
    if mr.description:
        head.append("MR description:\n" + _clip(mr.description, 1500))
    blocks = []
    for i, t in enumerate(threads, 1):
        lines = [f"=== Thread {i} (discussion_id={t.discussion_id}) ===",
                 f"type: {t.thread_type} · where: {t.anchor} · "
                 f"resolved: {'yes' if t.resolved else 'no'}"]
        opener = t.bot_note or (t.human_notes[0] if t.human_notes else None)
        if opener is not None:
            lines.append(f"reconciler label: {opener.disposition}")
        if t.bot_note is not None:
            if t.finding is not None:
                f = t.finding
                lines.append(f"BOT COMMENT ({f.severity} {f.type}): {f.title}")
                lines.append("  " + _clip(f.body, 1500))
                if f.suggestion_new:
                    lines.append("  suggested change:\n  " + _clip(f.suggestion_new, 800))
            else:
                lines.append("BOT COMMENT:\n  " + _clip(t.bot_note.body, 2000))
        lines.append("HUMAN NOTES:")
        for n in t.human_notes:
            lines.append(f"  [note {n.id}] " + _clip(n.body, 2000))
        lines += _format_events(t)
        blocks.append("\n".join(lines))
    return "\n\n".join(head) + "\n\n" + "\n\n".join(blocks)


def _relative(at: datetime, ref: datetime | None) -> str:
    if ref is None:
        return ""
    mins = int(abs((at - ref).total_seconds()) // 60)
    span = f"{mins} min" if mins < 120 else f"{mins // 60} h"
    return f" · {span} {'before' if at <= ref else 'after'} the first reply"


def _format_events(t: Thread) -> list[str]:
    who = "the bot comment" if t.bot_note is not None else "the first comment"
    first = t.bot_note or (t.human_notes[0] if t.human_notes else None)
    replies = [n.note_created_at for n in t.human_notes
               if first is not None and n is not first and n.note_created_at
               and first.note_created_at and n.note_created_at > first.note_created_at]
    ref = min(replies) if replies else None
    out = [f"CODE AFTER {who.upper()} (GitLab's record):"]
    inline = first is not None and first.file_path and first.line
    if inline and not any(e.kind == "line_changed" for e in t.events):
        out.append("  the commented line itself was not changed")
    if not any(e.kind == "commit" for e in t.events):
        out.append("  no commits pushed")
    for e in t.events:
        what = e.text if e.kind == "line_changed" else f"commit pushed: {e.text}"
        out.append(f"  {e.at:%Y-%m-%d %H:%M} UTC · {what}{_relative(e.at, ref)}")
    return out


async def payload_discussion_ids(session: AsyncSession, payload: dict) -> list[uuid.UUID]:
    """New jobs carry discussion_ids; jobs queued before the redesign carry note_ids."""
    if "discussion_ids" in payload:
        return [uuid.UUID(d) for d in payload["discussion_ids"]]
    ids = [uuid.UUID(n) for n in payload.get("note_ids") or []]
    if not ids:
        return []
    rows = (await session.execute(select(Note.discussion_id).where(
        Note.id.in_(ids), Note.discussion_id.isnot(None)).distinct())).scalars().all()
    return list(rows)


SWEEP_MR_LIMIT = 200
SWEEP_SCAN_LIMIT = 1000


async def sweep_pending_threads(session: AsyncSession, repo, now: datetime, *,
                                quiet_hours: int = QUIET_HOURS) -> int:
    """DB-only pass: without it, a thread that settles by going quiet, or a failed one,
    waits for a GitLab sync of its MR that may never come."""
    from sqlalchemy import and_, or_

    from argus.domain.models import MergeRequest
    from argus.knowledge.maintenance_state import read_cursor, write_cursor
    lt = DistillThread
    humans = (select(Note.discussion_id,
                     func.min(Note.note_created_at).label("oldest"),
                     func.md5(func.string_agg(cast(Note.id, String),
                         aggregate_order_by(",", cast(Note.id, String)))).label("content_hash"))
              .join(Discussion, Discussion.id == Note.discussion_id)
              .join(MergeRequest, MergeRequest.id == Discussion.mr_id)
              .where(MergeRequest.repo_id == repo.id,
                     Note.author_type == "human", Note.kind.in_(_HUMAN_KINDS),
                     Note.discussion_id.isnot(None))
              .group_by(Note.discussion_id).subquery())
    oldest = func.coalesce(func.min(humans.c.oldest), now)
    candidates = (
        select(MergeRequest.id.label("mr_id"), oldest.label("oldest"))
        .join(Discussion, Discussion.mr_id == MergeRequest.id)
        .join(humans, humans.c.discussion_id == Discussion.id)
        .outerjoin(lt, lt.discussion_id == Discussion.id)
        .where(MergeRequest.repo_id == repo.id,
               or_(lt.id.is_(None),
                   lt.content_hash != humans.c.content_hash,
                   and_(lt.status == "failed", lt.attempts < MAX_THREAD_ATTEMPTS),
                   and_(lt.status == "queued",
                        lt.updated_at < now - timedelta(hours=STALE_QUEUED_HOURS),
                        lt.attempts < MAX_THREAD_ATTEMPTS)))
        .group_by(MergeRequest.id)
        .subquery())
    jobs = eligible_mrs = scanned = 0
    cursor = None
    saved = await read_cursor(session, repo.id, "thread_sweep_cursor")
    if saved:
        try:
            at = datetime.fromisoformat(saved["oldest"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            cursor = (at, uuid.UUID(saved["mr_id"]))
        except (ValueError, TypeError, KeyError):
            pass  # A malformed cursor must not stop a repository's sweeps.

    def after_cursor(q):
        if cursor is None:
            return q
        at, mr_id = cursor
        return q.where(or_(candidates.c.oldest > at,
                          and_(candidates.c.oldest == at, candidates.c.mr_id > mr_id)))

    # Limit both expensive per-MR loads and enqueued MRs. Resume across ticks
    # so an arbitrarily large ineligible prefix cannot monopolize the poller.
    while eligible_mrs < SWEEP_MR_LIMIT and scanned < SWEEP_SCAN_LIMIT:
        q = select(candidates).order_by(candidates.c.oldest, candidates.c.mr_id)
        batch = (await session.execute(after_cursor(q).limit(
            min(SWEEP_MR_LIMIT, SWEEP_SCAN_LIMIT - scanned)))).all()
        if not batch:
            break
        for mr_id, at in batch:
            cursor = (at, mr_id)
            scanned += 1
            mr = await session.get(MergeRequest, mr_id)
            enqueued = await enqueue_ready_threads(session, mr, now, quiet_hours=quiet_hours)
            jobs += len(enqueued)
            eligible_mrs += bool(enqueued)
            if eligible_mrs >= SWEEP_MR_LIMIT:
                break
    more = (await session.execute(after_cursor(select(candidates.c.mr_id)).limit(1))).first()
    value = ({"oldest": cursor[0].isoformat(), "mr_id": str(cursor[1])}
             if more and cursor else None)
    await write_cursor(session, repo.id, "thread_sweep_cursor", value)
    return jobs

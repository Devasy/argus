"""Turn raw GitLab payloads into idempotent upserts of domain entities."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import (Actor, Discussion, MergeRequest,
                                     MRParticipant, MRVersion, Note, Repository)

_SYSTEM_PATTERNS = [
    ("requested review from", "review_requested"),
    ("added ", "commits_added"),          # "added N commits"
    ("requested changes", "changes_requested"),
    ("changed title", "title_changed"),
    ("changed this line in", "line_outdated"),
    ("assigned to", "assigned"),
]


def classify_system_note(body: str) -> str | None:
    b = (body or "").strip().lower()
    for prefix, event in _SYSTEM_PATTERNS:
        if b.startswith(prefix):
            if event == "commits_added" and "commit" not in b[:40]:
                continue
            return event
    return None


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def upsert_actor(session: AsyncSession, user: dict) -> Actor:
    actor = (await session.execute(select(Actor).where(
        Actor.provider == "gitlab",
        Actor.provider_user_id == user["id"]))).scalar_one_or_none()
    if actor is None:
        actor = Actor(provider="gitlab", provider_user_id=user["id"],
                      username=user.get("username", ""),
                      display_name=user.get("name"),
                      avatar_url=user.get("avatar_url"))
        session.add(actor)
        await session.flush()
    return actor


async def _upsert_participant(session, mr_id, actor_id, role, review_state=None,
                              approved_at=None) -> None:
    row = (await session.execute(select(MRParticipant).where(
        MRParticipant.mr_id == mr_id, MRParticipant.actor_id == actor_id,
        MRParticipant.role == role))).scalar_one_or_none()
    if row is None:
        row = MRParticipant(mr_id=mr_id, actor_id=actor_id, role=role)
        session.add(row)
    row.review_state = review_state
    row.approved_at = approved_at


async def sync_merge_request(session: AsyncSession, repo: Repository,
                             mr_payload: dict, discussions: list[dict],
                             versions: list[dict], approvals: dict,
                             reviewers: list[dict],
                             bot_usernames: set[str]) -> MergeRequest:
    author = await upsert_actor(session, mr_payload["author"]) if mr_payload.get("author") else None
    merged_by = (await upsert_actor(session, mr_payload["merged_by"])
                 if mr_payload.get("merged_by") else None)

    mr = (await session.execute(select(MergeRequest).where(
        MergeRequest.repo_id == repo.id,
        MergeRequest.mr_iid == mr_payload["iid"]))).scalar_one_or_none()
    if mr is None:
        mr = MergeRequest(repo_id=repo.id, mr_iid=mr_payload["iid"],
                          title="", state="opened", source_branch="",
                          target_branch="", head_sha="", web_url="")
        session.add(mr)
    mr.title = mr_payload.get("title") or ""
    mr.description = mr_payload.get("description")
    mr.state = mr_payload.get("state") or "opened"
    mr.author_id = author.id if author else None
    mr.merged_by_id = merged_by.id if merged_by else None
    mr.source_branch = mr_payload.get("source_branch") or ""
    mr.target_branch = mr_payload.get("target_branch") or ""
    mr.head_sha = mr_payload.get("sha") or ""
    mr.draft = bool(mr_payload.get("draft"))
    mr.has_conflicts = bool(mr_payload.get("has_conflicts"))
    mr.blocking_discussions_resolved = bool(
        mr_payload.get("blocking_discussions_resolved", True))
    mr.detailed_merge_status = mr_payload.get("detailed_merge_status")
    mr.labels = mr_payload.get("labels")
    mr.web_url = mr_payload.get("web_url") or ""
    mr.merged_at = _ts(mr_payload.get("merged_at"))
    mr.closed_at = _ts(mr_payload.get("closed_at"))
    mr.mr_created_at = _ts(mr_payload.get("created_at"))
    mr.mr_updated_at = _ts(mr_payload.get("updated_at"))
    mr.raw = mr_payload
    await session.flush()

    # participants
    if author:
        await _upsert_participant(session, mr.id, author.id, "author")
    for u in mr_payload.get("assignees") or []:
        a = await upsert_actor(session, u)
        await _upsert_participant(session, mr.id, a.id, "assignee")
    for r in reviewers or []:
        a = await upsert_actor(session, r["user"])
        await _upsert_participant(session, mr.id, a.id, "reviewer",
                                  review_state=r.get("state"))
    for ap in (approvals or {}).get("approved_by") or []:
        a = await upsert_actor(session, ap["user"])
        await _upsert_participant(session, mr.id, a.id, "approver",
                                  approved_at=_ts((approvals or {}).get("updated_at")))

    # versions
    for v in versions or []:
        existing = (await session.execute(select(MRVersion).where(
            MRVersion.mr_id == mr.id,
            MRVersion.provider_version_id == v["id"]))).scalar_one_or_none()
        if existing is None:
            session.add(MRVersion(
                mr_id=mr.id, provider_version_id=v["id"],
                head_commit_sha=v["head_commit_sha"],
                base_commit_sha=v["base_commit_sha"],
                start_commit_sha=v["start_commit_sha"],
                version_created_at=_ts(v.get("created_at"))))

    # discussions + notes
    for d in discussions or []:
        disc = (await session.execute(select(Discussion).where(
            Discussion.mr_id == mr.id,
            Discussion.provider_discussion_id == d["id"]))).scalar_one_or_none()
        if disc is None:
            disc = Discussion(mr_id=mr.id, provider_discussion_id=d["id"])
            session.add(disc)
        disc.individual_note = bool(d.get("individual_note"))
        first = (d.get("notes") or [{}])[0]
        disc.resolvable = bool(first.get("resolvable"))
        disc.resolved = bool(first.get("resolved"))
        disc.resolved_at = _ts(first.get("resolved_at"))
        if first.get("resolved_by"):
            resolver = await upsert_actor(session, first["resolved_by"])
            disc.resolved_by_id = resolver.id
        await session.flush()

        prev_note_id = None
        for raw_note in d.get("notes") or []:
            note = (await session.execute(select(Note).where(
                Note.mr_id == mr.id,
                Note.provider_note_id == raw_note["id"]))).scalar_one_or_none()
            if note is None:
                note = Note(mr_id=mr.id, provider_note_id=raw_note["id"],
                            author_type="human", kind="inline", body="")
                session.add(note)
            note.discussion_id = disc.id
            note.parent_note_id = prev_note_id
            note.body = raw_note.get("body") or ""
            note.note_created_at = _ts(raw_note.get("created_at"))
            note.position = raw_note.get("position")
            note.suggestions = raw_note.get("suggestions") or None
            note.raw = raw_note
            pos = raw_note.get("position") or {}
            note.file_path = pos.get("new_path")
            note.line = pos.get("new_line")
            if raw_note.get("system"):
                note.author_type, note.kind = "system", "system"
                note.system_event_type = classify_system_note(note.body)
            else:
                username = (raw_note.get("author") or {}).get("username", "")
                note.author_type = "bot" if username in bot_usernames else "human"
                note.kind = "inline" if raw_note.get("position") else "summary"
            if raw_note.get("author"):
                a = await upsert_actor(session, raw_note["author"])
                note.author_id = a.id
            await session.flush()
            prev_note_id = note.id

    return mr

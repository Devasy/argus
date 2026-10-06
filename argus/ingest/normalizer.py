"""Idempotent upserts of normalized hosting-provider records."""
import re
from datetime import datetime

from sqlalchemy import or_, select
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


# Detect only GitLab token bots; other bots need explicit configuration or metadata.
TOKEN_BOT_USERNAME = re.compile(r"^(project|group)_\d+_bot_[0-9a-zA-Z]+$", re.IGNORECASE)


def classify_author(username: str, bot_usernames: set[str], *, is_bot: bool = False) -> str:
    """argus itself is "bot"; another review bot is "external_bot", kept out of human learning."""
    u = username or ""
    if u in bot_usernames:
        return "bot"
    if is_bot or u == "pr_agent" or TOKEN_BOT_USERNAME.match(u):
        return "external_bot"
    return "human"


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def upsert_actor(session: AsyncSession, user: dict, provider: str = "gitlab") -> Actor:
    user_key = str(user["id"])
    # Hosts may identify users with UUIDs. Keep the historical numeric column
    # only for canonical integers that fit PostgreSQL's bigint range.
    numeric_id = int(user_key) if re.fullmatch(r"0|[1-9][0-9]*", user_key) else None
    if numeric_id is not None and numeric_id > 2**63 - 1:
        numeric_id = None
    identity = Actor.provider_user_key == user_key
    if numeric_id is not None:
        identity = or_(identity, (Actor.provider_user_key.is_(None)) &
                       (Actor.provider_user_id == numeric_id))
    actor = (await session.execute(select(Actor).where(
        Actor.provider == provider,
        identity))).scalar_one_or_none()
    if actor is None:
        actor = Actor(provider=provider, provider_user_id=numeric_id,
                      provider_user_key=user_key,
                      username=user.get("username", ""),
                      display_name=user.get("name"),
                      avatar_url=user.get("avatar_url"))
        session.add(actor)
        await session.flush()
    actor.provider_user_key = user_key
    if user.get("bot") is True:
        actor.is_bot = True
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
    author = await upsert_actor(session, mr_payload["author"], repo.provider) if mr_payload.get("author") else None
    merged_by = (await upsert_actor(session, mr_payload["merged_by"], repo.provider)
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
    if repo.provider == "github":
        from sqlalchemy import delete
        await session.execute(delete(MRParticipant).where(
            MRParticipant.mr_id == mr.id, MRParticipant.role.in_(("approver", "reviewer"))))
    if author:
        await _upsert_participant(session, mr.id, author.id, "author")
    for u in mr_payload.get("assignees") or []:
        a = await upsert_actor(session, u, repo.provider)
        await _upsert_participant(session, mr.id, a.id, "assignee")
    for r in reviewers or []:
        a = await upsert_actor(session, r["user"], repo.provider)
        await _upsert_participant(session, mr.id, a.id, "reviewer",
                                  review_state=r.get("state"))
    for ap in (approvals or {}).get("approved_by") or []:
        a = await upsert_actor(session, ap["user"], repo.provider)
        await _upsert_participant(session, mr.id, a.id, "approver",
                                  approved_at=_ts((approvals or {}).get("updated_at")))

    # versions
    for v in versions or []:
        identity = (MRVersion.snapshot_key == v["snapshot_key"] if v.get("snapshot_key")
                    else MRVersion.provider_version_id == v["id"])
        existing = (await session.execute(select(MRVersion).where(
            MRVersion.mr_id == mr.id,
            identity))).scalar_one_or_none()
        if existing is None:
            session.add(MRVersion(
                mr_id=mr.id, provider_version_id=v["id"],
                snapshot_key=v.get("snapshot_key"),
                head_commit_sha=v.get("head_commit_sha"),
                base_commit_sha=v.get("base_commit_sha"),
                start_commit_sha=v.get("start_commit_sha"),
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
            resolver = await upsert_actor(session, first["resolved_by"], repo.provider)
            disc.resolved_by_id = resolver.id
        await session.flush()

        prev_note_id = None
        note_ids = {}
        for raw_note in d.get("notes") or []:
            note_kind = raw_note.get("provider_note_key", "note:").split(":", 1)[0]
            note = (await session.execute(select(Note).where(
                Note.mr_id == mr.id,
                Note.provider_note_id == raw_note["id"],
                Note.provider_note_kind == note_kind))).scalar_one_or_none()
            if note is None:
                note = Note(mr_id=mr.id, provider_note_id=raw_note["id"],
                            provider_note_kind=note_kind,
                            author_type="human", kind="inline", body="")
                session.add(note)
            note.discussion_id = disc.id
            note.parent_note_id = (note_ids.get(raw_note["reply_to_id"])
                                   if raw_note.get("reply_to_id") else prev_note_id)
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
                note.author_type = classify_author(
                    username, bot_usernames,
                    is_bot=(raw_note.get("author") or {}).get("bot") is True)
                note.kind = "inline" if raw_note.get("position") else "summary"
            if raw_note.get("author"):
                a = await upsert_actor(session, raw_note["author"], repo.provider)
                note.author_id = a.id
                if not raw_note.get("system"):
                    note.author_type = classify_author(username, bot_usernames, is_bot=a.is_bot)
                if note.author_type in ("bot", "external_bot"):
                    a.is_bot = True
            await session.flush()
            if repo.provider == "github" and note.author_type == "bot":
                import uuid
                from argus.domain.models import Review, Finding
                marker = re.search(r"<!-- argus-review:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) -->", note.body)
                review = await session.get(Review, uuid.UUID(marker[1])) if marker else None
                if review is not None and review.mr_id == mr.id:
                    note.review_id = review.id
                    for comment in (review.publication_artifact or {}).get("comments", []):
                        if comment["body"] == note.body and comment.get("finding_db_id"):
                            finding = await session.get(Finding, uuid.UUID(comment["finding_db_id"]))
                            if finding and finding.review_id == review.id:
                                finding.published_note_id = note.id
            prev_note_id = note.id
            note_ids[raw_note["id"]] = note.id

    return mr

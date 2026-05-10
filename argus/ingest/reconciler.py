"""Classify what humans did with bot comments; emit distillation candidates."""
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import Discussion, Feedback, Learning, Note
from argus.gitlab.client import GitLabClient

logger = logging.getLogger("argus.reconciler")

# Stopgap keyword heuristic. Previously ANY non-empty human reply on a
# resolved discussion was classified as "rejected_with_rationale" — the mere
# presence of a reply was treated as evidence of disagreement, so replies
# like "fixed" or "Updated" (acknowledging a manual fix that didn't match
# the bot's exact suggested diff text) were mislabeled as rejections. This
# carves out short, unambiguous acceptance phrases as "accepted_manually";
# everything else keeps the existing conservative default of
# "rejected_with_rationale" for a non-empty reply.
_ACCEPTANCE_PHRASES = (
    "fixed", "fix applied", "fixed it", "done", "updated", "addressed",
    "resolved", "good catch", "will fix", "makes sense", "agreed",
    "thanks, fixed", "applied", "handled",
    # Observed being mis-filed as rejections: the reviewer says what they did.
    "added", "adding", "confirmed", "accepted", "adopted", "corrected",
    "changed", "removed", "renamed", "implemented", "incorporated",
)

# Matched anywhere in the reply, because the reasoning usually follows some
# context ("Action is a required field. We do not need to do..."). Curated so
# each phrase reads as a rejection mid-sentence too -- 'invalid' and 'expected'
# are deliberately absent, being just as likely inside a description of a fix.
_REJECTION_PHRASES = (
    "declined", "unrelated", "intentional", "not reachable", "not possible",
    "no fix required", "not needed", "not required", "by design",
    "as designed", "wont fix", "won't fix", "will not fix", "false positive",
    "we do not need", "we don't need", "not applicable", "disagree",
    "out of scope", "no action needed", "not an issue",
)


@dataclass
class ReconcileResult:
    note_ids: list = field(default_factory=list)


def _opens_with(reply: str, phrases: tuple[str, ...]) -> bool:
    normalized = reply.strip().strip(".!").lower()
    return any(normalized == p or normalized.startswith(p + " ")
               or normalized.startswith(p + ",")
               for p in phrases)


def _reply_looks_like_acceptance(reply: str) -> bool:
    return _opens_with(reply, _ACCEPTANCE_PHRASES)


def _reply_looks_like_rejection(reply: str) -> bool:
    normalized = reply.lower()
    return any(p in normalized for p in _REJECTION_PHRASES)


def classify_suggestion_disposition(note_suggestions: list[dict],
                                    final_diff_text: str,
                                    discussion_resolved: bool,
                                    human_replies: list[str]) -> str:
    if any(s.get("applied") for s in note_suggestions or []):
        return "accepted"
    replies = [r.strip() for r in human_replies if r.strip()]
    # What the reviewer SAID outranks both the diff and whether they remembered
    # to resolve the thread: "Fixed in 2f6b3be" sat in `open` and never counted.
    if any(_reply_looks_like_acceptance(r) for r in replies):
        return "accepted_manually"
    if any(_reply_looks_like_rejection(r) for r in replies):
        return "rejected_with_rationale"
    for s in note_suggestions or []:
        to_lines = [l.strip() for l in (s.get("to_content") or "").splitlines()
                    if l.strip()]
        if to_lines and all(l in final_diff_text for l in to_lines):
            return "accepted_manually"
    if not discussion_resolved:
        return "open"
    if not replies:
        return "dismissed_ambiguous"
    # Not a verdict -- we simply could not read this reply. Kept out of the
    # acceptance rate rather than counted against the bot.
    return "replied_unclassified"


async def reconcile_mr(session: AsyncSession, client: GitLabClient, repo, mr,
                       settings) -> ReconcileResult:
    diffs = await client.list_diffs(repo.gitlab_project_id, mr.mr_iid)
    final_diff = "\n".join(d.get("diff") or "" for d in diffs)
    note_ids: list = []

    bot_notes = (await session.execute(select(Note).where(
        Note.mr_id == mr.id, Note.author_type == "bot"))).scalars().all()
    for note in bot_notes:
        try:
            awards = await client.get_note_awards(repo.gitlab_project_id,
                                                  mr.mr_iid, note.provider_note_id)
        except Exception as e:
            logger.warning("award sweep failed for note %s: %s",
                           note.provider_note_id, e)
            awards = []
        for a in awards:
            exists = (await session.execute(select(Feedback).where(
                Feedback.note_id == note.id, Feedback.kind == "reaction",
                Feedback.payload["award_id"].as_integer() == a["id"]
            ))).scalar_one_or_none()
            if exists is None:
                session.add(Feedback(note_id=note.id, kind="reaction",
                                     payload={"award_id": a["id"],
                                              "name": a["name"],
                                              "user": a["user"]["username"]}))

        disc = (await session.get(Discussion, note.discussion_id)
                if note.discussion_id else None)
        replies = (await session.execute(select(Note).where(
            Note.discussion_id == note.discussion_id,
            Note.author_type == "human",
            Note.note_created_at > note.note_created_at))).scalars().all() \
            if note.discussion_id else []
        old = note.disposition
        note.disposition = classify_suggestion_disposition(
            note.suggestions or [], final_diff,
            bool(disc and disc.resolved), [r.body for r in replies])
        if note.disposition != old:
            session.add(Feedback(note_id=note.id, kind="resolved",
                                 payload={"disposition": note.disposition}))
            note_ids.append(note.id)

    # Bot-silent discussions: any human inline note in a discussion with no
    # bot-authored note at all — the bot's review never addressed this spot.
    already_distilled = set((await session.execute(
        select(Learning.source_note_id).where(
            Learning.source_note_id.isnot(None)))).scalars().all())

    human_notes = (await session.execute(select(Note).where(
        Note.mr_id == mr.id, Note.author_type == "human",
        Note.kind == "inline"))).scalars().all()
    bot_discussion_ids = {n.discussion_id for n in bot_notes if n.discussion_id}
    for note in human_notes:
        if note.discussion_id in bot_discussion_ids:
            continue  # bot did comment in this discussion — handled above
        if note.id in already_distilled:
            continue
        if note.id not in note_ids:
            note_ids.append(note.id)

    await session.flush()
    return ReconcileResult(note_ids=note_ids)

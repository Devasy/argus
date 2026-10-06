"""Classify what humans did with bot comments."""
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import Discussion, DistillThread, Feedback, Note
from argus.providers import repository_key
from argus.providers.base import GitProvider
from argus.knowledge.distill_threads import content_hash

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


# the distiller's reading outranks our keyword guess; a question or a deferral is no verdict on the bot
_VERDICT_TO_DISPOSITION = {"accepted": "accepted_manually", "rejected": "rejected_with_rationale",
                           "question": "replied_unclassified", "acknowledged": "replied_unclassified"}
_NEVER_OVERRIDDEN = frozenset({"accepted"})


@dataclass
class ReconcileResult:
    changed_note_ids: list = field(default_factory=list)


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
                                    human_replies: list[str],
                                    followup_verified: bool = False) -> str:
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
    # argus's follow-up saw the fix in the code; ranks below every human signal above.
    if followup_verified:
        return "accepted_by_followup"
    if not discussion_resolved:
        return "open"
    if not replies:
        return "dismissed_ambiguous"
    # Not a verdict -- we simply could not read this reply. Kept out of the
    # acceptance rate rather than counted against the bot.
    return "replied_unclassified"


async def _rate_human_reviewer_comments(session: AsyncSession, mr, bot_actor_ids: set,
                                        final_diff: str) -> None:
    """Same reading as for the bot, on the comment that opened a human-started thread. It is
    a hint for distillation only: no Feedback row, and every metric counts bot notes alone."""
    by_disc: dict = {}
    for n in (await session.execute(select(Note).where(
            Note.mr_id == mr.id, Note.discussion_id.isnot(None),
            Note.kind.in_(("inline", "summary"))).order_by(Note.note_created_at))).scalars():
        by_disc.setdefault(n.discussion_id, []).append(n)
    discs = {d.id: d for d in (await session.execute(select(Discussion).where(
        Discussion.id.in_(list(by_disc))))).scalars()} if by_disc else {}
    for disc_id, notes in by_disc.items():
        opener, replies = notes[0], [n for n in notes[1:] if n.author_type == "human"]
        if opener.author_type != "human":
            continue
        disc = discs.get(disc_id)
        opener.disposition = classify_suggestion_disposition(
            opener.suggestions or [], final_diff,
            bool(disc and disc.resolved and disc.resolved_by_id not in bot_actor_ids),
            [r.body for r in replies])


async def reconcile_mr(session: AsyncSession, client: GitProvider, repo, mr,
                       settings) -> ReconcileResult:
    diffs = await client.list_diffs(repository_key(repo), mr.mr_iid)
    final_diff = "\n".join(d.get("diff") or "" for d in diffs)
    changed_note_ids: list = []

    verdicts = {r.discussion_id: r for r in (await session.execute(select(DistillThread).where(
        DistillThread.mr_id == mr.id, DistillThread.status == "done",
        DistillThread.reply_verdict.in_(tuple(_VERDICT_TO_DISPOSITION))))).scalars().all()}
    human_ids_by_disc: dict = {}
    for hid, did in (await session.execute(select(Note.id, Note.discussion_id).where(
            Note.mr_id == mr.id, Note.author_type == "human",
            Note.kind.in_(("inline", "summary"))))).all():
        human_ids_by_disc.setdefault(did, []).append(hid)

    bot_notes = (await session.execute(select(Note).where(
        Note.mr_id == mr.id, Note.author_type == "bot"))).scalars().all()
    # A thread argus resolved itself (review/followup.py) is not a human outcome.
    bot_actor_ids = {n.author_id for n in bot_notes if n.author_id}
    verified = set((await session.execute(select(Feedback.note_id).where(
        Feedback.kind == "followup", Feedback.note_id.in_([n.id for n in bot_notes]),
        Feedback.payload["verdict"].as_string() == "addressed"))).scalars().all()) if bot_notes else set()
    for note in bot_notes:
        try:
            kwargs = ({"note_key": f"{note.provider_note_kind}:{note.provider_note_id}"}
                      if note.provider_note_kind != "note" else {})
            awards = await client.get_note_awards(repository_key(repo),
                                                  mr.mr_iid, note.provider_note_id, **kwargs)
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
            bool(disc and disc.resolved and disc.resolved_by_id not in bot_actor_ids),
            [r.body for r in replies], followup_verified=note.id in verified)
        v = verdicts.get(note.discussion_id)
        if (v is not None and note.disposition not in _NEVER_OVERRIDDEN
                and v.content_hash == content_hash(human_ids_by_disc.get(note.discussion_id, []))):
            note.disposition = _VERDICT_TO_DISPOSITION[v.reply_verdict]
        if note.disposition != old:
            session.add(Feedback(note_id=note.id, kind="resolved",
                                 payload={"disposition": note.disposition}))
            changed_note_ids.append(note.id)

    await _rate_human_reviewer_comments(session, mr, bot_actor_ids, final_diff)
    await session.flush()
    return ReconcileResult(changed_note_ids=changed_note_ids)

"""Save the input conversation alongside its decision, independently of the ledger."""
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from argus.api.schemas import CodeEventOut, DistillThreadOut, ThreadNoteOut, ThreadOut
from argus.domain.models import Actor, DistillationDecision


async def save_decision(session, run_id, mr, thread, *, status,
                        reply_verdict=None, verdict_reason=None,
                        learning_ids=None, decision_reason=None):
    if run_id is None:
        return
    usernames = dict((await session.execute(select(Actor.id, Actor.username).where(
        Actor.id.in_([n.author_id for n in thread.notes if n.author_id])))).all())
    bot = thread.bot_note
    first = thread.notes[0]
    label = bot.disposition if bot else None
    view = ThreadOut(
        discussion_id=thread.discussion_id, resolved=thread.resolved,
        anchor=thread.anchor, has_bot_comment=bot is not None,
        notes=[ThreadNoteOut(id=n.id, author_username=usernames.get(n.author_id),
            author_type=n.author_type, kind=n.kind, body=n.body,
            created_at=n.note_created_at, depth=0 if i == 0 else 1,
            disposition=n.disposition if n.author_type == "bot" else None)
            for i, n in enumerate(thread.notes)],
        events=[CodeEventOut(at=e.at, kind=e.kind, text=e.text) for e in thread.events],
        disposition=label, verdict=reply_verdict, verdict_reason=verdict_reason,
        gitlab_url=f"{mr.web_url}#note_{first.provider_note_id}" if mr.web_url else None)
    decision = DistillThreadOut(discussion_id=thread.discussion_id,
        thread_type=thread.thread_type, status=status, reconciler_label=label,
        reply_verdict=reply_verdict, verdict_reason=verdict_reason,
        learning_ids=learning_ids or [], decision_reason=decision_reason, thread=view)
    statement = insert(DistillationDecision).values(
        distillation_run_id=run_id, discussion_id=thread.discussion_id,
        content_hash=thread.content_hash, decision=decision.model_dump(mode="json"))
    # A failed attempt is provisional until this same run completes. Completed
    # decisions stay immutable, including when another job replays the run.
    await session.execute(statement.on_conflict_do_update(
        index_elements=["distillation_run_id", "discussion_id"],
        set_={"content_hash": statement.excluded.content_hash,
              "decision": statement.excluded.decision},
        where=DistillationDecision.decision["status"].astext == "failed"))

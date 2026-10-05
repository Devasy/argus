import uuid
from datetime import datetime, timedelta, timezone

import pytest

from argus.domain.models import (Discussion, DistillThread, MergeRequest, Note,
                                     Repository)
from argus.ingest.reconciler import (ReconcileResult,
                                         classify_suggestion_disposition,
                                         reconcile_mr)

SUGG = [{"id": 217, "applied": False, "appliable": False,
         "from_line": 598, "to_line": 608,
         "to_content": "wrapped = f\"<log_data>{lines}</log_data>\"\n"}]


def test_applied_wins():
    s = [{**SUGG[0], "applied": True}]
    assert classify_suggestion_disposition(s, "", True, []) == "accepted"


def test_accepted_manually_when_content_landed():
    diff = "+    wrapped = f\"<log_data>{lines}</log_data>\"\n"
    assert classify_suggestion_disposition(SUGG, diff, True, []) == "accepted_manually"


def test_rejected_with_rationale():
    """A rejection has to be recognisable to be recorded as one."""
    for reply in ["Declined — no observable effect", "Unrelated",
                  "intentional", "This is by design"]:
        assert classify_suggestion_disposition(SUGG, "", True, [reply]) == \
            "rejected_with_rationale", reply


def test_unreadable_explanation_is_not_scored_as_a_rejection():
    """This reply IS a disagreement, but nothing in it says so in a form we
    can detect. It used to fall through to 'rejected', which is how "we could
    not read this" became the largest source of recorded rejections. Counting
    it as unclassified undercounts real rejections, which is the right way to
    be wrong: the acceptance rate stays unbiased instead of flattering or
    damning the bot on replies nobody parsed."""
    replies = ["the original tool already encloses the log data"]
    assert classify_suggestion_disposition(SUGG, "", True, replies) == \
        "replied_unclassified"


def test_accepted_manually_when_reply_acknowledges_fix():
    for reply in ["fixed", "Fixed.", "Updated", "done", "Addressed, thanks"]:
        assert classify_suggestion_disposition(SUGG, "", True, [reply]) == \
            "accepted_manually", reply


def test_dismissed_ambiguous():
    assert classify_suggestion_disposition(SUGG, "", True, []) == "dismissed_ambiguous"


def test_open_when_unresolved():
    assert classify_suggestion_disposition(SUGG, "", False, []) == "open"


class FakeClient:
    async def list_diffs(self, project, iid):
        return []

    async def get_note_awards(self, project, iid, note_id):
        return []


async def test_reconcile_mr_reports_only_changed_bot_dispositions(db):
    repo = Repository(provider="gitlab", project_path=f"g/chg-{uuid.uuid4().hex[:6]}",
                      gitlab_project_id=int(uuid.uuid4().int % 10**8))
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=2, title="t", state="opened",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    changed = Discussion(mr_id=mr.id, provider_discussion_id="d1",
                         resolvable=True, resolved=True)
    same = Discussion(mr_id=mr.id, provider_discussion_id="d2",
                      resolvable=True, resolved=False)
    silent = Discussion(mr_id=mr.id, provider_discussion_id="d3",
                        resolvable=True, resolved=False)
    db.add_all([changed, same, silent])
    await db.flush()
    now = datetime.now(timezone.utc)
    flips = Note(mr_id=mr.id, discussion_id=changed.id, provider_note_id=1,
                 author_type="bot", kind="inline", body="suggestion A",
                 disposition="open", note_created_at=now)
    stays = Note(mr_id=mr.id, discussion_id=same.id, provider_note_id=2,
                 author_type="bot", kind="inline", body="suggestion B",
                 disposition="open", note_created_at=now)
    human = Note(mr_id=mr.id, discussion_id=silent.id, provider_note_id=3,
                 author_type="human", kind="inline", body="should be immutable",
                 disposition="open", note_created_at=now)
    db.add_all([flips, stays, human])
    await db.flush()

    result = await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert isinstance(result, ReconcileResult)
    assert result.changed_note_ids == [flips.id]


async def _bot_thread_with_reply(db, reply_body, suggestion_applied=False):
    repo = Repository(provider="gitlab", project_path=f"g/rc-{uuid.uuid4().hex[:6]}",
                      gitlab_project_id=int(uuid.uuid4().int % 10**8))
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="s", target_branch="m", head_sha="abc", web_url="http://x")
    db.add(mr)
    await db.flush()
    disc = Discussion(mr_id=mr.id, provider_discussion_id=uuid.uuid4().hex,
                      resolvable=True, resolved=True)
    db.add(disc)
    await db.flush()
    now = datetime.now(timezone.utc)
    bot = Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=1, author_type="bot",
               kind="inline", body="add a timeout", note_created_at=now,
               suggestions=[{"applied": True}] if suggestion_applied else None)
    reply = Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=2, author_type="human",
                 kind="inline", body=reply_body, note_created_at=now + timedelta(minutes=1))
    db.add_all([bot, reply])
    await db.flush()
    return repo, mr, disc, bot, reply


async def _verdict(db, mr, disc, human_ids, verdict, hash_override=None):
    from argus.knowledge.distill_threads import content_hash
    db.add(DistillThread(mr_id=mr.id, discussion_id=disc.id, thread_type="bot_thread",
                         content_hash=hash_override or content_hash(human_ids),
                         status="done", reply_verdict=verdict))
    await db.flush()


async def test_an_agent_verdict_replaces_replied_unclassified(db):
    repo, mr, disc, bot, reply = await _bot_thread_with_reply(db, "see helper/http.py")
    await _verdict(db, mr, disc, [reply.id], "rejected")
    await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert bot.disposition == "rejected_with_rationale"


async def test_an_agent_verdict_overrides_a_keyword_guess(db):
    repo, mr, disc, bot, reply = await _bot_thread_with_reply(db, "fixed?")
    await _verdict(db, mr, disc, [reply.id], "rejected")
    await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert bot.disposition == "rejected_with_rationale"


async def test_an_applied_suggestion_is_never_overridden(db):
    repo, mr, disc, bot, reply = await _bot_thread_with_reply(db, "meh", suggestion_applied=True)
    await _verdict(db, mr, disc, [reply.id], "rejected")
    await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert bot.disposition == "accepted"


async def test_an_unclear_verdict_changes_nothing(db):
    repo, mr, disc, bot, reply = await _bot_thread_with_reply(db, "this is intentional")
    await _verdict(db, mr, disc, [reply.id], "unclear")
    await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert bot.disposition == "rejected_with_rationale"


@pytest.mark.parametrize("verdict", ["question", "acknowledged"])
async def test_a_question_or_deferral_is_not_counted_as_a_rejection(db, verdict):
    # "not needed?" trips the rejection keywords, but a question is no verdict on the bot
    repo, mr, disc, bot, reply = await _bot_thread_with_reply(db, "not needed here?")
    await _verdict(db, mr, disc, [reply.id], verdict)
    await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert bot.disposition == "replied_unclassified"


async def test_a_stale_verdict_is_ignored_after_a_new_reply(db):
    repo, mr, disc, bot, reply = await _bot_thread_with_reply(db, "see helper/http.py")
    await _verdict(db, mr, disc, [reply.id], "rejected", hash_override="old-hash")
    await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert bot.disposition == "replied_unclassified"


def test_a_verified_followup_counts_as_accepted_but_ranks_below_humans():
    assert classify_suggestion_disposition([], "", False, [], followup_verified=True) == "accepted_by_followup"
    assert classify_suggestion_disposition([], "", True, [], followup_verified=True) == "accepted_by_followup"
    assert classify_suggestion_disposition(
        [], "", True, ["this is intentional"], followup_verified=True) == "rejected_with_rationale"
    assert classify_suggestion_disposition([], "", True, ["Fixed"], followup_verified=True) == "accepted_manually"
    assert classify_suggestion_disposition([], "", False, []) == "open"


async def test_bot_resolves_count_only_when_the_followup_verified_a_fix(db):
    from argus.domain.models import Actor, Feedback
    repo = Repository(provider="gitlab", project_path="g/bot-resolved", gitlab_project_id=9301)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened", source_branch="s",
                      target_branch="m", head_sha="abc", web_url="http://x")
    bot = Actor(provider_user_id=93010, username="argus")
    human = Actor(provider_user_id=93011, username="dev")
    db.add_all([mr, bot, human])
    await db.flush()
    now = datetime.now(timezone.utc)

    async def thread(key, resolved_by, verdict=None, reply=None):
        disc = Discussion(mr_id=mr.id, provider_discussion_id=key, resolvable=True,
                          resolved=resolved_by is not None,
                          resolved_by_id=resolved_by.id if resolved_by else None)
        db.add(disc)
        await db.flush()
        note = Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=abs(hash(key)) % 10**8,
                    author_id=bot.id, author_type="bot", kind="inline", body="x",
                    file_path="a.py", note_created_at=now)
        db.add(note)
        await db.flush()
        if verdict:
            db.add(Feedback(note_id=note.id, kind="followup", payload={"verdict": verdict}))
        if reply:
            db.add(Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=abs(hash(key + "r")) % 10**8,
                        author_id=human.id, author_type="human", kind="inline", body=reply,
                        note_created_at=now + timedelta(minutes=1)))
        await db.flush()
        return note

    bot_resolved_unverified = await thread("t1", bot, verdict="code_removed")
    bot_resolved_verified = await thread("t2", bot, verdict="addressed")
    reply_only_verified = await thread("t3", None, verdict="addressed")
    human_resolved = await thread("t4", human)
    human_disagrees = await thread("t5", None, verdict="addressed", reply="not needed, this is intentional")

    await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert bot_resolved_unverified.disposition == "open"
    assert bot_resolved_verified.disposition == "accepted_by_followup"
    assert reply_only_verified.disposition == "accepted_by_followup"
    assert human_resolved.disposition == "dismissed_ambiguous"
    assert human_disagrees.disposition == "rejected_with_rationale"


async def test_a_human_reviewers_comment_is_rated_like_the_bots_but_never_counted(db):
    from sqlalchemy import select
    from argus.domain.models import Feedback
    repo, mr, disc, bot, reply = await _bot_thread_with_reply(db, "add a timeout")
    now = datetime.now(timezone.utc)
    rdisc = Discussion(mr_id=mr.id, provider_discussion_id=uuid.uuid4().hex, resolvable=True,
                       resolved=True)
    db.add(rdisc)
    await db.flush()
    reviewer = Note(mr_id=mr.id, discussion_id=rdisc.id, provider_note_id=10, author_type="human",
                    kind="inline", body="This should be done in a transaction", note_created_at=now)
    author = Note(mr_id=mr.id, discussion_id=rdisc.id, provider_note_id=11, author_type="human",
                  kind="inline", body="Done", note_created_at=now + timedelta(minutes=5))
    db.add_all([reviewer, author])
    await db.flush()

    result = await reconcile_mr(db, FakeClient(), repo, mr, None)

    assert reviewer.disposition == "accepted_manually"
    assert author.disposition == "open", "only the comment that opened the thread is rated"
    assert reviewer.id not in result.changed_note_ids
    assert (await db.execute(select(Feedback).where(Feedback.note_id == reviewer.id))).first() is None

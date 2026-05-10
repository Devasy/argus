from datetime import datetime, timedelta, timezone

from argus.domain.models import (Actor, Discussion, Learning,
                                     MergeRequest, Note, Repository)
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


async def test_reconcile_mr_candidate_includes_repo_id(db):
    repo = Repository(provider="gitlab", project_path="g/reconciler-p",
                      gitlab_project_id=9001)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    disc = Discussion(mr_id=mr.id, provider_discussion_id="d1",
                      resolvable=True, resolved=True)
    db.add(disc)
    await db.flush()
    now = datetime.now(timezone.utc)
    bot_note = Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=1,
                    author_type="bot", kind="inline", body="wrap in <log_data>",
                    file_path="x/tools.py", note_created_at=now)
    db.add(bot_note)
    await db.flush()
    reply = Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=2,
                author_type="human", kind="inline", body="already wrapped",
                note_created_at=now + timedelta(minutes=1))
    db.add(reply)
    await db.flush()

    result = await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert bot_note.id in result.note_ids


async def test_reconcile_mr_batches_all_resolved_notes_for_the_mr(db):
    repo = Repository(provider="gitlab", project_path="g/batch-p",
                      gitlab_project_id=9101)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=2, title="t", state="opened",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    disc1 = Discussion(mr_id=mr.id, provider_discussion_id="d1",
                       resolvable=True, resolved=True)
    disc2 = Discussion(mr_id=mr.id, provider_discussion_id="d2",
                       resolvable=True, resolved=True)
    db.add_all([disc1, disc2])
    await db.flush()
    now = datetime.now(timezone.utc)
    note1 = Note(mr_id=mr.id, discussion_id=disc1.id, provider_note_id=1,
                author_type="bot", kind="inline", body="suggestion A",
                disposition="open", note_created_at=now)
    note2 = Note(mr_id=mr.id, discussion_id=disc2.id, provider_note_id=2,
                author_type="bot", kind="inline", body="suggestion B",
                disposition="open", note_created_at=now)
    db.add_all([note1, note2])
    await db.flush()

    result = await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert isinstance(result, ReconcileResult)
    assert len(result.note_ids) == 2


async def test_reconcile_mr_detects_bot_silent_human_discussion(db):
    repo = Repository(provider="gitlab", project_path="g/silent-p",
                      gitlab_project_id=9102)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=3, title="t", state="opened",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    disc = Discussion(mr_id=mr.id, provider_discussion_id="d3",
                      resolvable=True, resolved=False)
    db.add(disc)
    await db.flush()
    human = Actor(username="bob", provider_user_id=5, provider="gitlab")
    db.add(human)
    await db.flush()
    human_note = Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=3,
                      author_id=human.id, author_type="human", kind="inline",
                      body="This mutates shared state, should be immutable",
                      disposition="open", note_created_at=datetime.now(timezone.utc))
    db.add(human_note)
    await db.flush()

    result = await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert human_note.id in result.note_ids


async def test_reconcile_mr_does_not_redetect_already_distilled_note(db):
    repo = Repository(provider="gitlab", project_path="g/dedup-p",
                      gitlab_project_id=9103)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=4, title="t", state="opened",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    disc = Discussion(mr_id=mr.id, provider_discussion_id="d4",
                      resolvable=True, resolved=False)
    db.add(disc)
    await db.flush()
    human = Actor(username="carol", provider_user_id=6, provider="gitlab")
    db.add(human)
    await db.flush()
    human_note = Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=4,
                      author_id=human.id, author_type="human", kind="inline",
                      body="Already learned from this one",
                      disposition="open", note_created_at=datetime.now(timezone.utc))
    db.add(human_note)
    await db.flush()
    # Simulate a prior distillation already having consumed this note.
    learning = Learning(topic="x", hint_text="y", source_note_id=human_note.id)
    db.add(learning)
    await db.flush()

    result = await reconcile_mr(db, FakeClient(), repo, mr, None)
    assert human_note.id not in result.note_ids

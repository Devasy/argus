import inspect
import itertools
import json
from datetime import datetime, timezone

import pytest

import argus.review.followup as fu
from argus.review.followup import (eligible, follow_up_prior_comments, map_line, parse_verdict,
                                       reply_body, reviewed_sha_for)

BOT = "argus"
HEAD = "c" * 40
OLD = "a" * 40
_ids = itertools.count(93_000_001)


def _note(nid, author, body="", system=False, **extra):
    return {"id": nid, "author": {"username": author}, "body": body, "system": system, **extra}


def _thread(*replies, resolved=False, first_type="DiffNote", position=True):
    first = _note(1, BOT, "🟠 **Bug** the filter never matches", type=first_type,
                  resolved=resolved, created_at="2026-09-20T10:00:00Z",
                  position={"head_sha": OLD, "new_path": "app.py", "new_line": 3} if position else None)
    return {"id": "disc-1", "notes": [first, *replies]}


# ---- eligible ------------------------------------------------------------------------------

def test_silent_open_bot_thread_is_eligible_as_silent():
    first, kind = eligible(_thread(), BOT, {1}, {}, HEAD)
    assert first["id"] == 1 and kind == "silent"


def test_fix_claim_replies_make_it_a_claimed_thread():
    assert eligible(_thread(_note(2, "dev", "Done")), BOT, {1}, {}, HEAD)[1] == "claimed"


@pytest.mark.parametrize("thread,history", [
    (_thread(_note(2, "dev", "This is intentional, not needed")), {}),
    (_thread(_note(2, "dev", "Why would this happen?")), {}),
    (_thread(resolved=True), {}),
    (_thread(first_type="DiscussionNote"), {}),
    (_thread(position=False), {}),
    (_thread(), {1: [{"action": "resolved"}]}),
    (_thread(), {1: [{"action": "replied", "verdict": "not_addressed"}]}),
    (_thread(), {1: [{"verdict": "addressed", "action": "none"}]}),
    (_thread(), {1: [{"verdict": "not_addressed", "action": "none", "head_sha": HEAD}]}),
])
def test_threads_that_must_never_be_touched(thread, history):
    assert eligible(thread, BOT, {1}, history, HEAD) is None


def test_not_our_comment_is_skipped():
    assert eligible(_thread(), BOT, {999}, {}, HEAD) is None


def test_bot_and_system_notes_do_not_count_as_human_replies():
    t = _thread(_note(2, BOT, "earlier bot text"), _note(3, "dev", "changed title", system=True))
    assert eligible(t, BOT, {1}, {}, HEAD)[1] == "silent"


# ---- reviewed_sha_for / map_line / parse_verdict ------------------------------------------

VERSIONS = [{"head_commit_sha": OLD, "created_at": "2026-09-18T00:00:00Z"},
            {"head_commit_sha": "b" * 40, "created_at": "2026-09-19T00:00:00Z"},
            {"head_commit_sha": HEAD, "created_at": "2026-09-21T00:00:00Z"}]


def test_reviewed_sha_uses_the_position_when_it_existed_at_comment_time():
    assert reviewed_sha_for({"head_sha": OLD}, VERSIONS, "2026-09-20T00:00:00Z") == OLD


def test_reviewed_sha_ignores_a_position_gitlab_already_tracked_forward():
    assert reviewed_sha_for({"head_sha": HEAD}, VERSIONS, "2026-09-20T00:00:00Z") == "b" * 40


DIFF = "@@ -10,3 +10,5 @@\n a\n+x\n+y\n b\n c\n@@ -40,2 +42,1 @@\n p\n-q\n"


def test_map_line_before_inside_and_after_hunks():
    assert map_line(DIFF, 5) == 5
    assert map_line(DIFF, 11) == 10
    assert map_line(DIFF, 20) == 22
    assert map_line(DIFF, 50) == 51
    assert map_line("", 7) == 7


def test_parse_verdict_handles_json_think_noise_and_garbage():
    assert parse_verdict('{"verdict": "addressed", "evidence": "uses PAID"}') == ("addressed", "uses PAID")
    noisy = '<think>maybe {"verdict": "addressed"}</think>Result: {"verdict": "not_addressed", "evidence": "same"}'
    assert parse_verdict(noisy) == ("not_addressed", "same")
    assert parse_verdict("no json here")[0] == "unsure"
    assert parse_verdict('{"verdict": "maybe"}')[0] == "unsure"


def test_parse_verdict_handles_list_shaped_content_from_a_reasoning_model():
    """langchain_litellm returns .content as a list of blocks -- not a plain
    string -- whenever the model ran with reasoning enabled (see
    _inject_reasoning_content_into_content): a 'thinking' block first, then a
    'text' block with the actual answer. The thinking block's own text must
    be ignored, not scanned for a stray {...} that isn't the real verdict."""
    content = [
        {"type": "thinking", "thinking": 'musing... maybe {"verdict": "addressed"} but not sure'},
        {"type": "text", "text": '{"verdict": "not_addressed", "evidence": "still there"}'},
    ]
    assert parse_verdict(content) == ("not_addressed", "still there")
    assert parse_verdict([{"type": "thinking", "thinking": "only thinking, no answer"}])[0] == "unsure"
    assert parse_verdict([])[0] == "unsure"


# ---- reply_body: action table and tone -----------------------------------------------------

def test_reply_table():
    assert "Looks like this was addressed" in reply_body("silent", "addressed", "e", HEAD, "reply_only", "a.py")
    assert "leaving this open" in reply_body("silent", "addressed", "e", HEAD, "reply_only", "a.py")
    assert "resolved automatically" in reply_body("silent", "code_removed", "e", HEAD, "resolve", "a.py")
    assert reply_body("claimed", "addressed", "e", HEAD, "reply_only", "a.py") is None
    assert reply_body("claimed", "addressed", "e", HEAD, "resolve", "a.py").startswith("Thanks, confirmed")
    assert reply_body("silent", "not_addressed", "e", HEAD, "resolve", "a.py") is None
    assert reply_body("silent", "unsure", "e", HEAD, "reply_only", "a.py") is None
    assert reply_body("claimed", "unsure", "e", HEAD, "reply_only", "a.py") is None


def test_the_not_addressed_note_is_a_gentle_fixed_template():
    note = reply_body("claimed", "not_addressed", "STILL BROKEN you must fix", HEAD, "resolve", "a.py")
    assert "STILL BROKEN" not in note and "must" not in note.lower()
    assert "No action needed if it's covered" in note and "won't comment on this thread again" in note


# ---- follow_up_prior_comments end to end ---------------------------------------------------

class FakeGitLab:
    def __init__(self, discussions, file_changed=True):
        self.discussions, self.file_changed = discussions, file_changed
        self.notes, self.resolved = [], []

    async def get_current_user(self):
        return {"username": BOT}

    async def list_discussions(self, project, iid):
        return self.discussions

    async def list_versions(self, project, iid):
        return VERSIONS

    async def _get(self, path, **params):
        diff = "@@ -3,1 +3,1 @@\n-bad = 'succeeded'\n+bad = PAID\n"
        return {"diffs": [{"old_path": "app.py", "new_path": "app.py", "diff": diff}]
                if self.file_changed else []}

    async def get_file_raw(self, project, path, ref):
        return "a\nb\nbad = 'succeeded'\nd\n"

    async def create_note(self, project, iid, discussion_id, body):
        self.notes.append((discussion_id, body))

    async def resolve_discussion(self, project, iid, discussion_id, resolved):
        self.resolved.append(discussion_id)


class FakeModel:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    async def ainvoke(self, messages):
        self.calls += 1
        return type("A", (), {"content": json.dumps({"verdict": self.verdict, "evidence": "uses PAID"})})()


class _Cfg:
    def model_copy(self, update):
        return self


async def _seed(sf):
    from argus.domain.models import MergeRequest, Note, Repository
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path=f"g/fu-{next(_ids)}",
                          gitlab_project_id=next(_ids))
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened", source_branch="a",
                          target_branch="b", head_sha=HEAD, web_url="u")
        s.add(mr); await s.flush()
        pid = next(_ids)
        note = Note(mr_id=mr.id, provider_note_id=pid, author_type="bot", kind="inline", body="b",
                    file_path="app.py", line=3, note_created_at=datetime.now(timezone.utc))
        s.add(note); await s.commit()
        return mr.id, pid, note.id


async def _run(engine, tmp_path, monkeypatch, verdict, mode, replies=(), file_changed=True):
    from argus.db import session_factory
    from argus.domain.models import Feedback
    from sqlalchemy import select
    sf = session_factory(engine)
    mr_id, pid, note_id = await _seed(sf)
    thread = _thread(*replies)
    thread["notes"][0]["id"] = pid
    gl = FakeGitLab([thread], file_changed=file_changed)
    model = FakeModel(verdict)
    monkeypatch.setattr(fu, "build_chat_model", lambda cfg: model)
    (tmp_path / "app.py").write_text("a\nb\nbad = PAID\nd\n")
    stats = await follow_up_prior_comments(sf, gl, 1, 1, mr_id, HEAD, tmp_path, _Cfg(), mode)
    async with sf() as s:
        rows = (await s.execute(select(Feedback).where(
            Feedback.kind == "followup", Feedback.note_id == note_id))).scalars().all()
    assert len(rows) <= 1, "one check per thread per head"
    return stats, gl, model, rows[0].payload if rows else None, rows


async def test_reply_only_replies_without_resolving(engine, tmp_path, monkeypatch):
    stats, gl, model, last, _ = await _run(engine, tmp_path, monkeypatch, "addressed", "reply_only")
    assert len(gl.notes) == 1 and "Looks like this was addressed" in gl.notes[0][1]
    assert gl.resolved == [] and stats.replied == 1 and last["action"] == "replied"


async def test_resolve_mode_replies_and_resolves(engine, tmp_path, monkeypatch):
    stats, gl, _, last, _ = await _run(engine, tmp_path, monkeypatch, "addressed", "resolve")
    assert gl.resolved == ["disc-1"] and last["action"] == "resolved"


async def test_silent_not_addressed_stays_silent_but_is_recorded(engine, tmp_path, monkeypatch):
    stats, gl, _, last, _ = await _run(engine, tmp_path, monkeypatch, "not_addressed", "resolve")
    assert gl.notes == [] and gl.resolved == [] and last["verdict"] == "not_addressed"


async def test_claimed_fix_not_addressed_gets_one_gentle_note(engine, tmp_path, monkeypatch):
    _, gl, _, last, _ = await _run(engine, tmp_path, monkeypatch, "not_addressed", "reply_only",
                                   replies=[_note(2, "dev", "Done")], file_changed=False)
    assert len(gl.notes) == 1 and "No action needed" in gl.notes[0][1]
    assert gl.resolved == [] and last["thread"] == "claimed"


async def test_claimed_fix_confirmed_in_reply_only_posts_nothing(engine, tmp_path, monkeypatch):
    _, gl, _, last, _ = await _run(engine, tmp_path, monkeypatch, "addressed", "reply_only",
                                   replies=[_note(2, "dev", "Fixed.")])
    assert gl.notes == [] and last["verdict"] == "addressed" and last["action"] == "none"


async def test_silent_thread_on_an_unchanged_file_makes_no_llm_call(engine, tmp_path, monkeypatch):
    stats, gl, model, _, _ = await _run(engine, tmp_path, monkeypatch, "addressed", "resolve",
                                        file_changed=False)
    assert model.calls == 0 and gl.notes == [] and stats.checked == 0


async def test_off_mode_does_nothing(engine, tmp_path, monkeypatch):
    stats, gl, model, _, _ = await _run(engine, tmp_path, monkeypatch, "addressed", "off")
    assert model.calls == 0 and gl.notes == [] and stats.candidates == 0


# ---- runner wiring (read from source, as tests/test_startup_uses_effective_settings.py does) --

def test_runner_only_follows_up_on_published_full_rereviews_of_open_mrs():
    from argus.review import runner
    src = inspect.getsource(runner.execute_review_job)
    gate = src[src.index("if (is_rereview"):src.index("follow_up_prior_comments(")]
    for cond in ("is_rereview", "review_publish", 'review_mode == "full"',
                 'mr_payload.get("state") == "opened"', 'settings.followup_mode != "off"'):
        assert cond in gate
    assert "except Exception" in src[src.index("follow_up_prior_comments("):src.index("skills = ")]


def test_followup_mode_setting_only_accepts_known_modes():
    from argus.settings_store import _check
    for ok in ("off", "reply_only", "resolve"):
        _check("followup_mode", ok)
    with pytest.raises(ValueError, match="must be one of"):
        _check("followup_mode", "always")

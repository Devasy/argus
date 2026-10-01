import uuid

import pytest
from sqlalchemy import select

from argus.knowledge import agentic_distiller as _ad
from argus.knowledge.agentic_distiller import (DistillationResult, StagedLearning,
                                                   ThreadDecision,
                                                   build_propose_learning_tool,
                                                   resolve_kind, validate_decisions)
from argus.knowledge.distill_threads import load_threads
from argus.llm.config import LLMConfig
from argus.review import stages
from tests.distill_helpers import _mr_disc, _resolved_bot_thread, purge_repos

_COMMITTED_REPOS: list = []


@pytest.fixture(autouse=True)
async def _purge_committed(engine):
    yield
    await purge_repos(engine, list(_COMMITTED_REPOS))
    _COMMITTED_REPOS.clear()


async def test_run_thread_distillation_returns_validated_decisions(
        db, engine, settings, monkeypatch):
    sf, repo, mr, t = await _thread(engine)

    async def fake_stage_agent(model, tools, system_prompt, user_msg,
                               response_model, max_rounds, callbacks=None, metadata=None):
        assert response_model is DistillationResult
        assert "intentional, closed in finally" in user_msg
        return DistillationResult(decisions=[ThreadDecision(
            discussion_id=str(t.discussion_id), reply_verdict="rejected",
            reason="one-off, not generalizable")])
    monkeypatch.setattr(stages, "run_stage_agent", fake_stage_agent)

    class FakeGitLab:
        async def list_diffs(self, project, iid):
            return []

    decisions, staged = await _ad.run_thread_distillation(
        sf, settings, FakeGitLab(), repo, mr, [t],
        LLMConfig(provider="anthropic", model="claude-sonnet-4-6"), distillation_run_id=None)
    assert decisions[t.discussion_id].reply_verdict == "rejected" and staged == []


async def test_run_thread_distillation_degrades_when_clone_fails(
        db, engine, settings, monkeypatch):
    """WorkspaceManager.acquire failing (e.g. branch deleted post-merge) must
    not raise — the run proceeds with diff-only tools."""
    from argus.review.workspace import WorkspaceManager
    sf, repo, mr, t = await _thread(engine)

    async def failing_acquire(self, sha, mr_iid=None):
        raise RuntimeError("branch no longer exists")
    monkeypatch.setattr(WorkspaceManager, "acquire", failing_acquire)

    async def fake_stage_agent(model, tools, system_prompt, user_msg,
                               response_model, max_rounds, callbacks=None, metadata=None):
        tool_names = {x.name for x in tools}
        assert "get_file_lines" not in tool_names  # degraded: no file-content tool
        assert "get_hunk" in tool_names  # diff-hunk tool still present
        return DistillationResult(decisions=[])
    monkeypatch.setattr(stages, "run_stage_agent", fake_stage_agent)

    class FakeGitLab:
        async def list_diffs(self, project, iid):
            return []

    decisions, _ = await _ad.run_thread_distillation(
        sf, settings, FakeGitLab(), repo, mr, [t],
        LLMConfig(provider="anthropic", model="claude-sonnet-4-6"), distillation_run_id=None)
    assert decisions == {}


# --- distiller round budget ------------------------------------------------

def test_distiller_has_its_own_round_budget_setting():
    """It had none: a hardcoded max_rounds=10 while analyze gets 25 and scout
    20, despite a prompt that demands a learnings search per point plus code
    reading. run_stage_agent derives recursion_limit = max_rounds*2+4, so 10
    produced exactly the "Recursion limit of 24" that failed 111 runs."""
    from argus.config import Settings

    s = Settings(database_url="postgresql+asyncpg://x/y",
                 gitlab_url="https://g.test", gitlab_token="t")
    assert s.distiller_max_rounds > 10
    assert s.distiller_max_rounds * 2 + 4 > 24


def test_round_budget_prefers_an_explicit_override():
    from argus.config import Settings
    from argus.knowledge.agentic_distiller import _round_budget

    s = Settings(database_url="postgresql+asyncpg://x/y",
                 gitlab_url="https://g.test", gitlab_token="t")
    assert _round_budget(s, None) == s.distiller_max_rounds
    assert _round_budget(s, 7) == 7
    assert _round_budget(s, 0) == 0, "0 is a real value, not 'unset'"


# --- thread-based distillation: propose_learning + decisions ---------------


def _args(t, **kw):
    base = {"discussion_id": str(t.discussion_id), "action": "create", "topic": "resources",
            "hint_text": "Do not suggest context managers where finally closes it",
            "source_note_id": str(t.human_notes[-1].id)}
    base.update(kw)
    return base


async def _thread(engine):
    from argus.db import session_factory
    sf = session_factory(engine)
    async with sf() as s:
        repo, mr, _ = await _mr_disc(s)
        await _resolved_bot_thread(s, mr)
        await s.commit()
        _COMMITTED_REPOS.append(repo.id)
        [t] = await load_threads(s, mr)
    return sf, repo, mr, t


async def test_propose_learning_stages_a_valid_proposal(db, engine, settings, monkeypatch):
    import argus.knowledge.agentic_distiller as ad

    async def no_matches(*a, **k):
        return []
    monkeypatch.setattr(ad, "relevant_learnings", no_matches)
    sf, repo, mr, t = await _thread(engine)
    staged: list = []
    tool = build_propose_learning_tool(sf, settings, repo.id, [t], staged)
    out = await tool.ainvoke(_args(t))
    assert "staged" in out
    assert len(staged) == 1 and staged[0].source_note_id == t.human_notes[-1].id


async def test_propose_learning_rejects_the_bot_note_as_source(db, engine, settings):
    sf, repo, mr, t = await _thread(engine)
    staged: list = []
    tool = build_propose_learning_tool(sf, settings, repo.id, [t], staged)
    out = await tool.ainvoke(_args(t, source_note_id=str(t.bot_note.id)))
    assert "human note" in out and str(t.human_notes[-1].id) in out
    assert staged == []


async def test_propose_learning_rejects_an_unknown_thread(db, engine, settings):
    sf, repo, mr, t = await _thread(engine)
    staged: list = []
    tool = build_propose_learning_tool(sf, settings, repo.id, [t], staged)
    out = await tool.ainvoke(_args(t, discussion_id=str(uuid.uuid4())))
    assert "not a thread in this batch" in out and staged == []


async def test_propose_learning_refuses_to_update_another_repos_learning(db, engine, settings):
    from argus.domain.models import Learning, Repository
    sf, repo, mr, t = await _thread(engine)
    async with sf() as s:
        other = Repository(provider="gitlab", project_path=f"g/o-{uuid.uuid4().hex[:6]}",
                           gitlab_project_id=int(uuid.uuid4().int % 10**8))
        s.add(other)
        await s.flush()
        foreign = Learning(repo_id=other.id, topic="t", hint_text="theirs", kind="guidance")
        s.add(foreign)
        await s.commit()
        _COMMITTED_REPOS.append(other.id)
    staged: list = []
    tool = build_propose_learning_tool(sf, settings, repo.id, [t], staged)
    out = await tool.ainvoke(_args(t, action="update", learning_id=str(foreign.id)[:8]))
    assert "another repository" in out and staged == []


async def test_propose_learning_points_out_a_near_duplicate(db, engine, settings, monkeypatch):
    from types import SimpleNamespace
    import argus.knowledge.agentic_distiller as ad
    sf, repo, mr, t = await _thread(engine)
    dup = SimpleNamespace(id=uuid.uuid4(), topic="resources", hint_text="close in finally",
                          repo_id=repo.id)

    async def one_match(*a, **k):
        return [dup]
    monkeypatch.setattr(ad, "relevant_learnings", one_match)
    staged: list = []
    tool = build_propose_learning_tool(sf, settings, repo.id, [t], staged)
    out = await tool.ainvoke(_args(t))
    assert str(dup.id)[:8] in out and "update" in out
    assert len(staged) == 1


async def test_propose_learning_caps_proposals_per_thread(db, engine, settings, monkeypatch):
    import argus.knowledge.agentic_distiller as ad

    async def no_matches(*a, **k):
        return []
    monkeypatch.setattr(ad, "relevant_learnings", no_matches)
    sf, repo, mr, t = await _thread(engine)
    staged: list = []
    tool = build_propose_learning_tool(sf, settings, repo.id, [t], staged)
    for i in range(4):
        out = await tool.ainvoke(_args(t, topic=f"t{i}"))
    assert len(staged) == 3 and "limit" in out


async def test_validate_decisions_keeps_known_threads_and_fixes_verdicts(db, engine):
    sf, repo, mr, t = await _thread(engine)
    res = DistillationResult(decisions=[
        ThreadDecision(discussion_id=str(uuid.uuid4()), reason="stray"),
        ThreadDecision(discussion_id=str(t.discussion_id).upper(), reason="r"),
        ThreadDecision(discussion_id=str(t.discussion_id), reply_verdict="rejected", reason="dup")])
    out = validate_decisions(res, [t])
    assert list(out) == [t.discussion_id]
    assert out[t.discussion_id].reply_verdict == "rejected"


async def test_validate_decisions_keeps_the_agents_correction(db, engine):
    # MR 180: a verdict copied from a neighbouring thread, then corrected; the first one used to win.
    sf, repo, mr, t = await _thread(engine)
    res = DistillationResult(decisions=[
        ThreadDecision(discussion_id=str(t.discussion_id), reply_verdict="rejected", reason="wrong thread"),
        ThreadDecision(discussion_id=str(t.discussion_id), reply_verdict="accepted", reason="fixed")])
    assert validate_decisions(res, [t])[t.discussion_id].reply_verdict == "accepted"


def test_prompt_judges_the_humans_action_not_the_bots_correctness():
    from argus.knowledge.agentic_distiller import _SYSTEM
    assert "WHAT THE HUMAN DID" in _SYSTEM
    assert '"Updated", "Done"' in _SYSTEM
    assert "narrow" in _SYSTEM and "LAST decision counts" in _SYSTEM


def test_resolve_kind_rules():
    from types import SimpleNamespace
    bot, human = SimpleNamespace(thread_type="bot_thread"), SimpleNamespace(thread_type="human_thread")
    assert resolve_kind(bot, "rejected", "guidance") == "do_not_suggest"
    assert resolve_kind(bot, "accepted", "do_not_suggest") == "guidance"
    assert resolve_kind(human, None, "do_not_suggest") == "missed_pattern"
    assert resolve_kind(human, None, "guidance") == "guidance"
    assert resolve_kind(human, None, None) == "missed_pattern"


# --- running the agent over threads, then applying ----------------------------

async def test_apply_writes_staged_learnings_with_the_final_verdicts_kind(db, engine, settings, monkeypatch):
    import argus.knowledge.learnings as L
    from argus.domain.models import DistillThread, Learning
    from argus.knowledge.agentic_distiller import apply_decisions

    async def fake_embed(text, s, is_query=False):
        return [0.3] * 768
    monkeypatch.setattr(L, "embed_text", fake_embed)
    sf, repo, mr, t = await _thread(engine)
    async with sf() as s:
        s.add(DistillThread(mr_id=mr.id, discussion_id=t.discussion_id, thread_type="bot_thread",
                            content_hash="x", status="queued"))
        await s.commit()
    staged = [StagedLearning(t.discussion_id, "create", None, "guidance", "resources",
                             "Do not suggest context managers where finally closes it",
                             None, t.human_notes[-1].id)]
    decisions = {t.discussion_id: ThreadDecision(
        discussion_id=str(t.discussion_id), reply_verdict="rejected",
        verdict_reason="closed in finally", reason="team idiom")}

    counts = await apply_decisions(sf, settings, repo, mr, [t], decisions, staged, None)

    assert counts["created"] == 1 and counts["threads_done"] == 1
    async with sf() as s:
        row = (await s.execute(select(DistillThread).where(
            DistillThread.discussion_id == t.discussion_id))).scalar_one()
        assert (row.status, row.reply_verdict, row.content_hash) == ("done", "rejected", t.content_hash)
        lrn = await s.get(Learning, uuid.UUID(row.learning_ids[0]))
        assert lrn.kind == "do_not_suggest" and lrn.source_note_id == t.human_notes[-1].id
        assert lrn.mr_id == mr.id


async def test_a_thread_without_a_decision_writes_nothing_and_fails(db, engine, settings, monkeypatch):
    import argus.knowledge.learnings as L
    from argus.domain.models import DistillThread, Learning
    from argus.knowledge.agentic_distiller import apply_decisions

    async def fake_embed(text, s, is_query=False):
        return [0.3] * 768
    monkeypatch.setattr(L, "embed_text", fake_embed)
    sf, repo, mr, t = await _thread(engine)
    async with sf() as s:
        s.add(DistillThread(mr_id=mr.id, discussion_id=t.discussion_id, thread_type="bot_thread",
                            content_hash="x", status="queued"))
        await s.commit()
    staged = [StagedLearning(t.discussion_id, "create", None, None, "never", "written",
                             None, t.human_notes[-1].id)]

    counts = await apply_decisions(sf, settings, repo, mr, [t], {}, staged, None)

    assert counts["threads_failed"] == 1 and counts["created"] == 0
    async with sf() as s:
        row = (await s.execute(select(DistillThread).where(
            DistillThread.discussion_id == t.discussion_id))).scalar_one()
        assert (row.status, row.attempts) == ("failed", 1)
        assert (await s.execute(select(Learning).where(Learning.topic == "never"))).first() is None


async def test_run_thread_distillation_gives_the_agent_propose_not_upsert(db, engine, settings, monkeypatch):
    from argus.knowledge import agentic_distiller as ad
    from argus.llm.config import LLMConfig
    seen = {}

    async def fake_run(model, tools, system_prompt, user_msg, response_model, max_rounds, **k):
        seen["tools"] = {x.name for x in tools}
        seen["msg"] = user_msg
        return ad.DistillationResult(decisions=[])

    class _GL:
        async def list_diffs(self, *a):
            return []
    monkeypatch.setattr(ad.stages, "run_stage_agent", fake_run)
    sf, repo, mr, t = await _thread(engine)
    await ad.run_thread_distillation(
        sf, settings, _GL(), repo, mr, [t],
        LLMConfig(provider="claude_cli_proxy", model="openai/x", api_base="http://x/v1", api_key="k"),
        distillation_run_id=None)
    assert {"propose_learning", "search_learnings_tool", "get_learning"} <= seen["tools"]
    assert "upsert_learning_tool" not in seen["tools"]
    assert f"discussion_id={t.discussion_id}" in seen["msg"]



# --- final-review fixes ---------------------------------------------------

async def _fake_embed(monkeypatch):
    import argus.knowledge.learnings as L

    async def fake_embed(text, s, is_query=False):
        return [0.3] * 768
    monkeypatch.setattr(L, "embed_text", fake_embed)


def _rejected(t):
    return {t.discussion_id: ThreadDecision(discussion_id=str(t.discussion_id),
                                            reply_verdict="rejected", reason="r")}


async def test_apply_writes_a_decisive_verdict_onto_the_bot_note(db, engine, settings, monkeypatch):
    from argus.domain.models import Feedback, Note
    from argus.knowledge.agentic_distiller import apply_decisions
    await _fake_embed(monkeypatch)
    sf, repo, mr, t = await _thread(engine)
    async with sf() as s:
        (await s.get(Note, t.bot_note.id)).disposition = "replied_unclassified"
        await s.commit()

    await apply_decisions(sf, settings, repo, mr, [t], _rejected(t), [], None)

    async with sf() as s:
        assert (await s.get(Note, t.bot_note.id)).disposition == "rejected_with_rationale"
        fb = (await s.execute(select(Feedback).where(Feedback.note_id == t.bot_note.id,
                                                     Feedback.kind == "resolved"))).scalar_one()
        assert fb.payload["disposition"] == "rejected_with_rationale"


async def test_apply_never_overrides_an_applied_suggestion(db, engine, settings, monkeypatch):
    from argus.domain.models import Note
    from argus.knowledge.agentic_distiller import apply_decisions
    await _fake_embed(monkeypatch)
    sf, repo, mr, t = await _thread(engine)
    async with sf() as s:
        (await s.get(Note, t.bot_note.id)).disposition = "accepted"
        await s.commit()

    await apply_decisions(sf, settings, repo, mr, [t], _rejected(t), [], None)

    async with sf() as s:
        assert (await s.get(Note, t.bot_note.id)).disposition == "accepted"


async def test_an_embedding_failure_fails_the_thread_instead_of_raising(db, engine, settings, monkeypatch):
    import argus.knowledge.learnings as L
    from argus.domain.models import DistillThread
    from argus.knowledge.agentic_distiller import apply_decisions
    from argus.knowledge.embeddings import EmbeddingError

    async def down(text, s, is_query=False):
        raise EmbeddingError("ollama unreachable")
    monkeypatch.setattr(L, "embed_text", down)
    sf, repo, mr, t = await _thread(engine)
    staged = [StagedLearning(t.discussion_id, "create", None, None, "topic", "hint",
                             None, t.human_notes[-1].id)]

    counts = await apply_decisions(sf, settings, repo, mr, [t], _rejected(t), staged, None)

    assert counts["threads_failed"] == 1 and counts["threads_done"] == 0
    async with sf() as s:
        row = (await s.execute(select(DistillThread).where(
            DistillThread.discussion_id == t.discussion_id))).scalar_one()
        assert row.status == "failed"


async def test_an_update_that_would_change_the_kind_becomes_a_create(db, engine, settings, monkeypatch):
    from argus.domain.models import DistillThread, Learning
    from argus.knowledge.agentic_distiller import apply_decisions
    await _fake_embed(monkeypatch)
    sf, repo, mr, t = await _thread(engine)
    async with sf() as s:
        existing = Learning(repo_id=repo.id, topic="resources", hint_text="prefer with-blocks",
                            kind="guidance")
        s.add(existing)
        await s.commit()
    staged = [StagedLearning(t.discussion_id, "update", existing.id, None, "resources",
                             "Do not suggest with-blocks where finally closes it",
                             None, t.human_notes[-1].id)]

    counts = await apply_decisions(sf, settings, repo, mr, [t], _rejected(t), staged, None)

    assert counts["created"] == 1 and counts["updated"] == 0
    async with sf() as s:
        assert (await s.get(Learning, existing.id)).kind == "guidance"
        row = (await s.execute(select(DistillThread).where(
            DistillThread.discussion_id == t.discussion_id))).scalar_one()
        new = await s.get(Learning, uuid.UUID(row.learning_ids[0]))
        assert new.id != existing.id and new.kind == "do_not_suggest"


async def test_near_duplicate_hint_ignores_learnings_from_other_repos(db, engine, settings, monkeypatch):
    from types import SimpleNamespace
    import argus.knowledge.agentic_distiller as ad
    sf, repo, mr, t = await _thread(engine)
    glob = SimpleNamespace(id=uuid.uuid4(), topic="x", hint_text="y", repo_id=None)

    async def global_only(*a, **k):
        return [glob]
    monkeypatch.setattr(ad, "relevant_learnings", global_only)
    staged: list = []
    out = await build_propose_learning_tool(sf, settings, repo.id, [t], staged).ainvoke(_args(t))
    assert str(glob.id)[:8] not in out and len(staged) == 1

import uuid

import pytest

from argus.domain.models import Actor, MergeRequest, Note, Repository
from argus.knowledge.agentic_distiller import (
    DistillationEntry, DistillationResult, run_agentic_distillation_for_mr)
from argus.llm.config import LLMConfig
from argus.review import stages


@pytest.fixture
async def mr_with_human_notes(db):
    repo = Repository(provider="gitlab", project_path="grp/distill-test",
                      gitlab_project_id=90000500)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=42, title="Fix null deref",
                      state="merged", source_branch="fix", target_branch="main",
                      head_sha="deadbeef", web_url="http://x")
    db.add(mr)
    await db.flush()
    bot = Actor(username="argus-bot", provider_user_id=1, provider="gitlab")
    human = Actor(username="alice", provider_user_id=2, provider="gitlab")
    db.add_all([bot, human])
    await db.flush()
    bot_note = Note(mr_id=mr.id, author_id=bot.id, author_type="bot", kind="inline",
                    body="Consider adding a null check here", file_path="x.py",
                    line=10, disposition="rejected_with_rationale",
                    provider_note_id=1001)
    human_note = Note(mr_id=mr.id, author_id=human.id, author_type="human",
                      kind="inline", parent_note_id=None,
                      body="Not needed, this is validated upstream in y.py",
                      file_path="x.py", line=10, disposition="open",
                      provider_note_id=1002)
    db.add_all([bot_note, human_note])
    await db.flush()
    yield repo, mr, [human_note], bot_note


async def test_run_agentic_distillation_returns_agent_result(
        mr_with_human_notes, settings, engine, monkeypatch):
    from argus.db import session_factory
    repo, mr, notes, _bot_note = mr_with_human_notes
    sf = session_factory(engine)

    async def fake_stage_agent(model, tools, system_prompt, user_msg,
                               response_model, max_rounds, callbacks=None, metadata=None):
        assert response_model is DistillationResult
        assert "Not needed" in user_msg or "Not needed" in system_prompt
        return DistillationResult(entries=[DistillationEntry(
            action="skip", reason="one-off validation note, not generalizable")])
    monkeypatch.setattr(stages, "run_stage_agent", fake_stage_agent)

    class FakeGitLab:
        async def list_diffs(self, project, iid):
            return []
        async def aclose(self):
            pass

    llm_cfg = LLMConfig(provider="anthropic", model="claude-sonnet-4-6")
    result = await run_agentic_distillation_for_mr(
        sf, settings, FakeGitLab(), repo, mr, notes, llm_cfg)

    assert len(result.entries) == 1
    assert result.entries[0].action == "skip"


async def test_run_agentic_distillation_filters_out_bot_notes(
        mr_with_human_notes, settings, engine, monkeypatch):
    """Bot-authored notes must never reach the distillation prompt, even if
    a caller passes them in (e.g. an unfiltered note_ids batch) — only human
    commentary should ever be distilled into a learning."""
    from argus.db import session_factory
    repo, mr, human_notes, bot_note = mr_with_human_notes
    sf = session_factory(engine)

    async def fake_stage_agent(model, tools, system_prompt, user_msg,
                               response_model, max_rounds, callbacks=None, metadata=None):
        assert "Consider adding a null check" not in user_msg
        return DistillationResult(entries=[])
    monkeypatch.setattr(stages, "run_stage_agent", fake_stage_agent)

    class FakeGitLab:
        async def list_diffs(self, project, iid):
            return []
        async def aclose(self):
            pass

    llm_cfg = LLMConfig(provider="anthropic", model="claude-sonnet-4-6")
    await run_agentic_distillation_for_mr(
        sf, settings, FakeGitLab(), repo, mr, human_notes + [bot_note], llm_cfg)


async def test_distillation_entry_accepts_source_note_id_and_missed_pattern():
    entry = DistillationEntry(action="create", topic="t", hint_text="h",
                              kind="missed_pattern", source_note_id="abc-123",
                              reason="bot never commented on this")
    assert entry.source_note_id == "abc-123"
    assert entry.kind == "missed_pattern"


async def test_run_agentic_distillation_degrades_when_clone_fails(
        mr_with_human_notes, settings, engine, monkeypatch):
    """WorkspaceManager.acquire failing (e.g. branch deleted post-merge) must
    not raise — the run proceeds with diff-only tools."""
    from argus.db import session_factory
    from argus.review.workspace import WorkspaceManager
    repo, mr, notes, _bot_note = mr_with_human_notes
    sf = session_factory(engine)

    async def failing_acquire(self, sha, mr_iid=None):
        raise RuntimeError("branch no longer exists")
    monkeypatch.setattr(WorkspaceManager, "acquire", failing_acquire)

    async def fake_stage_agent(model, tools, system_prompt, user_msg,
                               response_model, max_rounds, callbacks=None, metadata=None):
        tool_names = {t.name for t in tools}
        assert "get_file_lines" not in tool_names  # degraded: no file-content tool
        assert "get_hunk" in tool_names  # diff-hunk tool still present
        return DistillationResult(entries=[])
    monkeypatch.setattr(stages, "run_stage_agent", fake_stage_agent)

    class FakeGitLab:
        async def list_diffs(self, project, iid):
            return []
        async def aclose(self):
            pass

    llm_cfg = LLMConfig(provider="anthropic", model="claude-sonnet-4-6")
    result = await run_agentic_distillation_for_mr(
        sf, settings, FakeGitLab(), repo, mr, notes, llm_cfg)
    assert result.entries == []


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

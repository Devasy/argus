"""deps.verify_context (linter output + do-not-suggest learnings) must reach
ONLY the verify stage's system prompt -- scout, every analyze chunk, and every
reviewer agent must not see it. Both blocks are pure "do not report this"
suppression constraints that only bear on the decide-to-keep-or-drop moment,
so paying for them in every stage (up to 40 linter items plus every active
do_not_suggest learning, on every scout/analyze/agent call) was pure waste.
"""
import uuid

import pytest

from argus.review import stages
from argus.review.artifacts import CandidateFinding, FileChange, Hunk, Verdict
from argus.review.stages import FindingList, ScoutOutput, VerdictList

MARKER = "## Previously rejected suggestions — do NOT re-suggest these\n- [aaaaaaaa] x: y"


@pytest.fixture
def capturing_stage(monkeypatch):
    calls = []

    async def fake(model, tools, system_prompt, user_msg, response_model,
                   max_rounds, callbacks=None, **_kwargs):
        calls.append((response_model.__name__, system_prompt))
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix bug", mr_summary="fixes X",
                               file_summaries={"f1": "touches auth"},
                               assigned_agents=["design"])
        if response_model is FindingList:
            if "Stage: design" in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="design", type="issue",
                    severity="high", confidence=0.9, file_path="auth/x.py",
                    line=3, title="design finding", body="d",
                    evidence_quote="x = 1")])
            return FindingList(findings=[CandidateFinding(
                finding_id="", stage="analysis", chunk_id="c1", type="issue",
                severity="high", confidence=0.9, file_path="auth/x.py", line=3,
                title="bug", body="d", evidence_quote="x = 1")])
        if response_model is VerdictList:
            return VerdictList(verdicts=[Verdict(finding_id="a1", valid=True,
                                                 reason="confirmed")])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)
    return calls


async def test_verify_context_reaches_only_verify(db, engine, settings, tmp_path,
                                                   capturing_stage, monkeypatch):
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import (PipelineDeps, ResolvedAgent,
                                           run_review_pipeline)
    from argus.review.tools import ToolContext
    from argus.domain.models import (MergeRequest, ReviewerAgent,
                                         ReviewerAgentVersion, Repository,
                                         Review)
    import argus.review.pipeline as pl

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="g/verify-ctx",
                         gitlab_project_id=99992)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.flush()
        agent = ReviewerAgent(name=f"design-{uuid.uuid4().hex[:8]}")
        s.add(agent); await s.flush()
        agent_version = ReviewerAgentVersion(agent_id=agent.id, version=1,
                                             guidelines="review design")
        s.add(agent_version); await s.commit()
        review_id = review.id
        agent_version_id = agent_version.id

    fc = FileChange(file_id="f1", path="auth/x.py", change_kind="modified",
                    language="python", hunk_ids=["h1"])
    hunk = Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-a\n+b\n")
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "auth" / "x.py").write_text("a = 0\nb = 0\nx = 1\n")
    deps = PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={"f1": fc},
                             hunks={"h1": hunk}),
        profile_static="You are a reviewer.", mr_context="MR !1: t",
        verify_context=MARKER,
        reviewer_agent_versions={"design": ResolvedAgent(
            name="design", guidelines="review design", max_rounds=10,
            version_id=agent_version_id)})

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    await run_review_pipeline(review_id, deps, checkpointer=None)

    by_kind = {}
    for name, prompt in capturing_stage:
        by_kind.setdefault(name, []).append(prompt)

    assert not any(MARKER in p for p in by_kind["ScoutOutput"])
    assert not any(MARKER in p for p in by_kind["FindingList"]), (
        "neither analyze_chunk nor the design reviewer agent should see "
        "verify_context")
    assert all(MARKER in p for p in by_kind["VerdictList"])

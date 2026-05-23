"""Per-stage degradation when a stage's prompt cannot fit the context window.

analyze_chunk has always split-and-retried on ContextWindowExceededError, but
scout / verify / run_reviewer_agent had no recovery path at all: the exception
propagated and failed the entire review (see review 2bdf08a9, which died in
scout). Each stage degrades differently because each has a different lever:

  scout               -- drop the inlined full diff, keep tools
  verify              -- halve the candidate-finding listing, merge verdicts
  run_reviewer_agent  -- record the agent as failed, yield no findings
"""
import uuid

import pytest

from argus.review import stages
from argus.review.artifacts import (CandidateFinding, FileChange, Hunk,
                                        Verdict)
from argus.review.stages import (ContextBudgetExceeded, FindingList,
                                     ScoutOutput, VerdictList)


async def _mk_review(sf, slug: str, project_id: int):
    from argus.domain.models import (MergeRequest, Repository, Review,
                                         ReviewerAgent, ReviewerAgentVersion)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path=f"g/{slug}",
                          gitlab_project_id=project_id)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.flush()
        agent = ReviewerAgent(name=f"design-{uuid.uuid4().hex[:8]}")
        s.add(agent); await s.flush()
        av = ReviewerAgentVersion(agent_id=agent.id, version=1,
                                  guidelines="review design")
        s.add(av); await s.commit()
        return review.id, av.id


def _mk_deps(sf, settings, tmp_path, agent_version_id, *, n_files=1):
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps, ResolvedAgent
    from argus.review.tools import ToolContext

    files, hunks = {}, {}
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        fid, hid = f"f{i+1}", f"h{i+1}"
        path = f"auth/x{i}.py"
        (tmp_path / path).write_text("a = 0\nb = 0\nx = 1\n")
        files[fid] = FileChange(file_id=fid, path=path, change_kind="modified",
                                language="python", hunk_ids=[hid])
        hunks[hid] = Hunk(hunk_id=hid, file_id=fid, old_start=1, old_lines=1,
                          new_start=1, new_lines=1,
                          diff_text="@@ -1 +1 @@\n-a\n+b\n")
    return PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id=files, hunks=hunks),
        profile_static="You are a reviewer.", mr_context="MR !1: t",
        reviewer_agent_versions={"design": ResolvedAgent(
            name="design", guidelines="review design", max_rounds=10,
            version_id=agent_version_id)})


def _stub_publish(monkeypatch):
    import argus.review.pipeline as pl

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)


@pytest.mark.parametrize("case,exc_factory", [
    ("preflight", lambda: ContextBudgetExceeded("pre-flight: input too large")),
    ("provider", lambda: __import__("litellm").ContextWindowExceededError(
        "too big", model="openai/x", llm_provider="openai")),
])
async def test_scout_retries_without_inlined_diff(db, engine, settings, tmp_path,
                                                  monkeypatch, case, exc_factory):
    """scout inlines the full diff into its first user message when it fits the
    review_token_ceiling. If the request still overflows, the diff is the one
    droppable input -- the agent can fetch the same hunks via get_hunk. It must
    retry without it rather than failing the whole review."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline

    sf = session_factory(engine)
    review_id, av_id = await _mk_review(
        sf, f"scout-degrade-{case}", 99801 if case == "preflight" else 99811)
    deps = _mk_deps(sf, settings, tmp_path, av_id)
    _stub_publish(monkeypatch)

    seen_user_msgs = []

    async def fake(model, tools, system_prompt, user_msg, response_model,
                   max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            seen_user_msgs.append(user_msg)
            if "Full diff:" in user_msg:
                raise exc_factory()
            return ScoutOutput(intent="fix", mr_summary="s",
                               file_summaries={}, assigned_agents=[])
        if response_model is FindingList:
            return FindingList(findings=[])
        if response_model is VerdictList:
            return VerdictList(verdicts=[])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    assert len(seen_user_msgs) == 2, "scout should have been retried exactly once"
    assert "Full diff:" in seen_user_msgs[0]
    assert "Full diff:" not in seen_user_msgs[1], (
        "the retry must drop the inlined diff, which is the oversized input")
    assert state.plan is not None and state.plan.intent == "fix"
    assert state.compiled is not None, "review must still complete"


async def test_scout_records_stage_error_when_retry_also_overflows(
        db, engine, settings, tmp_path, monkeypatch):
    """If scout overflows even with no diff inlined, there is nothing left to
    drop -- the review genuinely cannot proceed (every later stage needs the
    plan), but the failure must be recorded against the scout stage so the UI
    shows where it died instead of leaving review_stages empty."""
    from argus.db import session_factory
    from argus.domain.models import ReviewStage
    from argus.review.pipeline import run_review_pipeline
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, av_id = await _mk_review(sf, "scout-fatal", 99802)
    deps = _mk_deps(sf, settings, tmp_path, av_id)
    _stub_publish(monkeypatch)

    async def always_overflow(model, tools, system_prompt, user_msg,
                             response_model, max_rounds, callbacks=None, **_kwargs):
        raise ContextBudgetExceeded("input too large even without the diff")

    monkeypatch.setattr(stages, "run_stage_agent", always_overflow)

    with pytest.raises(ContextBudgetExceeded):
        await run_review_pipeline(review_id, deps, checkpointer=None)

    async with sf() as s:
        rows = (await s.execute(select(ReviewStage).where(
            ReviewStage.review_id == review_id))).scalars().all()
    scout_rows = [r for r in rows if r.stage_name == "scout"]
    assert scout_rows, "scout failure must be recorded in review_stages"
    assert scout_rows[0].status == "failed"
    assert "too large" in (scout_rows[0].error or "").lower()


async def test_verify_splits_findings_listing_on_overflow(
        db, engine, settings, tmp_path, monkeypatch):
    """verify puts EVERY grounded finding into one prompt, so a review with
    many findings can overflow on the listing alone. It must halve the batch
    and merge the verdicts rather than losing the whole verify stage."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline

    sf = session_factory(engine)
    review_id, av_id = await _mk_review(sf, "verify-degrade", 99803)
    deps = _mk_deps(sf, settings, tmp_path, av_id, n_files=4)
    _stub_publish(monkeypatch)

    verify_batches = []

    async def fake(model, tools, system_prompt, user_msg, response_model,
                   max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix", mr_summary="s",
                               file_summaries={}, assigned_agents=[])
        if response_model is FindingList:
            # four findings, one per file, all grounded on the "x = 1" line
            import re
            fids = re.findall(r"^(f\d+) \|", user_msg, re.M)
            return FindingList(findings=[CandidateFinding(
                finding_id="", stage="analysis", type="issue", severity="high",
                confidence=0.9, file_path=deps.tool_ctx.files_by_id[fid].path,
                line=3, title=f"bug in {fid}", body="d",
                evidence_quote="x = 1") for fid in fids])
        if response_model is VerdictList:
            ids = [ln.split("finding_id=")[1].split()[0]
                   for ln in user_msg.splitlines() if "finding_id=" in ln]
            verify_batches.append(ids)
            # Overflow on any batch bigger than 2 findings.
            if len(ids) > 2:
                raise ContextBudgetExceeded(f"listing of {len(ids)} too large")
            return VerdictList(verdicts=[
                Verdict(finding_id=i, valid=True, reason="confirmed")
                for i in ids])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    assert len(verify_batches[0]) > 2, "first attempt sends the whole listing"
    assert any(len(b) <= 2 for b in verify_batches[1:]), "must retry smaller"
    # every finding still got a verdict despite the initial overflow
    assert len(state.verdicts) == len(verify_batches[0])
    assert {v.finding_id for v in state.verdicts} == set(verify_batches[0])


async def test_reviewer_agent_overflow_does_not_fail_the_review(
        db, engine, settings, tmp_path, monkeypatch):
    """A specialist reviewer agent sees a whole-MR view it cannot split by
    design. Like a twice-failed analyze chunk, it must be recorded as failed
    and contribute no findings, while the rest of the review completes."""
    from argus.db import session_factory
    from argus.domain.models import ReviewStage
    from argus.review.pipeline import run_review_pipeline
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, av_id = await _mk_review(sf, "agent-degrade", 99804)
    deps = _mk_deps(sf, settings, tmp_path, av_id)
    _stub_publish(monkeypatch)

    async def fake(model, tools, system_prompt, user_msg, response_model,
                   max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix", mr_summary="s",
                               file_summaries={}, assigned_agents=["design"])
        if response_model is FindingList:
            if "Stage: design" in system_prompt:
                raise ContextBudgetExceeded("whole-MR view too large")
            return FindingList(findings=[CandidateFinding(
                finding_id="", stage="analysis", chunk_id="c1", type="issue",
                severity="high", confidence=0.9, file_path="auth/x0.py",
                line=3, title="bug", body="d", evidence_quote="x = 1")])
        if response_model is VerdictList:
            ids = [ln.split("finding_id=")[1].split()[0]
                   for ln in user_msg.splitlines() if "finding_id=" in ln]
            return VerdictList(verdicts=[
                Verdict(finding_id=i, valid=True, reason="ok") for i in ids])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    assert state.compiled is not None, "review must complete despite the agent"
    assert [f.stage for f in state.findings] == ["analysis"], (
        "the overflowing agent contributes nothing, analysis findings survive")

    async with sf() as s:
        rows = (await s.execute(select(ReviewStage).where(
            ReviewStage.review_id == review_id))).scalars().all()
    design = [r for r in rows if r.stage_name == "design"]
    assert design and design[0].status == "failed"
    assert "too large" in (design[0].error or "").lower()


def _recursion_error():
    from langgraph.errors import GraphRecursionError
    return GraphRecursionError(
        "Recursion limit of 24 reached without hitting a stop condition.")


async def test_reviewer_agent_recursion_limit_does_not_fail_the_review(
        db, engine, settings, tmp_path, monkeypatch):
    """Regression for review b9df9951: platform-reviewer got stuck in a
    tool-call loop and hit LangGraph's recursion limit (a GraphRecursionError,
    NOT a context-overflow error) with 13 rounds and 258k prompt tokens spent,
    and the review failed outright with zero rows recorded for that stage --
    "graceful degradation" only covered context overflow, not a stuck loop."""
    from argus.db import session_factory
    from argus.domain.models import ReviewStage
    from argus.review.pipeline import run_review_pipeline
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, av_id = await _mk_review(sf, "agent-recursion", 99805)
    deps = _mk_deps(sf, settings, tmp_path, av_id)
    _stub_publish(monkeypatch)

    async def fake(model, tools, system_prompt, user_msg, response_model,
                   max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix", mr_summary="s",
                               file_summaries={}, assigned_agents=["design"])
        if response_model is FindingList:
            if "Stage: design" in system_prompt:
                raise _recursion_error()
            return FindingList(findings=[CandidateFinding(
                finding_id="", stage="analysis", chunk_id="c1", type="issue",
                severity="high", confidence=0.9, file_path="auth/x0.py",
                line=3, title="bug", body="d", evidence_quote="x = 1")])
        if response_model is VerdictList:
            ids = [ln.split("finding_id=")[1].split()[0]
                   for ln in user_msg.splitlines() if "finding_id=" in ln]
            return VerdictList(verdicts=[
                Verdict(finding_id=i, valid=True, reason="ok") for i in ids])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    assert state.compiled is not None, "review must complete despite the agent"
    assert [f.stage for f in state.findings] == ["analysis"]

    async with sf() as s:
        rows = (await s.execute(select(ReviewStage).where(
            ReviewStage.review_id == review_id))).scalars().all()
    design = [r for r in rows if r.stage_name == "design"]
    assert design and design[0].status == "failed", (
        "the recursion-limit failure must be recorded, not left with zero "
        "rows the way review b9df9951 was")
    assert "recursion" in (design[0].error or "").lower() or \
           "GraphRecursionError" in (design[0].error or "")


async def test_scout_recursion_limit_fails_the_review_with_a_recorded_stage(
        db, engine, settings, tmp_path, monkeypatch):
    """scout has no split-retry lever for a stuck tool-call loop the way it
    does for an oversized diff -- there is nothing to drop. The review still
    cannot proceed without a plan, but the failure must land in review_stages
    instead of leaving it empty (which is what actually happened for
    b9df9951: scout succeeded, but nothing downstream survived being
    unprotected against this exact exception type)."""
    from argus.db import session_factory
    from argus.domain.models import ReviewStage
    from argus.review.pipeline import run_review_pipeline
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, av_id = await _mk_review(sf, "scout-recursion", 99806)
    deps = _mk_deps(sf, settings, tmp_path, av_id)
    _stub_publish(monkeypatch)

    async def always_stuck(model, tools, system_prompt, user_msg,
                           response_model, max_rounds, callbacks=None, **_kwargs):
        raise _recursion_error()

    monkeypatch.setattr(stages, "run_stage_agent", always_stuck)

    from langgraph.errors import GraphRecursionError
    with pytest.raises(GraphRecursionError):
        await run_review_pipeline(review_id, deps, checkpointer=None)

    async with sf() as s:
        rows = (await s.execute(select(ReviewStage).where(
            ReviewStage.review_id == review_id))).scalars().all()
    scout_rows = [r for r in rows if r.stage_name == "scout"]
    assert scout_rows, "scout's recursion failure must be recorded"
    assert scout_rows[0].status == "failed"


async def test_verify_recursion_limit_drops_batch_without_infinite_split(
        db, engine, settings, tmp_path, monkeypatch):
    """A recursion-limit error in verify must NOT trigger the context-overflow
    split-in-half retry loop (halving a batch does nothing to fix a stuck
    tool-call loop, and could recurse pointlessly) -- it should drop straight
    to no verdicts for that batch, same trade-off as an unverifiable single
    finding."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline

    sf = session_factory(engine)
    review_id, av_id = await _mk_review(sf, "verify-recursion", 99807)
    deps = _mk_deps(sf, settings, tmp_path, av_id, n_files=4)
    _stub_publish(monkeypatch)

    verify_attempts = []

    async def fake(model, tools, system_prompt, user_msg, response_model,
                   max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix", mr_summary="s",
                               file_summaries={}, assigned_agents=[])
        if response_model is FindingList:
            import re
            fids = re.findall(r"^(f\d+) \|", user_msg, re.M)
            return FindingList(findings=[CandidateFinding(
                finding_id="", stage="analysis", type="issue", severity="high",
                confidence=0.9, file_path=deps.tool_ctx.files_by_id[fid].path,
                line=3, title=f"bug in {fid}", body="d",
                evidence_quote="x = 1") for fid in fids])
        if response_model is VerdictList:
            verify_attempts.append(1)
            raise _recursion_error()
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    assert state.compiled is not None
    assert state.verdicts == [], "the stuck batch gets no verdicts"
    assert len(verify_attempts) == 1, (
        "must not recurse into halves the way context-overflow retry does -- "
        "a stuck loop is not fixed by a smaller batch")

import uuid

import pytest

from argus.review import stages
from argus.review.artifacts import (CandidateFinding, FileChange, Hunk,
                                        TestScenario, Verdict)
from argus.review.stages import (FindingList, ScoutOutput,
                                     TestScenarioList, VerdictList)


def test_design_static_covers_expanded_topics_and_raised_cap():
    from argus.review.stages import DESIGN_STATIC
    for topic in ("data-flow", "state-management", "backward-compat",
                 "migration safety", "cross-service", "error-handling"):
        assert topic in DESIGN_STATIC, f"missing topic: {topic}"
    assert "Max 6 findings" in DESIGN_STATIC


def test_scout_static_mentions_graphify_and_skill_tools():
    from argus.review.stages import SCOUT_STATIC
    assert "graph_diff_impact" in SCOUT_STATIC
    assert "graph_impact" in SCOUT_STATIC
    assert "list_module_skills" in SCOUT_STATIC
    assert "read_module_skill" in SCOUT_STATIC


@pytest.fixture
def fake_stage(monkeypatch):
    calls = []

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds,
                   callbacks=None, **_kwargs):
        calls.append((response_model.__name__, system_prompt))
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix bug", mr_summary="fixes X",
                               file_summaries={"f1": "touches auth"},
                               assigned_agents=["design"])
        if response_model is FindingList:
            if "Stage: design" in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="design", type="issue",
                    severity="high", confidence=0.9, file_path="auth/x.py", line=3,
                    title="design finding", body="details", evidence_quote="x = 1")])
            return FindingList(findings=[CandidateFinding(
                finding_id="", stage="analysis", chunk_id="c1", type="issue",
                severity="high", confidence=0.9, file_path="auth/x.py", line=3,
                title="bug", body="details", evidence_quote="x = 1")])
        if response_model is VerdictList:
            return VerdictList(verdicts=[Verdict(finding_id="a1", valid=True,
                                                 reason="confirmed")])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)
    return calls


async def test_pipeline_happy_path(db, engine, settings, tmp_path, fake_stage,
                                   monkeypatch):
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps, ResolvedAgent, run_review_pipeline
    from argus.review.tools import ToolContext
    from argus.domain.models import (MergeRequest, ReviewerAgent,
                                         ReviewerAgentVersion, Repository, Review,
                                         ReviewStage)
    from sqlalchemy import select

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="g/pipeline-test",
                         gitlab_project_id=99991)
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
        reviewer_agent_versions={"design": ResolvedAgent(
            name="design", guidelines="review design", max_rounds=10,
            version_id=agent_version_id)})

    # publisher stub: mark compile stage reached without hitting GitLab
    import argus.review.pipeline as pl

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    state = await run_review_pipeline(review_id, deps, checkpointer=None)
    assert state.plan and state.plan.files[0].summary == "touches auth"
    assert state.findings and state.findings[0].finding_id  # id assigned
    assert state.compiled and state.compiled.summary_markdown == "ok"

    async with sf() as s:
        names = {r.stage_name for r in (await s.execute(
            select(ReviewStage).where(ReviewStage.review_id == review_id)
        )).scalars().all()}
    assert {"scout", "analyze", "verify", "publish"} <= names


# --- grounding flags verify instead of gating it (2026-09-09) ---------------
# Grounding used to exclude a finding whose evidence_quote failed the
# mechanical substring check before verify ever ran -- a real hallucination
# and a genuine-but-decorated/relocated quote both got silently dropped with
# no verdict at all. Now every finding reaches verify; a failed mechanical
# check becomes a FLAG note in its listing entry, and verify -- which has
# get_file_lines/search_code and the actual file -- makes the real call.

async def test_verify_flags_rather_than_drops_a_mechanically_ungrounded_finding(
        db, engine, settings, tmp_path, monkeypatch):
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps, ResolvedAgent, run_review_pipeline
    from argus.review.tools import ToolContext
    from argus.domain.models import (MergeRequest, ReviewerAgent,
                                         ReviewerAgentVersion, Repository, Review)

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="g/flag-test",
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

    verify_listing = {}

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds,
                   callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix bug", mr_summary="fixes X",
                               file_summaries={"f1": "touches auth"},
                               assigned_agents=["design"])
        if response_model is FindingList:
            if "Stage: design" in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="design", type="issue",
                    severity="high", confidence=0.9, file_path="auth/x.py", line=3,
                    title="design finding", body="details",
                    # decorated/mislocated citation -- fails the mechanical
                    # substring check on purpose, on purpose it is NOT dropped.
                    evidence_quote="3: this line does not exist")])
            return FindingList(findings=[])
        if response_model is VerdictList:
            verify_listing["text"] = user_msg
            return VerdictList(verdicts=[Verdict(finding_id="design1", valid=True,
                                                 reason="confirmed via tools")])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    deps = PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={"f1": fc},
                             hunks={"h1": hunk}),
        profile_static="You are a reviewer.", mr_context="MR !1: t",
        reviewer_agent_versions={"design": ResolvedAgent(
            name="design", guidelines="review design", max_rounds=10,
            version_id=agent_version_id)})

    import argus.review.pipeline as pl

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    # It still reached verify, flagged for the check verify must now do itself.
    assert "design1" in state.ungrounded_ids
    assert "FLAG: mechanical check did not find this evidence_quote" in verify_listing["text"]
    # And verify's own confirmation is what decides -- not the mechanical flag.
    assert state.verdicts == [Verdict(finding_id="design1", valid=True,
                                      reason="confirmed via tools")]


@pytest.fixture
def fake_stage_multi_branch(monkeypatch):
    """Like fake_stage, but returns a distinctly-titled FindingList per
    analyze_chunk/run_reviewer_agent call (keyed off the chunk id / stage
    embedded in the system_prompt), so concurrent-branch merging can be
    verified."""
    calls = []

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds,
                   callbacks=None, **_kwargs):
        calls.append((response_model.__name__, system_prompt))
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix bug", mr_summary="fixes X",
                               file_summaries={}, assigned_agents=["design"])
        if response_model is FindingList:
            if "Stage: design" in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="design", chunk_id=None,
                    type="suggestion", severity="medium", confidence=0.7,
                    file_path="billing/models_0.py", line=1,
                    title="design finding", body="cross-cutting concern",
                    evidence_quote="class Model:")])
            if "chunk c1." in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="analysis", chunk_id="c1",
                    type="issue", severity="high", confidence=0.9,
                    file_path="auth/f0.py", line=3,
                    title="c1 finding", body="details", evidence_quote="x = 1")])
            if "chunk c2." in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="analysis", chunk_id="c2",
                    type="issue", severity="low", confidence=0.6,
                    file_path="billing/f1.py", line=5,
                    title="c2 finding", body="details", evidence_quote="y = 2")])
            raise AssertionError(f"unexpected chunk prompt: {system_prompt}")
        if response_model is VerdictList:
            return VerdictList(verdicts=[
                Verdict(finding_id=fid, valid=True, reason="confirmed")
                for fid in ("c1-a1", "c2-a1", "d1")])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)
    return calls


async def test_pipeline_merges_findings_from_multiple_concurrent_branches(
        db, engine, settings, tmp_path, fake_stage_multi_branch, monkeypatch):
    """Real MRs commonly span multiple directories and/or trigger extra
    reviewer agents, producing >=2 concurrent Send()s from fan_out.
    ReviewState.findings relies on the operator.add reducer to merge findings
    returned by each concurrent branch (analyze_chunk x N + run_reviewer_agent)
    rather than one branch's write clobbering another's. This test constructs
    a scenario with 2 analyze_chunk branches + 1 run_reviewer_agent("design")
    branch and asserts all 3 findings survive into the final merged
    state.findings."""
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps, ResolvedAgent, run_review_pipeline
    from argus.review.tools import ToolContext
    from argus.domain.models import (MergeRequest, ReviewerAgent,
                                         ReviewerAgentVersion, Repository, Review,
                                         ReviewStage)
    from sqlalchemy import select

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="g/pipeline-multi-test",
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

    # 5 files under auth/ and 5 files under billing/. Total files (10)
    # exceeds max_chunk_files (8) so plan_chunks' single-chunk merge-back
    # doesn't collapse the two directory groups back into one chunk: we get
    # exactly 2 chunks (c1=auth/*, c2=billing/*). The design branch is forced
    # explicitly via fake_stage_multi_branch's ScoutOutput.assigned_agents=
    # ["design"] (rather than any file-count/risk-flag heuristic) -> fan_out
    # emits 3 concurrent Sends.
    files_by_id = {}
    hunks = {}
    for i in range(5):
        fid = f"a{i}"
        files_by_id[fid] = FileChange(file_id=fid, path=f"auth/f{i}.py",
                                      change_kind="modified", language="python",
                                      hunk_ids=[f"h{fid}"])
        hunks[f"h{fid}"] = Hunk(hunk_id=f"h{fid}", file_id=fid, old_start=1,
                                old_lines=1, new_start=1, new_lines=1,
                                diff_text="@@ -1 +1 @@\n-a\n+b\n")
    for i in range(5):
        fid = f"b{i}"
        path = "billing/models_0.py" if i == 0 else f"billing/f{i}.py"
        files_by_id[fid] = FileChange(file_id=fid, path=path,
                                      change_kind="modified", language="python",
                                      hunk_ids=[f"h{fid}"])
        hunks[f"h{fid}"] = Hunk(hunk_id=f"h{fid}", file_id=fid, old_start=1,
                                old_lines=1, new_start=1, new_lines=1,
                                diff_text="@@ -1 +1 @@\n-a\n+b\n")

    deps = PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id=files_by_id,
                             hunks=hunks),
        profile_static="You are a reviewer.", mr_context="MR !1: t",
        reviewer_agent_versions={"design": ResolvedAgent(
            name="design", guidelines="review design", max_rounds=10,
            version_id=agent_version_id)})

    import argus.review.pipeline as pl

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    # Sanity: the scenario really did produce >=2 chunks + a design pass.
    assert state.plan is not None
    assert len(state.plan.chunks) == 2
    assert state.plan.assigned_agents == ["design"]

    # The reducer must have merged findings from all 3 concurrent branches,
    # not just kept the last (or first) writer's output.
    assert len(state.findings) == 3
    by_title = {f.title: f for f in state.findings}
    assert set(by_title) == {"c1 finding", "c2 finding", "design finding"}

    c1f = by_title["c1 finding"]
    assert c1f.stage == "analysis" and c1f.chunk_id == "c1"

    c2f = by_title["c2 finding"]
    assert c2f.stage == "analysis" and c2f.chunk_id == "c2"

    df = by_title["design finding"]
    assert df.stage == "design" and df.chunk_id is None

    assert state.compiled and state.compiled.summary_markdown == "ok"

    async with sf() as s:
        names = {r.stage_name for r in (await s.execute(
            select(ReviewStage).where(ReviewStage.review_id == review_id)
        )).scalars().all()}
    assert {"scout", "analyze", "design", "verify", "publish"} <= names


async def _make_deps(sf, settings, tmp_path):
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps
    from argus.review.tools import ToolContext

    fc = FileChange(file_id="f1", path="auth/x.py", change_kind="modified",
                    language="python", hunk_ids=["h1"])
    hunk = Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-a\n+b\n")
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "auth" / "x.py").write_text("a = 0\nb = 0\nx = 1\n")
    return PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={"f1": fc},
                             hunks={"h1": hunk}),
        profile_static="You are a reviewer.", mr_context="MR !1: t")


async def _make_review(sf):
    from argus.domain.models import MergeRequest, Repository, Review

    async with sf() as s:
        repo = Repository(provider="gitlab",
                         project_path=f"g/pipeline-test-{uuid.uuid4()}",
                         gitlab_project_id=90000000 + uuid.uuid4().int % 9000000)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.commit()
        return review.id


@pytest.fixture
async def pg_checkpointer(settings):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    pg_url = settings.database_url.replace("+asyncpg", "")
    async with AsyncPostgresSaver.from_conn_string(pg_url) as checkpointer:
        await checkpointer.setup()
        yield checkpointer


async def test_pipeline_resumes_from_checkpoint_on_retry(
        db, engine, settings, tmp_path, fake_stage, monkeypatch, pg_checkpointer):
    """A second run for the same review_id with a real checkpointer should
    resume from the completed checkpoint rather than re-running every stage
    agent from scratch."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline
    import argus.review.pipeline as pl

    sf = session_factory(engine)
    review_id = await _make_review(sf)
    deps = await _make_deps(sf, settings, tmp_path)

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    state1 = await run_review_pipeline(review_id, deps, checkpointer=pg_checkpointer)
    assert state1.compiled and state1.compiled.summary_markdown == "ok"
    calls_after_first_run = len(fake_stage)
    assert calls_after_first_run > 0

    # Simulate a retry for the same review_id/thread_id: the graph is already
    # fully complete, so a resumed invocation should not re-run any stage
    # agents (scout/analyze/design/verify) again.
    state2 = await run_review_pipeline(review_id, deps, checkpointer=pg_checkpointer)

    assert len(fake_stage) == calls_after_first_run, (
        "resumed run should not re-invoke stage agents for a completed thread")
    assert state2.plan and state2.plan.files[0].summary == "touches auth"
    assert state2.findings and state2.findings[0].finding_id
    assert state2.compiled and state2.compiled.summary_markdown == "ok"


async def test_pipeline_resumes_from_checkpoint_after_partial_failure(
        db, engine, settings, tmp_path, monkeypatch, pg_checkpointer):
    """A job that dies partway through (after scout/analyze succeed but
    before verify completes) must, on retry with the same review_id and
    checkpointer, resume from the checkpointed state rather than restarting
    -- and the recovered state must carry the real typed data produced by
    the first run's scout/analyze stages, not degraded/empty data."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline
    import argus.review.pipeline as pl

    sf = session_factory(engine)
    review_id = await _make_review(sf)
    deps = await _make_deps(sf, settings, tmp_path)

    calls = []

    async def failing_verify_stage(model, tools, system_prompt, user_msg,
                                   response_model, max_rounds, callbacks=None, **_kwargs):
        calls.append(response_model.__name__)
        if response_model is ScoutOutput:
            return ScoutOutput(intent="fix bug", mr_summary="fixes X",
                               file_summaries={"f1": "touches auth"})
        if response_model is FindingList:
            return FindingList(findings=[CandidateFinding(
                finding_id="", stage="analysis", chunk_id="c1", type="issue",
                severity="high", confidence=0.9, file_path="auth/x.py", line=3,
                title="bug", body="details", evidence_quote="x = 1")])
        if response_model is VerdictList:
            raise RuntimeError("simulated crash during verify stage")
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", failing_verify_stage)

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    # First run: scout and analyze_chunk succeed, verify raises. The job
    # "dies partway through" -- LangGraph should have already checkpointed
    # the completed scout/analyze/gather node outputs.
    with pytest.raises(RuntimeError, match="simulated crash during verify stage"):
        await run_review_pipeline(review_id, deps, checkpointer=pg_checkpointer)

    calls_after_first_run = list(calls)
    assert "ScoutOutput" in calls_after_first_run
    assert "FindingList" in calls_after_first_run
    assert "VerdictList" in calls_after_first_run  # the one that raised

    # Now swap in a stage function where verify succeeds. scout/analyze
    # should NOT need to run again if resume-from-checkpoint truly works.
    async def succeeding_verify_stage(model, tools, system_prompt, user_msg,
                                      response_model, max_rounds, callbacks=None, **_kwargs):
        calls.append(response_model.__name__)
        if response_model is VerdictList:
            return VerdictList(verdicts=[Verdict(finding_id="c1-a1", valid=True,
                                                 reason="confirmed")])
        raise AssertionError(
            f"{response_model.__name__} should not be re-invoked on resume")

    monkeypatch.setattr(stages, "run_stage_agent", succeeding_verify_stage)

    # Retry with the SAME review_id and SAME checkpointer instance.
    state2 = await run_review_pipeline(review_id, deps, checkpointer=pg_checkpointer)

    # scout/analyze must not have been re-invoked on the second call.
    calls_after_second_run = calls[len(calls_after_first_run):]
    assert calls_after_second_run == ["VerdictList"], (
        "resumed run after partial failure should only re-run the stage "
        "that hadn't completed (verify), not scout/analyze_chunk again")

    # The recovered state must carry the REAL typed data from the first
    # run's scout/analyze output -- not empty/None/degraded.
    assert state2.plan is not None
    assert state2.plan.intent == "fix bug"
    assert state2.plan.mr_summary == "fixes X"
    assert state2.findings and len(state2.findings) == 1
    finding = state2.findings[0]
    assert finding.title == "bug"
    assert finding.severity == "high"
    assert finding.file_path == "auth/x.py"

    # verify/publish only complete on this second call.
    assert state2.compiled is not None
    assert state2.compiled.summary_markdown == "ok"


async def test_pipeline_first_run_with_real_checkpointer_starts_fresh(
        db, engine, settings, tmp_path, fake_stage, monkeypatch, pg_checkpointer):
    """A brand-new review_id with a real (but empty-for-this-thread)
    checkpointer should still behave like a normal fresh run."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline
    import argus.review.pipeline as pl

    sf = session_factory(engine)
    review_id = await _make_review(sf)
    deps = await _make_deps(sf, settings, tmp_path)

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    state = await run_review_pipeline(review_id, deps, checkpointer=pg_checkpointer)
    assert state.plan and state.plan.files[0].summary == "touches auth"
    assert state.findings and state.findings[0].finding_id
    assert state.compiled and state.compiled.summary_markdown == "ok"
    assert len(fake_stage) > 0


async def test_scout_inlines_full_diff_when_under_ceiling(
        db, engine, settings, tmp_path, monkeypatch):
    """When the MR's total diff size is comfortably under
    settings.review_token_ceiling, scout's initial user message should
    contain the full diff text, not just the summary table."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline
    import argus.review.pipeline as pl

    sf = session_factory(engine)
    review_id = await _make_review(sf)
    deps = await _make_deps(sf, settings, tmp_path)

    captured = {}

    async def capturing_stage(model, tools, system_prompt, user_msg,
                              response_model, max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            captured["scout_user_msg"] = user_msg
            return ScoutOutput(intent="fix bug", mr_summary="fixes X",
                               file_summaries={"f1": "touches auth"})
        if response_model is FindingList:
            return FindingList(findings=[])
        if response_model is VerdictList:
            return VerdictList(verdicts=[])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", capturing_stage)

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    await run_review_pipeline(review_id, deps, checkpointer=None)

    assert "@@ -1 +1 @@" in captured["scout_user_msg"]
    assert "### auth/x.py [h1]" in captured["scout_user_msg"]


async def test_scout_omits_full_diff_when_over_ceiling(
        db, engine, tmp_path, monkeypatch):
    """When the MR's total diff size exceeds review_token_ceiling, scout's
    initial user message should NOT contain inlined diff text -- it must
    rely on get_hunk/get_file_lines instead."""
    from argus.config import Settings
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline
    import argus.review.pipeline as pl

    tiny_ceiling_settings = Settings(
        database_url="postgresql+asyncpg://argus:argus@localhost:5433/argus_test",
        gitlab_url="https://gitlab.test", gitlab_token="t",
        review_token_ceiling=1)

    sf = session_factory(engine)
    review_id = await _make_review(sf)
    deps = await _make_deps(sf, tiny_ceiling_settings, tmp_path)

    captured = {}

    async def capturing_stage(model, tools, system_prompt, user_msg,
                              response_model, max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            captured["scout_user_msg"] = user_msg
            return ScoutOutput(intent="fix bug", mr_summary="fixes X",
                               file_summaries={"f1": "touches auth"})
        if response_model is FindingList:
            return FindingList(findings=[])
        if response_model is VerdictList:
            return VerdictList(verdicts=[])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", capturing_stage)

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    await run_review_pipeline(review_id, deps, checkpointer=None)

    assert "@@ -1 +1 @@" not in captured["scout_user_msg"]


async def test_stage_tool_composition_matches_spec_table(
        db, engine, settings, tmp_path, monkeypatch):
    """scout and design_pass must receive graphify_tools + skill_tools on
    top of the base read tools; analyze_chunk must receive skill_tools but
    NOT graphify_tools; verify must receive graphify_tools (it is the stage
    that checks cross-file-impact claims, so it needs graph_diff_impact --
    see agent_complaints rows where verify reported the tool missing) but
    not skill_tools."""
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps, ResolvedAgent, run_review_pipeline
    from argus.review.tools import ToolContext
    from argus.domain.models import (MergeRequest, ReviewerAgent,
                                         ReviewerAgentVersion, Repository, Review)
    import argus.review.pipeline as pl

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="g/stage-tool-comp-test",
                         gitlab_project_id=99993)
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

    # Same 10-file auth/billing layout as
    # test_pipeline_merges_findings_from_multiple_concurrent_branches.
    # capturing_stage's ScoutOutput below assigns assigned_agents=["design"]
    # explicitly, which is what triggers the run_reviewer_agent("design")
    # branch now.
    files_by_id = {}
    hunks = {}
    for i in range(5):
        fid = f"a{i}"
        files_by_id[fid] = FileChange(file_id=fid, path=f"auth/f{i}.py",
                                      change_kind="modified", language="python",
                                      hunk_ids=[f"h{fid}"])
        hunks[f"h{fid}"] = Hunk(hunk_id=f"h{fid}", file_id=fid, old_start=1,
                                old_lines=1, new_start=1, new_lines=1,
                                diff_text="@@ -1 +1 @@\n-a\n+b\n")
    for i in range(5):
        fid = f"b{i}"
        path = "billing/models_0.py" if i == 0 else f"billing/f{i}.py"
        files_by_id[fid] = FileChange(file_id=fid, path=path,
                                      change_kind="modified", language="python",
                                      hunk_ids=[f"h{fid}"])
        hunks[f"h{fid}"] = Hunk(hunk_id=f"h{fid}", file_id=fid, old_start=1,
                                old_lines=1, new_start=1, new_lines=1,
                                diff_text="@@ -1 +1 @@\n-a\n+b\n")

    # verify() only calls run_stage_agent when at least one finding grounds
    # successfully (matches real file content on disk) -- write one so the
    # verify stage actually executes and its tools can be captured.
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "auth" / "f0.py").write_text("a = 0\nb = 0\nx = 1\n")

    from langchain_core.tools import tool as lc_tool

    @lc_tool
    async def graph_impact() -> str:
        """fake graphify tool"""
        return "ok"

    @lc_tool
    async def list_module_skills() -> str:
        """fake skill tool"""
        return "ok"

    deps = PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id=files_by_id,
                             hunks=hunks),
        profile_static="You are a reviewer.", mr_context="MR !1: t",
        graphify_tools=[graph_impact], skill_tools=[list_module_skills],
        reviewer_agent_versions={"design": ResolvedAgent(
            name="design", guidelines="review design", max_rounds=10,
            version_id=agent_version_id)})

    captured = {}

    async def capturing_stage(model, tools, system_prompt, user_msg,
                              response_model, max_rounds, callbacks=None, **_kwargs):
        names = {t.name for t in tools}
        if response_model is ScoutOutput:
            captured["scout"] = names
            return ScoutOutput(intent="i", mr_summary="s", file_summaries={},
                              assigned_agents=["design"])
        if response_model is FindingList:
            if "Stage: design" in system_prompt:
                captured["design"] = names
                return FindingList(findings=[])
            captured["analyze"] = names
            if "chunk c1." in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="analysis", chunk_id="c1",
                    type="issue", severity="high", confidence=0.9,
                    file_path="auth/f0.py", line=3,
                    title="c1 finding", body="details", evidence_quote="x = 1")])
            return FindingList(findings=[])
        if response_model is VerdictList:
            captured["verify"] = names
            return VerdictList(verdicts=[])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", capturing_stage)

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    await run_review_pipeline(review_id, deps, checkpointer=None)

    assert "graph_impact" in captured["scout"]
    assert "list_module_skills" in captured["scout"]
    assert "list_module_skills" in captured["analyze"]
    assert "graph_impact" not in captured["analyze"]
    assert "graph_impact" in captured["design"]
    assert "list_module_skills" in captured["design"]
    assert "graph_impact" in captured["verify"]
    assert "list_module_skills" not in captured["verify"]


def test_review_plan_assigned_agents_field():
    from argus.review.artifacts import ReviewPlan
    plan = ReviewPlan(intent="x", mr_summary="y", files=[],
                      assigned_agents=["design", "security-reviewer"])
    assert plan.assigned_agents == ["design", "security-reviewer"]
    assert not hasattr(plan, "run_design_pass")


def test_candidate_finding_accepts_dynamic_stage_name():
    from argus.review.artifacts import CandidateFinding
    f = CandidateFinding(finding_id="1", stage="security-reviewer", type="issue",
                         severity="high", confidence=0.9, file_path="a.py", line=1,
                         title="t", body="b", evidence_quote="q")
    assert f.stage == "security-reviewer"


async def test_pipeline_fans_out_to_multiple_named_reviewer_agents(
        db, engine, settings, tmp_path, monkeypatch):
    """Two independently-registered reviewer agents, both assigned by Scout,
    must both run with their OWN guidelines and OWN max_rounds — proving the
    fan-out is genuinely dynamic (N agents from the registry), not just the
    single seeded 'design' agent exercised by the other tests in this file."""
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps, ResolvedAgent, run_review_pipeline
    from argus.review.tools import ToolContext
    from argus.domain.models import (MergeRequest, ReviewerAgent,
                                         ReviewerAgentVersion, Repository, Review)

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="g/multi-agent-test",
                         gitlab_project_id=99994)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.flush()
        design_agent = ReviewerAgent(name=f"design-{uuid.uuid4().hex[:8]}")
        s.add(design_agent); await s.flush()
        design_version = ReviewerAgentVersion(agent_id=design_agent.id, version=1,
                                              guidelines="review design")
        s.add(design_version); await s.flush()
        sec_agent = ReviewerAgent(name=f"security-reviewer-{uuid.uuid4().hex[:8]}")
        s.add(sec_agent); await s.flush()
        sec_version = ReviewerAgentVersion(agent_id=sec_agent.id, version=1,
                                           guidelines="check for injection")
        s.add(sec_version); await s.commit()
        review_id = review.id
        design_version_id = design_version.id
        sec_version_id = sec_version.id

    fc = FileChange(file_id="f1", path="auth/x.py", change_kind="modified",
                    language="python", hunk_ids=["h1"])
    hunk = Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-a\n+b\n")
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "auth" / "x.py").write_text("a = 0\nb = 0\nx = 1\n")

    calls = []

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds,
                   callbacks=None, **_kwargs):
        calls.append((response_model.__name__, system_prompt, max_rounds))
        if response_model is ScoutOutput:
            return ScoutOutput(intent="x", mr_summary="y",
                               file_summaries={"f1": "touches auth"},
                               assigned_agents=["design", "security-reviewer"])
        if response_model is FindingList:
            if "Stage: security-reviewer" in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="placeholder", type="issue",
                    severity="high", confidence=0.9, file_path="auth/x.py", line=3,
                    title="sec finding", body="b", evidence_quote="x = 1")])
            if "Stage: design" in system_prompt:
                return FindingList(findings=[CandidateFinding(
                    finding_id="", stage="placeholder", type="issue",
                    severity="medium", confidence=0.7, file_path="auth/x.py", line=1,
                    title="design finding", body="b", evidence_quote="a = 0")])
            return FindingList(findings=[])
        if response_model is VerdictList:
            return VerdictList(verdicts=[])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    import argus.review.pipeline as pl

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[
            f.finding_id for f in state.findings], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    deps = PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={"f1": fc},
                             hunks={"h1": hunk}),
        profile_static="You are a reviewer.", mr_context="MR !1: t",
        reviewer_agent_versions={
            "design": ResolvedAgent(name="design", guidelines="review design",
                                    max_rounds=10, version_id=design_version_id),
            "security-reviewer": ResolvedAgent(
                name="security-reviewer", guidelines="check for injection",
                max_rounds=3, version_id=sec_version_id),
        })

    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    finding_stages = {f.stage for f in state.findings}
    assert finding_stages == {"security-reviewer", "design"}

    security_calls = [mr for name, p, mr in calls
                      if name == "FindingList" and "Stage: security-reviewer" in p]
    assert security_calls == [3], "security-reviewer's own max_rounds (3) must be used"
    design_calls = [mr for name, p, mr in calls
                   if name == "FindingList" and "Stage: design" in p]
    assert design_calls == [10], "design's own max_rounds (10) must be used"


# ---------------------------------------------------------------------------
# build_agent_tool: chunk-scoped specialist dispatch (agent-as-tool)
# ---------------------------------------------------------------------------


async def test_build_agent_tool_invokes_specialist_and_collects_findings(
        db, engine, settings, tmp_path, monkeypatch):
    """Calling the tool build_agent_tool returns must run the specialist's
    own run_stage_agent loop (own model/tools/max_rounds), append its raw
    findings to `collected` (independent of whatever the tool's string
    return value ends up being used for by the calling model), and record a
    ReviewStage row keyed by "<agent>:<chunk>" so the UI graph can show its
    outcome (see agentNodeStatus in pipelineGraph.ts)."""
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import ResolvedAgent, PipelineDeps, build_agent_tool
    from argus.review.tools import ToolContext
    from argus.domain.models import ReviewStage
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id = await _make_review(sf)
    deps = PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={}, hunks={}),
        profile_static="You are a reviewer.", mr_context="MR !1: t")

    agent = ResolvedAgent(name="security", guidelines="check for injection",
                          max_rounds=7, version_id=uuid.uuid4())

    captured = {}

    async def fake_run_stage_agent(model, tools, system_prompt, user_msg,
                                   response_model, max_rounds, callbacks=None,
                                   **_kwargs):
        captured["system_prompt"] = system_prompt
        captured["user_msg"] = user_msg
        captured["max_rounds"] = max_rounds
        return FindingList(findings=[CandidateFinding(
            finding_id="", stage="", type="issue", severity="high",
            confidence=0.9, file_path="auth/x.py", line=3,
            title="sql injection", body="details", evidence_quote="q = 1")])

    monkeypatch.setattr(stages, "run_stage_agent", fake_run_stage_agent)

    collected = []
    invoke_tool = build_agent_tool(deps, str(review_id), agent, "c1", [], collected)

    result_json = await invoke_tool.ainvoke(
        {"chunk_context": "chunk c1: auth/x.py touches raw SQL"})

    assert "check for injection" in captured["system_prompt"]
    assert captured["max_rounds"] == 7
    assert len(collected) == 1
    assert collected[0].title == "sql injection"
    assert "sql injection" in result_json

    async with sf() as s:
        stage = (await s.execute(select(ReviewStage).where(
            ReviewStage.review_id == review_id,
            ReviewStage.stage_name == "security:c1"))).scalar_one()
        assert stage.status == "done"
        assert stage.artifact["findings"][0]["title"] == "sql injection"


async def test_build_agent_tool_returns_empty_on_unrecoverable_error(
        db, engine, settings, tmp_path, monkeypatch):
    """A specialist that can't complete (context overflow, stuck tool loop)
    must not blow up analyze_chunk's own agent loop -- it returns an empty
    FindingList instead, the same 'lose this agent's findings, not the whole
    review' trade-off run_reviewer_agent already makes. Its ReviewStage row
    is still recorded, as failed, so the UI graph shows the real outcome
    instead of a stage that looks like it never ran."""
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import ResolvedAgent, PipelineDeps, build_agent_tool
    from argus.review.tools import ToolContext
    from argus.domain.models import ReviewStage
    from langgraph.errors import GraphRecursionError
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id = await _make_review(sf)
    deps = PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={}, hunks={}),
        profile_static="You are a reviewer.", mr_context="MR !1: t")

    agent = ResolvedAgent(name="security", guidelines="check for injection",
                          max_rounds=7, version_id=uuid.uuid4())

    async def failing_run_stage_agent(*args, **kwargs):
        raise GraphRecursionError("stuck in a tool-call loop")

    monkeypatch.setattr(stages, "run_stage_agent", failing_run_stage_agent)

    collected = []
    invoke_tool = build_agent_tool(deps, str(review_id), agent, "c1", [], collected)
    result_json = await invoke_tool.ainvoke({"chunk_context": "chunk c1"})

    assert collected == []
    assert FindingList.model_validate_json(result_json).findings == []

    async with sf() as s:
        stage = (await s.execute(select(ReviewStage).where(
            ReviewStage.review_id == review_id,
            ReviewStage.stage_name == "security:c1"))).scalar_one()
        assert stage.status == "failed"
        assert "agent could not complete" in stage.error


async def test_analyze_chunk_tools_include_one_per_reviewer_agent(
        db, engine, settings, tmp_path, monkeypatch):
    """analyze_chunk's tool list must include an invoke_{name}_agent tool for
    every entry in deps.reviewer_agent_versions, on top of base/analyze
    tools -- this is the chunk-scoped dispatch surface the model can call."""
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps, ResolvedAgent, run_review_pipeline
    from argus.review.tools import ToolContext
    from argus.domain.models import (MergeRequest, ReviewerAgent,
                                         ReviewerAgentVersion, Repository, Review)

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="g/agent-tool-comp-test",
                         gitlab_project_id=99995)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.flush()
        agent = ReviewerAgent(name=f"security-{uuid.uuid4().hex[:8]}")
        s.add(agent); await s.flush()
        agent_version = ReviewerAgentVersion(agent_id=agent.id, version=1,
                                             guidelines="check for injection")
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
        reviewer_agent_versions={"security": ResolvedAgent(
            name="security", guidelines="check for injection", max_rounds=10,
            version_id=agent_version_id)})

    captured = {}

    async def capturing_stage(model, tools, system_prompt, user_msg,
                              response_model, max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="i", mr_summary="s", file_summaries={})
        if response_model is FindingList:
            captured["analyze_tools"] = {t.name for t in tools}
            captured["analyze_prompt"] = system_prompt
            return FindingList(findings=[])
        if response_model is VerdictList:
            return VerdictList(verdicts=[])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", capturing_stage)

    import argus.review.pipeline as pl

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    await run_review_pipeline(review_id, deps, checkpointer=None)

    assert "invoke_security_agent" in captured["analyze_tools"]


async def test_analyze_chunk_prompt_hints_scout_flagged_agents_not_binding(
        db, engine, settings, tmp_path, monkeypatch):
    """Scout's assigned_agents shows up in analyze_chunk's prompt as an
    advisory hint (naming the invoke_*_agent tool), never as a forced call --
    unlike the deprecated run_reviewer_agent/fan_out path, which still fans
    out one Send per assigned agent regardless of what analyze_chunk does."""
    from argus.db import session_factory
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps, ResolvedAgent, run_review_pipeline
    from argus.review.tools import ToolContext
    from argus.domain.models import (MergeRequest, ReviewerAgent,
                                         ReviewerAgentVersion, Repository, Review)

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="g/agent-hint-test",
                         gitlab_project_id=99996)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.flush()
        agent = ReviewerAgent(name=f"security-{uuid.uuid4().hex[:8]}")
        s.add(agent); await s.flush()
        agent_version = ReviewerAgentVersion(agent_id=agent.id, version=1,
                                             guidelines="check for injection")
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
        reviewer_agent_versions={"security": ResolvedAgent(
            name="security", guidelines="check for injection", max_rounds=10,
            version_id=agent_version_id)})

    captured = {}

    async def capturing_stage(model, tools, system_prompt, user_msg,
                              response_model, max_rounds, callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="i", mr_summary="s", file_summaries={},
                              assigned_agents=["security"])
        if response_model is FindingList:
            if "Stage: security" in system_prompt:
                captured["security_ran"] = True
                return FindingList(findings=[])
            captured["analyze_prompt"] = system_prompt
            return FindingList(findings=[])
        if response_model is VerdictList:
            return VerdictList(verdicts=[])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", capturing_stage)

    import argus.review.pipeline as pl

    async def fake_publish(state, deps_):
        from argus.review.artifacts import CompiledReview
        return CompiledReview(published_finding_ids=[], summary_markdown="ok")
    monkeypatch.setattr(pl, "publish_compiled_review", fake_publish)

    await run_review_pipeline(review_id, deps, checkpointer=None)

    assert "invoke_security_agent" in captured["analyze_prompt"]
    assert "Scout flagged this MR as possibly needing" in captured["analyze_prompt"]
    # the deprecated whole-MR path still ran (fan_out still sends it) --
    # this test documents current behavior, not an endorsement to keep it.
    assert captured.get("security_ran") is True



# ---------------------------------------------------------------------------
# review_mode="qa_scenarios": dedicated per-chunk test-scenario checklist run
# ---------------------------------------------------------------------------


async def _make_qa_scenarios_deps(sf, settings, tmp_path, max_chunk_files=8):
    from argus.llm.config import LLMConfig
    from argus.review.pipeline import PipelineDeps
    from argus.review.tools import ToolContext

    fc1 = FileChange(file_id="f1", path="auth/x.py", change_kind="modified",
                     language="python", hunk_ids=["h1"])
    fc2 = FileChange(file_id="f2", path="billing/y.py", change_kind="modified",
                     language="python", hunk_ids=["h2"])
    hunk1 = Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-a\n+b\n")
    hunk2 = Hunk(hunk_id="h2", file_id="f2", old_start=1, old_lines=1,
                new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-c\n+d\n")
    (tmp_path / "auth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "auth" / "x.py").write_text("a = 0\nb = 0\nx = 1\n")
    (tmp_path / "billing").mkdir(parents=True, exist_ok=True)
    (tmp_path / "billing" / "y.py").write_text("c = 0\nd = 0\ny = 1\n")

    settings = settings.model_copy(update={"max_chunk_files": max_chunk_files})
    return PipelineDeps(
        sf=sf, settings=settings, review_mode="qa_scenarios",
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={"f1": fc1, "f2": fc2},
                             hunks={"h1": hunk1, "h2": hunk2}),
        profile_static="You are a reviewer.", mr_context="MR !1: t")


async def test_qa_scenarios_mode_runs_per_chunk_and_skips_analyze_verify(
        db, engine, settings, tmp_path, monkeypatch):
    """review_mode="qa_scenarios" must fan out qa_chunk per chunk (forcing
    two chunks via max_chunk_files=1), tag each scenario with its chunk_id,
    populate ReviewState.test_scenarios, and never call analyze_chunk/verify
    at all -- FindingList/VerdictList must not be requested in this mode."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline

    sf = session_factory(engine)
    review_id = await _make_review(sf)

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds,
                   callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="i", mr_summary="s",
                               file_summaries={"f1": "touches auth", "f2": "touches billing"})
        if response_model is TestScenarioList:
            if "auth" in user_msg:
                return TestScenarioList(scenarios=[TestScenario(
                    title="Login with valid credentials", area="happy_path",
                    steps=["Go to /login", "Submit"],
                    expected_result="Redirected to dashboard",
                    relevant_files=["auth/x.py"])])
            return TestScenarioList(scenarios=[TestScenario(
                title="Charge a valid card", area="happy_path",
                steps=["Go to /billing", "Submit payment"],
                expected_result="Payment succeeds",
                relevant_files=["billing/y.py"])])
        raise AssertionError(
            f"{response_model} must not be requested in qa_scenarios mode")

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    deps = await _make_qa_scenarios_deps(sf, settings, tmp_path, max_chunk_files=1)
    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    assert len(state.test_scenarios) == 2
    titles = {s.title for s in state.test_scenarios}
    assert titles == {"Login with valid credentials", "Charge a valid card"}
    for s in state.test_scenarios:
        assert s.chunk_id is not None
        assert s.scenario_id == f"{s.chunk_id}-qa1"
    # findings/verdicts must stay empty -- analyze_chunk/verify never ran
    assert state.findings == []
    assert state.verdicts == []

    from argus.domain.models import ReviewStage
    from sqlalchemy import select
    async with sf() as s:
        names = {r.stage_name for r in (await s.execute(
            select(ReviewStage).where(ReviewStage.review_id == review_id)
        )).scalars().all()}
    assert "verify" not in names
    assert "analyze" not in names
    assert any(n.startswith("qa_chunk:") for n in names)


async def test_qa_scenarios_mode_chunk_failure_is_resilient(
        db, engine, settings, tmp_path, monkeypatch):
    """A qa_chunk failing twice (context overflow / stuck loop) must record a
    failed ReviewStage for that chunk and contribute no scenarios, WITHOUT
    failing the whole run -- matching _analyze_chunk_resilient's contract."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline

    sf = session_factory(engine)
    review_id = await _make_review(sf)

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds,
                   callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="i", mr_summary="s",
                               file_summaries={"f1": "touches auth", "f2": "touches billing"})
        if response_model is TestScenarioList:
            if "auth" in user_msg:
                raise RuntimeError("stuck")
            return TestScenarioList(scenarios=[TestScenario(
                title="Charge a valid card", area="happy_path",
                steps=["Go to /billing"], expected_result="Payment succeeds",
                relevant_files=["billing/y.py"])])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    deps = await _make_qa_scenarios_deps(sf, settings, tmp_path, max_chunk_files=1)
    state = await run_review_pipeline(review_id, deps, checkpointer=None)

    # the failing chunk contributes nothing; the other chunk's scenario survives
    assert [s.title for s in state.test_scenarios] == ["Charge a valid card"]

    from argus.domain.models import ReviewStage
    from sqlalchemy import select
    async with sf() as s:
        stages_ = (await s.execute(select(ReviewStage).where(
            ReviewStage.review_id == review_id,
            ReviewStage.stage_name.like("qa_chunk:%")))).scalars().all()
    assert any(r.status == "failed" for r in stages_)
    assert any(r.status == "done" for r in stages_)


async def test_qa_scenarios_mode_publishes_checklist_only_comment(
        db, engine, settings, tmp_path, monkeypatch):
    """publish_review in qa_scenarios mode must post exactly one comment
    containing the checklist, with no finding-related DB writes."""
    from argus.db import session_factory
    from argus.review.pipeline import run_review_pipeline

    sf = session_factory(engine)
    review_id = await _make_review(sf)

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds,
                   callbacks=None, **_kwargs):
        if response_model is ScoutOutput:
            return ScoutOutput(intent="add login", mr_summary="adds OAuth login",
                               file_summaries={"f1": "touches auth", "f2": "touches billing"})
        if response_model is TestScenarioList:
            return TestScenarioList(scenarios=[TestScenario(
                title="Login with valid credentials", area="happy_path",
                steps=["Go to /login", "Submit"],
                expected_result="Redirected to dashboard",
                relevant_files=["auth/x.py"])] if "auth" in user_msg else [])
        raise AssertionError(response_model)

    monkeypatch.setattr(stages, "run_stage_agent", fake)

    class FakeGitLab:
        def __init__(self):
            self.calls = []

        async def create_discussion(self, project_id, mr_iid, body, position=None):
            self.calls.append(body)
            return {"notes": [{"id": 1}]}

    gitlab = FakeGitLab()
    deps = await _make_qa_scenarios_deps(sf, settings, tmp_path, max_chunk_files=1)
    deps = deps.model_copy(update={"gitlab": gitlab, "project_id": 1, "mr_iid": 1})

    await run_review_pipeline(review_id, deps, checkpointer=None)

    assert len(gitlab.calls) == 1
    assert "## argus QA scenarios" in gitlab.calls[0]
    assert "Login with valid credentials" in gitlab.calls[0]
    assert "inline comment" not in gitlab.calls[0]

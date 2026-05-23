"""Learnings are retrieved after scout, using its summaries, and traced."""
import uuid

from sqlalchemy import select

import argus.knowledge.learnings as L
from argus.db import session_factory
from argus.domain.models import (InjectionEvent, MergeRequest, Repository,
                                     Review, ReviewStage)
from argus.llm.config import LLMConfig
from argus.review.artifacts import FileChange, ReviewPlan
from argus.review.pipeline import PipelineDeps, _inject_learnings
from argus.review.tools import ToolContext


def _vec(i: int) -> list[float]:
    v = [0.0] * 768
    v[i % 768] = 1.0
    return v


async def _seed(sf, monkeypatch, settings):
    async with sf() as s:
        repo = Repository(provider="gitlab",
                          project_path=f"g/after-scout-{uuid.uuid4()}",
                          gitlab_project_id=81000000 + uuid.uuid4().int % 9000000)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.commit()
        repo_id, review_id = repo.id, review.id

    async with sf() as s:
        async def fake(text, settings_, is_query=False):
            return _vec(7)
        monkeypatch.setattr(L, "embed_text", fake)
        target = await L.upsert_learning(
            s, settings, repo_id=repo_id, topic="widgets",
            hint_text="always pass zzUniqueSentinel to renderWidget")
        await s.commit()
        target_id = target.id
    return repo_id, review_id, target_id


def _deps(sf, settings, tmp_path, repo_id, query="", paths=()):
    return PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={}, hunks={}),
        profile_static="You are a reviewer.", mr_context="MR !1: t",
        learnings_repo_id=repo_id, learnings_query=query,
        learnings_paths=list(paths))


def _plan(summary: str, path: str = "widgets/render_widget.py",
          mr_summary: str = "Adjusts widget rendering") -> ReviewPlan:
    return ReviewPlan(
        intent="change a thing", mr_summary=mr_summary,
        files=[FileChange(file_id="f1", path=path, change_kind="modified",
                          summary=summary)],
        chunks=[])


async def test_scout_summaries_reach_the_lexical_arm(
        db, engine, settings, tmp_path, monkeypatch):
    sf = session_factory(engine)
    repo_id, review_id, target_id = await _seed(sf, monkeypatch, settings)

    async def no_vector(text, settings_, is_query=False):
        return None
    monkeypatch.setattr(L, "embed_text", no_vector)

    deps = _deps(sf, settings, tmp_path, repo_id,
                 paths=["widgets/render_widget.py"])
    await _inject_learnings(deps, str(review_id),
                            _plan("passes zzUniqueSentinel into renderWidget"))

    async with sf() as s:
        injected = (await s.execute(
            select(InjectionEvent.learning_id).where(
                InjectionEvent.review_id == review_id))).scalars().all()
    assert target_id in injected


async def test_a_summary_with_no_overlap_injects_nothing(
        db, engine, settings, tmp_path, monkeypatch):
    sf = session_factory(engine)
    repo_id, review_id, target_id = await _seed(sf, monkeypatch, settings)

    async def no_vector(text, settings_, is_query=False):
        return None
    monkeypatch.setattr(L, "embed_text", no_vector)

    deps = _deps(sf, settings, tmp_path, repo_id, paths=["a/b.py"])
    await _inject_learnings(deps, str(review_id),
                            _plan("renames a local variable", path="a/b.py",
                                  mr_summary="Tidies naming"))

    async with sf() as s:
        injected = (await s.execute(
            select(InjectionEvent.learning_id).where(
                InjectionEvent.review_id == review_id))).scalars().all()
    assert target_id not in injected


async def test_retrieved_learnings_are_appended_to_shared_context(
        db, engine, settings, tmp_path, monkeypatch):
    sf = session_factory(engine)
    repo_id, review_id, _ = await _seed(sf, monkeypatch, settings)

    async def no_vector(text, settings_, is_query=False):
        return None
    monkeypatch.setattr(L, "embed_text", no_vector)

    deps = _deps(sf, settings, tmp_path, repo_id,
                 paths=["widgets/render_widget.py"])
    before = deps.mr_context
    await _inject_learnings(deps, str(review_id),
                            _plan("passes zzUniqueSentinel into renderWidget"))

    assert deps.mr_context.startswith(before)
    assert len(deps.mr_context) > len(before)


async def test_selection_trace_is_recorded_as_a_stage_artifact(
        db, engine, settings, tmp_path, monkeypatch):
    sf = session_factory(engine)
    repo_id, review_id, _ = await _seed(sf, monkeypatch, settings)

    async def no_vector(text, settings_, is_query=False):
        return None
    monkeypatch.setattr(L, "embed_text", no_vector)

    deps = _deps(sf, settings, tmp_path, repo_id,
                 paths=["widgets/render_widget.py"])
    await _inject_learnings(deps, str(review_id),
                            _plan("passes zzUniqueSentinel into renderWidget"))

    async with sf() as s:
        artifact = (await s.execute(
            select(ReviewStage.artifact).where(
                ReviewStage.review_id == review_id,
                ReviewStage.stage_name == "retrieval"))).scalars().first()
    assert artifact is not None
    rows = artifact["selection"]
    assert rows and {"learning_id", "chosen", "lexical_rank"} <= set(rows[0])


async def test_no_repo_id_means_runner_already_retrieved(
        db, engine, settings, tmp_path, monkeypatch):
    sf = session_factory(engine)
    _, review_id, _ = await _seed(sf, monkeypatch, settings)

    deps = _deps(sf, settings, tmp_path, None)
    before = deps.mr_context
    await _inject_learnings(deps, str(review_id), _plan("anything"))

    assert deps.mr_context == before
    async with sf() as s:
        artifact = (await s.execute(
            select(ReviewStage.artifact).where(
                ReviewStage.review_id == review_id,
                ReviewStage.stage_name == "retrieval"))).scalars().first()
    assert artifact is None

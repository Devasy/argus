import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from argus.domain.models import AuditRun, AuditStage, Repository


async def _run(db, status="queued"):
    repo = Repository(provider="gitlab", project_path=f"g/ap-{uuid.uuid4().hex[:8]}",
                      gitlab_project_id=int(uuid.uuid4().int % 10**8))
    db.add(repo)
    await db.flush()
    run = AuditRun(repo_id=repo.id, status=status, trigger="scheduled")
    db.add(run)
    await db.flush()
    return repo, run


async def test_an_audit_run_can_be_queued_and_carries_progress(db):
    _, run = await _run(db)
    assert (run.planned_count, run.grounded_count) == (0, 0)


async def test_one_stage_row_per_name(db):
    _, run = await _run(db)
    db.add(AuditStage(audit_run_id=run.id, stage_name="scout", status="done"))
    await db.flush()
    db.add(AuditStage(audit_run_id=run.id, stage_name="scout", status="running"))
    with pytest.raises(IntegrityError):
        await db.flush()


def test_new_audit_settings_are_editable():
    from argus.config import Settings
    from argus.settings_store import EDITABLE_KEYS, EDITABLE_MINIMUMS
    s = Settings()
    assert (s.audit_learnings_per_run, s.audit_max_parallel, s.audit_auto_archive_min_pct) == (40, 1, 80)
    for k in ("audit_learnings_per_run", "audit_max_parallel", "audit_auto_archive_min_pct"):
        assert EDITABLE_KEYS[k] is int
    assert EDITABLE_MINIMUMS["audit_max_parallel"] == 1
    assert "audit_max_clusters" not in EDITABLE_KEYS


def _deps(sf, settings, repo, run, tmp_path):
    from argus.knowledge.audit_pipeline import AuditDeps
    from argus.llm.config import LLMConfig
    return AuditDeps(sf=sf, settings=settings,
                     llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                                       api_base="http://x/v1", api_key="k"),
                     repo_id=repo.id, workspace=tmp_path, commit_sha="c" * 40, run_id=run.id,
                     graph_path=None, langfuse_metadata={}, gate=asyncio.Semaphore(1))


async def _seed(engine, n=3):
    from argus.db import session_factory
    from argus.domain.models import AuditRun, Learning, Repository
    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path=f"g/pl-{uuid.uuid4().hex[:6]}",
                          gitlab_project_id=int(uuid.uuid4().int % 10**8))
        s.add(repo)
        await s.flush()
        ls = [Learning(repo_id=repo.id, topic=f"t{i}", hint_text=f"h{i}", kind="guidance")
              for i in range(n)]
        s.add_all(ls)
        run = AuditRun(repo_id=repo.id, status="running")
        s.add(run)
        await s.commit()
    return sf, repo, run, [str(l.id) for l in ls]


async def test_pipeline_grounds_every_due_learning_and_records_stages(engine, settings, tmp_path, monkeypatch):
    from argus.domain.models import AuditStage
    from argus.knowledge import audit_agents as aa
    from argus.knowledge import audit_pipeline as ap
    from argus.review import stages
    from tests.distill_helpers import purge_repos
    sf, repo, run, ids = await _seed(engine, 3)
    seen = []

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds, **k):
        seen.append(response_model.__name__)
        if response_model is aa.AuditScoutPlan:
            return aa.AuditScoutPlan(groups=[aa.ScoutGroup(group_id="x", learning_ids=ids[:2]),
                                             aa.ScoutGroup(group_id="y", learning_ids=ids[2:])])
        if response_model is aa.GroundList:
            return aa.GroundList(verdicts=[aa.GroundVerdict(learning_id=i, verdict="corroborated",
                                                            confidence=0.9, rationale="ok")
                                           for i in ids if i in user_msg])
        if response_model is aa.RelationList:
            return aa.RelationList()
        return aa.CheckList()
    monkeypatch.setattr(stages, "run_stage_agent", fake)
    applied = {}

    async def fake_apply(deps, state):
        applied["n"] = len(state.grounded)
        return {"verdicts": len(state.grounded)}
    monkeypatch.setattr(ap, "_apply", fake_apply)

    try:
        state = await ap.run_audit_pipeline(_deps(sf, settings, repo, run, tmp_path), None)

        assert sorted(v.learning_id for v in state.grounded) == sorted(ids)
        assert applied["n"] == 3
        async with sf() as s:
            names = {r.stage_name for r in (await s.execute(select(AuditStage).where(
                AuditStage.audit_run_id == run.id))).scalars()}
        assert {"pick", "scout", "ground:g1", "ground:g2", "apply"} <= names
    finally:
        await purge_repos(engine, [repo.id])


async def test_a_learning_the_ground_agent_skips_gets_one_retry(engine, settings, tmp_path, monkeypatch):
    from argus.knowledge import audit_agents as aa
    from argus.knowledge import audit_pipeline as ap
    from argus.review import stages
    from tests.distill_helpers import purge_repos
    sf, repo, run, ids = await _seed(engine, 2)
    ground_calls = []

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds, **k):
        if response_model is aa.AuditScoutPlan:
            return aa.AuditScoutPlan(groups=[aa.ScoutGroup(group_id="x", learning_ids=ids)])
        if response_model is aa.GroundList:
            ground_calls.append(user_msg)
            first = [i for i in ids if i in user_msg][:1]
            return aa.GroundList(verdicts=[aa.GroundVerdict(learning_id=first[0], verdict="stale",
                                                            confidence=0.9, rationale="gone")])
        return response_model()
    monkeypatch.setattr(stages, "run_stage_agent", fake)

    async def fake_apply(deps, state):
        return {}
    monkeypatch.setattr(ap, "_apply", fake_apply)

    try:
        state = await ap.run_audit_pipeline(_deps(sf, settings, repo, run, tmp_path), None)
        assert len(ground_calls) == 2 and ids[1] in ground_calls[1] and ids[0] not in ground_calls[1]
        assert sorted(v.learning_id for v in state.grounded) == sorted(ids)
    finally:
        await purge_repos(engine, [repo.id])


async def test_verify_is_shown_the_archive_claim_not_the_merge_when_ground_is_destructive(
        engine, settings, tmp_path, monkeypatch):
    """A learning can be BOTH grounded as stale/contradicted (destructive) AND
    flagged as a duplicate by relate. apply lets the destructive ground verdict
    win over the relation (audit_apply_run.py's _DESTRUCTIVE_GROUND check) --
    verify's proposal text must describe the SAME claim apply will act on, or
    a human's "confirmed" on a merge gets read as confirming an archive verify
    never actually saw."""
    from types import SimpleNamespace

    from argus.knowledge import audit_agents as aa
    from argus.knowledge import audit_pipeline as ap
    from argus.knowledge import clustering as clustering_module
    from argus.review import stages
    from tests.distill_helpers import purge_repos

    sf, repo, run, ids = await _seed(engine, 2)
    a, b = ids
    captured = {}

    async def fake_cluster_learnings(session, *, repo_id, include_global=False):
        return [[SimpleNamespace(id=uuid.UUID(a)), SimpleNamespace(id=uuid.UUID(b))]]
    monkeypatch.setattr(clustering_module, "cluster_learnings", fake_cluster_learnings)

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds, **k):
        if response_model is aa.AuditScoutPlan:
            return aa.AuditScoutPlan(groups=[aa.ScoutGroup(group_id="x", learning_ids=ids)])
        if response_model is aa.GroundList:
            return aa.GroundList(verdicts=[
                aa.GroundVerdict(learning_id=a, verdict="stale", confidence=0.9, rationale="gone"),
                aa.GroundVerdict(learning_id=b, verdict="corroborated", confidence=0.9, rationale="ok"),
            ])
        if response_model is aa.RelationList:
            return aa.RelationList(relations=[
                aa.Relation(learning_id=a, relation="duplicate_of", related_learning_id=b,
                           confidence=0.9, rationale="same"),
            ])
        if response_model is aa.CheckList:
            captured["verify_msg"] = user_msg
            return aa.CheckList(checks=[aa.ProposalCheck(learning_id=a, confirmed=True, reason="ok")])
        return response_model()
    monkeypatch.setattr(stages, "run_stage_agent", fake)

    async def fake_apply(deps, state):
        return {}
    monkeypatch.setattr(ap, "_apply", fake_apply)

    try:
        await ap.run_audit_pipeline(_deps(sf, settings, repo, run, tmp_path), None)
        assert "archive (stale)" in captured["verify_msg"]
        assert "merge into" not in captured["verify_msg"]
    finally:
        await purge_repos(engine, [repo.id])


async def test_a_single_model_error_does_not_fail_the_whole_run(engine, settings, tmp_path, monkeypatch):
    """A transient provider error (timeout, connection drop) inside one node's
    model call must not take down the whole graph and lose every other
    group's finished work -- it should be recorded as a failed stage and the
    node returns None, same as an unrecoverable-stage-error today."""
    from argus.domain.models import AuditStage
    from argus.knowledge import audit_agents as aa
    from argus.knowledge import audit_pipeline as ap
    from argus.review import stages
    from tests.distill_helpers import purge_repos
    sf, repo, run, ids = await _seed(engine, 2)

    async def fake(model, tools, system_prompt, user_msg, response_model, max_rounds, **k):
        if response_model is aa.AuditScoutPlan:
            return aa.AuditScoutPlan(groups=[aa.ScoutGroup(group_id="x", learning_ids=ids)])
        if response_model is aa.GroundList:
            raise RuntimeError("simulated transient LLM failure")
        return response_model()
    monkeypatch.setattr(stages, "run_stage_agent", fake)

    async def fake_apply(deps, state):
        return {"verdicts": len(state.grounded)}
    monkeypatch.setattr(ap, "_apply", fake_apply)

    try:
        state = await ap.run_audit_pipeline(_deps(sf, settings, repo, run, tmp_path), None)
        assert state.result == {"verdicts": 0}
        async with sf() as s:
            ground_stages = {r.stage_name: r.status for r in (await s.execute(select(AuditStage).where(
                AuditStage.audit_run_id == run.id, AuditStage.stage_name.like("ground:%")
            ))).scalars()}
        assert ground_stages and all(status == "failed" for status in ground_stages.values())
    finally:
        await purge_repos(engine, [repo.id])


async def test_nothing_due_goes_straight_to_apply(engine, settings, tmp_path, monkeypatch):
    from argus.knowledge import audit_pipeline as ap
    from argus.review import stages
    from tests.distill_helpers import purge_repos
    sf, repo, run, _ = await _seed(engine, 0)

    async def boom(*a, **k):
        raise AssertionError("no agent should run")
    monkeypatch.setattr(stages, "run_stage_agent", boom)

    async def fake_apply(deps, state):
        return {"verdicts": 0}
    monkeypatch.setattr(ap, "_apply", fake_apply)
    try:
        state = await ap.run_audit_pipeline(_deps(sf, settings, repo, run, tmp_path), None)
        assert state.result == {"verdicts": 0}
    finally:
        await purge_repos(engine, [repo.id])

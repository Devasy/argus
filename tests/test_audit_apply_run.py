import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from argus.knowledge import audit_agents as aa
from argus.knowledge.audit_apply_run import apply_audit_results
from argus.knowledge.audit_pipeline import AuditDeps, AuditState
from argus.knowledge.auditor import Citation


async def _setup(engine, settings, tmp_path, n=2, **overrides):
    from argus.db import session_factory
    from argus.domain.models import AuditRun, Learning, Repository
    from argus.llm.config import LLMConfig
    (tmp_path / "a.py").write_text("def old():\n    return 1\n")
    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path=f"g/ar-{uuid.uuid4().hex[:6]}",
                          gitlab_project_id=int(uuid.uuid4().int % 10**8))
        s.add(repo)
        await s.flush()
        ls = [Learning(repo_id=repo.id, topic=f"t{i}", hint_text=f"h{i}", kind="guidance")
              for i in range(n)]
        s.add_all(ls)
        run = AuditRun(repo_id=repo.id, status="running")
        s.add(run)
        await s.commit()
    s2 = settings.model_copy(update={"audit_auto_apply": True, "audit_auto_archive_min_pct": 80,
                                     **overrides})
    deps = AuditDeps(sf=sf, settings=s2, llm_cfg=LLMConfig(provider="claude_cli_proxy",
                     model="openai/x", api_base="http://x/v1", api_key="k"),
                     repo_id=repo.id, workspace=tmp_path, commit_sha="d" * 40, run_id=run.id,
                     graph_path=None, langfuse_metadata={}, gate=asyncio.Semaphore(1))
    due = [{"id": str(l.id), "topic": l.topic, "hint_text": l.hint_text, "kind": l.kind,
            "file_paths": [], "file_pattern": None, "evidence": ""} for l in ls]
    return deps, [str(l.id) for l in ls], due, repo.id


def _stale(i, conf=0.9):
    return aa.GroundVerdict(learning_id=i, verdict="stale", confidence=conf, rationale="gone",
                            citations=[Citation(file="a.py", line=1, quote="def old():")])


async def test_a_grounded_but_uncited_verdict_still_stamps_the_learning_as_attempted(
        engine, settings, tmp_path):
    """validate_verdicts discards a verdict whose citation doesn't resolve
    (typical for code that's since been deleted). Before this fix, a
    discarded verdict never touched last_audited_at/audited_at_sha, so the
    learning stayed in tier 0 (last_audited_at IS NULL) forever -- the exact
    same stuck-first-in-every-run bug select_due_learnings' tiering exists to
    avoid. It must count as attempted (not_reached only covers learnings the
    ground agent never returned anything for at all) even though no
    AuditVerdict row is written for it."""
    from argus.domain.models import Learning
    deps, ids, due, repo_id = await _setup(engine, settings, tmp_path, 1)
    from tests.distill_helpers import purge_repos
    try:
        uncited = aa.GroundVerdict(learning_id=ids[0], verdict="corroborated",
                                   confidence=0.9, rationale="ok")  # no citations
        state = AuditState(audit_run_id=str(deps.run_id), due=due, grounded=[uncited])
        out = await apply_audit_results(deps, state)
        assert out["not_reached"] == 0
        async with deps.sf() as s:
            l = await s.get(Learning, uuid.UUID(ids[0]))
            assert l.last_audited_at is not None
            assert l.audited_at_sha == "d" * 40
    finally:
        await purge_repos(engine, [repo_id])


async def test_verified_confident_archive_is_applied_automatically(engine, settings, tmp_path):
    from argus.domain.models import AuditVerdict, Learning
    from tests.distill_helpers import purge_repos
    deps, ids, due, repo_id = await _setup(engine, settings, tmp_path, 1)
    try:
        state = AuditState(audit_run_id=str(deps.run_id), due=due, grounded=[_stale(ids[0])],
                           checks=[aa.ProposalCheck(learning_id=ids[0], confirmed=True, reason="ok")])
        out = await apply_audit_results(deps, state)
        assert out["auto_archived"] == 1
        async with deps.sf() as s:
            l = await s.get(Learning, uuid.UUID(ids[0]))
            v = (await s.execute(select(AuditVerdict).where(
                AuditVerdict.learning_id == l.id))).scalar_one()
            assert l.status == "archived" and v.state == "applied"
            assert l.audited_at_sha == "d" * 40 and l.last_audited_at is not None
    finally:
        await purge_repos(engine, [repo_id])


async def test_overturned_archive_goes_to_a_human(engine, settings, tmp_path):
    from argus.domain.models import AuditVerdict, Learning
    from tests.distill_helpers import purge_repos
    deps, ids, due, repo_id = await _setup(engine, settings, tmp_path, 1)
    try:
        state = AuditState(audit_run_id=str(deps.run_id), due=due, grounded=[_stale(ids[0])],
                           checks=[aa.ProposalCheck(learning_id=ids[0], confirmed=False,
                                                    reason="still used in b.py")])
        await apply_audit_results(deps, state)
        async with deps.sf() as s:
            v = (await s.execute(select(AuditVerdict).where(
                AuditVerdict.learning_id == uuid.UUID(ids[0])))).scalar_one()
            assert (v.proposed_action, v.state) == ("escalate_to_human", "proposed")
            assert (await s.get(Learning, uuid.UUID(ids[0]))).status == "active"
    finally:
        await purge_repos(engine, [repo_id])


async def test_low_confidence_or_switch_off_is_never_automatic(engine, settings, tmp_path):
    from argus.domain.models import Learning
    from tests.distill_helpers import purge_repos
    for kwargs, conf in (({}, 0.7), ({"audit_auto_apply": False}, 0.95)):
        deps, ids, due, repo_id = await _setup(engine, settings, tmp_path, 1, **kwargs)
        try:
            state = AuditState(audit_run_id=str(deps.run_id), due=due,
                               grounded=[_stale(ids[0], conf)],
                               checks=[aa.ProposalCheck(learning_id=ids[0], confirmed=True,
                                                        reason="ok")])
            assert (await apply_audit_results(deps, state))["auto_archived"] == 0
            async with deps.sf() as s:
                assert (await s.get(Learning, uuid.UUID(ids[0]))).status == "active"
        finally:
            await purge_repos(engine, [repo_id])


async def test_duplicate_becomes_a_merge_proposal_and_unreached_stay_due(engine, settings, tmp_path):
    from argus.domain.models import AuditVerdict, Learning
    from tests.distill_helpers import purge_repos
    deps, ids, due, repo_id = await _setup(engine, settings, tmp_path, 3)
    try:
        a, b, c = ids
        corroborated = aa.GroundVerdict(
            learning_id=a, verdict="corroborated", confidence=0.9, rationale="ok",
            citations=[Citation(file="a.py", line=1, quote="def old():")])
        state = AuditState(
            audit_run_id=str(deps.run_id), due=due,
            grounded=[corroborated.model_copy(update={"learning_id": a}),
                      corroborated.model_copy(update={"learning_id": b})],
            relations=[aa.Relation(learning_id=a, relation="duplicate_of", related_learning_id=b,
                                   confidence=0.9, rationale="same", suggested_hint_text="merged")],
            checks=[aa.ProposalCheck(learning_id=a, confirmed=True, reason="ok")])
        out = await apply_audit_results(deps, state)
        assert out["not_reached"] == 1
        async with deps.sf() as s:
            v = (await s.execute(select(AuditVerdict).where(
                AuditVerdict.learning_id == uuid.UUID(a)))).scalar_one()
            assert (v.proposed_action, v.state) == ("merge", "proposed")
            assert (await s.get(Learning, uuid.UUID(c))).last_audited_at is None
    finally:
        await purge_repos(engine, [repo_id])

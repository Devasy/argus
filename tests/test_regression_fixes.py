"""Behavioral regressions found while comparing the upstream snapshots and Argus."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from argus.db import session_factory
from argus.domain.models import (DistillationDecision, DistillationRun, DistillThread,
                                 Learning, MRVersion, Note)
from argus.knowledge import distill_threads, learnings
from argus.knowledge.agentic_distiller import StagedLearning, ThreadDecision, apply_decisions
from argus.llm.config import LLMConfig
from argus.llm.factory import build_chat_model
from tests.distill_helpers import _mr_disc, _note, _resolved_bot_thread, purge_repos


async def test_recent_reply_keeps_an_old_commit_in_reconciliation(db):
    from argus.ingest.poller import _pending_reconciliation_mr_iids
    repo, mr, disc = await _mr_disc(db)
    old = datetime.now(timezone.utc) - timedelta(days=90)
    db.add(MRVersion(mr_id=mr.id, provider_version_id=1, version_created_at=old))
    bot = await _note(db, mr, disc, "bot", "Review finding", 0)
    bot.note_created_at = old
    bot.disposition = "open"
    await db.flush()
    assert await _pending_reconciliation_mr_iids(db, repo.id) == []
    await _note(db, mr, disc, "human", "Please review this reply", 0)
    assert await _pending_reconciliation_mr_iids(db, repo.id) == [mr.mr_iid]


async def test_recent_mr_activity_keeps_an_old_commit_in_reconciliation(db):
    from argus.ingest.poller import _pending_reconciliation_mr_iids
    repo, mr, disc = await _mr_disc(db)
    old = datetime.now(timezone.utc) - timedelta(days=90)
    db.add(MRVersion(mr_id=mr.id, provider_version_id=1, version_created_at=old))
    bot = await _note(db, mr, disc, "bot", "Review finding", 0)
    bot.note_created_at, bot.disposition = old, "open"
    mr.mr_updated_at = datetime.now(timezone.utc)
    await db.flush()
    assert await _pending_reconciliation_mr_iids(db, repo.id) == [mr.mr_iid]


async def test_sweep_does_not_starve_ready_threads_behind_ineligible_mrs(db, monkeypatch):
    monkeypatch.setattr(distill_threads, "SWEEP_MR_LIMIT", 1)
    repo, mr, disc = await _mr_disc(db)
    await _note(db, mr, disc, "human", "Still discussing", -30)
    for iid, external in [(2, True), (3, False)]:
        _, other, d = await _mr_disc(db)
        other.repo_id, other.mr_iid = repo.id, iid
        if external:
            await _note(db, other, d, "external_bot", "Another bot's finding", -25)
        await _note(db, other, d, "human", "Recent reply", -20)
        d.resolved = external
    _, ready, _ = await _mr_disc(db)
    ready.repo_id, ready.mr_iid = repo.id, 4
    ready_disc = await _resolved_bot_thread(db, ready)
    await db.flush()
    assert await distill_threads.sweep_pending_threads(db, repo, datetime.now(timezone.utc)) == 1
    ledger = (await db.execute(select(DistillThread))).scalar_one()
    assert ledger.discussion_id == ready_disc.id


async def test_sweep_detects_new_human_notes_with_backdated_timestamps(db):
    repo, mr, _ = await _mr_disc(db)
    disc = await _resolved_bot_thread(db, mr)
    [thread] = await distill_threads.load_threads(db, mr)
    now = datetime.now(timezone.utc)
    db.add(DistillThread(mr_id=mr.id, discussion_id=disc.id,
        thread_type=thread.thread_type, content_hash=thread.content_hash,
        status="done", updated_at=now))
    await db.flush()
    await _note(db, mr, disc, "human", "Imported old reply", -120)
    assert await distill_threads.sweep_pending_threads(db, repo, now) == 1


async def test_learning_dedup_never_crosses_kinds(db, settings, monkeypatch):
    async def embed(*args, **kwargs):
        return [0.3] * 768
    monkeypatch.setattr(learnings, "embed_text", embed)
    repo, _, _ = await _mr_disc(db)
    rows = []
    for kind in ("guidance", "do_not_suggest", "missed_pattern"):
        rows.append(await learnings.upsert_learning(db, settings, repo_id=repo.id,
            topic="Resources", hint_text="Use the established cleanup idiom", kind=kind))
    assert len({r.id for r in rows}) == 3
    same = await learnings.upsert_learning(db, settings, repo_id=repo.id,
        topic="Resources", hint_text="Equivalent guidance", kind="guidance")
    assert same.id == rows[0].id


async def test_failed_second_learning_rolls_back_first_and_its_counter(engine, settings, monkeypatch):
    sf = session_factory(engine)
    async with sf() as s:
        repo, mr, _ = await _mr_disc(s)
        await _resolved_bot_thread(s, mr)
        [thread] = await distill_threads.load_threads(s, mr)
        await s.commit()
    calls = 0
    async def embed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("embedding unavailable")
        return [0.3] * 768
    monkeypatch.setattr(learnings, "embed_text", embed)
    staged = [StagedLearning(thread.discussion_id, "create", None, None,
        f"topic {i}", f"Hint {i}", None, thread.human_notes[-1].id) for i in range(2)]
    decisions = {thread.discussion_id: ThreadDecision(discussion_id=str(thread.discussion_id),
        reply_verdict="rejected", reason="Team idiom")}
    try:
        counts = await apply_decisions(sf, settings, repo, mr, [thread], decisions, staged, None)
        assert counts["threads_failed"] == 1
        assert counts["created"] == counts["updated"] == 0
        async with sf() as s:
            assert (await s.execute(select(Learning).where(Learning.repo_id == repo.id))).all() == []
    finally:
        await purge_repos(engine, [repo.id])


async def test_audit_merge_cannot_combine_opposite_learning_kinds(db):
    from argus.domain.models import AuditRun, AuditVerdict
    from argus.knowledge.audit_apply import apply_verdict
    repo, _, _ = await _mr_disc(db)
    run = AuditRun(repo_id=repo.id, status="done")
    guidance = Learning(repo_id=repo.id, topic="Cleanup", hint_text="Suggest cleanup", kind="guidance")
    suppression = Learning(repo_id=repo.id, topic="Cleanup", hint_text="Suppress cleanup", kind="do_not_suggest")
    db.add_all([run, guidance, suppression])
    await db.flush()
    verdict = AuditVerdict(audit_run_id=run.id, learning_id=suppression.id,
        verdict="duplicate_of", confidence=0.9, rationale="Similar wording",
        proposed_action="merge", related_learning_id=guidance.id, state="approved")
    db.add(verdict)
    await db.flush()
    assert await apply_verdict(db, verdict) is False
    assert suppression.status == guidance.status == "active"


async def test_rewritten_learning_uses_its_new_words_in_lexical_retrieval(db, settings, monkeypatch):
    from argus.domain.models import AuditRun, AuditVerdict
    from argus.knowledge.audit_apply import apply_verdict
    repo, _, _ = await _mr_disc(db)
    run = AuditRun(repo_id=repo.id, status="done")
    learning = Learning(repo_id=repo.id, topic="Cleanup", hint_text="Original wording",
                        kind="guidance", embedding=[0.3] * 768)
    db.add_all([run, learning])
    await db.flush()
    verdict = AuditVerdict(audit_run_id=run.id, learning_id=learning.id,
        verdict="unfalsifiable", confidence=0.9, rationale="Needs detail",
        proposed_action="flag_for_rewrite", suggested_hint_text="Use explicit_resource_cleanup",
        state="approved")
    db.add(verdict)
    await db.flush()
    assert await apply_verdict(db, verdict)
    async def unavailable(*args, **kwargs):
        return None
    monkeypatch.setattr(learnings, "embed_text", unavailable)
    found = await learnings.relevant_learnings(db, settings, repo_id=repo.id,
        query_text="", file_paths=[], lexical_query="explicit_resource_cleanup", explore=False)
    assert learning.id in [row.id for row in found]


async def test_run_history_survives_new_replies_and_a_second_distillation(engine, settings):
    sf = session_factory(engine)
    async with sf() as s:
        repo, mr, _ = await _mr_disc(s)
        disc = await _resolved_bot_thread(s, mr)
        [thread] = await distill_threads.load_threads(s, mr)
        runs = [DistillationRun(mr_id=mr.id, note_ids=[], status="done") for _ in range(2)]
        s.add_all(runs)
        await s.commit()
    try:
        decision = ThreadDecision(discussion_id=str(disc.id), reply_verdict="rejected",
                                  reason="First decision")
        await apply_decisions(sf, settings, repo, mr, [thread], {disc.id: decision}, [], runs[0].id)
        async with sf() as s:
            (await s.get(Note, thread.human_notes[-1].id)).body = "Edited later"
            await _note(s, mr, disc, "human", "New reply", 10)
            [new_thread] = await distill_threads.load_threads(s, mr)
            await s.commit()
        decision = decision.model_copy(update={"reply_verdict": "accepted", "reason": "Second decision"})
        await apply_decisions(sf, settings, repo, mr, [new_thread], {disc.id: decision}, [], runs[1].id)
        async with sf() as s:
            saved = (await s.execute(select(DistillationDecision).where(
                DistillationDecision.distillation_run_id == runs[0].id))).scalar_one()
            original = saved.decision
            assert original["reply_verdict"] == "rejected"
            assert len(original["thread"]["notes"]) == 2
            assert original["thread"]["notes"][-1]["body"] == "intentional, closed in finally"
            assert (await s.execute(select(DistillThread).where(
                DistillThread.discussion_id == disc.id))).scalar_one().distillation_run_id == runs[1].id
        from httpx import ASGITransport, AsyncClient
        from argus.api.app import create_app
        async with AsyncClient(transport=ASGITransport(app=create_app(settings, engine)),
                               base_url="http://test") as client:
            response = await client.get(f"/distillation-runs/{runs[0].id}")
            assert response.status_code == 200
            assert response.json()["threads"] == [original]
    finally:
        await purge_repos(engine, [repo.id])


@pytest.mark.parametrize("primary,fallback", [("ollama", "groq"), ("groq", "ollama")])
async def test_provider_fallback_converts_original_history_and_preserves_tools(monkeypatch, primary, fallback):
    cfg = LLMConfig(provider=primary, model=f"{primary}/primary", num_retries=0,
        reasoning_budget_tokens=123,
        fallback={"provider": fallback, "model": f"{fallback}/secondary", "api_key": "example"})
    model = build_chat_model(cfg)
    requests = []
    async def completion(**kwargs):
        requests.append(kwargs)
        if kwargs["model"] == cfg.model:
            raise RuntimeError("primary unavailable")
        return {"choices": [{"message": {"role": "assistant", "content": "Recovered"},
                             "finish_reason": "stop"}], "usage": {}}
    monkeypatch.setattr(model.client, "acompletion", completion)
    tool = {"type": "function", "function": {"name": "get_file", "parameters": {"type": "object"}}}
    history = [AIMessage(content="Earlier answer", additional_kwargs={"reasoning_content": "Reasoning"})]
    answer = await model.bind_tools([tool]).ainvoke(history)
    assert answer.content == "Recovered"
    assert len(requests) == 2
    for request, provider in zip(requests, (primary, fallback)):
        assert ("reasoning_content" in request["messages"][0]) == (provider != "groq")
        assert ("reasoning_budget_tokens" in request) == (provider == "ollama")
        assert request["tools"][0]["function"]["name"] == "get_file"
        assert "fallbacks" not in request
    assert history[0].additional_kwargs["reasoning_content"] == "Reasoning"


async def test_fallback_never_swallow_cancellation(monkeypatch):
    model = build_chat_model(LLMConfig(provider="openai", model="openai/primary",
        num_retries=0, fallback={"provider": "groq", "model": "groq/secondary"}))
    calls = []
    async def cancelled(**kwargs):
        calls.append(kwargs["model"])
        raise asyncio.CancelledError()
    monkeypatch.setattr(model.client, "acompletion", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await model._agenerate([AIMessage(content="Question")])
    assert calls == ["openai/primary"]


@pytest.mark.parametrize("metadata", [True, False])
def test_author_metadata_and_own_bot_precedence(metadata):
    from argus.gitlab.normalizer import classify_author
    assert classify_author("service.account", set(), is_bot=metadata) == (
        "external_bot" if metadata else "human")
    assert classify_author("service.account", {"service.account"}, is_bot=metadata) == "bot"


@pytest.mark.parametrize("saved_head,expected_resume", [(None, False), ("a" * 40, True), ("b" * 40, False)])
async def test_legacy_checkpoint_requires_a_verified_matching_head(monkeypatch, saved_head, expected_resume):
    from argus.review import pipeline
    seen = {}
    class Graph:
        async def ainvoke(self, state, config):
            seen.update(state=state, config=config)
            return {"review_id": "test", "head_sha": "a" * 40}
    class Checkpointer:
        async def aget_tuple(self, config):
            if "@" in config["configurable"]["thread_id"]:
                return None
            return SimpleNamespace(checkpoint={"channel_values": {"head_sha": saved_head}})
    monkeypatch.setattr(pipeline, "build_graph", lambda *args: Graph())
    await pipeline.run_review_pipeline("test", SimpleNamespace(diff_refs={"head_sha": "a" * 40}), Checkpointer())
    assert (seen["state"] is None) == expected_resume
    if not expected_resume:
        assert seen["state"].head_sha == "a" * 40


@pytest.mark.parametrize("partial", [False, True])
async def test_streaming_fallback_only_before_the_first_chunk(monkeypatch, partial):
    model = build_chat_model(LLMConfig(provider="ollama", model="openai/primary",
        num_retries=0, fallback={"provider": "groq", "model": "groq/secondary"}))
    requests = []
    async def completion(**kwargs):
        requests.append(kwargs)
        async def chunks():
            if kwargs["model"] == "openai/primary":
                if partial:
                    yield {"choices": [{"delta": {"role": "assistant", "content": "Partial"}}]}
                raise RuntimeError("stream interrupted")
            yield {"choices": [{"delta": {"role": "assistant", "content": "Recovered"}}]}
        return chunks()
    monkeypatch.setattr(model.client, "acompletion", completion)
    history = [AIMessage(content="Prior", additional_kwargs={"reasoning_content": "Reasoning"})]
    stream = model._astream(history)
    if partial:
        assert (await anext(stream)).message.content == "Partial"
        with pytest.raises(RuntimeError, match="stream interrupted"):
            await anext(stream)
        assert len(requests) == 1
    else:
        assert [chunk.message.content async for chunk in stream] == ["Recovered"]
        assert len(requests) == 2
        assert "reasoning_content" not in requests[1]["messages"][0]


async def test_scheduler_fetches_current_code_for_recently_audited_learnings(engine, settings, tmp_path, monkeypatch):
    from argus.domain.models import AuditRun
    from argus.knowledge.audit_scheduler import _schedule_repo
    from argus.review import workspace
    from tests.test_audit_pick import _git, _repo_with_two_commits
    source, first_sha = _repo_with_two_commits(tmp_path)
    sf = session_factory(engine)
    now = datetime.now(timezone.utc)
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        repo.enabled = True
        s.add(Learning(repo_id=repo.id, topic="Changed file", hint_text="Keep cleanup safe",
            kind="guidance", last_audited_at=now, audited_at_sha=first_sha, file_paths=["b.py"]))
        await s.commit()
    root = tmp_path / "workspaces"
    actual_manager = workspace.WorkspaceManager
    acquired = []
    def local_manager(path, url):
        assert path == root / str(repo.id)
        acquired.append(path)
        return actual_manager(path, str(source))
    monkeypatch.setattr(workspace, "WorkspaceManager", local_manager)
    cfg = settings.model_copy(update={"workspace_root": str(root)})
    try:
        async with sf() as s:
            await _schedule_repo(s, cfg, repo.id, now)
            await s.commit()
            assert (await s.execute(select(AuditRun).where(AuditRun.repo_id == repo.id))).all() == []
        (source / "b.py").write_text("y = 2\n", encoding="utf-8")
        _git(source, "commit", "-qam", "Relevant file changed")
        async with sf() as s:
            await _schedule_repo(s, cfg, repo.id, now)
            await s.commit()
            run = (await s.execute(select(AuditRun).where(AuditRun.repo_id == repo.id))).scalar_one()
            assert run.trigger == "scheduled"
        assert len(acquired) == 2
        assert not list((root / str(repo.id)).glob("wt-*")), "scheduler releases temporary worktrees"
    finally:
        await purge_repos(engine, [repo.id])


async def test_resumed_audit_keeps_its_original_commit_when_the_branch_moves(engine, settings, tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from argus.domain.models import AuditRun
    from argus.jobs import workers
    from argus.knowledge import audit_job, audit_pipeline, graphify
    from argus.review import workspace
    from tests.test_audit_pick import _repo_with_two_commits

    source, original_sha = _repo_with_two_commits(tmp_path)
    sf = session_factory(engine)
    async with sf() as s:
        repo, _, _ = await _mr_disc(s)
        run = AuditRun(repo_id=repo.id, status="running", audited_ref="main", commit_sha=original_sha)
        s.add(run)
        await s.commit()

    actual_manager = workspace.WorkspaceManager
    def local_manager(path, url):
        return actual_manager(tmp_path / "checkout", str(source))
    monkeypatch.setattr(workspace, "WorkspaceManager", local_manager)
    async def cfg(*args):
        return LLMConfig(provider="openai", model="openai/example")
    monkeypatch.setattr(workers, "distill_llm_config", cfg)
    async def no_graph(*args):
        return None
    monkeypatch.setattr(graphify, "build_graph", no_graph)
    monkeypatch.setattr(audit_job, "_delete_checkpoint", no_graph)
    class Checkpointer:
        async def setup(self):
            pass
    @asynccontextmanager
    async def checkpoint(*args):
        yield Checkpointer()
    monkeypatch.setattr(AsyncPostgresSaver, "from_conn_string", checkpoint)
    async def pipeline(deps, cp):
        assert deps.commit_sha == original_sha
        assert (deps.workspace / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    monkeypatch.setattr(audit_pipeline, "run_audit_pipeline", pipeline)
    try:
        await audit_job.run_audit_job(sf, settings, {"audit_run_id": str(run.id)}, None)
        async with sf() as s:
            finished = await s.get(AuditRun, run.id)
            assert finished.status == "done"
            assert finished.commit_sha == original_sha
            assert finished.audited_ref == "main"
    finally:
        await purge_repos(engine, [repo.id])

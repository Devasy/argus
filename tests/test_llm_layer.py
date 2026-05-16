import os
import uuid

import httpx
import pytest
import respx

from argus.domain.models import LLMEndpoint, LLMRound, ToolCall
from argus.llm.config import LLMConfig, resolve_llm_config
from argus.llm.health import check_llm_health
from argus.llm.prompts import PromptBlocks, assemble_system_prompt
from argus.llm.trace import DBTraceCallback


async def test_proxy_url_wins(db):
    cfg = await resolve_llm_config(db, None, "http://localhost:8484")
    assert cfg.provider == "claude_cli_proxy"
    assert cfg.api_base == "http://localhost:8484/v1"


async def test_check_llm_health_skips_when_no_api_base():
    cfg = LLMConfig(provider="anthropic", model="anthropic/claude-sonnet-4-5")
    assert await check_llm_health(cfg) is True


def test_server_root_strips_v1_suffix():
    from argus.llm.health import _server_root
    assert _server_root("http://llama:8080/v1") == "http://llama:8080"
    assert _server_root("http://llama:8080/v1/") == "http://llama:8080"
    assert _server_root("http://llama:8080") == "http://llama:8080"


@respx.mock
async def test_check_llm_health_true_when_endpoint_responds():
    respx.get("http://llama:8080/health").mock(return_value=httpx.Response(200))
    cfg = LLMConfig(provider="ollama", model="m", api_base="http://llama:8080/v1")
    assert await check_llm_health(cfg) is True


@respx.mock
async def test_check_llm_health_false_when_endpoint_unreachable():
    respx.get("http://llama:8080/health").mock(
        side_effect=httpx.ConnectError("connection refused"))
    cfg = LLMConfig(provider="ollama", model="m", api_base="http://llama:8080/v1")
    assert await check_llm_health(cfg) is False


@respx.mock
async def test_check_llm_health_false_on_server_error():
    respx.get("http://llama:8080/health").mock(return_value=httpx.Response(503))
    cfg = LLMConfig(provider="ollama", model="m", api_base="http://llama:8080/v1")
    assert await check_llm_health(cfg) is False


async def test_endpoint_resolution(db, monkeypatch):
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "sk-test")
    ep = LLMEndpoint(name="claude-api", provider="anthropic",
                     model="anthropic/claude-sonnet-4-5",
                     api_key_ref="TEST_ANTHROPIC_KEY", is_default=True)
    db.add(ep)
    await db.flush()
    cfg = await resolve_llm_config(db, None, None)
    assert cfg.model == "anthropic/claude-sonnet-4-5"
    assert cfg.api_key == "sk-test"
    assert cfg.supports_prompt_cache is True   # anthropic supports caching


async def test_no_endpoint_raises(db):
    with pytest.raises(ValueError):
        await resolve_llm_config(db, None, None)


def test_prompt_block_order():
    blocks = PromptBlocks(static="S", per_mr="M", per_agent="A")
    assert assemble_system_prompt(blocks) == "S\n\nM\n\nA"


async def test_trace_callback_requires_exactly_one_parent():
    with pytest.raises(ValueError):
        DBTraceCallback(None, "scout")
    with pytest.raises(ValueError):
        DBTraceCallback(None, "scout", review_id=uuid.uuid4(),
                        distillation_run_id=uuid.uuid4())


async def test_trace_callback_writes_distillation_run_id(db, engine):
    from argus.db import session_factory
    from argus.domain.models import (Actor, MergeRequest, Repository,
                                         DistillationRun, LLMRound)
    sf = session_factory(engine)
    repo = Repository(provider="gitlab", project_path="g/trace-p",
                      gitlab_project_id=90100)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="merged",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    run = DistillationRun(mr_id=mr.id, note_ids=[])
    db.add(run)
    await db.flush()
    run_id = run.id
    await db.commit()

    cb = DBTraceCallback(sf, "distill", distillation_run_id=run_id)
    rid = uuid.uuid4()
    await cb.on_llm_start({}, [], run_id=rid)

    class FakeResponse:
        llm_output = {"token_usage": {"prompt_tokens": 5, "completion_tokens": 2},
                     "model_name": "m"}

    await cb.on_llm_end(FakeResponse(), run_id=rid)

    async with sf() as s:
        rows = (await s.execute(
            __import__("sqlalchemy").select(LLMRound).where(
                LLMRound.distillation_run_id == run_id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].review_id is None
    assert rows[0].prompt_tokens == 5
    assert rows[0].status == "done"


async def test_trace_callback_marks_running_before_completion(db, engine):
    """on_llm_start must persist a 'running' row immediately (not only in
    memory) so a poller/websocket can see the call is in flight before it
    finishes — this is what makes a long-running LLM call visible as
    'in progress' rather than looking identical to 'nothing happening yet'."""
    from argus.db import session_factory
    from argus.domain.models import MergeRequest, Repository, DistillationRun
    import sqlalchemy as sa

    sf = session_factory(engine)
    repo = Repository(provider="gitlab", project_path="g/trace-running",
                      gitlab_project_id=90101)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="merged",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    run = DistillationRun(mr_id=mr.id, note_ids=[])
    db.add(run)
    await db.flush()
    run_id = run.id
    await db.commit()

    cb = DBTraceCallback(sf, "distill", distillation_run_id=run_id)
    rid = uuid.uuid4()
    await cb.on_llm_start({}, [], run_id=rid)

    async with sf() as s:
        row = (await s.execute(sa.select(LLMRound).where(
            LLMRound.distillation_run_id == run_id))).scalar_one()
        assert row.status == "running"
        assert row.started_at is not None
        running_row_id = row.id

    class FakeResponse:
        llm_output = {"token_usage": {"prompt_tokens": 1, "completion_tokens": 1},
                     "model_name": "m"}

    await cb.on_llm_end(FakeResponse(), run_id=rid)

    async with sf() as s:
        rows = (await s.execute(sa.select(LLMRound).where(
            LLMRound.distillation_run_id == run_id))).scalars().all()
    # the running row is updated in place, not superseded by a second insert
    assert len(rows) == 1
    assert rows[0].id == running_row_id
    assert rows[0].status == "done"


async def test_trace_callback_marks_failed_on_error(db, engine):
    from argus.db import session_factory
    from argus.domain.models import MergeRequest, Repository, DistillationRun
    import sqlalchemy as sa

    sf = session_factory(engine)
    repo = Repository(provider="gitlab", project_path="g/trace-failed",
                      gitlab_project_id=90102)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="merged",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr)
    await db.flush()
    run = DistillationRun(mr_id=mr.id, note_ids=[])
    db.add(run)
    await db.flush()
    run_id = run.id
    await db.commit()

    cb = DBTraceCallback(sf, "distill", distillation_run_id=run_id)
    rid = uuid.uuid4()
    await cb.on_llm_start({}, [], run_id=rid)
    await cb.on_llm_error(RuntimeError("boom"), run_id=rid)

    async with sf() as s:
        rows = (await s.execute(sa.select(LLMRound).where(
            LLMRound.distillation_run_id == run_id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert "boom" in rows[0].error


# --- served-model probe: which model actually answered, not which we asked for ---

# Trimmed from a real llama.cpp /v1/models response on the GPU box. The GGUF
# filename is the useful part: it carries both the model and its quantization,
# so a swap from qwen3.6-35b to a qwen3.8-27b build is visible here even though
# the configured model string never changes.
_LLAMACPP_MODELS = {
    "models": [{"name": "/root/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"}],
    "object": "list",
    "data": [{"id": "/root/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
              "object": "model", "owned_by": "llamacpp",
              "meta": {"n_params": 35505251456, "ftype": "Q4_K - Medium"}}],
}


@respx.mock
async def test_served_model_reports_what_the_endpoint_actually_serves():
    from argus.llm.health import resolve_served_model

    respx.get("http://llama:8080/v1/models").mock(
        return_value=httpx.Response(200, json=_LLAMACPP_MODELS))
    cfg = LLMConfig(provider="ollama", model="openai/qwen3.6-35b-a3b",
                    api_base="http://llama:8080/v1")
    assert await resolve_served_model(cfg) == \
        "/root/models/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"


@respx.mock
async def test_served_model_falls_back_to_ollama_style_key():
    from argus.llm.health import resolve_served_model

    respx.get("http://llama:8080/v1/models").mock(return_value=httpx.Response(
        200, json={"models": [{"name": "qwen3.8-27b"}]}))
    cfg = LLMConfig(provider="ollama", model="m", api_base="http://llama:8080/v1")
    assert await resolve_served_model(cfg) == "qwen3.8-27b"


@respx.mock
async def test_served_model_is_none_when_endpoint_is_unreachable():
    """Telemetry must never fail a review: an unreachable or non-llama.cpp
    endpoint just leaves the model unattributed."""
    from argus.llm.health import resolve_served_model

    respx.get("http://llama:8080/v1/models").mock(
        side_effect=httpx.ConnectError("refused"))
    cfg = LLMConfig(provider="ollama", model="m", api_base="http://llama:8080/v1")
    assert await resolve_served_model(cfg) is None


@respx.mock
async def test_served_model_is_none_on_unexpected_payload():
    from argus.llm.health import resolve_served_model

    respx.get("http://llama:8080/v1/models").mock(
        return_value=httpx.Response(200, json={"unexpected": True}))
    cfg = LLMConfig(provider="ollama", model="m", api_base="http://llama:8080/v1")
    assert await resolve_served_model(cfg) is None


async def test_served_model_skipped_for_hosted_providers():
    """A hosted provider serves exactly the model we named, so there is
    nothing to discover and no call to make."""
    from argus.llm.health import resolve_served_model

    cfg = LLMConfig(provider="anthropic", model="anthropic/claude-sonnet-4-5")
    assert await resolve_served_model(cfg) is None

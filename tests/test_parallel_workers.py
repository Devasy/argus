import itertools
import logging
from uuid import uuid4

from argus.jobs.workers import (WorkerSpec, distill_llm_config, parse_worker_specs,
                                    review_handler, route_review_to_endpoint)

KINDS = ["review", "distill_mr"]
_project_ids = itertools.count(91_000_001)


def test_empty_specs_keep_one_worker_for_every_kind():
    assert parse_worker_specs("", KINDS) == [WorkerSpec(id="w1", kinds=("review", "distill_mr"))]


def test_valid_specs_parse_with_endpoints():
    raw = ('[{"id": "rev", "kinds": ["review"]},'
           ' {"id": "dis", "kinds": ["distill_mr"], "endpoint": "vm2"}]')
    assert parse_worker_specs(raw, KINDS) == [
        WorkerSpec(id="rev", kinds=("review",)),
        WorkerSpec(id="dis", kinds=("distill_mr",), endpoint="vm2"),
    ]


def test_malformed_specs_fall_back_instead_of_breaking_startup(caplog):
    for raw in ['not json', '[]', '{"id": "a"}', '[{"id": "a", "kinds": ["nope"]}]',
                '[{"id": "a", "kinds": ["review"]}, {"id": "a", "kinds": ["review"]}]',
                '[{"id": "a", "kinds": []}]', '[{"id": "a", "kinds": ["review"], "endpoint": 3}]']:
        with caplog.at_level(logging.ERROR, logger="argus.jobs"):
            assert parse_worker_specs(raw, KINDS) == [WorkerSpec(id="w1", kinds=tuple(KINDS))], raw


def test_a_kind_no_worker_claims_is_warned_about(caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("argus"), "propagate", True)
    with caplog.at_level(logging.WARNING, logger="argus.jobs"):
        specs = parse_worker_specs('[{"id": "rev", "kinds": ["review"]}]', KINDS)
    assert specs == [WorkerSpec(id="rev", kinds=("review",))]
    assert "distill_mr" in caplog.text


def test_audit_repo_is_assigned_to_the_default_fallback_worker(caplog):
    """An OSS install with one worker can run audits without a GPU-specific lane."""
    with caplog.at_level(logging.WARNING, logger="argus.jobs"):
        specs = parse_worker_specs("", KINDS + ["audit_repo"])
    assert specs == [WorkerSpec(id="w1", kinds=("review", "distill_mr", "audit_repo"))]


async def _review_row(sf, llm_config: dict, tag: str):
    from argus.domain.models import MergeRequest, Repository, Review
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path=f"grp/pw-{tag}",
                          gitlab_project_id=next(_project_ids))
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s", web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="queued", llm_config=llm_config)
        s.add(review); await s.flush()
        await s.commit()
        return review.id


async def _endpoint(sf, name: str, base_url: str):
    from argus.domain.models import LLMEndpoint
    async with sf() as s:
        s.add(LLMEndpoint(name=name, provider="ollama", model="openai/m", base_url=base_url))
        await s.commit()


async def test_route_review_rewrites_llm_config_to_the_workers_endpoint(engine):
    from argus.db import session_factory
    from argus.domain.models import Review
    sf = session_factory(engine)
    await _endpoint(sf, "pw-vm2", "http://vm2:8090/v1")
    rid = await _review_row(sf, {"provider": "ollama", "model": "old", "api_base": "http://vm1"}, "route")

    assert await route_review_to_endpoint(sf, rid, "pw-vm2") is True
    async with sf() as s:
        assert (await s.get(Review, rid)).llm_config["api_base"] == "http://vm2:8090/v1"


async def test_route_review_leaves_config_alone_when_the_endpoint_is_missing(engine):
    from argus.db import session_factory
    from argus.domain.models import Review
    sf = session_factory(engine)
    original = {"provider": "ollama", "model": "old", "api_base": "http://vm1"}
    rid = await _review_row(sf, original, "missing")

    assert await route_review_to_endpoint(sf, rid, "pw-no-such-endpoint") is False
    async with sf() as s:
        assert (await s.get(Review, rid)).llm_config == original


async def test_review_handler_skips_routing_for_pinned_reviews(engine):
    from argus.db import session_factory
    from argus.domain.models import Review
    sf = session_factory(engine)
    await _endpoint(sf, "pw-vm2-pinned", "http://vm2-pinned/v1")
    original = {"provider": "ollama", "model": "chosen", "api_base": "http://chosen"}
    pinned = await _review_row(sf, original, "pinned")
    free = await _review_row(sf, original, "free")
    ran = []

    async def run(p):
        ran.append(p["review_id"])

    handler = review_handler(sf, run, "pw-vm2-pinned")
    await handler({"review_id": str(pinned), "pinned": True})
    await handler({"review_id": str(free)})

    async with sf() as s:
        assert (await s.get(Review, pinned)).llm_config == original
        assert (await s.get(Review, free)).llm_config["api_base"] == "http://vm2-pinned/v1"
    assert ran == [str(pinned), str(free)]


async def test_review_handler_without_an_endpoint_never_touches_the_review(engine):
    from argus.db import session_factory
    from argus.domain.models import Review
    sf = session_factory(engine)
    original = {"provider": "ollama", "model": "m", "api_base": "http://vm1"}
    rid = await _review_row(sf, original, "noendpoint")

    async def run(p):
        return None

    await review_handler(sf, run, None)({"review_id": str(rid)})
    async with sf() as s:
        assert (await s.get(Review, rid)).llm_config == original


async def test_distill_uses_the_workers_endpoint_when_present(engine):
    from argus.db import session_factory
    sf = session_factory(engine)
    await _endpoint(sf, "pw-distill-vm2", "http://distill-vm2/v1")
    async with sf() as s:
        cfg = await distill_llm_config(s, "pw-distill-vm2")
    assert cfg.api_base == "http://distill-vm2/v1"


async def test_distill_falls_back_to_the_default_when_the_endpoint_is_missing(engine, monkeypatch):
    import argus.llm.config as llm_config
    from argus.db import session_factory
    sentinel = object()

    async def fake_default(session, endpoint_id, proxy_url):
        assert endpoint_id is None and proxy_url is None
        return sentinel

    monkeypatch.setattr(llm_config, "resolve_llm_config", fake_default)
    async with session_factory(engine)() as s:
        assert await distill_llm_config(s, "pw-not-there") is sentinel
        assert await distill_llm_config(s, None) is sentinel



async def test_app_starts_one_loop_per_spec_and_each_claims_only_its_kinds(engine, settings, monkeypatch):
    import asyncio
    import time

    from starlette.testclient import TestClient

    import argus.api.app as app_module
    from argus.db import get_engine, session_factory
    from argus.domain.models import Job, Review
    from argus.jobs.queue import enqueue

    sf = session_factory(engine)
    await _endpoint(sf, "pw-e2e-vm2", "http://e2e-vm2/v1")
    rid = await _review_row(sf, {"provider": "ollama", "model": "m", "api_base": "http://vm1"}, "e2e")
    async with sf() as s:
        review_job = await enqueue(s, "review", {"review_id": str(rid)}, dedup_key=None)
        distill_job = await enqueue(s, "distill_mr", {"mr_id": str(uuid4()), "note_ids": []},
                                    dedup_key=None)
        await s.commit()
        review_job_id, distill_job_id = review_job.id, distill_job.id

    distill_endpoints = []

    async def fake_review(sf_, eff, payload):
        return None

    async def fake_distill(sf_, eff, payload, endpoint_name=None):
        distill_endpoints.append(endpoint_name)

    monkeypatch.setattr(app_module, "execute_review_job", fake_review)
    monkeypatch.setattr(app_module, "_run_distill_mr_job", fake_distill)
    specs = ('[{"id": "rev", "kinds": ["review"], "endpoint": "pw-e2e-vm2"},'
             ' {"id": "dis", "kinds": ["distill_mr"]}]')
    app = app_module.create_app(
        settings=settings.model_copy(update={"poller_enabled": False, "audit_enabled": False,
                                             "worker_enabled": True, "worker_specs": specs}),
        engine=get_engine(str(settings.database_url)))

    def _run():
        with TestClient(app):
            assert len(app.state.workers) == 2
            deadline = time.time() + 20
            while time.time() < deadline:
                done = asyncio.run(_statuses())
                if all(st == "done" for st, _ in done.values()):
                    return done
                time.sleep(0.5)
            return asyncio.run(_statuses())

    async def _statuses():
        eng = get_engine(str(settings.database_url))
        try:
            async with session_factory(eng)() as s:
                rows = {j: await s.get(Job, j) for j in (review_job_id, distill_job_id)}
                return {j: (r.status, r.locked_by) for j, r in rows.items()}
        finally:
            await eng.dispose()

    try:
        done = await asyncio.to_thread(_run)
    finally:
        from sqlalchemy import delete
        async with engine.begin() as conn:
            await conn.execute(delete(Job).where(Job.id.in_([review_job_id, distill_job_id])))
    assert done[review_job_id] == ("done", "rev")
    assert done[distill_job_id] == ("done", "dis")
    assert distill_endpoints == [None]
    async with sf() as s:
        assert (await s.get(Review, rid)).llm_config["api_base"] == "http://e2e-vm2/v1"

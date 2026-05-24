async def test_ws_survives_send_failure_mid_stream(engine, settings, monkeypatch):
    """Regression test: if send_json fails mid-stream because the client is
    already gone, the handler's finally-block close() must not itself raise
    (Starlette's WebSocket.close() can still hit the dead transport and
    raise WebSocketDisconnect/RuntimeError depending on exactly where the
    underlying send failed)."""
    import asyncio
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocket, WebSocketDisconnect
    from sqlalchemy import delete
    from argus.api.app import create_app
    from argus.db import session_factory
    from argus.domain.models import MergeRequest, Repository, Review

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/trace-ws-disc-test",
                          gitlab_project_id=90000004)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.commit()
        rid, mr_id, repo_id = review.id, mr.id, repo.id

    ws_settings = settings.model_copy(update={"poller_enabled": False,
                                               "worker_enabled": False})

    from argus.db import get_engine
    ws_engine = get_engine(str(settings.database_url))
    app = create_app(settings=ws_settings, engine=ws_engine)

    # send_json succeeds once (so the app observes a "running" status and
    # loops back around), then raises like it would against a dead socket.
    # close() is left as the real implementation, so it will hit whatever
    # broken state send_json's failure left the WebSocket in.
    calls = {"n": 0}
    real_send_json = WebSocket.send_json

    async def flaky_send_json(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_send_json(self, *args, **kwargs)
        raise WebSocketDisconnect(code=1006)

    monkeypatch.setattr(WebSocket, "send_json", flaky_send_json)

    try:
        def _sync():
            with TestClient(app) as client:
                with client.websocket_connect(f"/ws/reviews/{rid}") as ws:
                    ws.receive_json()
                    import time
                    time.sleep(3)
        await asyncio.to_thread(_sync)
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(Review).where(Review.id == rid))
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))
        await ws_engine.dispose()


async def test_ws_streams_until_done(engine, settings):
    import asyncio
    from starlette.testclient import TestClient
    from sqlalchemy import delete
    from argus.api.app import create_app
    from argus.db import session_factory
    from argus.domain.models import MergeRequest, Repository, Review, ReviewStage

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/trace-ws-test",
                          gitlab_project_id=90000003)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="done")
        s.add(review); await s.flush()
        s.add(ReviewStage(review_id=review.id, stage_name="scout", status="done"))
        await s.commit()
        rid, mr_id, repo_id = review.id, mr.id, repo.id

    # Disable the poller/worker background loops for this test: they are
    # started via asyncio.create_task on whatever loop is current at ASGI
    # lifespan startup, which for TestClient is a separate portal thread's
    # loop from the session-scoped `engine` fixture's loop. Since this test
    # only needs a terminal-status review streamed once, they're unneeded.
    ws_settings = settings.model_copy(update={"poller_enabled": False,
                                               "worker_enabled": False})

    # TestClient drives the ASGI app from its own background thread with its
    # own event loop. If the app reuses the session-scoped `engine` fixture
    # (whose asyncpg connections are bound to the main thread's loop),
    # pool_pre_ping's checkout ping fails cross-loop ("attached to a
    # different loop"). Give the app-under-test a dedicated engine so all of
    # its connections are opened on the portal thread's loop instead.
    from argus.db import get_engine
    ws_engine = get_engine(str(settings.database_url))
    app = create_app(settings=ws_settings, engine=ws_engine)

    try:
        def _sync():
            with TestClient(app) as client:
                with client.websocket_connect(f"/ws/reviews/{rid}") as ws:
                    msg = ws.receive_json()
                    assert msg["status"] == "done"
                    stage = msg["stages"][0]
                    assert stage["name"] == "scout"
                    assert "started_at" in stage and "finished_at" in stage
                    assert msg["active_llm_call"] is None
        await asyncio.to_thread(_sync)
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(ReviewStage).where(ReviewStage.review_id == rid))
            await conn.execute(delete(Review).where(Review.id == rid))
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))
        await ws_engine.dispose()


async def test_ws_surfaces_active_llm_call(engine, settings):
    """A running LLMRound (persisted by DBTraceCallback.on_llm_start before
    the LLM call finishes) must appear as active_llm_call in the WS snapshot
    — this is the mechanism that lets a slow-but-healthy review look
    'in progress' instead of frozen while a single long LLM call is still
    running."""
    import asyncio
    from datetime import datetime, timezone
    from starlette.testclient import TestClient
    from sqlalchemy import delete
    from argus.api.app import create_app
    from argus.db import session_factory
    from argus.domain.models import (LLMRound, MergeRequest, Repository,
                                        Review)

    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/trace-ws-active",
                          gitlab_project_id=90000005)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.flush()
        s.add(LLMRound(review_id=review.id, stage_name="scout", seq=1,
                       status="running", started_at=datetime.now(timezone.utc)))
        await s.commit()
        rid, mr_id, repo_id = review.id, mr.id, repo.id

    ws_settings = settings.model_copy(update={"poller_enabled": False,
                                               "worker_enabled": False})
    from argus.db import get_engine
    ws_engine = get_engine(str(settings.database_url))
    app = create_app(settings=ws_settings, engine=ws_engine)

    try:
        def _sync():
            with TestClient(app) as client:
                with client.websocket_connect(f"/ws/reviews/{rid}") as ws:
                    msg = ws.receive_json()
                    assert msg["status"] == "running"
                    assert msg["llm_rounds"] == 0  # running rounds don't count as completed
                    active = msg["active_llm_call"]
                    assert active is not None
                    assert active["stage"] == "scout"
                    assert active["elapsed_s"] is not None and active["elapsed_s"] >= 0
        await asyncio.to_thread(_sync)
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(LLMRound).where(LLMRound.review_id == rid))
            await conn.execute(delete(Review).where(Review.id == rid))
            await conn.execute(delete(MergeRequest).where(MergeRequest.id == mr_id))
            await conn.execute(delete(Repository).where(Repository.id == repo_id))
        await ws_engine.dispose()

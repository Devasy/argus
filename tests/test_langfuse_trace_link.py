"""reviews.langfuse_trace_id existed but was never written -- NULL for every
review ever run. That made Langfuse near-useless for the failures it was
deployed to explain: finding a dead review's trace meant matching timestamps
by hand against ClickHouse.

The id is a pure function of review_id, so it is knowable before any work
starts. It must be committed with the "running" transition, because a review
that dies mid-run is exactly the one whose trace someone will go looking for.
"""
import inspect

from argus.review import runner


def _source():
    return inspect.getsource(runner.execute_review_job)


def test_trace_id_is_assigned_to_the_review_row():
    assert "review.langfuse_trace_id = langfuse_run.trace_id" in _source()


def test_trace_id_is_committed_before_any_review_work():
    """Assigning it but committing only at the end would still leave a crashed
    review with a NULL column -- the exact case that needs the link."""
    src = _source()
    assign = src.index("review.langfuse_trace_id = langfuse_run.trace_id")
    commit = src.index("await s.commit()", assign)
    gitlab_call = src.index("await gitlab.get_merge_request")
    assert assign < commit < gitlab_call


def test_span_reuses_the_persisted_id():
    """If the span recomputed its own id, the DB column and the real trace
    could drift apart and the link would silently point at nothing. Trace-id
    generation now lives in LangfuseRun (shared with distillation/audit), so
    runner.py must not compute its own -- it reads langfuse_run.trace_id,
    the same value already assigned to the review row."""
    src = _source()
    assert "langfuse_trace_id = langfuse_run.trace_id" in src
    assert 'review_span_stack.enter_context(langfuse_run.span("review"))' in src
    assert "create_trace_id" not in src


def test_langfuse_run_span_reuses_its_own_trace_id():
    """The invariant test_span_reuses_the_persisted_id used to check inline in
    runner.py now lives in LangfuseRun itself: span() must trace_context onto
    self.trace_id, the same id start() handed back for DB persistence."""
    from argus.llm import langfuse_run as mod
    from argus.llm.langfuse_run import LangfuseRun
    src = inspect.getsource(LangfuseRun.span)
    assert 'trace_context={"trace_id": self.trace_id}' in src
    # Generation lives in _trace_id_for now; what matters is that the whole
    # module derives the id exactly once, so the DB and the trace cannot drift.
    assert inspect.getsource(mod).count("create_trace_id") == 1


def test_start_degrades_to_a_noop_when_langfuse_is_unreachable(monkeypatch):
    """Flagged by argus on its own MR !3, and correct: start() built the
    client and handler with no guard, and runner.py calls it before any review
    work -- so a Langfuse outage failed every review outright. Telemetry must
    never take the product down."""
    from argus.config import Settings
    from argus.llm.langfuse_run import LangfuseRun

    settings = Settings(database_url="postgresql+asyncpg://x/y",
                        gitlab_url="https://g.test", gitlab_token="t",
                        langfuse_enabled=True, langfuse_host="http://unreachable",
                        langfuse_public_key="pk", langfuse_secret_key="sk")

    import argus.llm.langfuse_run as mod

    def boom(*a, **kw):
        raise RuntimeError("langfuse host unreachable")
    monkeypatch.setattr(mod, "_build_client_and_handler", boom)

    run = LangfuseRun.start(settings, "review", "rid", session_id="s", tags=[])

    assert run.trace_id is None
    assert run.handler is None
    # and it must stay usable, not just constructible
    with run.span("review"):
        pass
    run.score("acceptance", 1.0)


def test_start_still_returns_a_live_run_when_langfuse_works(monkeypatch):
    from argus.config import Settings
    from argus.llm.langfuse_run import LangfuseRun
    import argus.llm.langfuse_run as mod

    class FakeClient:
        pass

    monkeypatch.setattr(mod, "_build_client_and_handler",
                        lambda s: (FakeClient(), object()))
    monkeypatch.setattr(mod, "_trace_id_for", lambda run_id: "abc123")
    settings = Settings(database_url="postgresql+asyncpg://x/y",
                        gitlab_url="https://g.test", gitlab_token="t",
                        langfuse_enabled=True)

    run = LangfuseRun.start(settings, "review", "rid", session_id="s", tags=["t"])
    assert run.trace_id == "abc123"
    assert run.handler is not None

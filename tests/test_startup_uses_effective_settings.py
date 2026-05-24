"""Background loops must be started from the EFFECTIVE settings.

audit_enabled, poller_enabled and worker_enabled are editable from the UI,
which writes to runtime_settings. Startup gated on the static Settings object
instead, so a toggle turned on in the UI wrote `true` to the database and
then never started its loop.

That is not hypothetical: auditing was enabled in production on 2026-07-31
and nine days later had produced zero audit_runs and zero audit_verdicts
against 345 learnings. The DB override was read only INSIDE the `if` it was
meant to satisfy.
"""
import inspect

from argus.api.app import create_app
from argus.settings_store import EDITABLE_KEYS


def _startup_source() -> str:
    """The startup hook is a closure inside create_app, so read it from the
    factory's source rather than trying to reach the inner function."""
    src = inspect.getsource(create_app)
    start = src.index("async def _startup()")
    end = src.index("@app.on_event(\"shutdown\")", start)
    return src[start:end]


def test_effective_settings_are_loaded_before_any_gate():
    """The load must precede the first `if`, or the gates see stale values."""
    body = _startup_source()
    load_at = body.index("load_effective_settings")
    first_gate = body.index("if getattr(")
    assert load_at < first_gate, "gates run before effective settings are loaded"


def test_toggleable_loops_gate_on_effective_settings():
    """Each UI-editable toggle must be read from the effective settings."""
    body = _startup_source()
    for key in ("audit_enabled", "poller_enabled", "worker_enabled"):
        assert f'getattr(boot, "{key}"' in body, f"{key} gates on static settings"
        assert f'getattr(settings, "{key}"' not in body


def test_the_toggles_are_actually_ui_editable():
    """If these stopped being editable the bug class would not exist, and this
    test should be deleted rather than silently kept passing."""
    assert "audit_enabled" in EDITABLE_KEYS
    assert "poller_enabled" in EDITABLE_KEYS


# An end-to-end variant (enable audit in the DB only, assert the loop starts)
# was tried and dropped: driving FastAPI's startup hook inside the async test
# fixtures requires stubbing asyncio.create_task, which fights the event loop
# and made the test flaky rather than informative. The three assertions above
# pin the actual defect -- gates reading `settings` instead of `boot` -- and
# would have caught it.


def test_manual_audit_records_a_returned_failure_as_failed():
    """run_audit_for_repo RETURNS {"status": "failed"} for a failed workspace
    clone rather than raising, so keying status purely off exceptions recorded
    "done" for a run that examined zero clusters -- indistinguishable from a
    repo whose learnings were all fine. Observed on audit_run
    68cd3025-5e4c-45cb-b969-51dcfda7fb14."""
    src = inspect.getsource(create_app)
    start = src.index("async def trigger_audit(")
    body = src[start:src.index("@router.get(\"/audit-verdicts\"", start)]
    assert 'result.get("status") == "failed"' in body
    assert 'status = "failed"' in body

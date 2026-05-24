"""tool_calls.args persistence.

on_tool_start received the tool's input but discarded it, and on_tool_end wrote
the row without ever setting args -- so every tool_calls row had args = NULL.
That gap is what made review 2bdf08a9 hard to diagnose: the trace showed ten
get_file_lines calls each returning ~20KB, but not WHICH ranges, so it was
impossible to tell from the data alone whether the agent was walking a file or
looping on the same range.
"""
import uuid

import pytest
import sqlalchemy as sa

from argus.domain.models import ToolCall
from argus.llm.trace import DBTraceCallback


async def _mk_run(db, engine, slug: str, project_id: int):
    from argus.domain.models import (DistillationRun, MergeRequest,
                                         Repository)
    repo = Repository(provider="gitlab", project_path=f"g/{slug}",
                      gitlab_project_id=project_id)
    db.add(repo); await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="merged",
                      source_branch="s", target_branch="m", head_sha="abc",
                      web_url="http://x")
    db.add(mr); await db.flush()
    run = DistillationRun(mr_id=mr.id, note_ids=[])
    db.add(run); await db.flush()
    run_id = run.id
    await db.commit()
    return run_id


async def test_tool_args_are_persisted(db, engine):
    """The args passed to on_tool_start must land on the tool_calls row."""
    from argus.db import session_factory

    sf = session_factory(engine)
    run_id = await _mk_run(db, engine, "tool-args", 90201)
    cb = DBTraceCallback(sf, "scout", distillation_run_id=run_id)

    rid = uuid.uuid4()
    payload = '{"requests": [{"path": "auth/x.py", "start": 1, "end": 300}]}'
    await cb.on_tool_start({}, payload, run_id=rid)
    await cb.on_tool_end("--- auth/x.py:1-300 ---\n1 | a", run_id=rid,
                         name="get_file_lines")

    async with sf() as s:
        row = (await s.execute(sa.select(ToolCall).where(
            ToolCall.distillation_run_id == run_id))).scalar_one()
    assert row.tool_name == "get_file_lines"
    assert row.args is not None, "args must not be NULL"
    assert "auth/x.py" in str(row.args)
    assert "300" in str(row.args)


async def test_distinct_ranges_are_distinguishable_across_calls(db, engine):
    """The diagnostic case: consecutive get_file_lines calls must be
    distinguishable so a walking-the-file pattern can be told apart from a
    stuck loop re-reading one range."""
    from argus.db import session_factory

    sf = session_factory(engine)
    run_id = await _mk_run(db, engine, "tool-args-seq", 90202)
    cb = DBTraceCallback(sf, "scout", distillation_run_id=run_id)

    for i in range(3):
        rid = uuid.uuid4()
        start = 1 + i * 300
        await cb.on_tool_start(
            {}, f'{{"requests": [{{"path": "a.py", "start": {start}, "end": {start + 299}}}]}}',
            run_id=rid)
        await cb.on_tool_end("lines", run_id=rid, name="get_file_lines")

    async with sf() as s:
        rows = (await s.execute(sa.select(ToolCall).where(
            ToolCall.distillation_run_id == run_id
        ).order_by(ToolCall.seq))).scalars().all()

    starts = [str(r.args) for r in rows]
    assert len(starts) == 3
    assert len(set(starts)) == 3, "each call's distinct range must be visible"
    assert "601" in starts[2]


async def test_tool_args_persisted_on_error(db, engine):
    """A failing tool call is exactly when the arguments matter most."""
    from argus.db import session_factory

    sf = session_factory(engine)
    run_id = await _mk_run(db, engine, "tool-args-err", 90203)
    cb = DBTraceCallback(sf, "scout", distillation_run_id=run_id)

    rid = uuid.uuid4()
    await cb.on_tool_start({}, '{"hunk_ids": ["h999"]}', run_id=rid)
    await cb.on_tool_error(RuntimeError("unknown hunk"), run_id=rid,
                           name="get_hunk")

    async with sf() as s:
        row = (await s.execute(sa.select(ToolCall).where(
            ToolCall.distillation_run_id == run_id))).scalar_one()
    assert row.status == "error"
    assert row.args is not None and "h999" in str(row.args)


async def test_oversized_args_are_truncated_not_dropped(db, engine):
    """Args must never be able to bloat the trace table unboundedly, but a
    huge payload should still leave evidence rather than reverting to NULL."""
    from argus.db import session_factory

    sf = session_factory(engine)
    run_id = await _mk_run(db, engine, "tool-args-big", 90204)
    cb = DBTraceCallback(sf, "scout", distillation_run_id=run_id)

    rid = uuid.uuid4()
    await cb.on_tool_start({}, "z" * 50_000, run_id=rid)
    await cb.on_tool_end("out", run_id=rid, name="get_hunk")

    async with sf() as s:
        row = (await s.execute(sa.select(ToolCall).where(
            ToolCall.distillation_run_id == run_id))).scalar_one()
    assert row.args is not None
    assert len(str(row.args)) < 50_000


async def test_tool_end_without_start_still_records_row(db, engine):
    """Defensive: a missing on_tool_start (dropped callback, resumed run) must
    not lose the tool_calls row entirely -- args simply stays unknown."""
    from argus.db import session_factory

    sf = session_factory(engine)
    run_id = await _mk_run(db, engine, "tool-args-nostart", 90205)
    cb = DBTraceCallback(sf, "scout", distillation_run_id=run_id)

    await cb.on_tool_end("out", run_id=uuid.uuid4(), name="get_hunk")

    async with sf() as s:
        row = (await s.execute(sa.select(ToolCall).where(
            ToolCall.distillation_run_id == run_id))).scalar_one()
    assert row.tool_name == "get_hunk"
    assert row.status == "ok"

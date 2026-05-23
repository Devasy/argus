from pathlib import Path

import pytest

import argus.knowledge.learnings as L
from argus.review.artifacts import FileChange, Hunk
from argus.review.tools import ToolContext, build_read_tools


def _ctx(tmp_path: Path) -> ToolContext:
    f1 = FileChange(file_id="f1", path="a.py", change_kind="modified",
                    hunk_ids=["h1", "h2"])
    f2 = FileChange(file_id="f2", path="b.py", change_kind="modified",
                    hunk_ids=["h3"])
    hunks = {
        "h1": Hunk(hunk_id="h1", file_id="f1", old_start=1, old_lines=1,
                   new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-x\n+y\n"),
        "h2": Hunk(hunk_id="h2", file_id="f1", old_start=5, old_lines=1,
                   new_start=5, new_lines=1, diff_text="@@ -5 +5 @@\n-p\n+q\n"),
        "h3": Hunk(hunk_id="h3", file_id="f2", old_start=1, old_lines=1,
                   new_start=1, new_lines=1, diff_text="@@ -1 +1 @@\n-m\n+n\n"),
    }
    (tmp_path / "a.py").write_text("l1\nl2\nl3\nl4\nl5\n")
    (tmp_path / "b.py").write_text("m1\nm2\nm3\n")
    return ToolContext(workspace=tmp_path, files_by_id={"f1": f1, "f2": f2},
                       hunks=hunks)


async def test_get_hunk_batches_multiple_hunk_ids(tmp_path):
    tools = build_read_tools(_ctx(tmp_path))
    get_hunk = next(t for t in tools if t.name == "get_hunk")
    out = await get_hunk.ainvoke({"hunk_ids": ["h1", "h2"]})
    assert "-x\n+y" in out
    assert "-p\n+q" in out
    assert "--- hunk h1" in out
    assert "--- hunk h2" in out


async def test_get_hunk_resolves_file_ids_to_member_hunks(tmp_path):
    tools = build_read_tools(_ctx(tmp_path))
    get_hunk = next(t for t in tools if t.name == "get_hunk")
    out = await get_hunk.ainvoke({"file_ids": ["f1"]})
    assert "-x\n+y" in out
    assert "-p\n+q" in out
    assert "-m\n+n" not in out


async def test_get_hunk_single_element_list_still_works(tmp_path):
    tools = build_read_tools(_ctx(tmp_path))
    get_hunk = next(t for t in tools if t.name == "get_hunk")
    out = await get_hunk.ainvoke({"hunk_ids": ["h3"]})
    assert "-m\n+n" in out


async def test_get_hunk_unknown_id_reports_inline_without_failing_others(tmp_path):
    tools = build_read_tools(_ctx(tmp_path))
    get_hunk = next(t for t in tools if t.name == "get_hunk")
    out = await get_hunk.ainvoke({"hunk_ids": ["h1", "hXX"]})
    assert "-x\n+y" in out
    assert "unknown hunk_id hXX" in out


async def test_get_file_lines_batches_multiple_ranges(tmp_path):
    tools = build_read_tools(_ctx(tmp_path))
    get_file_lines = next(t for t in tools if t.name == "get_file_lines")
    out = await get_file_lines.ainvoke({"requests": [
        {"path": "a.py", "start": 1, "end": 2},
        {"path": "b.py", "start": 1, "end": 1},
    ]})
    assert "--- a.py:1-2 ---" in out
    assert "1 | l1" in out and "2 | l2" in out
    assert "--- b.py:1-1 ---" in out
    assert "1 | m1" in out


async def test_get_file_lines_single_element_list_still_works(tmp_path):
    tools = build_read_tools(_ctx(tmp_path))
    get_file_lines = next(t for t in tools if t.name == "get_file_lines")
    out = await get_file_lines.ainvoke({"requests": [
        {"path": "a.py", "start": 1, "end": 3}]})
    assert "1 | l1" in out and "3 | l3" in out


async def test_get_file_lines_one_bad_request_does_not_fail_others(tmp_path):
    tools = build_read_tools(_ctx(tmp_path))
    get_file_lines = next(t for t in tools if t.name == "get_file_lines")
    out = await get_file_lines.ainvoke({"requests": [
        {"path": "a.py", "start": 1, "end": 1},
        {"path": "nope.py", "start": 1, "end": 1},
    ]})
    assert "1 | l1" in out
    assert "no such file nope.py" in out


async def test_get_file_lines_directory_path_does_not_crash_the_stage(tmp_path):
    """A directory passed as a file path used to raise IsADirectoryError
    straight out of the tool, uncaught -- which killed the whole review
    instead of telling the agent to pick a real file."""
    (tmp_path / "a_directory").mkdir()
    tools = build_read_tools(_ctx(tmp_path))
    get_file_lines = next(t for t in tools if t.name == "get_file_lines")
    out = await get_file_lines.ainvoke({"requests": [
        {"path": "a_directory", "start": 1, "end": 1},
    ]})
    assert "a_directory" in out
    assert "directory" in out.lower()


async def test_get_file_lines_directory_path_does_not_fail_other_ranges(tmp_path):
    (tmp_path / "a_directory").mkdir()
    tools = build_read_tools(_ctx(tmp_path))
    get_file_lines = next(t for t in tools if t.name == "get_file_lines")
    out = await get_file_lines.ainvoke({"requests": [
        {"path": "a.py", "start": 1, "end": 1},
        {"path": "a_directory", "start": 1, "end": 1},
    ]})
    assert "1 | l1" in out


def _write_skill(root: Path, name: str, description: str,
                 extra_docs: dict[str, str] | None = None) -> None:
    skill_dir = root / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n")
    for rel_path, content in (extra_docs or {}).items():
        doc_path = skill_dir / rel_path
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(content)


def test_discover_module_skills_finds_skill_md(tmp_path):
    from argus.review.tools import discover_module_skills
    _write_skill(tmp_path, "acme-threat-intel", "threat_intel guide for the module.")
    skills = discover_module_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0]["name"] == "acme-threat-intel"
    assert skills[0]["description"] == "threat_intel guide for the module."


def test_discover_module_skills_empty_when_absent(tmp_path):
    from argus.review.tools import discover_module_skills
    assert discover_module_skills(tmp_path) == []


async def test_list_module_skills_lists_name_and_description(tmp_path):
    from argus.review.tools import build_skill_tools, discover_module_skills
    _write_skill(tmp_path, "acme-threat-intel", "threat_intel guide for the module.")
    _write_skill(tmp_path, "acme-ticket-sync", "ticket_sync service_desk module guide.")
    skills = discover_module_skills(tmp_path)
    tools = build_skill_tools(tmp_path, skills)
    list_module_skills = next(t for t in tools if t.name == "list_module_skills")
    out = await list_module_skills.ainvoke({})
    assert "acme-threat-intel: threat_intel guide for the module." in out
    assert "acme-ticket-sync: ticket_sync service_desk module guide." in out


async def test_read_module_skill_returns_skill_md_by_default(tmp_path):
    from argus.review.tools import build_skill_tools, discover_module_skills
    _write_skill(tmp_path, "acme-threat-intel", "threat_intel guide.")
    skills = discover_module_skills(tmp_path)
    tools = build_skill_tools(tmp_path, skills)
    read_module_skill = next(t for t in tools if t.name == "read_module_skill")
    out = await read_module_skill.ainvoke({"name": "acme-threat-intel"})
    assert "# acme-threat-intel" in out


async def test_read_module_skill_reads_referenced_doc(tmp_path):
    from argus.review.tools import build_skill_tools, discover_module_skills
    _write_skill(tmp_path, "acme-threat-intel", "threat_intel guide.",
                extra_docs={"docs/domain-concepts.md": "# Domain concepts\nstatus machine details"})
    skills = discover_module_skills(tmp_path)
    tools = build_skill_tools(tmp_path, skills)
    read_module_skill = next(t for t in tools if t.name == "read_module_skill")
    out = await read_module_skill.ainvoke(
        {"name": "acme-threat-intel", "doc_path": "docs/domain-concepts.md"})
    assert "status machine details" in out


async def test_read_module_skill_unknown_name_returns_error_string(tmp_path):
    from argus.review.tools import build_skill_tools, discover_module_skills
    _write_skill(tmp_path, "acme-threat-intel", "threat_intel guide.")
    skills = discover_module_skills(tmp_path)
    tools = build_skill_tools(tmp_path, skills)
    read_module_skill = next(t for t in tools if t.name == "read_module_skill")
    out = await read_module_skill.ainvoke({"name": "does-not-exist"})
    assert "unknown skill does-not-exist" in out


async def test_read_module_skill_rejects_escaping_doc_path(tmp_path):
    from argus.review.tools import build_skill_tools, discover_module_skills
    _write_skill(tmp_path, "acme-threat-intel", "threat_intel guide.")
    skills = discover_module_skills(tmp_path)
    tools = build_skill_tools(tmp_path, skills)
    read_module_skill = next(t for t in tools if t.name == "read_module_skill")
    out = await read_module_skill.ainvoke(
        {"name": "acme-threat-intel", "doc_path": "../../../etc/passwd"})
    assert "escapes" in out


async def test_search_learnings_tool_returns_formatted_candidates(db, settings, monkeypatch):
    from argus.domain.models import Learning
    from argus.review.tools import build_learnings_search_tool

    async def fake_embed(text, settings_, is_query=False):
        return [0.5] * 768
    monkeypatch.setattr(L, "embed_text", fake_embed)

    # The tool opens its own session (a separate connection) via sf(), so the
    # fixture row must be committed rather than merely flushed for that other
    # connection to see it under READ COMMITTED isolation — commit here and
    # clean up manually afterward (same pattern as test_distiller.py).
    learning = Learning(repo_id=None, topic="null checks", hint_text="always null-check X",
                        kind="guidance", embedding=[0.5] * 768, hit_count=3, miss_count=1)
    db.add(learning)
    await db.commit()

    try:
        from argus.db import session_factory
        sf = session_factory(db.bind)
        tool = build_learnings_search_tool(sf, settings, repo_id=None)
        result = await tool.ainvoke({"query": "null checks"})
        assert "null checks" in result
        assert str(learning.id)[:8] in result
    finally:
        await db.delete(await db.get(Learning, learning.id))
        await db.commit()


async def test_upsert_learning_tool_create_and_update(db, settings, monkeypatch):
    from argus.domain.models import Learning
    from argus.review.tools import build_learnings_upsert_tool
    from sqlalchemy import select

    async def fake_embed(text, settings_, is_query=False):
        return [0.7] * 768
    monkeypatch.setattr(L, "embed_text", fake_embed)

    from argus.db import session_factory
    sf = session_factory(db.bind)
    tool = build_learnings_upsert_tool(sf, settings, repo_id=None)

    create_result = await tool.ainvoke({
        "action": "create", "topic": "topic A", "hint_text": "hint A",
        "kind": "guidance", "file_paths": ["x.py"]})
    assert "created" in create_result.lower() or "deduped" in create_result.lower()

    async with sf() as s:
        row = (await s.execute(select(Learning).where(Learning.topic == "topic A"))
              ).scalar_one()
        learning_id = str(row.id)

    update_result = await tool.ainvoke({
        "action": "update", "learning_id": learning_id,
        "topic": "topic A revised", "hint_text": "hint A revised", "kind": "guidance"})
    assert "updated" in update_result.lower()

    async with sf() as s:
        refreshed = await s.get(Learning, row.id)
        assert refreshed.topic == "topic A revised"

    # cleanup: the tool commits internally (not rolled back by the `db`
    # fixture), so remove the row to avoid leaking state into other tests
    # that share the session-scoped test database.
    async with sf() as s:
        await s.delete(await s.get(Learning, row.id))
        await s.commit()


async def test_upsert_learning_tool_bad_learning_id_returns_error_string(db, settings, monkeypatch):
    from argus.review.tools import build_learnings_upsert_tool

    async def fake_embed(text, settings_, is_query=False):
        return [0.7] * 768
    monkeypatch.setattr(L, "embed_text", fake_embed)

    from argus.db import session_factory
    sf = session_factory(db.bind)
    tool = build_learnings_upsert_tool(sf, settings, repo_id=None)

    result = await tool.ainvoke({
        "action": "update", "learning_id": "00000000-0000-0000-0000-000000000000",
        "topic": "x", "hint_text": "y", "kind": "guidance"})
    assert "no learning with id" in result.lower()

    result_malformed = await tool.ainvoke({
        "action": "update", "learning_id": "not-a-uuid",
        "topic": "x", "hint_text": "y", "kind": "guidance"})
    assert "no learning with id" in result_malformed.lower()


async def test_get_file_lines_missing_key_explains_itself(tmp_path):
    """A weak model omits keys. This raised KeyError('end') straight out of the
    tool, which burns a budgeted round and tells the agent nothing -- while the
    very next line already coerced bad int types helpfully. Recorded 13 times
    in tool_calls under the placeholder tool name."""
    tools = build_read_tools(_ctx(tmp_path))
    get_file_lines = next(t for t in tools if t.name == "get_file_lines")

    out = await get_file_lines.ainvoke({"requests": [
        {"path": "a.py", "start": 1},
        {"start": 1, "end": 2},
        {"path": "a.py", "start": 1, "end": 1},
    ]})
    assert "end" in out and "start" in out
    assert "1 | l1" in out, "a valid range in the same batch must still return"

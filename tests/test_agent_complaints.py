"""Agents can see problems with our tools, prompts and context that we cannot,
and until now had nowhere to put them.

Every bug found while investigating the 2026-08 review failures was already
known to an agent and buried in a `thinking` block: "graph_impact didn't give
me much useful information" (the call graph had zero edges) and "the hunk_ids
don't seem to be working" (a misleading error message). Both were correct
diagnoses that took a human reading Langfuse traces to surface.
"""
import logging
import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from argus.api.app import create_app
from argus.domain.models import AgentComplaint
from argus.review.pipeline import ALWAYS_AVAILABLE_TOOLS
from argus.review.tools import COMPLAINT_CATEGORIES, build_report_problem_tool


@pytest.fixture
def sf(engine):
    from argus.db import session_factory
    return session_factory(engine)


@pytest.fixture
async def seeded_review_id(sf):
    """A committed review to hang complaints off: review_id is a FK and the
    parent CHECK requires exactly one non-null parent."""
    from argus.domain.models import MergeRequest, Repository, Review

    async with sf() as s:
        repo = Repository(provider="gitlab",
                          project_path=f"g/complaints-test-{uuid.uuid4()}",
                          gitlab_project_id=70000000 + uuid.uuid4().int % 9000000)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.commit()
        rid = review.id
    yield rid
    async with sf() as s:
        await s.execute(delete(AgentComplaint).where(
            AgentComplaint.review_id == rid))
        await s.commit()


def _tool(sf, review_id, stage="scout"):
    return build_report_problem_tool(sf, review_id, stage)


# --- the tool ---------------------------------------------------------------

async def test_complaint_is_persisted_with_its_stage(sf, seeded_review_id):
    tool = _tool(sf, seeded_review_id, "scout")
    out = await tool.ainvoke({
        "category": "tool_broken", "target": "graph_impact",
        "detail": "reported 134/134 covered but every symbol had callers=[none]",
        "blocked": False})
    assert "recorded" in out.lower()

    async with sf() as s:
        row = (await s.execute(select(AgentComplaint).where(
            AgentComplaint.review_id == seeded_review_id))).scalars().one()
    assert row.category == "tool_broken"
    assert row.target == "graph_impact"
    assert row.stage_name == "scout"
    assert row.blocked is False
    assert "134/134" in row.detail


async def test_complaint_logs_a_warning(sf, seeded_review_id, caplog):
    """Loud at the moment it happens, the same way the zero-edge graph warning
    surfaced a bug that had silently degraded every review."""
    tool = _tool(sf, seeded_review_id)
    with caplog.at_level(logging.WARNING, logger="argus.tools"):
        await tool.ainvoke({"category": "prompt_irrelevant",
                            "target": "SCOUT_STATIC",
                            "detail": "instructions describe reviewing code but "
                                      "this stage only summarizes",
                            "blocked": True})
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "agent complaint" in msg
    assert "prompt_irrelevant" in msg and "SCOUT_STATIC" in msg


async def test_unknown_category_is_rejected_with_the_valid_list(sf, seeded_review_id):
    """The agent must be able to fix its own call without guessing."""
    out = await _tool(sf, seeded_review_id).ainvoke({
        "category": "everything_is_bad", "detail": "x"})
    assert "unknown category" in out
    for c in ("tool_broken", "prompt_irrelevant"):
        assert c in out
    async with sf() as s:
        assert (await s.execute(select(AgentComplaint))).scalars().all() == []


async def test_empty_detail_is_rejected(sf, seeded_review_id):
    """A complaint with no specifics cannot be acted on, so it is not worth
    storing -- "the graph is broken" tells us nothing."""
    out = await _tool(sf, seeded_review_id).ainvoke({
        "category": "tool_broken", "detail": "   "})
    assert "detail is required" in out


async def test_blocked_flag_is_recorded(sf, seeded_review_id):
    await _tool(sf, seeded_review_id).ainvoke({
        "category": "task_impossible",
        "detail": "no diff and no file access", "blocked": True})
    async with sf() as s:
        row = (await s.execute(select(AgentComplaint))).scalars().one()
    assert row.blocked is True


async def test_target_is_optional(sf, seeded_review_id):
    """Not every problem has a nameable culprit."""
    await _tool(sf, seeded_review_id).ainvoke({
        "category": "context_insufficient",
        "detail": "the MR description does not say what this is for"})
    async with sf() as s:
        row = (await s.execute(select(AgentComplaint))).scalars().one()
    assert row.target is None


def test_tool_description_is_short_and_complete():
    """This description is re-sent on EVERY model call, so its cost is paid per
    round, not per stage -- the first draft was ~330 tokens, roughly 12k per
    review for an optional feature. It must still name every category (or the
    agent cannot call it correctly) and scope itself to our tooling (or it gets
    mistaken for a way to report findings)."""
    doc = build_report_problem_tool(None, None, "scout").description
    flat = " ".join(doc.split())
    assert "not the code under review" in flat
    assert "Optional" in flat
    for category in COMPLAINT_CATEGORIES:
        assert category in doc
    assert len(doc) < 700, f"{len(doc)} chars is too expensive to re-send"


def test_prompt_mention_stays_a_two_liner():
    """Same reasoning at the prompt layer: this is optional, and should not
    read like a required workflow step."""
    from argus.review import stages

    block = stages._TOOL_ANTI_PATTERNS
    mention = block[block.find("Optional: if one of OUR"):]
    assert "report_problem" in mention
    assert len(mention) < 300, f"{len(mention)} chars is more than a two-liner"


def test_report_problem_survives_a_legacy_tool_allowlist():
    """The agents running custom, allowlist-bearing prompts are exactly the
    ones whose configuration is most likely to be wrong."""
    assert "report_problem" in ALWAYS_AVAILABLE_TOOLS


# --- the API ----------------------------------------------------------------

@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    async with engine.begin() as conn:
        await conn.execute(delete(AgentComplaint))


async def test_complaints_endpoint_rolls_up_by_target(api, sf, seeded_review_id):
    """The rollup is the point: one complaint about a tool is an agent having
    a bad day, forty is a bug."""
    tool = _tool(sf, seeded_review_id)
    for _ in range(3):
        await tool.ainvoke({"category": "tool_broken", "target": "graph_impact",
                            "detail": "no edges", "blocked": True})
    await tool.ainvoke({"category": "tool_missing", "target": "run_tests",
                        "detail": "cannot execute the suite"})

    r = await api.get("/complaints")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    top = body["by_target"][0]
    assert top["target"] == "graph_impact"
    assert top["count"] == 3
    assert top["blocked_count"] == 3


async def test_complaints_endpoint_filters(api, sf, seeded_review_id):
    tool = _tool(sf, seeded_review_id)
    await tool.ainvoke({"category": "tool_broken", "target": "graph_impact",
                        "detail": "no edges", "blocked": True})
    await tool.ainvoke({"category": "prompt_unclear", "target": "SCOUT_STATIC",
                        "detail": "ambiguous", "blocked": False})

    only_blocked = (await api.get("/complaints", params={"blocked": True})).json()
    assert only_blocked["total"] == 1
    assert only_blocked["items"][0]["target"] == "graph_impact"

    by_cat = (await api.get("/complaints",
                            params={"category": "prompt_unclear"})).json()
    assert by_cat["total"] == 1
    assert by_cat["items"][0]["target"] == "SCOUT_STATIC"


async def test_complaints_endpoint_rejects_unknown_category(api):
    r = await api.get("/complaints", params={"category": "nonsense"})
    assert r.status_code == 422


async def test_complaints_endpoint_is_empty_by_default(api):
    body = (await api.get("/complaints")).json()
    assert body["total"] == 0 and body["items"] == [] and body["by_target"] == []


# --- target field guidance (2026-08-11) -------------------------------------
# In production, every complaint came from the verify stage (14 of 14), and
# most set `target` to a finding_id ("rules_v2-reviewer6", "design1", "c1-a1")
# rather than the tool/prompt the docstring's one example implied ("what is
# at fault, e.g. 'graph_impact'"). That is not a misuse -- for
# context_insufficient/context_wrong, the finding_id IS the right target, but
# the docstring never said so, and an agent guessing from a tool-shaped
# example could reasonably describe the problem in prose instead. `blocked`
# was also false on every single row: agents always worked around the gap and
# still produced a real verdict, which is a good sign but means the field has
# never yet carried a "this actually failed" signal.

async def test_docstring_gives_per_category_target_guidance():
    """The tool's docstring is what the agent actually reads -- it must not
    lean on a single tool-shaped example for a field whose right value
    depends on the category. sf/review_id are only closed over, never called,
    so None is safe here -- this only inspects the built tool's docstring."""
    tool = _tool(None, None)
    doc = tool.coroutine.__doc__ or ""
    assert "context_insufficient" in doc and "finding_id" in doc
    assert "tool_broken" in doc and "tool name" in doc
    assert "not a description" in doc


async def test_docstring_clarifies_blocked_is_not_required_for_a_real_problem(sf,
                                                                              seeded_review_id):
    """Every production complaint had blocked=False while still reporting a
    real, worth-fixing gap -- the docstring must not imply blocked=True is
    the normal case, or agents may hesitate to report things they worked
    around."""
    tool = _tool(sf, seeded_review_id)
    doc = tool.coroutine.__doc__ or ""
    assert "Worked around it -> false" in doc


async def test_finding_id_as_target_is_a_valid_context_insufficient_report(
        sf, seeded_review_id):
    """This is the real, common shape from production -- not a misuse to
    guard against, but the expected one for this category."""
    tool = _tool(sf, seeded_review_id, "verify")
    out = await tool.ainvoke({
        "category": "context_insufficient", "target": "rules_v2-reviewer6",
        "detail": "could not verify whether the global handler in main.py "
                  "also logs the error, because main.py is outside scope",
        "blocked": False})
    assert "recorded" in out.lower()
    async with sf() as s:
        row = (await s.execute(select(AgentComplaint).where(
            AgentComplaint.review_id == seeded_review_id))).scalars().one()
    assert row.target == "rules_v2-reviewer6"
    assert row.category == "context_insufficient"

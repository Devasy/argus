"""The agent loop's only stop condition is the model calling its
structured-output tool. Nothing told the model how much budget it had, and
nothing stopped it re-fetching what it already had, so runs drifted into
GraphRecursionError -- 66 failed reviews producing no findings at all despite
having done most of the work. These tests pin the two guards that turn that
hard wall into a soft landing.
"""
import pytest
from langchain_core.messages import SystemMessage

from argus.review.stages import (FindingList, ScoutOutput, TestScenarioList,
                                     VerdictList, _RoundBudget, _ToolCallMemory)


class _Req:
    """Stands in for langchain's ModelRequest: only .system_message and
    .override(system_message=...) are exercised by _RoundBudget."""

    def __init__(self, system_message=None):
        self.system_message = system_message

    def override(self, **kw):
        self.system_message = kw.get("system_message", self.system_message)
        return self


async def _passthrough(req):
    return req


async def _nudges(max_rounds=10, warn_at=5, force_at=2, system=None):
    mw = _RoundBudget(max_rounds, warn_at=warn_at, force_at=force_at)
    out = []
    for _ in range(max_rounds):
        req = _Req(system)
        await mw.awrap_model_call(req, _passthrough)
        out.append(req.system_message.content if req.system_message else "")
    return out


@pytest.mark.asyncio
async def test_early_rounds_are_left_alone():
    """Nudging from round one would waste budget and rush good analysis."""
    nudges = await _nudges()
    assert nudges[0] == "" and nudges[3] == ""


@pytest.mark.asyncio
async def test_warning_then_hard_force_as_budget_runs_out():
    nudges = await _nudges()
    assert "Start converging" in nudges[5]
    assert "ROUND BUDGET EXHAUSTED" in nudges[8]
    # The model must be told an incomplete answer beats the recursion error,
    # or it keeps exploring in the hope of a better one.
    assert "better than no answer" in nudges[9]


@pytest.mark.asyncio
async def test_nudge_is_appended_not_substituted():
    """The stage's own instructions must survive the nudge."""
    nudges = await _nudges(system=SystemMessage("ORIGINAL STAGE PROMPT"))
    assert "ORIGINAL STAGE PROMPT" in nudges[9]
    assert "ROUND BUDGET EXHAUSTED" in nudges[9]


class _ToolReq:
    def __init__(self, name, args, id="call-1"):
        self.tool_call = {"name": name, "args": args, "id": id}

    def override(self, **kw):
        tc = kw.get("tool_call", self.tool_call)
        return _ToolReq(tc["name"], tc["args"], tc["id"])


@pytest.mark.asyncio
async def test_duplicate_call_is_short_circuited_not_re_executed():
    """Re-sending the payload re-pays its token cost and pushes the stage
    toward context overflow; a pointer costs almost nothing and tells the
    model its own loop is the problem."""
    mw = _ToolCallMemory()
    calls = []

    async def handler(req):
        calls.append(req.tool_call["args"])
        return "REAL_RESULT"

    first = await mw.awrap_tool_call(_ToolReq("get_hunk", {"file_ids": ["f84"]}), handler)
    second = await mw.awrap_tool_call(_ToolReq("get_hunk", {"file_ids": ["f84"]}), handler)

    assert first == "REAL_RESULT"
    assert "DUPLICATE CALL" in second.content
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_distinct_arguments_still_execute():
    """Walking a large file legitimately needs many calls -- only identical
    ones are suppressed."""
    mw = _ToolCallMemory()

    async def handler(req):
        return "REAL_RESULT"

    a = await mw.awrap_tool_call(_ToolReq("get_hunk", {"file_ids": ["f1"]}), handler)
    b = await mw.awrap_tool_call(_ToolReq("get_hunk", {"file_ids": ["f2"]}), handler)
    c = await mw.awrap_tool_call(_ToolReq("get_file_lines", {"file_ids": ["f1"]}), handler)
    assert a == b == c == "REAL_RESULT"


@pytest.mark.asyncio
async def test_argument_order_does_not_defeat_dedup():
    mw = _ToolCallMemory()

    async def handler(req):
        return "REAL_RESULT"

    await mw.awrap_tool_call(_ToolReq("get_file_lines", {"a": 1, "b": 2}), handler)
    dup = await mw.awrap_tool_call(_ToolReq("get_file_lines", {"b": 2, "a": 1}), handler)
    assert "DUPLICATE CALL" in dup.content


@pytest.mark.asyncio
async def test_duplicate_reply_carries_the_tool_call_id():
    """Without the matching id the provider rejects the whole turn."""
    mw = _ToolCallMemory()

    async def handler(req):
        return "REAL_RESULT"

    await mw.awrap_tool_call(_ToolReq("get_hunk", {"x": 1}, id="abc"), handler)
    dup = await mw.awrap_tool_call(_ToolReq("get_hunk", {"x": 1}, id="xyz"), handler)
    assert dup.tool_call_id == "xyz"


# --- sub-item dedup for batchable tools -------------------------------------
# get_hunk and get_file_lines let the model pack several sub-requests into ONE
# call to save round trips. That batching defeated whole-call dedup: a call
# bundling one already-fetched item with new ones has a novel overall
# signature every time, so it was never recognized as a repeat. In production
# this was exactly what got through -- scout re-requested lines 450-598 of a
# file bundled alongside two genuinely new files in the same get_file_lines
# call, and the whole three-item call sailed past the dedup check.

async def _tool_message_handler(req):
    from langchain_core.messages import ToolMessage
    return ToolMessage(content=f"RESULT for {req.tool_call['args']}",
                       tool_call_id=req.tool_call["id"])


@pytest.mark.asyncio
async def test_stale_item_bundled_with_new_ones_is_dropped_not_the_whole_call():
    mw = _ToolCallMemory()
    await mw.awrap_tool_call(
        _ToolReq("get_file_lines", {"requests": [
            {"path": "a.py", "start": 450, "end": 598}]}),
        _tool_message_handler)

    result = await mw.awrap_tool_call(
        _ToolReq("get_file_lines", {"requests": [
            {"path": "a.py", "start": 450, "end": 598},   # stale
            {"path": "b.py", "start": 1, "end": 100},      # new
            {"path": "c.py", "start": 1, "end": 50},       # new
        ]}),
        _tool_message_handler)

    assert "already fetched and dropped" in result.content
    assert "a.py:450-598" in result.content
    # the new items were still actually fetched
    assert "'path': 'b.py'" in result.content
    assert "'path': 'c.py'" in result.content
    # the stale item was not re-sent to the handler
    body_after_note = result.content.split("RESULT")[1]
    assert "'start': 450, 'end': 598" not in body_after_note


@pytest.mark.asyncio
async def test_get_hunk_dedupes_hunk_ids_and_file_ids_independently():
    mw = _ToolCallMemory()
    await mw.awrap_tool_call(
        _ToolReq("get_hunk", {"hunk_ids": ["h1", "h2"], "file_ids": []}),
        _tool_message_handler)

    result = await mw.awrap_tool_call(
        _ToolReq("get_hunk", {"hunk_ids": ["h1", "h3"], "file_ids": []}),
        _tool_message_handler)

    assert "hunk_id h1" in result.content
    assert "'h3'" in result.content
    assert "'h1'" not in result.content.split("RESULT")[1]


@pytest.mark.asyncio
async def test_batch_call_where_every_item_is_stale_is_a_pure_duplicate():
    """When nothing new survives the filter, this must behave exactly like
    whole-call dedup: no handler invocation, just the pointer."""
    mw = _ToolCallMemory()
    calls = 0

    async def counting_handler(req):
        nonlocal calls
        calls += 1
        return await _tool_message_handler(req)

    await mw.awrap_tool_call(
        _ToolReq("get_hunk", {"hunk_ids": ["h1", "h2"], "file_ids": []}),
        counting_handler)
    result = await mw.awrap_tool_call(
        _ToolReq("get_hunk", {"hunk_ids": ["h1", "h2"], "file_ids": []}),
        counting_handler)

    assert result.content.startswith("DUPLICATE CALL")
    assert calls == 1


@pytest.mark.asyncio
async def test_batch_dedup_does_not_affect_non_batch_tools():
    """Only get_hunk/get_file_lines are sub-item deduped; everything else
    keeps the original whole-call behaviour."""
    mw = _ToolCallMemory()

    async def handler(req):
        return "REAL_RESULT"

    await mw.awrap_tool_call(_ToolReq("list_changed_files", {}), handler)
    dup = await mw.awrap_tool_call(_ToolReq("list_changed_files", {}), handler)
    assert dup.content.startswith("DUPLICATE CALL")


@pytest.mark.asyncio
async def test_get_hunk_without_batchable_ids_falls_back_to_whole_call_dedup():
    """A malformed or unusual call with neither hunk_ids nor file_ids must
    still be caught by whole-call dedup rather than silently passing the
    sub-item check with nothing to compare."""
    mw = _ToolCallMemory()

    async def handler(req):
        return "REAL_RESULT"

    await mw.awrap_tool_call(_ToolReq("get_hunk", {"x": 1}), handler)
    dup = await mw.awrap_tool_call(_ToolReq("get_hunk", {"x": 1}), handler)
    assert dup.content.startswith("DUPLICATE CALL")


# --- truncated results must not be marked "fetched" -------------------------
# get_hunk/get_file_lines cap their own output size (MAX_TOOL_RESULT_CHARS in
# tools.py) and truncate silently past it. platform-reviewer, analyze:c2 and
# scout independently hit the same bug via report_problem on 2026-08-16: a
# get_hunk call whose result said "10 of 27 hunks omitted" still marked all
# requested ids as fetched, so every retry for the omitted ones came back
# DUPLICATE CALL -- "results are still in the conversation above" -- when
# those results were never shown at all.

async def _truncated_handler(req):
    from langchain_core.messages import ToolMessage
    from argus.review.tools import TRUNCATION_MARKER
    return ToolMessage(
        content=f"[truncated: 2 of 4 hunks {TRUNCATION_MARKER} the "
                f"24000-character tool-result limit.] RESULT for {req.tool_call['args']}",
        tool_call_id=req.tool_call["id"])


@pytest.mark.asyncio
async def test_truncated_get_hunk_does_not_block_a_narrower_retry():
    mw = _ToolCallMemory()

    first = await mw.awrap_tool_call(
        _ToolReq("get_hunk", {"file_ids": ["f6", "f7", "f8", "f10"]}),
        _truncated_handler)
    assert "truncated" in first.content

    # A narrower retry for the same file_ids must actually run, not be
    # rejected as a duplicate of a call whose result was never delivered.
    retry = await mw.awrap_tool_call(
        _ToolReq("get_hunk", {"file_ids": ["f8", "f10"]}),
        _tool_message_handler)
    assert "DUPLICATE CALL" not in retry.content
    assert "RESULT for" in retry.content


@pytest.mark.asyncio
async def test_exact_repeat_of_a_truncated_call_is_still_a_duplicate():
    """The one case that SHOULD still be blocked: asking for the identical
    over-sized batch again, which would just truncate the same way."""
    mw = _ToolCallMemory()
    calls = 0

    async def counting_truncated_handler(req):
        nonlocal calls
        calls += 1
        return await _truncated_handler(req)

    await mw.awrap_tool_call(
        _ToolReq("get_hunk", {"file_ids": ["f6", "f7", "f8", "f10"]}),
        counting_truncated_handler)
    dup = await mw.awrap_tool_call(
        _ToolReq("get_hunk", {"file_ids": ["f6", "f7", "f8", "f10"]}),
        counting_truncated_handler)

    assert dup.content.startswith("DUPLICATE CALL")
    assert calls == 1


@pytest.mark.parametrize("model", [ScoutOutput, FindingList, VerdictList,
                                   TestScenarioList])
def test_structured_output_tools_describe_themselves(model):
    """These reach the model as tools whose description is this docstring.
    They were empty, so the one tool that ENDS the loop was the only tool that
    never said when to call it -- agents reasoned to a complete answer and
    then never emitted it."""
    doc = model.__doc__ or ""
    assert "SUBMIT YOUR ANSWER" in doc
    assert len(doc) > 120


# --- proportional thresholds ----------------------------------------------
# Stage budgets differ by more than 8x (verify runs at max_rounds=3, analysis
# at 25). Fixed thresholds were wrong at both ends: warning 5 rounds out is
# half the run for a 10-round agent and a rounding error for a 25-round one.
# The design stage, at max_rounds=10, still hit GraphRecursionError on MR !52
# after this middleware shipped -- the warning fired at round 5 of 10, too
# early to pace with and too coarse to help.

@pytest.mark.parametrize("max_rounds", [3, 6, 10, 20, 25, 64])
def test_thresholds_scale_with_the_budget(max_rounds):
    b = _RoundBudget(max_rounds)
    # forcing must leave at least two rounds to comply: the forced turn can be
    # spent finishing an in-flight tool call, and one round is not a chance.
    assert b.force_at >= 2
    assert b.force_at < max_rounds
    # a warning, when there is room for one, must precede forcing
    assert b.warn_at == 0 or b.warn_at > b.force_at


@pytest.mark.parametrize("max_rounds,exploring", [(10, 5), (20, 12), (25, 16)])
def test_agents_get_room_to_explore_before_being_nudged(max_rounds, exploring):
    """The failure mode this guards against is nagging an agent through most
    of its budget, which wastes tokens and rushes the analysis."""
    b = _RoundBudget(max_rounds)
    assert max_rounds - b.warn_at >= exploring


def test_tiny_budget_drops_the_warning_instead_of_firing_it_immediately():
    """verify runs at max_rounds=3. Warning from round 1 would tell the agent
    to wrap up before it has read anything, so only the hard force remains."""
    b = _RoundBudget(3)
    assert b.warn_at == 0
    assert b.force_at >= 2


@pytest.mark.asyncio
async def test_zero_warn_at_never_emits_a_warning():
    """Guard the sentinel: `left <= 0` would otherwise match every round."""
    mw = _RoundBudget(3)
    seen = []
    for _ in range(3):
        req = _Req()
        await mw.awrap_model_call(req, _passthrough)
        seen.append(req.system_message.content if req.system_message else "")
    assert not any("Start converging" in s for s in seen)


@pytest.mark.asyncio
async def test_explicit_thresholds_still_win():
    """Callers can override; the proportional values are only defaults."""
    mw = _RoundBudget(20, warn_at=3, force_at=1)
    assert mw.warn_at == 3 and mw.force_at == 1


@pytest.mark.asyncio
async def test_design_sized_budget_warns_before_it_forces():
    """The concrete regression: max_rounds=10 must produce a usable
    explore -> warn -> force progression, not a warning at the halfway mark."""
    mw = _RoundBudget(10)
    phases = []
    for _ in range(10):
        req = _Req()
        await mw.awrap_model_call(req, _passthrough)
        c = req.system_message.content if req.system_message else ""
        phases.append("F" if "EXHAUSTED" in c else ("W" if "converging" in c else "."))
    assert phases.count(".") >= 5      # real exploration room
    assert "W" in phases               # a warning phase exists
    assert phases.index("W") < phases.index("F")


def test_recursion_limit_leaves_room_for_the_forced_convergence_window():
    """Every one of the 111 recursion-limit distillation failures used exactly
    12 LLM rounds against max_rounds=10 and recursion_limit=24 (12 rounds at
    ~2 steps each). _RoundBudget forces convergence with force_at rounds left
    and its own comment allows the model up to 2 rounds to comply, so the hard
    limit has to sit beyond max_rounds by more than that -- otherwise the
    limit fires first and GraphRecursionError leaves no chance to salvage an
    answer. The old +4 gave exactly 2 rounds of overshoot and every failure
    used all of it."""
    from argus.review.stages import _RoundBudget, recursion_limit_for

    for max_rounds in (3, 10, 20, 25):
        budget = _RoundBudget(max_rounds)
        allowed_rounds = recursion_limit_for(max_rounds) / 2
        # the model may keep going a little past max_rounds; it must not be
        # killed before it has had a real chance to comply
        assert allowed_rounds >= max_rounds + budget.force_at + 2, max_rounds


def test_recursion_limit_still_grows_with_the_budget():
    from argus.review.stages import recursion_limit_for

    assert recursion_limit_for(3) < recursion_limit_for(10) < recursion_limit_for(25)
    assert recursion_limit_for(20) > 24, "the old distiller value must be cleared"

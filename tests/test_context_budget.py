"""Context-window budget enforcement.

Regression tests for reviews failing with litellm.ContextWindowExceededError
(e.g. review 2bdf08a9, where scout's prompt grew ~10.5k tokens/round from
accumulated get_file_lines results: 15k -> 129k over nine rounds, and the
tenth request hit 140,837 tokens against a 131,072-token limit).

Two independent defects are covered:
  1. _DynamicMaxTokens only shrank max_tokens (the OUTPUT cap) and never
     refused/trimmed an oversized INPUT, so it sent max_tokens=1 requests
     that the provider rejected outright.
  2. get_file_lines had no cap on total bytes returned across ranges, so a
     single call could add ~20KB (~10k tokens) of history per round.
"""
import pytest

from argus.review.stages import ContextBudgetExceeded, _DynamicMaxTokens


class _FakeModel:
    def __init__(self, name="openai/claude-sonnet-4-6"):
        self.model = name


class _FakeMsg:
    def __init__(self, content, type_="user"):
        self.content = content
        self.type = type_


class _FakeSystem:
    def __init__(self, content):
        self.content = content


class _FakeRequest:
    def __init__(self, messages, system=None, model_settings=None):
        self.model = _FakeModel()
        self.messages = messages
        self.system_message = _FakeSystem(system) if system else None
        self.model_settings = model_settings or {}
        self.tools = []
        self.overrides = None

    def override(self, **kwargs):
        self.overrides = kwargs
        self.model_settings = kwargs.get("model_settings", self.model_settings)
        return self


def _req(approx_tokens: int):
    """A request whose message content measures ~approx_tokens under
    litellm.token_counter. "x " is one token per two chars for this tokenizer,
    so the count is driven directly rather than via a chars/4 estimate."""
    return _FakeRequest([_FakeMsg("x " * approx_tokens)])


async def test_max_tokens_shrinks_as_input_grows():
    """Baseline behaviour that must be preserved: while input still fits, the
    output cap is reduced to keep input + output inside the window."""
    mw = _DynamicMaxTokens(context_window=130_000, output_margin=4_000)
    called = {}

    async def handler(request):
        called["max_tokens"] = request.model_settings["max_tokens"]
        return "ok"

    await mw.awrap_model_call(_req(10_000), handler)
    small_input_cap = called["max_tokens"]
    await mw.awrap_model_call(_req(80_000), handler)
    large_input_cap = called["max_tokens"]

    assert small_input_cap > large_input_cap > 0


async def test_raises_before_sending_when_input_alone_exceeds_window():
    """The core defect: at ~140k input tokens against a 130k window the
    middleware used to compute max(1, negative) = 1 and send anyway, letting
    the provider raise ContextWindowExceededError. It must fail fast instead
    of issuing a request that cannot succeed."""
    mw = _DynamicMaxTokens(context_window=130_000, output_margin=4_000)
    sent = []

    async def handler(request):
        sent.append(request)
        return "ok"

    with pytest.raises(ContextBudgetExceeded):
        await mw.awrap_model_call(_req(140_000), handler)

    assert sent == [], "no request may be sent once input alone overflows"


async def test_raises_when_input_plus_margin_leaves_no_room_for_output():
    """Input under the window but within output_margin of it leaves no usable
    completion budget -- a max_tokens=1 request is never worth sending."""
    mw = _DynamicMaxTokens(context_window=130_000, output_margin=4_000)
    sent = []

    async def handler(request):
        sent.append(request)
        return "ok"

    # 127k tokens in: under the 130k window, but 130000-127000-4000 < 0.
    with pytest.raises(ContextBudgetExceeded):
        await mw.awrap_model_call(_req(127_000), handler)

    assert sent == []


async def test_error_reports_the_measured_overage():
    """The message must carry the numbers needed to diagnose which stage blew
    the budget and by how much, since the provider error alone did not say."""
    mw = _DynamicMaxTokens(context_window=130_000, output_margin=4_000)

    async def handler(request):
        return "ok"

    with pytest.raises(ContextBudgetExceeded) as ei:
        await mw.awrap_model_call(_req(140_000), handler)

    msg = str(ei.value)
    assert "130000" in msg or "130,000" in msg
    assert any(tok in msg for tok in ("input", "tokens"))


def test_get_file_lines_caps_total_output_across_ranges(tmp_path):
    """Defect 2: MAX_LINES capped each range, but a requests=[...] list of many
    ranges had no aggregate cap -- ten ~20KB results were what walked scout's
    prompt from 15k to 129k tokens."""
    import asyncio

    from argus.review.artifacts import FileChange
    from argus.review.tools import MAX_TOOL_RESULT_CHARS, ToolContext, build_read_tools

    big = tmp_path / "big.py"
    big.write_text("\n".join(f"line {i} " + "y" * 100 for i in range(1, 5001)))

    ctx = ToolContext(
        workspace=tmp_path,
        files_by_id={"f1": FileChange(file_id="f1", path="big.py",
                                     change_kind="modified")},
        hunks={})
    get_file_lines = [t for t in build_read_tools(ctx)
                      if t.name == "get_file_lines"][0]

    # 20 ranges x 300 lines x ~110 chars = ~660KB uncapped.
    reqs = [{"path": "big.py", "start": 1 + i * 300, "end": 300 + i * 300}
            for i in range(20)]
    out = asyncio.get_event_loop().run_until_complete(
        get_file_lines.ainvoke({"requests": reqs}))

    assert len(out) <= MAX_TOOL_RESULT_CHARS
    assert "truncated" in out.lower()

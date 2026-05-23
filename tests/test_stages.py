from types import SimpleNamespace

import pytest

from argus.review import stages
from argus.review.stages import NoStructuredResponseError, ScoutOutput


class _FakeAgent:
    """Stands in for the object create_agent() returns -- run_stage_agent
    only ever calls .ainvoke() on it, so that's all this needs to provide."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def ainvoke(self, input_, config=None):
        self.calls.append({"input": input_, "config": config})
        return self._results.pop(0)


def _no_tool_call_result(text="thinking out loud forever"):
    return {"messages": [SimpleNamespace(content=text)]}


def _structured_result(value):
    return {"messages": [SimpleNamespace(content="done")], "structured_response": value}


async def test_run_stage_agent_returns_structured_response_on_first_try(monkeypatch):
    scout_output = ScoutOutput(intent="i", mr_summary="s", file_summaries={})
    agent = _FakeAgent([_structured_result(scout_output)])
    monkeypatch.setattr("langchain.agents.create_agent", lambda *a, **k: agent)

    result = await stages.run_stage_agent(
        model=object(), tools=[], system_prompt="sys", user_msg="msg",
        response_model=ScoutOutput, max_rounds=5)

    assert result is scout_output
    assert len(agent.calls) == 1


async def test_run_stage_agent_retries_with_forced_convergence_directive(monkeypatch):
    """First attempt ends with no tool call (the observed production failure
    mode -- a reasoning model burns its budget on <think> and never calls
    the tool). The retry must be given the original user message, the first
    attempt's own messages, AND the forced-convergence directive -- and if
    THAT succeeds, run_stage_agent returns its structured_response without
    raising."""
    scout_output = ScoutOutput(intent="i", mr_summary="s", file_summaries={})
    first_attempt_result = _no_tool_call_result()
    agent = _FakeAgent([first_attempt_result, _structured_result(scout_output)])
    monkeypatch.setattr("langchain.agents.create_agent", lambda *a, **k: agent)

    result = await stages.run_stage_agent(
        model=object(), tools=[], system_prompt="sys", user_msg="the user message",
        response_model=ScoutOutput, max_rounds=5)

    assert result is scout_output
    assert len(agent.calls) == 2
    retry_messages = agent.calls[1]["input"]["messages"]
    assert retry_messages[0] == ("user", "the user message")
    assert retry_messages[-1][0] == "user"
    assert "Stop reasoning" in retry_messages[-1][1]
    # the first attempt's own (incomplete) RESULT messages are carried into
    # the retry, sandwiched between the original user message and the
    # directive -- not the first attempt's input, its output.
    assert retry_messages[1:-1] == first_attempt_result["messages"]


async def test_run_stage_agent_raises_no_structured_response_error_after_failed_retry(monkeypatch):
    """If the forced-convergence retry ALSO ends with no tool call,
    run_stage_agent must give up with NoStructuredResponseError (not a bare
    RuntimeError) -- this type is what unrecoverable_stage_errors() checks
    for, so callers like scout can catch it and degrade gracefully instead
    of failing the whole review."""
    agent = _FakeAgent([
        _no_tool_call_result("first attempt thinking"),
        _no_tool_call_result("retry also just thinking"),
    ])
    monkeypatch.setattr("langchain.agents.create_agent", lambda *a, **k: agent)

    with pytest.raises(NoStructuredResponseError) as exc_info:
        await stages.run_stage_agent(
            model=object(), tools=[], system_prompt="sys", user_msg="msg",
            response_model=ScoutOutput, max_rounds=5)

    assert len(agent.calls) == 2
    assert "first attempt thinking" in str(exc_info.value)
    assert "retry also just thinking" in str(exc_info.value)


def test_no_structured_response_error_is_an_unrecoverable_stage_error():
    assert NoStructuredResponseError in stages.unrecoverable_stage_errors()

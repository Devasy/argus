import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.domain.models import LLMRound, ToolCall

# Tool inputs are usually small (a few ids or ranges), but a tool could be
# called with a large literal payload -- cap what lands in the trace table.
MAX_TOOL_ARGS_CHARS = 4_000


class DBTraceCallback(AsyncCallbackHandler):
    """Persists llm_rounds and tool_calls for one review stage.

    on_llm_start writes a 'running' row immediately (not just in-memory) so a
    long-running call is visible to pollers/websockets while it's still in
    flight, rather than only appearing once it finishes or errors."""

    def __init__(self, sf: async_sessionmaker, stage_name: str, *,
                review_id: uuid.UUID | None = None,
                distillation_run_id: uuid.UUID | None = None,
                audit_run_id: uuid.UUID | None = None):
        parent_count = sum(
            p is not None for p in (review_id, distillation_run_id, audit_run_id))
        if parent_count != 1:
            raise ValueError(
                "DBTraceCallback requires exactly one of review_id, "
                "distillation_run_id, audit_run_id")
        self._sf = sf
        self._review_id = review_id
        self._distillation_run_id = distillation_run_id
        self._audit_run_id = audit_run_id
        self._stage = stage_name
        self._seq = 0
        self._starts: dict[Any, float] = {}
        self._round_ids: dict[Any, uuid.UUID] = {}
        # on_tool_start receives the tool input but the row is only written in
        # on_tool_end, so the args have to be carried across by run_id --
        # without this every tool_calls.args was NULL, hiding *which* ranges a
        # stage was reading when its context window overflowed.
        self._tool_args: dict[Any, str] = {}

    async def on_llm_start(self, serialized, prompts, *, run_id, **kw):
        self._starts[run_id] = time.monotonic()
        self._seq += 1
        round_id = uuid.uuid4()
        self._round_ids[run_id] = round_id
        async with self._sf() as s:
            s.add(LLMRound(id=round_id, review_id=self._review_id,
                           distillation_run_id=self._distillation_run_id,
                           audit_run_id=self._audit_run_id,
                           stage_name=self._stage, seq=self._seq,
                           status="running",
                           started_at=datetime.now(timezone.utc)))
            await s.commit()

    async def on_llm_end(self, response, *, run_id, **kw):
        elapsed = int((time.monotonic() - self._starts.pop(run_id, time.monotonic())) * 1000)
        usage = {}
        try:
            usage = response.llm_output.get("token_usage") or {}
        except Exception:
            pass
        round_id = self._round_ids.pop(run_id, None)
        async with self._sf() as s:
            row = await s.get(LLMRound, round_id) if round_id else None
            if row is None:
                row = LLMRound(id=round_id, review_id=self._review_id,
                               distillation_run_id=self._distillation_run_id,
                               audit_run_id=self._audit_run_id,
                               stage_name=self._stage, seq=self._seq)
                s.add(row)
            row.status = "done"
            row.model = (response.llm_output or {}).get("model_name")
            row.prompt_tokens = usage.get("prompt_tokens", 0)
            row.completion_tokens = usage.get("completion_tokens", 0)
            row.latency_ms = elapsed
            await s.commit()

    async def on_llm_error(self, error, *, run_id, **kw):
        round_id = self._round_ids.pop(run_id, None)
        self._starts.pop(run_id, None)
        async with self._sf() as s:
            row = await s.get(LLMRound, round_id) if round_id else None
            if row is None:
                self._seq += 1
                row = LLMRound(id=round_id, review_id=self._review_id,
                               distillation_run_id=self._distillation_run_id,
                               audit_run_id=self._audit_run_id,
                               stage_name=self._stage, seq=self._seq)
                s.add(row)
            row.status = "failed"
            row.error = str(error)[:2000]
            await s.commit()

    async def on_tool_start(self, serialized, input_str, *, run_id, **kw):
        self._starts[run_id] = time.monotonic()
        self._tool_args[run_id] = str(input_str)[:MAX_TOOL_ARGS_CHARS]

    def _args_for(self, run_id) -> dict | None:
        """None (SQL NULL) only when on_tool_start never fired for this run_id
        -- distinguishable from a call that genuinely had no arguments."""
        raw = self._tool_args.pop(run_id, None)
        return None if raw is None else {"input": raw}

    async def on_tool_end(self, output, *, run_id, **kw):
        elapsed = int((time.monotonic() - self._starts.pop(run_id, time.monotonic())) * 1000)
        self._seq += 1
        async with self._sf() as s:
            s.add(ToolCall(review_id=self._review_id,
                           distillation_run_id=self._distillation_run_id,
                           audit_run_id=self._audit_run_id,
                           stage_name=self._stage,
                           seq=self._seq, tool_name=kw.get("name") or "tool",
                           args=self._args_for(run_id),
                           result={"output": str(output)[:20000]},
                           status="ok", duration_ms=elapsed))
            await s.commit()

    async def on_tool_error(self, error, *, run_id, **kw):
        self._starts.pop(run_id, None)
        self._seq += 1
        async with self._sf() as s:
            s.add(ToolCall(review_id=self._review_id,
                           distillation_run_id=self._distillation_run_id,
                           audit_run_id=self._audit_run_id,
                           stage_name=self._stage,
                           seq=self._seq, tool_name=kw.get("name") or "tool",
                           args=self._args_for(run_id),
                           result={"error": str(error)[:2000]}, status="error"))
            await s.commit()

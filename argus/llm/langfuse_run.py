import logging
from contextlib import nullcontext
from typing import Literal

from argus.config import Settings

logger = logging.getLogger("argus.langfuse_run")

RunType = Literal["review", "distillation", "audit"]


def _build_client_and_handler(settings: Settings):
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler
    client = Langfuse(public_key=settings.langfuse_public_key,
                      secret_key=settings.langfuse_secret_key,
                      base_url=settings.langfuse_host)
    return client, CallbackHandler(public_key=settings.langfuse_public_key)


def _trace_id_for(run_id) -> str:
    from langfuse import Langfuse
    return Langfuse.create_trace_id(seed=str(run_id))


class LangfuseRun:
    """One Langfuse trace for a review/distillation/audit run.

    Centralizes what runner.py and auditor.py used to each hand-roll: trace
    id (computed up front so it can be persisted to the DB before any work
    starts), the LangChain callback handler, session/user/tag metadata, and
    a safe create_score wrapper. Callers no longer branch on
    settings.langfuse_enabled -- start() returns a no-op instance instead."""

    def __init__(self, client, trace_id: str | None, handler,
                metadata: dict):
        self._client = client
        self.trace_id = trace_id
        self.handler = handler
        self.metadata = metadata

    @classmethod
    def start(cls, settings: Settings, run_type: RunType, run_id,
             session_id: str, tags: list[str], user_id: str | None = None,
             metadata: dict | None = None) -> "LangfuseRun":
        if not settings.langfuse_enabled or run_id is None:
            return cls(None, None, None, {})
        try:
            client, handler = _build_client_and_handler(settings)
            trace_id = _trace_id_for(run_id)
        except Exception:
            # Telemetry must never take a review down with it.
            logger.warning("langfuse unavailable; running untraced",
                           exc_info=True)
            return cls(None, None, None, {})
        full_metadata = {
            "langfuse_session_id": session_id,
            "langfuse_tags": tags,
            "run_type": run_type,
            **(metadata or {}),
        }
        if user_id is not None:
            full_metadata["langfuse_user_id"] = user_id
        return cls(client, trace_id, handler, full_metadata)

    def span(self, name: str):
        if self._client is None:
            return nullcontext()
        return self._client.start_as_current_observation(
            as_type="span", name=name, trace_context={"trace_id": self.trace_id})

    def score(self, name: str, value, data_type: str = "NUMERIC",
             comment: str | None = None) -> None:
        if self._client is None:
            return
        try:
            self._client.create_score(
                trace_id=self.trace_id, name=name, value=value,
                data_type=data_type, comment=comment)
        except Exception:
            logger.warning("failed to push %s score to langfuse for trace %s",
                           name, self.trace_id, exc_info=True)

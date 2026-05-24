"""DB-backed overlay for runtime-tunable settings.

Precedence: runtime_settings row > env var > code default.
Secrets never enter this table and are never returned to callers.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.config import Settings
from argus.domain.models import RuntimeSetting

SECRET_KEYS: tuple[str, ...] = (
    "gitlab_token", "api_token", "distiller_api_key",
    "langfuse_public_key", "langfuse_secret_key",
)

# Settings field -> expected python type. Everything editable from the UI.
EDITABLE_KEYS: dict[str, type] = {
    "gitlab_url": str, "gitlab_ssl_verify": bool,
    "embedding_model": str, "ollama_base_url": str,
    "distiller_model": str, "distiller_api_base": str,
    "poll_interval_s": int, "poller_enabled": bool,
    "log_level": str,
    "scout_max_rounds": int, "analysis_max_rounds": int,
    "verify_max_rounds": int, "qa_scenarios_max_rounds": int,
    "distiller_max_rounds": int,
    "reasoning_budget_tokens": int,
    "max_inline_comments": int, "max_chunk_files": int,
    "review_token_ceiling": int, "review_llm_timeout_s": int,
    "model_context_window": int, "model_output_margin": int,
    "langfuse_enabled": bool, "langfuse_host": str,
    "audit_enabled": bool, "audit_interval_days": int,
    "audit_max_clusters": int, "audit_auto_apply": bool,
}

# Integer settings with a meaningful floor. A value below this could break
# poller/review behavior (e.g. poll_interval_s=0 causes a busy-loop against
# GitLab; review_token_ceiling too low aborts every review; *_max_rounds=0
# runs zero analysis rounds). Mirrors the >=10 floor update_repository
# already enforces for the per-repo poll_interval_s field.
EDITABLE_MINIMUMS: dict[str, int] = {
    "poll_interval_s": 10,
    "scout_max_rounds": 1,
    "analysis_max_rounds": 1,
    "verify_max_rounds": 1,
    "qa_scenarios_max_rounds": 1,
    "distiller_max_rounds": 1,
    "reasoning_budget_tokens": 256,
    "max_inline_comments": 0,
    "max_chunk_files": 1,
    "review_token_ceiling": 1000,
    "review_llm_timeout_s": 60,
    "model_context_window": 8000,
    "model_output_margin": 0,
    "audit_interval_days": 1,
    "audit_max_clusters": 1,
}


def _check(key: str, value):
    if key in SECRET_KEYS:
        raise ValueError(f"{key} is a secret and cannot be set via the API")
    expected = EDITABLE_KEYS.get(key)
    if expected is None:
        raise ValueError(f"unknown setting: {key}")
    # bool is a subclass of int — check bool explicitly first
    if expected is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{key} expects a boolean")
    elif expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} expects an integer")
    elif not isinstance(value, expected):
        raise ValueError(f"{key} expects {expected.__name__}")

    minimum = EDITABLE_MINIMUMS.get(key)
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")


async def _rows(session: AsyncSession) -> dict[str, object]:
    result = await session.execute(select(RuntimeSetting))
    return {r.key: r.value["v"] for r in result.scalars()
            if r.key in EDITABLE_KEYS}


async def load_effective_settings(session: AsyncSession,
                                  base: Settings | None = None) -> Settings:
    base = base or Settings()
    overrides = await _rows(session)
    if not overrides:
        return base
    return base.model_copy(update=overrides)


async def read_settings_view(session: AsyncSession, base: Settings) -> dict:
    overrides = await _rows(session)
    values = {k: overrides.get(k, getattr(base, k)) for k in EDITABLE_KEYS}
    return {
        "values": values,
        "overridden": sorted(overrides),
        "secrets": {k: bool(getattr(base, k)) for k in SECRET_KEYS},
    }


async def apply_settings(session: AsyncSession, changes: dict) -> None:
    for key, value in changes.items():
        _check(key, value)
    for key, value in changes.items():
        row = await session.get(RuntimeSetting, key)
        if row is None:
            session.add(RuntimeSetting(key=key, value={"v": value}))
        else:
            row.value = {"v": value}

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARGUS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://argus:argus@localhost:5433/argus"
    api_token: str = ""
    # Separate from api_token: gates the admin-only stats routes (per-user
    # stats, reviewer graph, ops tiles) so the regular app token doesn't also
    # grant access to org-wide people data.
    admin_token: str = ""
    gitlab_url: str = ""
    gitlab_token: str = ""
    gitlab_ca_bundle: str | None = None
    gitlab_ssl_verify: bool = True
    github_token: str = ""
    github_app_id: str = ""
    github_installation_id: str = ""
    github_app_private_key_path: str = ""
    # Conservative local budgets. Set these below your project's AI Studio limits.
    gemini_free_rpm: int = 5
    gemini_free_tpm: int = 16000
    gemini_free_rpd: int = 100

    embedding_model: str = "ollama/nomic-embed-text"
    embedding_dim: int = 768
    embedding_api_key_ref: str = "GEMINI_API_KEY"
    repository_knowledge_only: bool = False
    ollama_base_url: str = "http://localhost:11434"
    validate_embeddings_on_boot: bool = False

    distiller_model: str = "openai/claude-sonnet-4-6"
    distiller_api_base: str = ""
    distiller_api_key: str = ""

    poll_interval_s: int = 120
    poller_enabled: bool = False
    worker_enabled: bool = False
    # JSON list of {"id", "kinds", "endpoint"?}; empty = one worker for every kind (see jobs/workers.py).
    worker_specs: str = ""
    # Incremental re-reviews check earlier bot comments: off | reply_only (say "looks fixed") | resolve.
    followup_mode: str = "reply_only"
    # Diff tokens per analysis chunk; 0 = auto (model_context_window // 5). Large files split by hunks.
    chunk_token_budget: int = 0
    workspace_root: str = "~/.argus/repos"

    log_level: str = "INFO"
    log_dir: str | None = None
    log_max_bytes: int = 10_000_000
    log_backup_count: int = 5

    scout_max_rounds: int = 20
    analysis_max_rounds: int = 25
    verify_max_rounds: int = 3
    # The distiller searches learnings for every point and reads code, so it
    # needs a budget like scout's, not the hardcoded 10 it used to get.
    distiller_max_rounds: int = 20
    # hours without a new human note before an open, unresolved thread is distilled; 0 = immediately
    distill_quiet_hours: int = 24
    qa_scenarios_max_rounds: int = 10
    # Budget for generate_test_scenario, the on-demand tool analyze_chunk (and
    # the specialist agents it invokes) can call mid-review when a specific
    # change looks risky. Smaller than qa_scenarios_max_rounds: this run is
    # scoped to ONE named concern, not a whole chunk's worth of behavior.
    qa_scenario_max_rounds: int = 10
    # Sent as reasoning_budget_tokens (+ chat_template_kwargs.enable_thinking)
    # to ollama-provider endpoints (self-hosted reasoning models, e.g. Qwen3
    # via llama.cpp) -- forces the model to stop reasoning and emit its
    # structured-output tool call before burning the whole output budget on
    # a <think> block. See argus/llm/factory.py.
    reasoning_budget_tokens: int = 8192
    max_inline_comments: int = 5
    max_chunk_files: int = 8
    # Hybrid learning retrieval: a BM25 arm over the whole active corpus fused
    # with the vector arm by RRF, queried from scout's file summaries. Measured
    # at recall@8 18/44 vs the vector-only path's 11/44 on the gold standard
    # (see docs/superpowers/plans/2026-09-10-hybrid-retrieval-results.md).
    # False restores the pre-2026-09-10 vector-only path with no code change.
    retrieval_hybrid: bool = True
    review_token_ceiling: int = 500_000
    review_llm_timeout_s: int = 1800
    model_context_window: int = 130_000
    model_output_margin: int = 4_000

    langfuse_enabled: bool = False
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    audit_enabled: bool = False
    audit_interval_days: int = 7
    audit_auto_apply: bool = False
    # learnings checked per audit run; the scheduler keeps queueing runs while more are due
    audit_learnings_per_run: int = 40
    # concurrent model calls per audit run; GPU 2 has 2 slots and distillation uses one
    audit_max_parallel: int = 1
    # verify-confirmed archives at or above this confidence (percent) apply without a human
    audit_auto_archive_min_pct: int = 80


@lru_cache
def get_settings() -> Settings:
    return Settings()

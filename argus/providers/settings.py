"""Repository overrides keep an OSS pilot isolated in a shared installation."""
from argus.domain.models import Repository


def require_free_pilot(config):
    gemini = config.provider == "gemini" and config.model.startswith("gemini/") and not config.api_base
    openrouter = (config.provider == "openai"
                  and (config.api_base or "").rstrip("/") == "https://openrouter.ai/api/v1"
                  and config.model.startswith("openai/") and config.model.endswith(":free"))
    if config.fallback is not None or not (gemini or openrouter):
        raise ValueError("GitHub pilot requires Gemini or an explicit OpenRouter free model without fallback")
    config.free_only = True
    config.num_retries = 0


def configure_quota(config, settings):
    if config.free_only:
        config.quota_database_url = settings.database_url
        # OpenRouter accounts without purchased credits allow 50 free calls daily.
        daily = min(settings.gemini_free_rpd, 50) if config.provider == "openai" else settings.gemini_free_rpd
        config.quota_limits = (settings.gemini_free_rpm, settings.gemini_free_tpm, daily)


async def settings_for_repository(session, settings, repo_id):
    repo = await session.get(Repository, repo_id) if repo_id else None
    if repo is None or repo.provider != "github":
        return settings
    embedding = repo.embedding_config or {}
    return settings.model_copy(update={
        "embedding_model": embedding.get("model", "gemini-embedding-2"),
        "embedding_dim": embedding.get("dim", 768),
        "embedding_api_key_ref": embedding.get("api_key_ref", "GEMINI_API_KEY"),
        "repository_knowledge_only": True, "followup_mode": "off",
        "audit_max_parallel": 1,
        "model_context_window": min(settings.model_context_window, settings.gemini_free_tpm)})

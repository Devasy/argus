"""Repository overrides keep an OSS pilot isolated in a shared installation."""
from argus.domain.models import Repository


def require_gemini(config):
    if (config.provider != "gemini" or config.fallback is not None
            or not config.model.startswith("gemini/") or config.api_base):
        raise ValueError("GitHub pilot requires Gemini without a fallback endpoint")
    config.free_only = True
    config.num_retries = 0


def configure_quota(config, settings):
    if config.free_only:
        config.quota_database_url = settings.database_url
        config.quota_limits = (settings.gemini_free_rpm, settings.gemini_free_tpm, settings.gemini_free_rpd)


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

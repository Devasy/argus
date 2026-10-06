"""Repository overrides keep an OSS pilot isolated in a shared installation."""
from argus.domain.models import Repository, LLMEndpoint


def require_pilot_endpoint(config):
    if config.provider == "vertex_ai":
        from argus.llm.vertex import validate_vertex
        validate_vertex(config)
        config.num_retries = 0
        return
    gemini = config.provider == "gemini" and config.model.startswith("gemini/") and not config.api_base
    openrouter = (config.provider == "openai"
                  and (config.api_base or "").rstrip("/") == "https://openrouter.ai/api/v1"
                  and config.model.startswith("openai/") and config.model.endswith(":free"))
    if config.fallback is not None or not (gemini or openrouter):
        raise ValueError("GitHub pilot requires Gemini or an explicit OpenRouter free model without fallback")
    config.free_only = True
    config.num_retries = 0


def configure_quota(config, settings):
    if config.free_only or config.provider == "vertex_ai":
        config.quota_database_url = settings.database_url
        if config.provider == "vertex_ai":
            config.quota_limits = (settings.vertex_rpm, settings.vertex_tpm, settings.vertex_rpd)
        elif config.provider == "openai":
            config.quota_limits = (settings.openrouter_free_rpm, settings.openrouter_free_tpm,
                                   min(settings.openrouter_free_rpd, 50))
        else:
            config.quota_limits = (settings.gemini_free_rpm, settings.gemini_free_tpm, settings.gemini_free_rpd)



async def settings_for_repository(session, settings, repo_id):
    repo = await session.get(Repository, repo_id) if repo_id else None
    if repo is None or repo.provider != "github":
        return settings
    embedding = repo.embedding_config or {}
    endpoint = await session.get(LLMEndpoint, repo.default_llm_endpoint_id) if repo.default_llm_endpoint_id else None
    budget = settings.openrouter_free_tpm if endpoint and (endpoint.base_url or "").rstrip("/") == "https://openrouter.ai/api/v1" else settings.gemini_free_tpm
    if endpoint and getattr(endpoint, "provider", None) == "vertex_ai":
        budget = settings.vertex_tpm
    return settings.model_copy(update={
        "embedding_model": embedding.get("model", "gemini-embedding-2"),
        "embedding_dim": embedding.get("dim", 768),
        "embedding_api_key_ref": embedding.get("api_key_ref", "GEMINI_API_KEY"),
        "repository_knowledge_only": True, "followup_mode": "off",
        "audit_max_parallel": 1,
        "model_context_window": min(settings.model_context_window, budget)})

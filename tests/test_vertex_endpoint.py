from types import SimpleNamespace
import pytest
from langchain_core.messages import AIMessage
from argus.llm.config import LLMConfig
from argus.llm.factory import build_chat_model, _GeminiChatLiteLLM
from argus.llm.vertex import VERTEX_API_BASE
from argus.providers.settings import require_pilot_endpoint, configure_quota, settings_for_repository
from argus.config import Settings
from argus.domain.models import Repository


def config(**updates):
    return LLMConfig(provider="vertex_ai", model="vertex_ai/gemini-3.8-flash",
                     api_key="test-key", **updates)


def test_vertex_reuses_gemini_transport_and_keeps_signature():
    cfg = config()
    model = build_chat_model(cfg)
    assert isinstance(model, _GeminiChatLiteLLM)
    assert model.model == "gemini/gemini-3.8-flash"
    assert model.api_base == VERTEX_API_BASE
    assert model.max_tokens == 16384
    assert cfg.model == "vertex_ai/gemini-3.8-flash"
    message = AIMessage(content="", tool_calls=[{"id": "call", "name": "probe", "args": {}}],
                        additional_kwargs={"tool_calls": [{"id": "call", "provider_specific_fields": {"thought_signature": "signature"}}]})
    wire, _ = model._create_message_dicts([message], None)
    assert wire[0]["tool_calls"][0]["provider_specific_fields"]["thought_signature"] == "signature"
    assert "api_key" not in cfg.model_dump()


@pytest.mark.parametrize("updates", [
    {"api_base": "https://evil.example/v1/publishers/google"},
    {"api_base": VERTEX_API_BASE + "?key=secret"},
    {"model": "openai/paid"},
    {"free_only": True},
    {"fallback": {"model": "openai/paid"}},
])
def test_vertex_rejects_wrong_route_or_fallback(updates):
    cfg = config().model_copy(update=updates)
    with pytest.raises(ValueError):
        build_chat_model(cfg)


def test_vertex_pilot_has_separate_budget():
    cfg = config()
    require_pilot_endpoint(cfg)
    configure_quota(cfg, Settings(gemini_free_tpm=1000))
    assert cfg.free_only is False
    assert cfg.num_retries == 0
    assert cfg.quota_limits == (10, 262144, 1000)
    assert build_chat_model(cfg).quota_database_url == cfg.quota_database_url


async def test_vertex_repository_preserves_full_configured_context():
    repo = SimpleNamespace(provider="github", embedding_config={}, default_llm_endpoint_id="endpoint")
    endpoint = SimpleNamespace(provider="vertex_ai", base_url=VERTEX_API_BASE)
    class Session:
        async def get(self, model, identifier):
            return repo if model is Repository else endpoint
    scoped = await settings_for_repository(Session(), Settings(model_context_window=130000), "repo")
    assert scoped.model_context_window == 130000
    assert scoped.repository_knowledge_only is True


async def test_vertex_health_avoids_local_server_probes():
    from argus.llm.health import check_llm_health, resolve_served_model
    assert await check_llm_health(config(api_base=VERTEX_API_BASE)) is True
    assert await resolve_served_model(config(api_base=VERTEX_API_BASE)) == "vertex_ai/gemini-3.8-flash"


async def test_pinned_vertex_context_overrides_repository_free_default():
    repo = SimpleNamespace(provider="github", embedding_config={}, default_llm_endpoint_id="free")
    default = SimpleNamespace(provider="gemini", base_url=None)
    class Session:
        async def get(self, model, identifier):
            return repo if model is Repository else default
    settings = Settings(model_context_window=130000, gemini_free_tpm=16000)
    scoped = await settings_for_repository(Session(), settings, "repo", config())
    assert scoped.model_context_window == 130000
    free = LLMConfig(provider="gemini", model="gemini/test")
    assert (await settings_for_repository(Session(), settings, "repo", free)).model_context_window == 16000

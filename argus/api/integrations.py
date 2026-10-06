"""Small integration routes; hosting and model details live in adapters."""
import os
import re
import uuid
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from argus.domain.models import LLMEndpoint, Repository
from argus.ingest.poller import _sync_and_reconcile_mr
from argus.providers import create_provider


class GitHubImport(BaseModel):
    url: str


class EndpointInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str
    model: str
    base_url: str | None = None
    api_key_ref: str | None = None
    is_default: bool = False


def endpoint_view(ep):
    return {"id": str(ep.id), "name": ep.name, "provider": ep.provider,
            "model": ep.model, "base_url": ep.base_url, "api_key_ref": ep.api_key_ref,
            "secret_present": bool(os.environ.get(ep.api_key_ref)) if ep.api_key_ref else False,
            "is_default": ep.is_default}


def parse_github_url(value):
    u = urlparse(value.strip())
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?", u.path)
    if u.scheme != "https" or u.netloc != "github.com" or not match or u.query or u.fragment:
        raise ValueError("enter https://github.com/owner/repo/pull/number")
    return f"{match[1]}/{match[2]}", int(match[3])


def integrations_router(sf, settings, require_user, require_admin):
    router = APIRouter(dependencies=[Depends(require_user)])

    @router.get("/integrations/github")
    async def github_status():
        if not (settings.github_token or settings.github_app_id):
            return {"configured": False, "username": None}
        provider = create_provider(settings, provider="github")
        try:
            identity = await provider.get_current_user()
            return {"configured": bool(settings.github_token or settings.github_app_id), "username": identity["username"],
                    "auth_mode": "app" if settings.github_app_id else "pat"}
        except httpx.HTTPError:
            return {"configured": bool(settings.github_token or settings.github_app_id), "username": None,
                    "error": "GitHub identity could not be verified"}
        finally:
            await provider.aclose()

    @router.post("/imports/github-pull-request")
    async def import_github(payload: GitHubImport):
        try:
            path, number = parse_github_url(payload.url)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        if not (settings.github_token or settings.github_app_id):
            raise HTTPException(422, "configure ARGUS_GITHUB_TOKEN or GitHub App credentials first")
        provider = create_provider(settings, provider="github")
        try:
            project = await provider.get_project(path)
            identity = await provider.get_current_user()
            async with sf() as s:
                # Serialize imports so duplicate URLs cannot create duplicate repositories.
                from sqlalchemy import text
                await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                                {"key": f"github-import:{project['id']}"})
                repo = (await s.execute(select(Repository).where(Repository.provider == "github",
                    Repository.provider_project_id == str(project["id"])))).scalar_one_or_none()
                if repo is None:
                    await s.execute(text("SELECT pg_advisory_xact_lock(hashtext('argus-model-endpoints'))"))
                    ep = (await s.execute(select(LLMEndpoint).where(
                        LLMEndpoint.name == settings.github_llm_endpoint_name))).scalar_one_or_none()
                    if ep is None:
                        if settings.github_llm_endpoint_name != "github-gemini-free":
                            raise HTTPException(422, "configured GitHub endpoint does not exist")
                        ep = LLMEndpoint(name="github-gemini-free", provider="gemini",
                            model="gemini/gemini-3.8-flash", api_key_ref="GEMINI_API_KEY")
                        s.add(ep)
                        await s.flush()
                    from argus.llm.config import resolve_llm_config
                    from argus.providers.settings import require_pilot_endpoint
                    try:
                        require_pilot_endpoint(await resolve_llm_config(s, ep.id, None))
                    except ValueError as error:
                        raise HTTPException(422, str(error)) from None
                    repo = Repository(provider="github", project_path=project["path_with_namespace"],
                        provider_project_id=str(project["id"]), default_branch=project.get("default_branch"),
                        default_llm_endpoint_id=ep.id, learnings_cooldown_hours=0,
                        embedding_config={"model": "gemini-embedding-2", "api_key_ref": "GEMINI_API_KEY", "dim": 768},
                        auto_review_enabled=False)
                    s.add(repo)
                    await s.flush()
                selected = set((repo.poll_cursor or {}).get("selected_iids", []))
                selected.add(number)
                repo.poll_cursor = {**(repo.poll_cursor or {}), "selected_iids": sorted(selected)}
                await _sync_and_reconcile_mr(s, provider, repo, number, {identity["username"]}, False)
                from argus.domain.models import MergeRequest
                mr = (await s.execute(select(MergeRequest).where(MergeRequest.repo_id == repo.id,
                    MergeRequest.mr_iid == number))).scalar_one()
                await s.commit()
                return {"repo_id": str(repo.id), "mr_id": str(mr.id), "web_url": mr.web_url}
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"GitHub returned HTTP {e.response.status_code}; verify token permissions and PR access") from None
        except (ValueError, RuntimeError) as e:
            raise HTTPException(422, str(e)) from None
        finally:
            await provider.aclose()

    @router.get("/llm-endpoints")
    async def endpoints():
        async with sf() as s:
            return [endpoint_view(ep) for ep in (await s.execute(
                select(LLMEndpoint).order_by(LLMEndpoint.name))).scalars()]

    @router.post("/reviews/{review_id}/publish")
    async def publish_preview(review_id: uuid.UUID):
        from argus.review.preview import publish_saved_review
        try:
            return await publish_saved_review(sf, settings, review_id)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        except httpx.HTTPError:
            raise HTTPException(502, "GitHub delivery failed; retry reconciles the saved review before posting") from None

    async def save_endpoint(payload, endpoint_id=None):
        from argus.llm.config import LLMConfig
        if payload.provider not in LLMConfig.model_fields["provider"].annotation.__args__:
            raise HTTPException(422, "unsupported model provider")
        if payload.api_key_ref and not re.fullmatch(r"[A-Z][A-Z0-9_]*", payload.api_key_ref):
            raise HTTPException(422, "api_key_ref must be an environment variable name")
        async with sf() as s:
            from sqlalchemy import text
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtext('argus-model-endpoints'))"))
            duplicate = (await s.execute(select(LLMEndpoint).where(LLMEndpoint.name == payload.name,
                LLMEndpoint.id != endpoint_id) if endpoint_id else select(LLMEndpoint).where(
                LLMEndpoint.name == payload.name))).scalar_one_or_none()
            if duplicate:
                raise HTTPException(409, "endpoint name already exists")
            ep = await s.get(LLMEndpoint, endpoint_id) if endpoint_id else LLMEndpoint()
            if ep is None:
                raise HTTPException(404, "endpoint not found")
            if payload.is_default:
                from sqlalchemy import update
                await s.execute(update(LLMEndpoint).values(is_default=False))
            for key, value in payload.model_dump().items():
                setattr(ep, key, value)
            s.add(ep)
            await s.commit()
            return endpoint_view(ep)

    @router.post("/llm-endpoints", dependencies=[Depends(require_admin)], status_code=201)
    async def add_endpoint(payload: EndpointInput):
        return await save_endpoint(payload)

    @router.put("/llm-endpoints/{endpoint_id}", dependencies=[Depends(require_admin)])
    async def edit_endpoint(endpoint_id: uuid.UUID, payload: EndpointInput):
        return await save_endpoint(payload, endpoint_id)

    @router.post("/llm-endpoints/{endpoint_id}/test", dependencies=[Depends(require_admin)])
    async def test_endpoint(endpoint_id: uuid.UUID):
        from argus.llm.config import resolve_llm_config
        from argus.llm.factory import build_chat_model
        async with sf() as s:
            if await s.get(LLMEndpoint, endpoint_id) is None:
                raise HTTPException(404, "endpoint not found")
            cfg = await resolve_llm_config(s, endpoint_id, None)
        cfg.timeout, cfg.num_retries = 30, 0
        if cfg.provider in {"gemini", "vertex_ai"} or (cfg.api_base or "").rstrip("/") == "https://openrouter.ai/api/v1":
            from argus.providers.settings import require_pilot_endpoint, configure_quota
            require_pilot_endpoint(cfg)
            configure_quota(cfg, settings)
        try:
            from langchain_core.messages import HumanMessage, ToolMessage
            model = build_chat_model(cfg).bind_tools([{
                "type": "function", "function": {"name": "argus_probe", "description": "Check connectivity",
                    "parameters": {"type": "object", "properties": {}, "required": []}}}])
            history = [HumanMessage(content="Call argus_probe once to check connectivity.")]
            response = await model.ainvoke(history)
            if not response.tool_calls or response.tool_calls[0]["name"] != "argus_probe":
                raise ValueError("model did not call the probe tool")
            history.extend([response, ToolMessage(content="READY", tool_call_id=response.tool_calls[0]["id"])])
            response = await model.ainvoke(history)
            return {"ok": True, "model": cfg.model, "response": str(response.content)[:100],
                    "tool_round_trip": True}
        except Exception:
            raise HTTPException(502, "model probe failed; check credentials, model availability and free-tier quota") from None

    return router

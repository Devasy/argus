import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import String, delete, select

from argus.api.integrations import parse_github_url
from argus.db import session_factory
from argus.domain.models import (Actor, Finding, Job, Learning, LLMEndpoint,
                                 MergeRequest, MRVersion, Note, Repository, Review, RuntimeSetting)
from argus.ingest.normalizer import sync_merge_request, upsert_actor
from argus.llm.config import LLMConfig
from argus.llm.factory import build_chat_model
from argus.providers.github import GitHubProvider
from argus.review.preview import diff_fingerprint, publish_saved_review


USER = {"id": 17, "login": "reviewer", "type": "User"}
REFS = {"base_sha": "a" * 40, "start_sha": "b" * 40, "head_sha": "c" * 40}
DIFFS = [{"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@\n-old\n+new"}]


@pytest.mark.parametrize("url", ["http://github.com/o/r/pull/1", "https://evil.test/o/r/pull/1",
    "https://github.com/o/r/pull/0", "https://github.com/o/r/pull/1?x=y", "https://github.com/o/r/pull/1#x"])
def test_import_requires_exact_github_pr_url(url):
    with pytest.raises(ValueError):
        parse_github_url(url)


def test_import_url():
    assert parse_github_url("https://github.com/o/r/pull/42/") == ("o/r", 42)


@pytest.mark.parametrize("project", [123, "Devasy/argus"])
def test_pipeline_accepts_provider_project_keys(settings, tmp_path, project):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from argus.review.pipeline import PipelineDeps
    from argus.review.tools import ToolContext
    deps = PipelineDeps(sf=async_sessionmaker(), settings=settings,
        llm_cfg=LLMConfig(provider="gemini", model="gemini/test"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={}, hunks={}),
        profile_static="Review public code", mr_context="Public PR", project_id=project)
    assert deps.project_id == project


async def test_github_pagination_and_private_repository_rejection(settings, respx_mock):
    provider = GitHubProvider(settings)
    respx_mock.get("https://api.github.com/repos/o/r").respond(200, json={"private": True})
    with pytest.raises(ValueError, match="public"):
        await provider.get_project("o/r")
    first = respx_mock.get("https://api.github.com/repos/o/r/pulls/1/files", params={"per_page": 100})
    first.respond(200, json=[{"filename": "a.py", "status": "added", "patch": "p"}],
                  headers={"Link": '<https://api.github.com/repos/o/r/pulls/1/files?page=2>; rel="next"'})
    respx_mock.get("https://api.github.com/repos/o/r/pulls/1/files?page=2").respond(
        200, json=[{"filename": "b.py", "status": "renamed", "previous_filename": "old.py"}])
    files = await provider.list_diffs("o/r", 1)
    assert [f["new_path"] for f in files] == ["a.py", "b.py"]
    assert files[1]["old_path"] == "old.py"
    assert provider.review_ref(1) == "refs/pull/1/head"
    await provider.aclose()


async def test_pagination_cannot_forward_token_to_another_host(settings, respx_mock):
    provider = GitHubProvider(settings)
    respx_mock.get("https://api.github.com/repos/o/r/pulls/1/files").respond(200, json=[],
        headers={"Link": '<https://evil.test/page>; rel="next"'})
    with pytest.raises(ValueError, match="outside"):
        await provider.list_diffs("o/r", 1)
    await provider.aclose()


async def test_github_approvals_dismissed_and_comment_only_reviews(settings, respx_mock):
    provider = GitHubProvider(settings)
    reviews = [{"id": 1, "user": USER, "state": "APPROVED"},
               {"id": 2, "user": USER, "state": "COMMENTED"}]
    route = respx_mock.get("https://api.github.com/repos/o/r/pulls/1/reviews")
    route.respond(200, json=reviews)
    assert len((await provider.get_approvals("o/r", 1))["approved_by"]) == 1
    route.respond(200, json=reviews + [{"id": 3, "user": USER, "state": "DISMISSED"}])
    assert (await provider.get_approvals("o/r", 1))["approved_by"] == []
    await provider.aclose()


async def test_explicit_reply_parent_and_note_namespaces(db):
    repo = Repository(provider="github", project_path=f"test/{uuid.uuid4()}", provider_project_id="9")
    db.add(repo)
    await db.flush()
    user = {"id": 123456789, "username": "argus", "bot": False}
    def n(number, kind="review_comment", reply=None):
        return {"id": number, "provider_note_key": f"{kind}:{number}", "body": "text",
                "author": user, "created_at": "2026-10-01T00:00:00Z", "reply_to_id": reply}
    payload = {"iid": 1, "title": "PR", "sha": "c" * 40}
    discussions = [{"id": "thread", "notes": [n(1), n(2, reply=1), n(3, reply=1)]},
                   {"id": "issue", "notes": [n(1, "issue_comment")]}]
    mr = await sync_merge_request(db, repo, payload, discussions, [], {}, [], {"argus"})
    await sync_merge_request(db, repo, payload, discussions, [], {}, [], {"argus"})
    rows = (await db.execute(select(Note).where(Note.mr_id == mr.id))).scalars().all()
    assert len(rows) == 4
    inline = {r.provider_note_id: r for r in rows if r.provider_note_kind == "review_comment"}
    assert inline[2].parent_note_id == inline[1].id == inline[3].parent_note_id
    actor = await db.get(Actor, inline[1].author_id)
    assert actor.provider == "github" and actor.is_bot


async def test_provider_identities_accept_uuid_keys_and_preserve_legacy_actors(db):
    key = str(uuid.uuid4())
    first = await upsert_actor(db, {"id": key, "username": "alice"}, "bitbucket")
    assert first.provider_user_key == key and first.provider_user_id is None
    assert (await upsert_actor(db, {"id": key, "username": "alice"}, "bitbucket")).id == first.id
    other_host = await upsert_actor(db, {"id": key, "username": "alice"}, "future-host")
    assert other_host.id != first.id
    legacy = Actor(provider="gitlab", provider_user_id=78245341, username="legacy")
    db.add(legacy)
    await db.flush()
    upgraded = await upsert_actor(db, {"id": 78245341, "username": "legacy"})
    assert upgraded.id == legacy.id and upgraded.provider_user_key == "78245341"
    repo = Repository(provider="bitbucket", project_path=f"test/{uuid.uuid4()}", provider_project_id=key)
    db.add(repo)
    await db.flush()
    assert repo.provider_project_id == key


def test_secret_references_survive_serialization_without_secret_values(monkeypatch):
    monkeypatch.setenv("TEST_GEMINI_SECRET", "secret-value")
    cfg = LLMConfig(provider="gemini", model="gemini/test", api_key="secret-value",
        api_key_ref="TEST_GEMINI_SECRET", fallback={"model": "openai/test", "api_key": "fallback-secret",
                                                    "api_key_ref": "FALLBACK_KEY"})
    serialized = cfg.model_dump_json()
    assert "secret-value" not in serialized and "fallback-secret" not in serialized
    assert "TEST_GEMINI_SECRET" in serialized and "FALLBACK_KEY" in serialized


def test_gemini_thought_signatures_survive_checkpoint_and_tool_conversion():
    from langchain_litellm.chat_models.litellm import _convert_dict_to_message
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    raw = {"role": "assistant", "content": "", "tool_calls": [{"id": "call1", "type": "function",
        "function": {"name": "read", "arguments": "{}"},
        "provider_specific_fields": {"thought_signature": "signed-tool"}}],
        "provider_specific_fields": {"thought_signature": "signed-message"}}
    message = _convert_dict_to_message(raw)
    serializer = JsonPlusSerializer()
    message = serializer.loads_typed(serializer.dumps_typed(message))
    model = build_chat_model(LLMConfig(provider="gemini", model="gemini/test"))
    wire, _ = model._create_message_dicts([message], None)
    assert wire[0]["provider_specific_fields"]["thought_signature"] == "signed-message"
    assert wire[0]["tool_calls"][0]["provider_specific_fields"]["thought_signature"] == "signed-tool"


@pytest.fixture
async def saved_preview(engine):
    sf = session_factory(engine)
    async with sf() as session:
        repo = Repository(provider="github", project_path=f"test/{uuid.uuid4()}", provider_project_id="999")
        session.add(repo)
        await session.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="PR", state="opened", source_branch="feature",
                          target_branch="main", head_sha=REFS["head_sha"], web_url="https://github.com/o/r/pull/1")
        session.add(mr)
        await session.flush()
        review = Review(mr_id=mr.id, status="done", publish=False, publication_status="preview",
            publication_artifact={"diff_refs": REFS, "diff_fingerprint": diff_fingerprint(DIFFS),
                                  "body": "saved content", "comments": []})
        session.add(review)
        await session.commit()
    yield sf, review
    async with engine.begin() as connection:
        await connection.execute(delete(Finding).where(Finding.review_id == review.id))
        await connection.execute(delete(Review).where(Review.id == review.id))
        await connection.execute(delete(MergeRequest).where(MergeRequest.id == mr.id))
        await connection.execute(delete(Repository).where(Repository.id == repo.id))


class Delivery:
    submissions = 0
    refs = REFS
    found = None
    timeout = False
    async def get_project(self, path): return {}
    async def get_merge_request(self, path, number): return {"state": "opened", "draft": False, "diff_refs": self.refs}
    async def list_diffs(self, path, number): return DIFFS
    async def find_review(self, path, number, marker): return self.found
    async def aclose(self): pass
    async def submit_review(self, path, number, artifact):
        self.submissions += 1
        assert artifact["body"] == "saved content"
        if self.timeout:
            raise httpx.ReadTimeout("uncertain")
        return {"id": 42, "html_url": "https://github.com/o/r/pull/1#review"}


async def test_saved_publish_idempotent_without_model_calls(saved_preview, settings, monkeypatch):
    from argus.review import preview
    sf, review = saved_preview
    provider = Delivery()
    monkeypatch.setattr(preview, "create_provider", lambda *args: provider)
    assert (await publish_saved_review(sf, settings, review.id))["id"] == 42
    assert (await publish_saved_review(sf, settings, review.id))["id"] == 42
    assert provider.submissions == 1


async def test_stale_preview_cannot_publish(saved_preview, settings, monkeypatch):
    from argus.review import preview
    sf, review = saved_preview
    provider = Delivery()
    provider.refs = {**REFS, "start_sha": "d" * 40}
    monkeypatch.setattr(preview, "create_provider", lambda *args: provider)
    with pytest.raises(ValueError, match="changed"):
        await publish_saved_review(sf, settings, review.id)
    assert provider.submissions == 0


async def test_ambiguous_delivery_reconciles_instead_of_reposting(saved_preview, settings, monkeypatch):
    from argus.review import preview
    sf, review = saved_preview
    provider = Delivery()
    provider.timeout = True
    monkeypatch.setattr(preview, "create_provider", lambda *args: provider)
    with pytest.raises(httpx.ReadTimeout):
        await publish_saved_review(sf, settings, review.id)
    with pytest.raises(ValueError, match="uncertain"):
        await publish_saved_review(sf, settings, review.id)
    provider.found = {"id": 42}
    assert (await publish_saved_review(sf, settings, review.id))["id"] == 42
    assert provider.submissions == 1


async def test_github_retrieval_excludes_global_other_repos_and_other_embedding_models(db, settings, monkeypatch):
    from argus.knowledge import learnings
    from argus.knowledge.embeddings import embedding_fingerprint
    from argus.providers.settings import settings_for_repository
    repo = Repository(provider="github", project_path=f"test/{uuid.uuid4()}")
    other = Repository(provider="gitlab", project_path=f"internal/{uuid.uuid4()}", gitlab_project_id=888)
    db.add_all([repo, other])
    await db.flush()
    scoped = await settings_for_repository(db, settings, repo.id)
    vector = [1.0] + [0.0] * 767
    rows = [Learning(repo_id=repo.id, topic="public", hint_text="public", embedding=vector,
                     embedding_fingerprint=embedding_fingerprint(scoped)),
            Learning(repo_id=None, topic="private-global", hint_text="private", embedding=vector),
            Learning(repo_id=other.id, topic="private-repo", hint_text="private", embedding=vector),
            Learning(repo_id=repo.id, topic="wrong-model", hint_text="public", embedding=vector,
                     embedding_fingerprint="old")]
    db.add_all(rows)
    await db.flush()
    async def embed(text, config, **kwargs):
        assert config.embedding_model == "gemini-embedding-2"
        assert "private" not in text
        return vector
    monkeypatch.setattr(learnings, "embed_text", embed)
    found, count = await learnings.search_learnings(db, settings, query_text="public", repo_id=repo.id)
    assert count == 1 and found[0][0].topic == "public"


async def test_durable_daily_budget_and_pause(engine, settings):
    from argus.llm.quota import reserve, pause, quota_key, QuotaDeferred
    reference = f"test-{uuid.uuid4()}"
    model = "gemini/test"
    key = quota_key(reference, model)
    try:
        await reserve(settings.database_url, reference, model, 10, (100, 1000, 1))
        with pytest.raises(QuotaDeferred):
            await reserve(settings.database_url, reference, model, 10, (100, 1000, 1))
        await pause(settings.database_url, reference, model, 3600)
        with pytest.raises(QuotaDeferred) as error:
            await reserve(settings.database_url, reference, model, 10, (100, 1000, 100))
        assert error.value.retry_at > datetime.now(timezone.utc) + timedelta(minutes=59)
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(RuntimeSetting).where(RuntimeSetting.key == key))


def test_app_jwt_signature_and_expiry(tmp_path):
    import base64
    import time
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from argus.providers.github_auth import app_jwt
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "app.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    token = app_jwt("1234", path)
    header, claims, signature = token.split(".")
    decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    key.public_key().verify(decode(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    payload = json.loads(decode(claims))
    assert payload["iss"] == "1234" and payload["iat"] < time.time() < payload["exp"]
    assert payload["exp"] - payload["iat"] <= 600


async def test_preview_compiler_anchors_only_diff_lines_and_keeps_summary_findings(saved_preview, settings):
    from argus.review.artifacts import ReviewState, ReviewPlan, CandidateFinding, Verdict
    from argus.review.diffsvc import parse_diffs
    from argus.review.preview import save_preview
    sf, review = saved_preview
    files, hunks = parse_diffs(DIFFS)
    provider = Delivery()
    deps = SimpleNamespace(sf=sf, settings=settings, mr_db_id=review.mr_id,
        gitlab=provider, project_id="o/r", mr_iid=1, diff_refs=REFS, review_mode="full",
        tool_ctx=SimpleNamespace(files_by_id={f.file_id: f for f in files}, hunks=hunks))
    def finding(fid, line):
        return CandidateFinding(finding_id=fid, stage="analyze", type="issue", severity="high",
            confidence=.9, file_path="a.py", line=line, title=f"Issue {fid}", body="Evidence",
            evidence_quote=f"quote {fid}")
    state = ReviewState(review_id=str(review.id), plan=ReviewPlan(intent="fix", mr_summary="change", files=files),
        findings=[finding("inline", 1), finding("outside", 30)],
        verdicts=[Verdict(finding_id="inline", valid=True, reason="confirmed"),
                  Verdict(finding_id="outside", valid=True, reason="confirmed")])
    await save_preview(state, deps)
    async with sf() as session:
        stored = await session.get(Review, review.id)
        assert stored.publication_status == "preview"
        assert len(stored.publication_artifact["comments"]) == 1
        assert stored.publication_artifact["comments"][0]["line"] == 1
        assert "Issue outside" in stored.summary
        assert len((await session.execute(select(Finding).where(Finding.review_id == review.id))).scalars().all()) == 2
    assert provider.submissions == 0


async def test_gemini_embedding_profile_dimension_and_normalization(settings, respx_mock, monkeypatch):
    from argus.knowledge import embeddings
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "unit-test-key")
    async def reserve(*args): pass
    from argus.llm import quota
    monkeypatch.setattr(quota, "reserve", reserve)
    config = settings.model_copy(update={"embedding_model": "gemini-embedding-2", "embedding_api_key_ref": "TEST_EMBEDDING_KEY"})
    def response(request):
        payload = json.loads(request.content)
        assert payload["outputDimensionality"] == 768
        assert payload["content"]["parts"][0]["text"] == "task: search result | query: public query"
        assert "taskType" not in payload
        return httpx.Response(200, json={"embedding": {"values": [3.0, 4.0] + [0.0] * 766}})
    respx_mock.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent").mock(side_effect=response)
    vector = await embeddings.embed_text("public query", config, is_query=True)
    assert vector[:2] == [.6, .8] and len(vector) == 768


async def test_selected_import_and_repository_model_routing(engine, settings, respx_mock, monkeypatch):
    from argus.api.app import create_app
    from argus.domain.models import RawEvent, MRParticipant
    config = settings.model_copy(update={"github_token": "unit-test-pat", "poller_enabled": False, "worker_enabled": False})
    respx_mock.get("https://api.github.com/user").respond(200, json=USER)
    respx_mock.get("https://api.github.com/repos/o/r").respond(200, json={"id": 99111999,
        "full_name": "o/r", "default_branch": "main", "private": False})
    pr = {"number": 1, "title": "Public PR", "body": "public change", "state": "open", "user": USER,
        "base": {"sha": REFS["start_sha"], "ref": "main"}, "head": {"sha": REFS["head_sha"], "ref": "feature"},
        "html_url": "https://github.com/o/r/pull/1", "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z"}
    respx_mock.get("https://api.github.com/repos/o/r/pulls/1").respond(200, json=pr)
    respx_mock.get(f"https://api.github.com/repos/o/r/compare/{REFS['start_sha']}...{REFS['head_sha']}").respond(
        200, json={"merge_base_commit": {"sha": REFS["base_sha"]}})
    for suffix in ("/pulls/1/comments", "/pulls/1/reviews", "/issues/1/comments"):
        respx_mock.get("https://api.github.com/repos/o/r" + suffix).respond(200, json=[])
    respx_mock.get("https://api.github.com/repos/o/r/pulls/1/files").respond(200, json=[
        {"filename": "a.py", "status": "modified", "patch": DIFFS[0]["diff"]}])
    respx_mock.post("https://api.github.com/graphql").respond(200, json={"data": {"repository": {"pullRequest": {
        "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}})
    sf = session_factory(engine)
    repo_id = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, engine)), base_url="http://test") as client:
            response = await client.post("/imports/github-pull-request", json={"url": "https://github.com/o/r/pull/1"})
            assert response.status_code == 200, response.text
            imported = response.json()
            repo_id = uuid.UUID(imported["repo_id"])
            again = await client.post("/imports/github-pull-request", json={"url": "https://github.com/o/r/pull/1"})
            assert again.json() == imported
            detail = (await client.get(f"/repositories/{repo_id}")).json()
            assert detail["provider"] == "github" and detail["provider_project_id"] == "99111999"
            listed = (await client.get("/repositories")).json()["items"]
            assert next(item for item in listed if item["id"] == str(repo_id))["provider"] == "github"
            triggered = await client.post(f"/merge-requests/{imported['mr_id']}/reviews", json={"publish": True})
            assert triggered.status_code == 202, triggered.text
            async with sf() as session:
                repo = await session.get(Repository, repo_id)
                assert repo.poll_cursor["selected_iids"] == [1]
                row = await session.get(Review, uuid.UUID(triggered.json()["review_id"]))
                assert row.publish is False and row.llm_config["provider"] == "gemini"
                assert row.llm_config["api_key_ref"] == "GEMINI_API_KEY"
                assert "api_key" not in row.llm_config
                job = (await session.execute(select(Job).where(Job.payload["review_id"].astext == str(row.id)))).scalar_one()
                assert job.payload["pinned"] is True
                assert (await session.execute(select(LLMEndpoint).where(LLMEndpoint.is_default == True))).scalars().all() == []
    finally:
        if repo_id:
            async with engine.begin() as connection:
                mr_ids = select(MergeRequest.id).where(MergeRequest.repo_id == repo_id)
                await connection.execute(delete(Job).where(Job.payload["review_id"].astext.in_(
                    select(Review.id.cast(String)).where(Review.mr_id.in_(mr_ids)))))
                await connection.execute(delete(Review).where(Review.mr_id.in_(mr_ids)))
                await connection.execute(delete(MRVersion).where(MRVersion.mr_id.in_(mr_ids)))
                await connection.execute(delete(MRParticipant).where(MRParticipant.mr_id.in_(mr_ids)))
                await connection.execute(delete(RawEvent).where(RawEvent.repo_id == repo_id))
                await connection.execute(delete(MergeRequest).where(MergeRequest.repo_id == repo_id))
                await connection.execute(delete(Repository).where(Repository.id == repo_id))
                await connection.execute(delete(LLMEndpoint).where(LLMEndpoint.name == "github-gemini-free"))
                await connection.execute(delete(Actor).where(Actor.provider == "github", Actor.provider_user_id == USER["id"]))


async def test_quota_deferral_retains_job_without_consuming_attempts(engine):
    import asyncio
    from argus.jobs.queue import enqueue, run_worker_forever
    from argus.llm.quota import QuotaDeferred
    sf = session_factory(engine)
    stop = asyncio.Event()
    kind = f"quota-test-{uuid.uuid4()}"
    retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with sf() as session:
        job = await enqueue(session, kind, {}, dedup_key=kind)
        await session.commit()
    async def handler(payload):
        stop.set()
        raise QuotaDeferred(retry_at)
    try:
        await run_worker_forever(sf, {kind: handler}, stop, worker_id="quota-test")
        async with sf() as session:
            row = await session.get(Job, job.id)
            assert row.status == "queued" and row.run_after == retry_at
            assert row.attempts == 0 and "quota" in row.error
            assert row.locked_by is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(Job).where(Job.id == job.id))


async def test_gemini_429_defers_model_and_persists_shared_cooldown(engine, settings, monkeypatch):
    from langchain_core.messages import HumanMessage
    from litellm import RateLimitError
    from argus.llm.quota import QuotaDeferred, quota_key
    reference = "TEST_GEMINI_QUOTA_KEY"
    monkeypatch.setenv(reference, "unit-test-key")
    model = build_chat_model(LLMConfig(provider="gemini", model="gemini/gemini-3.8-flash",
        api_key_ref=reference, free_only=True, quota_database_url=settings.database_url,
        quota_limits=(100, 10000, 100), num_retries=0))
    async def limited(**kwargs):
        raise RateLimitError("Please retry after 120 seconds", llm_provider="gemini", model=model.model)
    monkeypatch.setattr(model.client, "acompletion", limited)
    key = quota_key(reference, model.model)
    try:
        with pytest.raises(QuotaDeferred) as error:
            await model.ainvoke([HumanMessage(content="public probe")])
        assert error.value.retry_at > datetime.now(timezone.utc) + timedelta(seconds=119)
        async with session_factory(engine)() as session:
            row = await session.get(RuntimeSetting, key)
            assert row.value["requests"] == 1 and row.value["paused_until"]
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(RuntimeSetting).where(RuntimeSetting.key == key))

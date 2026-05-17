import uuid

import httpx
import pytest

from argus.api.app import create_app
from argus.domain.models import Repository


@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_trigger_audit_404s_for_unknown_repo(api):
    r = await api.post(f"/repositories/{uuid.uuid4()}/audit")
    assert r.status_code == 404


async def test_trigger_audit_422s_cleanly_with_no_llm_endpoint_configured(api, db):
    """No LLMEndpoint row exists in this test DB, so this exercises the real
    failure path an operator hits before configuring one — must be a clean
    4xx via the route's own error handling, not an unhandled 500."""
    repo = Repository(provider="gitlab", project_path="manual-trigger/smoke",
                      gitlab_project_id=999999)
    db.add(repo)
    await db.flush()
    await db.commit()
    r = await api.post(f"/repositories/{repo.id}/audit")
    assert r.status_code == 422
    assert "no LLM endpoint configured" in r.json()["detail"]


async def test_trigger_audit_409s_when_already_running(api, db):
    """A second trigger for the same repo while one is 'running' must be
    rejected, not silently start a duplicate concurrent audit."""
    from argus.domain.models import AuditRun, LLMEndpoint

    repo = Repository(provider="gitlab", project_path="manual-trigger/inflight",
                      gitlab_project_id=999998)
    db.add(repo)
    db.add(LLMEndpoint(name="t", provider="openai", model="m", is_default=True))
    await db.flush()
    db.add(AuditRun(repo_id=repo.id, status="running"))
    await db.flush()
    await db.commit()

    r = await api.post(f"/repositories/{repo.id}/audit")
    assert r.status_code == 409


async def test_audit_verdicts_expose_both_repos(api, db):
    """A verdict is uninterpretable without knowing which repo was SEARCHED
    and which repo the learning is ABOUT. Conflating them is what let an audit
    of the Python backend propose archiving a React learning: "not found in
    the codebase" was true of the repo it searched and irrelevant to the
    learning's own repo."""
    import uuid as _uuid

    from argus.domain.models import (AuditRun, AuditVerdict, Learning,
                                         Repository)

    audited = Repository(provider="gitlab",
                         project_path=f"g/audited-{_uuid.uuid4()}",
                         gitlab_project_id=50000000 + _uuid.uuid4().int % 9000000)
    db.add(audited)
    await db.flush()

    learning = Learning(repo_id=None, topic="global learning",
                        hint_text="applies everywhere", kind="guidance",
                        status="active")
    db.add(learning)
    await db.flush()

    run = AuditRun(repo_id=audited.id, status="done")
    db.add(run)
    await db.flush()
    db.add(AuditVerdict(audit_run_id=run.id, learning_id=learning.id,
                        verdict="unfalsifiable", confidence=0.8,
                        rationale="no matches in this codebase",
                        proposed_action="flag_for_rewrite", state="proposed"))
    await db.commit()

    body = (await api.get("/audit-verdicts", params={"state": "proposed"})).json()
    row = next(i for i in body["items"] if i["learning_topic"] == "global learning")

    assert row["audited_repo_path"] == audited.project_path
    # None marks it global -- the UI must be able to warn that one repo's
    # evidence cannot settle it.
    assert row["learning_repo_id"] is None
    assert row["learning_repo_path"] is None

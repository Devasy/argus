import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from argus.api.app import create_app
from argus.domain.models import AuditRun, AuditStage, Repository


@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_audit_run_detail_returns_stages_sorted_by_start_and_progress(api, db, engine):
    from tests.distill_helpers import purge_repos

    repo = Repository(provider="gitlab", project_path=f"g/ard-{uuid.uuid4().hex[:6]}",
                      gitlab_project_id=int(uuid.uuid4().int % 10**8))
    db.add(repo)
    await db.flush()
    run = AuditRun(repo_id=repo.id, status="running", trigger="manual",
                   planned_count=40, grounded_count=12)
    db.add(run)
    await db.flush()
    now = datetime.now(timezone.utc)
    db.add(AuditStage(audit_run_id=run.id, stage_name="scout", status="done",
                      started_at=now))
    db.add(AuditStage(audit_run_id=run.id, stage_name="pick", status="done",
                      started_at=now - timedelta(seconds=5)))
    await db.flush()
    await db.commit()
    try:
        r = await api.get(f"/audit-runs/{run.id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["planned_count"] == 40 and body["grounded_count"] == 12
        assert [s["stage_name"] for s in body["stages"]] == ["pick", "scout"]
    finally:
        await purge_repos(engine, [repo.id])


async def test_audit_run_detail_404s_for_unknown_run(api):
    r = await api.get(f"/audit-runs/{uuid.uuid4()}")
    assert r.status_code == 404

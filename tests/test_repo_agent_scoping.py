"""Two fixes for audits and reviews running against the wrong thing.

1. The auditor checked out `repo.default_branch`. Two repositories here are
   set to `master` while every MR targets `develop-7.0.0`, so their audits
   searched years-old code and confidently declared live patterns nonexistent:
   a learning about `deprecated_features.permit.*` in data/rabbitmq/custom.conf
   was written off as "a pattern that does not exist in this codebase" -- the
   file is on develop-7.0.0 and not on master.

2. Reviewer agents were global, so every specialist was offered to Scout on
   every repository including ones where it has nothing useful to say.
"""
import uuid

import httpx
import pytest
from sqlalchemy import delete

from argus.api.app import create_app
from argus.domain.models import (Learning, MergeRequest, Repository,
                                     ReviewerAgent, ReviewerAgentRepoSetting,
                                     ReviewerAgentVersion)
from argus.knowledge.auditor import resolve_audit_ref


async def _repo(db, default_branch="master"):
    repo = Repository(provider="gitlab",
                      project_path=f"g/scope-{uuid.uuid4()}",
                      gitlab_project_id=40000000 + uuid.uuid4().int % 9000000,
                      default_branch=default_branch)
    db.add(repo)
    await db.flush()
    return repo


async def _learning_from_mr(db, repo, target_branch, iid):
    mr = MergeRequest(repo_id=repo.id, mr_iid=iid, title="t", state="merged",
                      source_branch="feat", target_branch=target_branch,
                      head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    db.add(Learning(repo_id=repo.id, mr_id=mr.id, topic=f"t{iid}",
                    hint_text="h", kind="guidance", status="active"))
    await db.flush()


# --- audit branch resolution ------------------------------------------------

async def test_audit_ref_follows_the_learnings_not_default_branch(db):
    """The exact production shape: default_branch=master, all work on
    develop-7.0.0."""
    repo = await _repo(db, default_branch="master")
    for i in (1, 2, 3):
        await _learning_from_mr(db, repo, "develop-7.0.0", i)
    await _learning_from_mr(db, repo, "master", 4)

    assert await resolve_audit_ref(db, repo) == "develop-7.0.0"


async def test_audit_ref_picks_the_majority_branch(db):
    repo = await _repo(db, default_branch="main")
    await _learning_from_mr(db, repo, "develop-6.1.0", 1)
    for i in (2, 3, 4):
        await _learning_from_mr(db, repo, "develop-7.0.0", i)

    assert await resolve_audit_ref(db, repo) == "develop-7.0.0"


async def test_audit_ref_falls_back_when_there_is_no_provenance(db):
    """A repo whose learnings have no source MR still has to be auditable."""
    repo = await _repo(db, default_branch="develop-7.0.0")
    db.add(Learning(repo_id=repo.id, topic="orphan", hint_text="h",
                    kind="guidance", status="active"))
    await db.flush()

    assert await resolve_audit_ref(db, repo) == "develop-7.0.0"


async def test_audit_ref_falls_back_to_head_when_nothing_is_known(db):
    repo = await _repo(db, default_branch=None)
    assert await resolve_audit_ref(db, repo) == "HEAD"


async def test_archived_learnings_do_not_steer_the_audit_branch(db):
    """Only live knowledge should decide where we look for it."""
    repo = await _repo(db, default_branch="main")
    await _learning_from_mr(db, repo, "develop-7.0.0", 1)

    mr = MergeRequest(repo_id=repo.id, mr_iid=99, title="t", state="merged",
                      source_branch="old", target_branch="ancient-branch",
                      head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()
    for i in range(5):
        db.add(Learning(repo_id=repo.id, mr_id=mr.id, topic=f"old{i}",
                        hint_text="h", kind="guidance", status="archived"))
    await db.flush()

    assert await resolve_audit_ref(db, repo) == "develop-7.0.0"


# --- per-repo agent scoping -------------------------------------------------

@pytest.fixture
async def api(engine, settings):
    app = create_app(settings=settings, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    async with engine.begin() as conn:
        await conn.execute(delete(ReviewerAgentRepoSetting))


async def _agent(db, name):
    agent = ReviewerAgent(name=name, description="d", enabled=True)
    db.add(agent)
    await db.flush()
    version = ReviewerAgentVersion(agent_id=agent.id, version=1,
                                   guidelines="g", max_rounds=10)
    db.add(version)
    await db.flush()
    agent.current_version_id = version.id
    await db.flush()
    return agent


async def test_agents_run_everywhere_until_explicitly_disabled(api, db):
    """Opt-out: adding this feature must not silently stop existing agents."""
    repo = await _repo(db)
    agent = await _agent(db, f"react-{uuid.uuid4().hex[:6]}")
    await db.commit()

    rows = (await api.get(f"/repositories/{repo.id}/agents")).json()
    row = next(r for r in rows if r["agent_id"] == str(agent.id))
    assert row["enabled_here"] is True
    assert row["globally_enabled"] is True


async def test_disabling_an_agent_is_scoped_to_one_repo(api, db):
    """The actual ask: keep a specialist off the plugins repo without
    disabling it for everything else."""
    plugins = await _repo(db)
    ui = await _repo(db)
    agent = await _agent(db, f"frontend-{uuid.uuid4().hex[:6]}")
    await db.commit()

    r = await api.put(f"/repositories/{plugins.id}/agents/{agent.id}",
                      json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled_here"] is False

    off = next(x for x in (await api.get(f"/repositories/{plugins.id}/agents")).json()
               if x["agent_id"] == str(agent.id))
    on = next(x for x in (await api.get(f"/repositories/{ui.id}/agents")).json()
              if x["agent_id"] == str(agent.id))
    assert off["enabled_here"] is False
    assert on["enabled_here"] is True
    # the agent itself is untouched -- this is a per-repo override, not a
    # global disable
    assert off["globally_enabled"] is True


async def test_a_disabled_agent_can_be_re_enabled(api, db):
    repo = await _repo(db)
    agent = await _agent(db, f"toggle-{uuid.uuid4().hex[:6]}")
    await db.commit()

    await api.put(f"/repositories/{repo.id}/agents/{agent.id}",
                  json={"enabled": False})
    r = await api.put(f"/repositories/{repo.id}/agents/{agent.id}",
                      json={"enabled": True})
    assert r.json()["enabled_here"] is True


async def test_unknown_repo_or_agent_is_rejected(api, db):
    repo = await _repo(db)
    await db.commit()
    missing = uuid.uuid4()

    assert (await api.get(f"/repositories/{missing}/agents")).status_code == 404
    assert (await api.put(f"/repositories/{repo.id}/agents/{missing}",
                          json={"enabled": False})).status_code == 404


def test_runner_withholds_disabled_agents_from_scout():
    """The API is only bookkeeping unless the runner honours it."""
    import inspect

    from argus.review.runner import execute_review_job

    src = inspect.getsource(execute_review_job)
    assert "ReviewerAgentRepoSetting" in src
    assert "disabled_here" in src
    assert "if agent.id in disabled_here" in src

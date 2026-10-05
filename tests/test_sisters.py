import itertools
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete

import argus.review.sisters as sis
from argus.review.sisters import (Sister, build_sister_tools, pick_branch,
                                      sister_prompt_block)

_ids = itertools.count(95_000_001)


# ---- branch rule ---------------------------------------------------------------------------

class FakeBranches:
    def __init__(self, existing: set[str]):
        self.existing, self.calls = existing, []

    async def get_branch(self, project, branch):
        self.calls.append(branch)
        return {"commit": {"id": f"sha-{branch}"}} if branch in self.existing else None


SISTER = SimpleNamespace(gitlab_project_id=1, default_branch="develop")


def _link(branch=None, match=True):
    return SimpleNamespace(branch=branch, match_source_branch=match)


async def test_same_named_source_branch_wins():
    gl = FakeBranches({"feature/pp-240", "develop", "release"})
    assert await pick_branch(gl, SISTER, _link("release"), "feature/pp-240") == (
        "feature/pp-240", "sha-feature/pp-240", "same branch as this MR")


async def test_configured_branch_when_no_same_named_branch():
    gl = FakeBranches({"develop", "release"})
    assert (await pick_branch(gl, SISTER, _link("release"), "feature/x"))[0] == "release"


async def test_default_branch_is_the_fallback():
    gl = FakeBranches({"develop"})
    assert await pick_branch(gl, SISTER, _link(), "feature/x") == ("develop", "sha-develop",
                                                                    "default branch")


async def test_matching_can_be_switched_off():
    gl = FakeBranches({"feature/x", "develop"})
    assert (await pick_branch(gl, SISTER, _link(match=False), "feature/x"))[0] == "develop"
    assert "feature/x" not in gl.calls


async def test_no_usable_branch_is_none():
    assert await pick_branch(FakeBranches(set()), SISTER, _link("gone"), "feature/x") is None


# ---- tools -------------------------------------------------------------------------------

@pytest.fixture
def sister_ws(tmp_path):
    ws = tmp_path / "backend"
    (ws / "src" / "dto").mkdir(parents=True)
    (ws / "src" / "dto" / "media.dto.ts").write_text(
        "export class EmbeddedMediaDto {\n  @IsOptional() @IsBoolean()\n  isPublic?: boolean;\n}\n")
    (ws / ".git").mkdir()
    (ws / ".git" / "HEAD").write_text("ref")
    (ws / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    (tmp_path / "secret.txt").write_text("outside")
    return ws


def _tools(ws):
    s = Sister("event-mgr-backend", "event-mgr/event-mgr-backend", ws, "develop", "a" * 40,
               "default branch")
    return {t.name: t for t in build_sister_tools([s])}


async def test_every_result_is_labelled_reference_only(sister_ws):
    out = await _tools(sister_ws)["sister_list_files"].ainvoke({"repo": "event-mgr-backend"})
    assert out.startswith("[sister repo event-mgr-backend @ develop aaaaaaaa (default branch)")
    assert "reference only" in out


async def test_list_files_root_glob_and_skips_git(sister_ws):
    t = _tools(sister_ws)["sister_list_files"]
    root = await t.ainvoke({"repo": "event-mgr-backend"})
    assert "src/" in root and ".git" not in root
    hits = await t.ainvoke({"repo": "event-mgr/event-mgr-backend", "glob": "**/*.dto.ts"})
    assert "src/dto/media.dto.ts" in hits


async def test_list_files_cannot_escape_the_checkout(sister_ws):
    t = _tools(sister_ws)["sister_list_files"]
    assert "secret.txt" not in await t.ainvoke({"repo": "event-mgr-backend", "glob": "../*"})
    assert "no such directory" in await t.ainvoke({"repo": "event-mgr-backend", "path": "../.."})


async def test_read_file_ranges_binary_escape_and_unknown_repo(sister_ws):
    t = _tools(sister_ws)["sister_read_file"]
    out = await t.ainvoke({"repo": "event-mgr-backend", "path": "src/dto/media.dto.ts",
                           "start": 2, "end": 3})
    assert "isPublic?: boolean" in out and "export class" not in out and "(lines 2-3 of 4)" in out
    assert "binary file" in await t.ainvoke({"repo": "event-mgr-backend", "path": "logo.png"})
    assert "escapes the repository root" in await t.ainvoke(
        {"repo": "event-mgr-backend", "path": "../secret.txt"})
    assert "unknown sister repo" in await t.ainvoke({"repo": "frontend", "path": "x"})


async def test_search_finds_the_field_the_bot_missed(sister_ws):
    t = _tools(sister_ws)["sister_search"]
    out = await t.ainvoke({"repo": "event-mgr-backend", "pattern": "isPublic"})
    assert "media.dto.ts" in out and out.startswith("[sister repo")
    assert "invalid regex" in await t.ainvoke({"repo": "event-mgr-backend", "pattern": "("})


def test_no_sisters_means_no_tools_and_no_prompt():
    assert build_sister_tools([]) == []
    assert sister_prompt_block([], []) == ""


def test_prompt_names_sisters_marks_unavailable_ones_and_forbids_findings(tmp_path):
    s = Sister("event-mgr-backend", "p/b", tmp_path, "feature/x", "a" * 40, "same branch as this MR")
    block = sister_prompt_block([s], [{"name": "event-mgr-backend"}, {"name": "ui", "error": "403"}])
    assert "event-mgr-backend @ feature/x (same branch as this MR)" in block
    assert "ui: unavailable this review" in block
    assert "Never report a finding on sister-repo code" in block


# ---- acquisition -------------------------------------------------------------------------

async def _repos(sf, n):
    from argus.domain.models import Repository
    out = []
    async with sf() as s:
        for _ in range(n):
            r = Repository(provider="gitlab", project_path=f"grp/sis-{next(_ids)}",
                           gitlab_project_id=next(_ids), default_branch="develop")
            s.add(r); await s.flush(); out.append(r)
        await s.commit()
    return out


async def test_no_links_means_no_gitlab_calls(engine, tmp_path):
    from argus.db import session_factory
    sf = session_factory(engine)
    (repo,) = await _repos(sf, 1)
    gl = FakeBranches({"develop"})
    assert await sis.acquire_sisters(sf, gl, repo.id, "feature/x", tmp_path, str) == ([], [], [])
    assert gl.calls == []


async def test_a_failing_sister_is_recorded_and_the_others_still_load(engine, tmp_path, monkeypatch):
    from argus.db import session_factory
    from argus.domain.models import RepoSisterLink
    sf = session_factory(engine)
    repo, good, bad = await _repos(sf, 3)
    async with sf() as s:
        s.add_all([RepoSisterLink(repo_id=repo.id, sister_repo_id=good.id),
                   RepoSisterLink(repo_id=repo.id, sister_repo_id=bad.id, branch="gone",
                                  match_source_branch=False)])
        await s.commit()

    class FakeWM:
        def __init__(self, root, url):
            self.root = root

        async def acquire(self, sha):
            return self.root

        async def release(self, sha):
            pass

    monkeypatch.setattr(sis, "WorkspaceManager", FakeWM)
    gl = FakeBranches({"develop"})
    gl_bad = {bad.gitlab_project_id}

    async def get_branch(project, branch):
        return None if project in gl_bad else await FakeBranches.get_branch(gl, project, branch)

    gl.get_branch = get_branch
    sisters, held, record = await sis.acquire_sisters(sf, gl, repo.id, "feature/x", tmp_path, str)
    assert [s.name for s in sisters] == [good.project_path.rsplit("/", 1)[-1]]
    assert len(held) == 1
    errors = [r for r in record if r.get("error")]
    assert len(errors) == 1 and errors[0]["error"] == "no usable branch"


# ---- publish guard -----------------------------------------------------------------------

def test_foreign_file_findings_are_dropped(tmp_path):
    from argus.review.publisher import drop_foreign_files
    (tmp_path / "in_repo.py").write_text("x")
    ctx = SimpleNamespace(workspace=tmp_path, files_by_id={
        "f1": SimpleNamespace(path="changed.py")})
    findings = [SimpleNamespace(file_path=p, title=p) for p in
                ("changed.py", "in_repo.py", "src/dto/media.dto.ts")]
    kept = drop_foreign_files(findings, ctx)
    assert [f.file_path for f in kept] == ["changed.py", "in_repo.py"]


def test_publish_guard_only_runs_for_repos_with_sisters():
    import inspect
    from argus.review import publisher
    src = inspect.getsource(publisher)
    assert 'if getattr(deps, "sister_repos", None):\n        ranked = drop_foreign_files(' in src


# ---- API ---------------------------------------------------------------------------------

@pytest.fixture
async def api(engine, settings):
    from argus.api.app import create_app
    app = create_app(settings=settings, engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.fixture
async def three_repos(engine):
    from argus.db import session_factory
    from argus.domain.models import RepoSisterLink, Repository
    repos = await _repos(session_factory(engine), 3)
    yield repos
    async with engine.begin() as conn:
        ids = [r.id for r in repos]
        await conn.execute(delete(RepoSisterLink).where(RepoSisterLink.repo_id.in_(ids)))
        await conn.execute(delete(Repository).where(Repository.id.in_(ids)))


async def test_sisters_default_to_none_and_round_trip(api, three_repos, monkeypatch):
    from argus.gitlab.client import GitLabClient

    async def fake_get_branch(self, project, branch):
        return {"commit": {"id": "x"}} if branch == "develop" else None

    monkeypatch.setattr(GitLabClient, "get_branch", fake_get_branch)
    repo, b, c = three_repos
    assert (await api.get(f"/repositories/{repo.id}/sisters")).json() == []
    r = await api.put(f"/repositories/{repo.id}/sisters", json=[
        {"sister_repo_id": str(b.id)},
        {"sister_repo_id": str(c.id), "branch": "develop", "match_source_branch": False}])
    assert r.status_code == 200, r.text
    got = {x["sister_project_path"]: x for x in r.json()}
    assert got[b.project_path]["branch"] is None and got[b.project_path]["match_source_branch"]
    assert got[c.project_path]["branch"] == "develop" and got[c.project_path]["sister_default_branch"] == "develop"
    r = await api.put(f"/repositories/{repo.id}/sisters", json=[])
    assert r.json() == []


@pytest.mark.parametrize("bad,msg", [
    ("self", "cannot be related to itself"),
    ("dup", "listed twice"),
    ("unknown", "unknown repository"),
    ("branch", "not found in"),
    ("many", "at most 5"),
])
async def test_sister_validation(api, three_repos, monkeypatch, bad, msg):
    import uuid
    from argus.gitlab.client import GitLabClient

    async def fake_get_branch(self, project, branch):
        return None

    monkeypatch.setattr(GitLabClient, "get_branch", fake_get_branch)
    repo, b, _ = three_repos
    body = {
        "self": [{"sister_repo_id": str(repo.id)}],
        "dup": [{"sister_repo_id": str(b.id)}, {"sister_repo_id": str(b.id)}],
        "unknown": [{"sister_repo_id": str(uuid.uuid4())}],
        "branch": [{"sister_repo_id": str(b.id), "branch": "nope"}],
        "many": [{"sister_repo_id": str(uuid.uuid4())} for _ in range(6)],
    }[bad]
    r = await api.put(f"/repositories/{repo.id}/sisters", json=body)
    assert r.status_code == 422 and msg in r.text, r.text


async def test_same_named_target_branch_is_second_choice():
    gl = FakeBranches({"feature/PP-5930-affiliate", "develop", "release"})
    assert await pick_branch(gl, SISTER, _link("release"), "feature/PP-5930-x",
                             "feature/PP-5930-affiliate") == (
        "feature/PP-5930-affiliate", "sha-feature/PP-5930-affiliate", "same target branch as this MR")


async def test_target_branch_ignored_when_matching_is_off():
    gl = FakeBranches({"feature/int", "develop"})
    assert (await pick_branch(gl, SISTER, _link(match=False), "feature/x", "feature/int"))[0] == "develop"

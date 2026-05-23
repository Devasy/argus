import uuid
from datetime import datetime, timezone

import pytest

from argus.db import session_factory
from argus.domain.models import MergeRequest, MRVersion, Repository, Review
from argus.gitlab.client import GitLabClient
from argus.review import runner as runner_module
from argus.review.runner import execute_review_job
from argus.review.workspace import WorkspaceManager

MR_PAYLOAD = {
    "sha": "new_sha",
    "title": "t",
    "description": "",
    "source_branch": "a",
    "target_branch": "b",
    "diff_refs": {},
}

DIFFS = [
    {"old_path": "a.py", "new_path": "a.py", "diff": "", "new_file": False,
     "deleted_file": False, "renamed_file": False},
    {"old_path": "b.py", "new_path": "b.py", "diff": "", "new_file": False,
     "deleted_file": False, "renamed_file": False},
]


class _FakeCompiledState:
    compiled = None


def _patch_common(monkeypatch, tmp_path):
    async def fake_get_merge_request(self, project_id, mr_iid):
        return MR_PAYLOAD

    async def fake_list_diffs(self, project_id, mr_iid):
        return DIFFS

    async def fake_acquire(self, sha, mr_iid=None):
        return tmp_path

    async def fake_release(self, sha):
        return None

    async def fake_build_graph(workspace):
        return None

    async def fake_run_review_pipeline(review_id, deps, checkpointer):
        return _FakeCompiledState()

    class _FakeCheckpointerCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def setup(self):
            return None

    monkeypatch.setattr(GitLabClient, "get_merge_request", fake_get_merge_request)
    monkeypatch.setattr(GitLabClient, "list_diffs", fake_list_diffs)
    monkeypatch.setattr(WorkspaceManager, "acquire", fake_acquire)
    monkeypatch.setattr(WorkspaceManager, "release", fake_release)
    monkeypatch.setattr(runner_module, "build_graph", fake_build_graph)
    monkeypatch.setattr(runner_module, "run_review_pipeline", fake_run_review_pipeline)
    monkeypatch.setattr(
        "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver.from_conn_string",
        lambda *a, **kw: _FakeCheckpointerCtx())


async def test_execute_review_job_sets_incremental_files_when_narrowed(
        db, engine, settings, monkeypatch, tmp_path):
    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/incr-narrow-test",
                          gitlab_project_id=90000700)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=11, title="t", state="opened",
                          source_branch="a", target_branch="b",
                          head_sha="old_sha", web_url="u")
        s.add(mr); await s.flush()
        prior_version = MRVersion(mr_id=mr.id, provider_version_id=1,
                                  head_commit_sha="old_sha",
                                  base_commit_sha="base", start_commit_sha="start")
        s.add(prior_version); await s.flush()
        prior_review = Review(mr_id=mr.id, status="done", trigger="manual",
                              mr_version_id=prior_version.id,
                              finished_at=datetime.now(timezone.utc),
                              llm_config={"provider": "anthropic", "model": "m",
                                         "temperature": 0.1})
        s.add(prior_review); await s.flush()
        review = Review(mr_id=mr.id, status="queued", trigger="manual",
                        llm_config={"provider": "anthropic", "model": "m",
                                   "temperature": 0.1})
        s.add(review); await s.flush()
        review_id = review.id
        await s.commit()

    _patch_common(monkeypatch, tmp_path)

    async def fake_compare(self, project_id, frm, to):
        return {"diffs": [{"new_path": "b.py"}]}

    monkeypatch.setattr(GitLabClient, "compare", fake_compare)

    await execute_review_job(sf, settings, {"review_id": str(review_id)})

    async with sf() as s:
        updated = await s.get(Review, review_id)
        assert updated.status == "done"
        assert updated.incremental_files == ["b.py"]


async def test_execute_review_job_leaves_incremental_files_null_on_full_review(
        db, engine, settings, monkeypatch, tmp_path):
    sf = session_factory(engine)
    async with sf() as s:
        repo = Repository(provider="gitlab", project_path="grp/incr-full-test",
                          gitlab_project_id=90000701)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=12, title="t", state="opened",
                          source_branch="a", target_branch="b",
                          head_sha="new_sha", web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="queued", trigger="manual",
                        llm_config={"provider": "anthropic", "model": "m",
                                   "temperature": 0.1})
        s.add(review); await s.flush()
        review_id = review.id
        await s.commit()

    _patch_common(monkeypatch, tmp_path)

    await execute_review_job(sf, settings, {"review_id": str(review_id)})

    async with sf() as s:
        updated = await s.get(Review, review_id)
        assert updated.status == "done"
        assert updated.incremental_files is None

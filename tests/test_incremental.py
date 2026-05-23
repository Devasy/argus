from argus.review.artifacts import FileChange
from argus.review.incremental import restrict_files


def _f(i, path):
    return FileChange(file_id=f"f{i}", path=path, change_kind="modified")


def test_restrict_keeps_changed_only():
    files = [_f(1, "a.py"), _f(2, "b.py"), _f(3, "c.py")]
    out = restrict_files(files, {"b.py"})
    assert [f.path for f in out] == ["b.py"]


def test_restrict_never_returns_empty():
    files = [_f(1, "a.py")]
    assert restrict_files(files, {"zzz.py"}) == files


async def test_last_reviewed_version(db):
    from argus.domain.models import (MergeRequest, MRVersion, Repository,
                                         Review)
    from argus.review.incremental import last_reviewed_version
    repo = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=1)
    db.add(repo); await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s2",
                      web_url="u")
    db.add(mr); await db.flush()
    v1 = MRVersion(mr_id=mr.id, provider_version_id=1, head_commit_sha="s1",
                   base_commit_sha="b", start_commit_sha="st")
    db.add(v1); await db.flush()
    db.add(Review(mr_id=mr.id, status="done", mr_version_id=v1.id))
    db.add(Review(mr_id=mr.id, status="failed"))
    await db.flush()
    got = await last_reviewed_version(db, mr.id)
    assert got is not None and got.head_commit_sha == "s1"

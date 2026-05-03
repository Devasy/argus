async def test_review_incremental_files_roundtrip(db):
    from argus.domain.models import MergeRequest, Repository, Review
    repo = Repository(provider="gitlab", project_path="g/p2", gitlab_project_id=2)
    db.add(repo); await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=2, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s1",
                      web_url="u")
    db.add(mr); await db.flush()
    rev = Review(mr_id=mr.id, status="done", incremental_files=["a.py", "b.py"])
    db.add(rev); await db.flush()
    await db.refresh(rev)
    assert rev.incremental_files == ["a.py", "b.py"]

    rev2 = Review(mr_id=mr.id, status="done")
    db.add(rev2); await db.flush()
    await db.refresh(rev2)
    assert rev2.incremental_files is None

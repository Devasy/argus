import uuid

from argus.domain.models import Actor, MergeRequest, Note, Repository
from argus.knowledge.backfill_distill import find_qualifying_mrs


async def test_find_qualifying_mrs_filters_merged_with_human_notes(db):
    repo = Repository(provider="gitlab", project_path="grp/backfill-test",
                      gitlab_project_id=90000501)
    db.add(repo)
    await db.flush()

    merged_with_human = MergeRequest(
        repo_id=repo.id, mr_iid=1, title="a", state="merged",
        source_branch="a", target_branch="main", head_sha="s1", web_url="u")
    merged_no_notes = MergeRequest(
        repo_id=repo.id, mr_iid=2, title="b", state="merged",
        source_branch="b", target_branch="main", head_sha="s2", web_url="u")
    open_with_human = MergeRequest(
        repo_id=repo.id, mr_iid=3, title="c", state="opened",
        source_branch="c", target_branch="main", head_sha="s3", web_url="u")
    db.add_all([merged_with_human, merged_no_notes, open_with_human])
    await db.flush()

    human = Actor(username="alice", provider_user_id=1, provider="gitlab")
    db.add(human)
    await db.flush()

    db.add_all([
        Note(mr_id=merged_with_human.id, author_id=human.id, author_type="human",
            kind="inline", body="x", file_path="a.py", line=1,
            disposition="open", provider_note_id=1),
        Note(mr_id=open_with_human.id, author_id=human.id, author_type="human",
            kind="inline", body="x", file_path="a.py", line=1,
            disposition="open", provider_note_id=2),
        Note(mr_id=merged_with_human.id, author_id=human.id, author_type="system",
            kind="system", body="approved", disposition="open", provider_note_id=3),
    ])
    await db.flush()

    results = await find_qualifying_mrs(db, repo_id=repo.id)
    result_ids = {mr.id for mr in results}
    assert result_ids == {merged_with_human.id}  # not merged_no_notes, not open_with_human

import json
from pathlib import Path

from sqlalchemy import func, select

from argus.domain.models import (Actor, Discussion, MergeRequest, MRVersion,
                                     Note, Repository)
from argus.gitlab.normalizer import classify_system_note, sync_merge_request

FIX = Path(__file__).parent / "fixtures" / "gitlab"


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


async def _make_repo(db):
    repo = Repository(provider="gitlab", project_path="grp/ncte", gitlab_project_id=848)
    db.add(repo)
    await db.flush()
    return repo


async def _sync(db, repo):
    return await sync_merge_request(
        db, repo, fx("mr"), fx("discussions"), fx("versions"),
        fx("approvals"), fx("reviewers"), bot_usernames={"pr_agent"})


async def test_sync_creates_entities(db):
    repo = await _make_repo(db)
    mr = await _sync(db, repo)
    assert mr.mr_iid == 36
    n_notes = (await db.execute(select(func.count(Note.id)))).scalar()
    n_disc = (await db.execute(select(func.count(Discussion.id)))).scalar()
    n_ver = (await db.execute(select(func.count(MRVersion.id)))).scalar()
    assert n_notes > 20 and n_disc > 10 and n_ver == len(fx("versions"))
    # bot author classified
    bot_notes = (await db.execute(
        select(Note).join(Actor, Note.author_id == Actor.id)
        .where(Actor.username == "pr_agent"))).scalars().all()
    assert bot_notes and all(n.author_type == "bot" for n in bot_notes)
    # the !36 suggestion note carries its suggestions payload
    sugg = [n for n in bot_notes if n.suggestions]
    assert sugg and sugg[0].suggestions[0]["applied"] is False


async def test_sync_is_idempotent(db):
    repo = await _make_repo(db)
    await _sync(db, repo)
    count1 = (await db.execute(select(func.count(Note.id)))).scalar()
    await _sync(db, repo)
    count2 = (await db.execute(select(func.count(Note.id)))).scalar()
    assert count1 == count2


async def test_resolution_captured(db):
    repo = await _make_repo(db)
    await _sync(db, repo)
    resolved = (await db.execute(
        select(Discussion).where(Discussion.resolved == True))).scalars().all()  # noqa: E712
    assert resolved and resolved[0].resolved_by_id is not None


def test_classify_system_note():
    assert classify_system_note("requested review from @developer.two") == "review_requested"
    assert classify_system_note("added 7 commits\n\n<ul>...</ul>") == "commits_added"
    assert classify_system_note("requested changes") == "changes_requested"
    assert classify_system_note("changed this line in [version 7 of the diff](/x)") == "line_outdated"
    assert classify_system_note("assigned to @lead.dev") == "assigned"
    assert classify_system_note("some unknown body") is None

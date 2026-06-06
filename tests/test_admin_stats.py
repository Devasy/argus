"""compute_user_stats/compute_reviewer_graph classify human review comments
via a resolved/reply-based heuristic, since (unlike bot notes) human notes
never get a `disposition` set -- see argus/api/stats.py's
_human_reviewer_note_rows docstring for why."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from argus.api.app import create_app
from argus.api.stats import (compute_comment_source_stats, compute_ops_stats,
                                 compute_reviewer_graph, compute_user_stats)
from argus.domain.models import Actor, Discussion, MergeRequest, Note, Repository


@pytest.fixture
async def reviewer_fixture(db):
    now = datetime.now(timezone.utc)
    repo = Repository(provider="gitlab", project_path="grp/admin-stats-test",
                      gitlab_project_id=90000100)
    db.add(repo)
    await db.flush()

    author = Actor(provider="gitlab", provider_user_id=910001, username="pr.author")
    reviewer = Actor(provider="gitlab", provider_user_id=910002, username="the.reviewer")
    db.add_all([author, reviewer])
    await db.flush()

    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      author_id=author.id, source_branch="a", target_branch="b",
                      head_sha="s", web_url="u")
    db.add(mr)
    await db.flush()

    resolved_disc = Discussion(mr_id=mr.id, provider_discussion_id="r1",
                               resolvable=True, resolved=True)
    rejected_disc = Discussion(mr_id=mr.id, provider_discussion_id="r2",
                               resolvable=True, resolved=False)
    ignored_disc = Discussion(mr_id=mr.id, provider_discussion_id="r3",
                              resolvable=True, resolved=False)
    db.add_all([resolved_disc, rejected_disc, ignored_disc])
    await db.flush()

    resolved_note = Note(mr_id=mr.id, provider_note_id=1, discussion_id=resolved_disc.id,
                         author_id=reviewer.id, author_type="human", kind="inline",
                         body="please fix", note_created_at=now)
    rejected_note = Note(mr_id=mr.id, provider_note_id=2, discussion_id=rejected_disc.id,
                         author_id=reviewer.id, author_type="human", kind="inline",
                         body="please fix this too", note_created_at=now)
    author_pushback = Note(mr_id=mr.id, provider_note_id=3, discussion_id=rejected_disc.id,
                           author_id=author.id, author_type="human", kind="inline",
                           body="disagree, won't fix", note_created_at=now)
    ignored_note = Note(mr_id=mr.id, provider_note_id=4, discussion_id=ignored_disc.id,
                        author_id=reviewer.id, author_type="human", kind="inline",
                        body="anyone?", note_created_at=now)
    db.add_all([resolved_note, rejected_note, author_pushback, ignored_note])
    await db.flush()
    return {"author_id": author.id, "reviewer_id": reviewer.id, "mr_id": mr.id}


async def test_reviewer_stats_classify_resolved_rejected_ignored(db, reviewer_fixture):
    result = await compute_user_stats(db)
    by_id = {item["actor_id"]: item for item in result["items"]}
    reviewer_stats = by_id[reviewer_fixture["reviewer_id"]]["reviewer_stats"]
    assert reviewer_stats == {"comments": 3, "resolved": 1, "rejected": 1, "ignored": 1}


async def test_reviewer_graph_has_one_edge_with_breakdown(db, reviewer_fixture):
    graph = await compute_reviewer_graph(db)
    node_ids = {n["actor_id"] for n in graph["nodes"]}
    assert reviewer_fixture["reviewer_id"] in node_ids
    assert reviewer_fixture["author_id"] in node_ids

    edge = next(e for e in graph["edges"]
                if e["reviewer_id"] == reviewer_fixture["reviewer_id"]
                and e["author_id"] == reviewer_fixture["author_id"])
    assert edge["comments"] == 3
    assert edge["resolved"] == 1
    assert edge["rejected"] == 1
    assert edge["ignored"] == 1


async def test_user_stats_days_window_excludes_old_comments(db, reviewer_fixture):
    old_disc = Discussion(mr_id=reviewer_fixture["mr_id"], provider_discussion_id="old1",
                          resolvable=True, resolved=True)
    db.add(old_disc)
    await db.flush()
    old_note = Note(mr_id=reviewer_fixture["mr_id"], provider_note_id=99,
                    discussion_id=old_disc.id, author_id=reviewer_fixture["reviewer_id"],
                    author_type="human", kind="inline", body="ancient comment",
                    note_created_at=datetime.now(timezone.utc) - timedelta(days=30))
    db.add(old_note)
    await db.flush()

    all_time = await compute_user_stats(db)
    by_id = {item["actor_id"]: item for item in all_time["items"]}
    assert by_id[reviewer_fixture["reviewer_id"]]["reviewer_stats"]["comments"] == 4

    windowed = await compute_user_stats(db, days=7)
    assert windowed["window_days"] == 7
    by_id_windowed = {item["actor_id"]: item for item in windowed["items"]}
    assert by_id_windowed[reviewer_fixture["reviewer_id"]]["reviewer_stats"]["comments"] == 3


async def test_comment_source_stats_compares_bot_and_human_on_same_buckets(db, reviewer_fixture):
    bot_note = Note(mr_id=reviewer_fixture["mr_id"], provider_note_id=50,
                    author_type="bot", kind="inline", body="fix this",
                    disposition="accepted", note_created_at=datetime.now(timezone.utc))
    db.add(bot_note)
    await db.flush()

    result = await compute_comment_source_stats(db)
    assert result["bot"] == {"resolved": 1, "rejected": 0, "ignored": 0}
    assert result["human"] == {"resolved": 1, "rejected": 1, "ignored": 1}


async def test_ops_stats_shape(db):
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    ops = await compute_ops_stats(db, cutoff)
    assert set(ops) == {"jobs_by_kind", "reviews_queued", "reviews_running",
                        "reviews_failed", "distillation_queued",
                        "distillation_running", "distillation_failed",
                        "audits_running", "audits_failed"}


@pytest.fixture
async def secured_admin_api(engine, settings):
    settings2 = settings.model_copy(update={"admin_token": "admin-sekrit"})
    app = create_app(settings=settings2, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_admin_routes_require_admin_token_not_regular_token(secured_admin_api):
    # No credential at all, and settings.api_token=="" (default test settings)
    # -- resolves to an anonymous "user" Principal, so this is 403 (identified,
    # insufficient role), not 401. See api/auth.py's resolve_principal.
    assert (await secured_admin_api.get("/stats/users")).status_code == 403
    wrong = await secured_admin_api.get(
        "/stats/users", headers={"Authorization": "Bearer admin-sekrit-typo"})
    assert wrong.status_code == 401
    ok = await secured_admin_api.get(
        "/stats/users", headers={"Authorization": "Bearer admin-sekrit"})
    assert ok.status_code == 200


async def test_admin_routes_disabled_without_admin_token_configured(engine, settings):
    app = create_app(settings=settings, engine=engine)  # settings.admin_token == ""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # A token WAS presented; with no admin_token configured it can never
        # match anything, so it's an invalid credential (401), not a
        # role-mismatch (403) -- distinct from the "presented nothing" case
        # above, which is treated as anonymous "user" role.
        r = await c.get("/stats/users", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 401

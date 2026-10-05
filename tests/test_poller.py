import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import Job, MergeRequest, MRVersion, Note, Repository
from argus.ingest.poller import _pending_reconciliation_mr_iids, poll_repo

FIX = Path(__file__).parent / "fixtures" / "gitlab"


@pytest.fixture
async def db(engine):
    """poll_repo commits; retain that behavior inside a rollback-only outer transaction."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(bind=connection, expire_on_commit=False,
                    join_transaction_mode="create_savepoint") as session:
                yield session
        finally:
            await transaction.rollback()


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, mrs_by_iid=None):
        self.mr = fx("mr")
        # iid -> mr payload, for MRs fetched individually (get_merge_request)
        # but NOT necessarily returned by list_merge_requests.
        self.mrs_by_iid = mrs_by_iid or {self.mr["iid"]: self.mr}
        # which iids list_merge_requests should return (cursor-fetched)
        self.listed_iids = list(self.mrs_by_iid.keys())

    async def list_merge_requests(self, project, updated_after=None, state="all"):
        return [self.mrs_by_iid[i] for i in self.listed_iids]

    async def get_merge_request(self, project, iid):
        return self.mrs_by_iid[iid]

    async def list_discussions(self, project, iid):
        return fx("discussions")

    async def list_versions(self, project, iid):
        return fx("versions")

    async def get_approvals(self, project, iid):
        return fx("approvals")

    async def get_reviewers(self, project, iid):
        return fx("reviewers")

    async def list_diffs(self, project, iid):
        return []

    async def get_note_awards(self, project, iid, note_id):
        return []


async def test_poll_repo_syncs_and_advances_cursor(db):
    repo = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=848)
    db.add(repo)
    await db.flush()
    n = await poll_repo(db, FakeClient(), repo, bot_usernames={"pr_agent"}, llm_healthy=True)
    assert n == 1
    assert repo.poll_cursor and repo.poll_cursor["updated_after"] == fx("mr")["updated_at"]


async def test_poll_repo_does_not_resolve_outcomes_twice_within_an_hour(db, monkeypatch):
    """The learning-outcome pass is gated to once per hour per repo,
    independent of poll_interval_s. reconcile_mr/sync still runs every call;
    only the resolve_outcomes_for_mr batch should be skipped on the second."""
    import argus.ingest.poller as P

    calls = {"n": 0}

    async def fake_resolve(session, mr_id):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(P, "resolve_outcomes_for_mr", fake_resolve)

    repo = Repository(provider="gitlab", project_path="g/p5", gitlab_project_id=848)
    db.add(repo)
    await db.flush()

    await poll_repo(db, FakeClient(), repo, bot_usernames=set(), llm_healthy=True)
    first_calls = calls["n"]
    assert first_calls > 0

    await poll_repo(db, FakeClient(), repo, bot_usernames=set(), llm_healthy=True)
    assert calls["n"] == first_calls  # second call within the hour resolves nothing more


async def test_poll_repo_skips_unchanged(db):
    repo = Repository(provider="gitlab", project_path="g/p2", gitlab_project_id=848)
    db.add(repo)
    await db.flush()
    await poll_repo(db, FakeClient(), repo, bot_usernames=set(), llm_healthy=True)
    # second poll: cursor equals newest updated_at, fake still returns the MR,
    # but sync is idempotent — no error, count still 1
    n = await poll_repo(db, FakeClient(), repo, bot_usernames=set(), llm_healthy=True)
    assert n == 1


async def test_poll_repo_resyncs_mr_with_pending_reconciliation_despite_stale_updated_at(db):
    """GitLab does NOT bump an MR's updated_at when a discussion reply/resolve
    happens on it. So an MR with an unresolved (disposition == 'open') bot Note
    must still get synced+reconciled even though list_merge_requests(updated_after=...)
    no longer returns it (its updated_at predates the cursor)."""
    repo = Repository(provider="gitlab", project_path="g/p3", gitlab_project_id=848)
    db.add(repo)
    await db.flush()

    base_mr = fx("mr")
    stale_iid = base_mr["iid"]  # 36
    fresh_iid = 9999

    fresh_mr = copy.deepcopy(base_mr)
    fresh_mr["iid"] = fresh_iid
    fresh_mr["updated_at"] = "2026-07-01T00:00:00.000Z"

    # First poll: only the "fresh" MR is visible via list_merge_requests. This
    # both creates the fresh MR row and advances the cursor.
    client = FakeClient(mrs_by_iid={fresh_iid: fresh_mr})
    n = await poll_repo(db, client, repo, bot_usernames=set(), llm_healthy=True)
    assert n == 1
    assert repo.poll_cursor["updated_after"] == fresh_mr["updated_at"]

    # Simulate the stale MR already having been synced previously (e.g. by an
    # earlier poll cycle before its updated_at fell behind the cursor), and
    # now carrying an unresolved bot suggestion the reconciler still needs to
    # revisit.
    stale_mr_row = MergeRequest(
        repo_id=repo.id, mr_iid=stale_iid, title=base_mr["title"],
        state="opened", source_branch=base_mr["source_branch"],
        target_branch=base_mr["target_branch"], head_sha=base_mr["sha"],
        web_url=base_mr["web_url"],
    )
    db.add(stale_mr_row)
    await db.flush()
    db.add(Note(
        mr_id=stale_mr_row.id, provider_note_id=1, author_type="bot",
        kind="inline", body="consider renaming this", disposition="open",
    ))
    await db.flush()

    # Second poll: list_merge_requests (cursor-scoped) returns ONLY the fresh
    # MR again (stale MR's updated_at is older than the cursor, per the real
    # GitLab behavior this bug is about). The stale MR must still be picked
    # up via the pending-reconciliation path.
    client2 = FakeClient(mrs_by_iid={stale_iid: base_mr, fresh_iid: fresh_mr})
    client2.listed_iids = [fresh_iid]  # simulate GitLab excluding the stale MR
    # The sweep is rate-limited to hourly (see
    # test_reconciliation_sweep_is_gated_to_once_an_hour), and the first poll
    # above already consumed this repo's slot. Lapse the cooldown so this test
    # exercises the stale-MR capability rather than the cadence.
    repo.poll_cursor = {**repo.poll_cursor,
                        "reconciled_at": (datetime.now(timezone.utc)
                                          - timedelta(hours=2)).isoformat()}
    await db.flush()
    n2 = await poll_repo(db, client2, repo, bot_usernames=set(), llm_healthy=True)

    # Both the cursor-fetched fresh MR and the pending-reconciliation stale MR
    # should have been synced+reconciled.
    assert n2 == 2

    # Cursor must NOT be affected by the pending-reconciliation MR: it should
    # still reflect only what list_merge_requests returned.
    assert repo.poll_cursor["updated_after"] == fresh_mr["updated_at"]


async def test_reconciliation_sweep_is_gated_to_once_an_hour(db):
    """The sweep re-fetches every MR carrying an open bot note -- five GitLab
    calls each -- and previously ran on every tick, which is as low as 120s on
    some repos. Cursor-driven sync stays per-tick so auto-review keeps its
    responsiveness; only the sweep is rate-limited."""
    repo = Repository(provider="gitlab", project_path="g/p6", gitlab_project_id=848)
    db.add(repo)
    await db.flush()

    base_mr = fx("mr")
    stale_iid = base_mr["iid"]
    fresh_iid = 9999
    fresh_mr = copy.deepcopy(base_mr)
    fresh_mr["iid"] = fresh_iid
    fresh_mr["updated_at"] = "2026-07-01T00:00:00.000Z"

    client = FakeClient(mrs_by_iid={fresh_iid: fresh_mr})
    await poll_repo(db, client, repo, bot_usernames=set(), llm_healthy=True)

    stale_row = MergeRequest(
        repo_id=repo.id, mr_iid=stale_iid, title=base_mr["title"], state="opened",
        source_branch=base_mr["source_branch"], target_branch=base_mr["target_branch"],
        head_sha=base_mr["sha"], web_url=base_mr["web_url"])
    db.add(stale_row)
    await db.flush()
    db.add(Note(mr_id=stale_row.id, provider_note_id=1, author_type="bot",
                kind="inline", body="b", disposition="open"))
    await db.flush()

    client2 = FakeClient(mrs_by_iid={stale_iid: base_mr, fresh_iid: fresh_mr})
    client2.listed_iids = [fresh_iid]

    # The first poll already ran the sweep, so within the hour the stale MR is
    # not re-fetched: only the cursor-listed fresh MR is.
    assert await poll_repo(db, client2, repo, bot_usernames=set(),
                           llm_healthy=True) == 1

    # Once the cooldown lapses the sweep runs again and picks the stale MR up.
    repo.poll_cursor = {**repo.poll_cursor,
                        "reconciled_at": (datetime.now(timezone.utc)
                                          - timedelta(hours=2)).isoformat()}
    await db.flush()
    assert await poll_repo(db, client2, repo, bot_usernames=set(),
                          llm_healthy=True) == 2


def test_reconciliation_due_only_after_the_cooldown():
    from argus.ingest.poller import (mark_reconciliation_ran,
                                         reconciliation_due)

    now = datetime.now(timezone.utc)
    repo = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=1)

    assert reconciliation_due(repo, now) is True  # never swept
    mark_reconciliation_ran(repo, now)
    assert reconciliation_due(repo, now) is False
    assert reconciliation_due(repo, now + timedelta(hours=2)) is True


def test_repo_in_learnings_cooldown_uses_created_at_and_the_per_repo_threshold():
    from argus.ingest.poller import repo_in_learnings_cooldown

    now = datetime.now(timezone.utc)
    fresh_repo = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=1,
                            created_at=now, learnings_cooldown_hours=72)
    assert repo_in_learnings_cooldown(fresh_repo, now) is True
    assert repo_in_learnings_cooldown(fresh_repo, now + timedelta(hours=71)) is True
    assert repo_in_learnings_cooldown(fresh_repo, now + timedelta(hours=73)) is False

    no_cooldown_repo = Repository(provider="gitlab", project_path="g/p2", gitlab_project_id=2,
                                  created_at=now, learnings_cooldown_hours=0)
    assert repo_in_learnings_cooldown(no_cooldown_repo, now) is False


async def test_distill_mr_not_enqueued_for_a_repo_still_in_learnings_cooldown(db, monkeypatch):
    """A newly-added repo must not get distill_mr jobs enqueued even when the
    reconciler surfaces notes to distill -- otherwise a new repo's initial
    historical backfill would flood the learnings store with noise mined
    from old, no-longer-relevant discussions."""
    from argus.ingest import poller as poller_module
    from argus.ingest.reconciler import ReconcileResult

    calls = []

    async def fake_reconcile_mr(session, client, repo, mr_row, x):
        return ReconcileResult(changed_note_ids=[])

    async def fake_enqueue(session, mr, now, *, only_bot_threads=False, quiet_hours=24):
        calls.append(mr.id)
        return []
    monkeypatch.setattr(poller_module, "reconcile_mr", fake_reconcile_mr)
    monkeypatch.setattr(poller_module, "enqueue_ready_threads", fake_enqueue)

    now = datetime.now(timezone.utc)
    fresh_repo = Repository(provider="gitlab", project_path="g/p8", gitlab_project_id=850,
                            created_at=now)
    db.add(fresh_repo)
    await db.flush()

    await poller_module._sync_and_reconcile_mr(
        db, FakeClient(), fresh_repo, fx("mr")["iid"], bot_usernames=set(), llm_healthy=True)

    assert calls == []


async def test_distill_mr_enqueued_once_learnings_cooldown_has_elapsed(db, monkeypatch):
    from argus.ingest import poller as poller_module
    from argus.ingest.reconciler import ReconcileResult

    calls = []

    async def fake_reconcile_mr(session, client, repo, mr_row, x):
        return ReconcileResult(changed_note_ids=[])

    async def fake_enqueue(session, mr, now, *, only_bot_threads=False, quiet_hours=24):
        calls.append(mr.id)
        return []
    monkeypatch.setattr(poller_module, "reconcile_mr", fake_reconcile_mr)
    monkeypatch.setattr(poller_module, "enqueue_ready_threads", fake_enqueue)

    now = datetime.now(timezone.utc)
    old_repo = Repository(provider="gitlab", project_path="g/p9", gitlab_project_id=851,
                          created_at=now - timedelta(hours=100))
    db.add(old_repo)
    await db.flush()

    await poller_module._sync_and_reconcile_mr(
        db, FakeClient(), old_repo, fx("mr")["iid"], bot_usernames=set(), llm_healthy=True)

    assert len(calls) == 1


async def test_the_poller_passes_the_settle_time_setting(db, monkeypatch):
    from argus.config import Settings
    from argus.ingest import poller as poller_module
    from argus.ingest.reconciler import ReconcileResult
    seen = {}

    async def fake_reconcile_mr(session, client, repo, mr_row, x):
        return ReconcileResult(changed_note_ids=[])

    async def fake_enqueue(session, mr, now, *, only_bot_threads=False, quiet_hours=24):
        seen["quiet_hours"] = quiet_hours
        return []

    async def fake_eff(session, base=None):
        return Settings(distill_quiet_hours=6)
    monkeypatch.setattr(poller_module, "reconcile_mr", fake_reconcile_mr)
    monkeypatch.setattr(poller_module, "enqueue_ready_threads", fake_enqueue)
    monkeypatch.setattr(poller_module, "load_effective_settings", fake_eff)
    now = datetime.now(timezone.utc)
    repo = Repository(provider="gitlab", project_path="g/p10", gitlab_project_id=852,
                      created_at=now - timedelta(hours=100))
    db.add(repo)
    await db.flush()

    await poller_module._sync_and_reconcile_mr(
        db, FakeClient(), repo, fx("mr")["iid"], bot_usernames=set(), llm_healthy=True)

    assert seen["quiet_hours"] == 6


def test_mark_reconciliation_preserves_other_cursor_keys():
    from argus.ingest.poller import mark_reconciliation_ran

    repo = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=1)
    repo.poll_cursor = {"updated_after": "2026-01-01T00:00:00Z",
                        "outcomes_resolved_at": "2026-01-01T00:00:00Z"}
    mark_reconciliation_ran(repo, datetime.now(timezone.utc))
    assert repo.poll_cursor["updated_after"] == "2026-01-01T00:00:00Z"
    assert repo.poll_cursor["outcomes_resolved_at"] == "2026-01-01T00:00:00Z"
    assert "reconciled_at" in repo.poll_cursor


async def test_pending_reconciliation_excludes_summary_notes(db):
    """A bot 'summary' note (the whole-MR review comment) is never attached to
    a resolvable discussion and never carries suggestions, so the reconciler's
    classify_suggestion_disposition can never move it out of 'open'. If
    _pending_reconciliation_mr_iids matched on disposition alone, EVERY MR
    argus has ever reviewed would be pinned in the pending set forever,
    causing the poller to re-sync+reconcile it on every single cycle. Only
    'inline' bot notes (which point at real, resolvable suggestions) should
    keep an MR in the pending set."""
    repo = Repository(provider="gitlab", project_path="g/p4", gitlab_project_id=848)
    db.add(repo)
    await db.flush()

    base_mr = fx("mr")

    summary_only_mr = MergeRequest(
        repo_id=repo.id, mr_iid=1001, title=base_mr["title"],
        state="opened", source_branch=base_mr["source_branch"],
        target_branch=base_mr["target_branch"], head_sha=base_mr["sha"],
        web_url=base_mr["web_url"],
    )
    inline_pending_mr = MergeRequest(
        repo_id=repo.id, mr_iid=1002, title=base_mr["title"],
        state="opened", source_branch=base_mr["source_branch"],
        target_branch=base_mr["target_branch"], head_sha=base_mr["sha"],
        web_url=base_mr["web_url"],
    )
    db.add_all([summary_only_mr, inline_pending_mr])
    await db.flush()

    db.add(Note(
        mr_id=summary_only_mr.id, provider_note_id=1, author_type="bot",
        kind="summary", body="Overall review summary", disposition="open",
    ))
    db.add(Note(
        mr_id=inline_pending_mr.id, provider_note_id=2, author_type="bot",
        kind="inline", body="consider renaming this", disposition="open",
    ))
    await db.flush()

    pending = await _pending_reconciliation_mr_iids(db, repo.id)

    assert inline_pending_mr.mr_iid in pending
    assert summary_only_mr.mr_iid not in pending


async def test_pending_reconciliation_excludes_stale_mrs(db):
    """An open MR nobody has pushed to in longer than stale_after_days is
    treated as abandoned and dropped from the pending-reconciliation set,
    even though it still carries an open bot note -- otherwise it would be
    re-synced and re-reconciled forever. An MR with no commit history at all
    (no mr_versions row) is kept: we can't call something stale when we
    don't know its age."""
    repo = Repository(provider="gitlab", project_path="g/p7", gitlab_project_id=849)
    db.add(repo)
    await db.flush()

    base_mr = fx("mr")
    now = datetime.now(timezone.utc)

    fresh_mr = MergeRequest(
        repo_id=repo.id, mr_iid=2001, title=base_mr["title"], state="opened",
        source_branch=base_mr["source_branch"], target_branch=base_mr["target_branch"],
        head_sha=base_mr["sha"], web_url=base_mr["web_url"],
    )
    stale_mr = MergeRequest(
        repo_id=repo.id, mr_iid=2002, title=base_mr["title"], state="opened",
        source_branch=base_mr["source_branch"], target_branch=base_mr["target_branch"],
        head_sha=base_mr["sha"], web_url=base_mr["web_url"],
    )
    no_version_mr = MergeRequest(
        repo_id=repo.id, mr_iid=2003, title=base_mr["title"], state="opened",
        source_branch=base_mr["source_branch"], target_branch=base_mr["target_branch"],
        head_sha=base_mr["sha"], web_url=base_mr["web_url"],
    )
    db.add_all([fresh_mr, stale_mr, no_version_mr])
    await db.flush()

    db.add_all([
        MRVersion(mr_id=fresh_mr.id, provider_version_id=1,
                 head_commit_sha="a" * 40, version_created_at=now - timedelta(days=1)),
        MRVersion(mr_id=stale_mr.id, provider_version_id=2,
                 head_commit_sha="b" * 40, version_created_at=now - timedelta(days=45)),
    ])
    db.add_all([
        Note(mr_id=fresh_mr.id, provider_note_id=1, author_type="bot",
            kind="inline", body="x", disposition="open"),
        Note(mr_id=stale_mr.id, provider_note_id=2, author_type="bot",
            kind="inline", body="x", disposition="open"),
        Note(mr_id=no_version_mr.id, provider_note_id=3, author_type="bot",
            kind="inline", body="x", disposition="open"),
    ])
    await db.flush()

    pending = await _pending_reconciliation_mr_iids(db, repo.id, stale_after_days=30)

    assert fresh_mr.mr_iid in pending
    assert stale_mr.mr_iid not in pending
    assert no_version_mr.mr_iid in pending


class _NoopClient:
    # run_poller_forever unconditionally calls get_current_user() (with a
    # try/except) at startup and aclose() at shutdown; a bare `lambda:
    # None` factory can't satisfy those calls, so this stub stands in for
    # the real GitLabClient without exercising any GitLab-specific logic.
    async def get_current_user(self):
        return {"username": "bot"}

    async def aclose(self):
        pass


async def test_run_poller_forever_rereads_interval(engine):
    import asyncio
    from argus.db import session_factory
    from argus.ingest.poller import run_poller_forever

    calls = []

    async def get_interval() -> int:
        calls.append(1)
        return 1

    stop = asyncio.Event()
    sf = session_factory(engine)
    task = asyncio.create_task(
        run_poller_forever(sf, _NoopClient, stop, interval_s=999,
                           get_interval=get_interval))
    await asyncio.sleep(0.1)
    stop.set()
    await task
    assert calls, "get_interval was never consulted"


async def test_run_poller_forever_survives_get_interval_failure(engine):
    """A transient failure in get_interval (e.g. a DB blip while loading
    effective settings) must not kill the poller loop: it should log, fall
    back to the static interval_s for that cycle, and keep polling."""
    import asyncio
    from argus.db import session_factory
    from argus.ingest.poller import run_poller_forever

    calls = []

    async def get_interval():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient settings-load failure")
        return 0.01

    stop = asyncio.Event()
    sf = session_factory(engine)
    # interval_s is the fallback used for the cycle where get_interval raises;
    # keep it short so the loop reaches a second get_interval call quickly.
    task = asyncio.create_task(
        run_poller_forever(sf, _NoopClient, stop, interval_s=0.05,
                           get_interval=get_interval))
    await asyncio.sleep(0.3)
    stop.set()
    await task
    assert len(calls) >= 2, (
        "poller loop did not survive a raising get_interval "
        f"(get_interval called {len(calls)} time(s))")


async def test_poller_syncs_nothing_until_it_knows_its_own_account(engine):
    # a failed lookup at startup used to sync for the process's lifetime with no bot name,
    # filing every argus comment as human
    import asyncio
    from argus.db import session_factory
    from argus.ingest.poller import run_poller_forever

    class _FlakyMe(_NoopClient):
        lookups, listed = 0, 0

        async def get_current_user(self):
            type(self).lookups += 1
            if type(self).lookups == 1:
                raise RuntimeError("DNS")
            return {"username": "pr_agent"}

        async def list_merge_requests(self, project, updated_after=None, state="all"):
            type(self).listed += 1
            return []

    sf = session_factory(engine)
    async with sf() as session:
        session.add(Repository(provider="gitlab", project_path="g/flaky", gitlab_project_id=903))
        await session.commit()
    stop = asyncio.Event()
    task = asyncio.create_task(run_poller_forever(sf, _FlakyMe, stop, interval_s=0.05))
    await asyncio.sleep(0.03)
    assert _FlakyMe.listed == 0, "synced before knowing which account is argus"
    await asyncio.sleep(0.2)
    stop.set()
    await task
    async with sf() as session:
        await session.execute(delete(Repository).where(Repository.gitlab_project_id == 903))
        await session.commit()
    assert _FlakyMe.lookups == 2 and _FlakyMe.listed >= 1


def test_authors_are_argus_people_or_other_bots():
    from argus.gitlab.normalizer import classify_author
    bots = {"pr_agent"}
    assert classify_author("pr_agent", bots) == "bot"
    assert classify_author("dev.op", bots) == "human"
    assert classify_author("developer.one", bots) == "human"
    assert classify_author("project_571_bot_8c10b9c981c91a557925aa2e40bd9d2c", bots) == "external_bot"
    assert classify_author("pr_agent", set()) == "external_bot", "never a human, even unresolved"


async def test_run_poller_forever_one_repo_failure_does_not_block_others(engine, caplog, monkeypatch):
    """A crash reconciling one repo must not silently skip every repo that
    comes after it in the same tick. Previously all enabled repos shared one
    session for the whole tick, so a failure that left that session's
    transaction unusable (or even just triggered session.rollback(), which
    expires every attached ORM object) meant the *next* repo's attribute
    access could raise on its own -- and the resulting secondary crash
    happened inside the except block's own logging call, escaping the
    per-repo try/except and aborting the rest of the tick with zero log
    output for any repo after the first failure."""
    import asyncio
    import logging
    monkeypatch.setattr(logging.getLogger("argus"), "propagate", True)
    from argus.db import session_factory
    from argus.ingest.poller import run_poller_forever

    sf = session_factory(engine)
    async with sf() as session:
        session.add_all([
            Repository(provider="gitlab", project_path="g/bad", gitlab_project_id=901),
            Repository(provider="gitlab", project_path="g/good", gitlab_project_id=902),
        ])
        await session.commit()

    class _FailingClient(_NoopClient):
        async def list_merge_requests(self, project, updated_after=None, state="all"):
            if project == 901:
                raise RuntimeError("simulated GitLab error for the bad repo")
            return []

    stop = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="argus.poller"):
        task = asyncio.create_task(
            run_poller_forever(sf, _FailingClient, stop, interval_s=999))
        await asyncio.sleep(0.2)
        stop.set()
        await task

    messages = [r.message for r in caplog.records]
    assert any("poll failed for g/bad" in m for m in messages), messages
    assert any("polled g/good: 0 MRs" in m for m in messages), (
        "the good repo was never reached/logged after the bad repo's "
        f"failure in the same tick: {messages}")



async def test_the_reconciliation_cycle_sweeps_pending_threads(db, monkeypatch):
    from argus.ingest import poller as poller_module
    swept = []

    async def fake_sweep(session, repo, now, *, quiet_hours=24):
        swept.append((repo.id, quiet_hours))
        return 0

    class _Empty(FakeClient):
        async def list_merge_requests(self, *a, **k):
            return []
    monkeypatch.setattr(poller_module, "sweep_pending_threads", fake_sweep)
    now = datetime.now(timezone.utc)
    repo = Repository(provider="gitlab", project_path="g/p11", gitlab_project_id=853,
                      created_at=now - timedelta(hours=100))
    db.add(repo)
    await db.flush()

    await poll_repo(db, _Empty(), repo, bot_usernames=set(), llm_healthy=True)
    assert [r for r, _ in swept] == [repo.id]

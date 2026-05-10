import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argus.domain.models import MergeRequest, Note, Repository
from argus.ingest.poller import _pending_reconciliation_mr_iids, poll_repo

FIX = Path(__file__).parent / "fixtures" / "gitlab"


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


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

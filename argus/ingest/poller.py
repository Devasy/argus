"""Cursor-based MR sweep. Optionally triggers incremental auto-reviews for
repos with auto_review_enabled, gated on a changed head_sha since the last
done review."""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from argus.domain.models import (Job, MergeRequest, Note, RawEvent,
                                     Repository, Review, ReviewerProfile)
from argus.gitlab.client import GitLabClient
from argus.gitlab.normalizer import sync_merge_request
from argus.ingest.reconciler import reconcile_mr
from argus.jobs.queue import enqueue
from argus.knowledge.outcomes import (mark_outcome_resolution_ran,
                                          outcome_resolution_due,
                                          resolve_outcomes_for_mr)
from argus.llm.config import resolve_llm_config
from argus.llm.health import check_llm_health
from argus.review.incremental import last_reviewed_version

logger = logging.getLogger("argus.poller")


async def _maybe_auto_review(session: AsyncSession, repo: Repository,
                             mr_row: MergeRequest, llm_healthy: bool) -> None:
    if not repo.auto_review_enabled or mr_row.draft or mr_row.state != "opened":
        return
    prior_version = await last_reviewed_version(session, mr_row.id)
    if prior_version is not None and prior_version.head_commit_sha == mr_row.head_sha:
        return
    if not llm_healthy:
        logger.warning("auto-review skipped for %s!%d: LLM endpoint unhealthy",
                       repo.project_path, mr_row.mr_iid)
        return
    # Matches trigger_review's dedup_key format (mode-suffixed, argus/api/
    # app.py) so a poller-triggered full review and a manually-triggered full
    # review dedup against each other. The poller only ever runs mode="full".
    dedup_key = f"review:{mr_row.id}:full"
    in_flight = (await session.execute(select(Job).where(
        Job.dedup_key == dedup_key, Job.status.in_(["queued", "running"])
    ))).scalar_one_or_none()
    if in_flight is not None:
        return
    try:
        cfg = await resolve_llm_config(session, None, None)
    except ValueError as e:
        logger.warning("auto-review skipped for %s!%d: %s",
                       repo.project_path, mr_row.mr_iid, e)
        return
    profile_version_id = None
    if repo.default_profile_id is not None:
        default_profile = await session.get(ReviewerProfile, repo.default_profile_id)
        if default_profile is not None:
            profile_version_id = default_profile.current_version_id
    review = Review(mr_id=mr_row.id, status="queued", llm_config=cfg.model_dump(),
                    profile_version_id=profile_version_id, trigger="poll")
    session.add(review)
    await session.flush()
    await enqueue(session, "review", {"review_id": str(review.id)},
                 dedup_key=dedup_key)


async def _pending_reconciliation_mr_iids(session: AsyncSession, repo_id) -> list[int]:
    """MR iids (scoped to repo_id) that have at least one bot Note still in the
    'open' disposition. GitLab does not bump an MR's updated_at when a human
    replies to / resolves a discussion, so these MRs can silently fall behind
    the poller's updated_after cursor while the reconciler still needs a fresh
    sync of them."""
    rows = (await session.execute(
        select(MergeRequest.mr_iid)
        .join(Note, Note.mr_id == MergeRequest.id)
        .where(MergeRequest.repo_id == repo_id,
              Note.author_type == "bot",
              Note.kind == "inline",
              Note.disposition == "open")
        .distinct()
    )).scalars().all()
    return list(rows)


def reconciliation_due(repo, now: datetime, cooldown_s: int = 3600) -> bool:
    """True when this repo's pending-reconciliation sweep has not run recently.

    The sweep costs five GitLab calls per MR carrying an open bot note, and it
    grows with every unresolved comment. Human dispositions only change on
    human timescales, so paying that on every tick (`poll_interval_s` is 120s
    on some repos) bought nothing. Cursor-driven sync is deliberately NOT
    gated: auto-review must still fire promptly on a new commit.
    """
    raw = (repo.poll_cursor or {}).get("reconciled_at")
    if raw is None:
        return True
    last = datetime.fromisoformat(raw)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= cooldown_s


def mark_reconciliation_ran(repo, now: datetime) -> None:
    """Stamp the sweep cooldown, preserving the other poll_cursor keys."""
    repo.poll_cursor = {**(repo.poll_cursor or {}),
                        "reconciled_at": now.isoformat()}


async def _sync_and_reconcile_mr(session: AsyncSession, client: GitLabClient,
                                 repo: Repository, iid: int,
                                 bot_usernames: set[str], llm_healthy: bool) -> None:
    mr_payload = await client.get_merge_request(repo.gitlab_project_id, iid)
    discussions = await client.list_discussions(repo.gitlab_project_id, iid)
    versions = await client.list_versions(repo.gitlab_project_id, iid)
    approvals = await client.get_approvals(repo.gitlab_project_id, iid)
    reviewers = await client.get_reviewers(repo.gitlab_project_id, iid)
    session.add(RawEvent(repo_id=repo.id, mr_iid=iid, event_type="mr_sweep",
                         payload={"mr": mr_payload, "approvals": approvals,
                                  "reviewers": reviewers}))
    mr_row = await sync_merge_request(session, repo, mr_payload, discussions,
                                      versions, approvals, reviewers, bot_usernames)
    await _maybe_auto_review(session, repo, mr_row, llm_healthy)
    result = await reconcile_mr(session, client, repo, mr_row, None)
    if result.note_ids:
        note_id_strs = sorted(str(n) for n in result.note_ids)
        batch_hash = hashlib.sha256(",".join(note_id_strs).encode()).hexdigest()[:16]
        await enqueue(session, "distill_mr",
                     {"mr_id": str(mr_row.id), "note_ids": note_id_strs},
                     dedup_key=f"distill_mr:{mr_row.id}:{batch_hash}")


async def poll_repo(session: AsyncSession, client: GitLabClient,
                    repo: Repository, bot_usernames: set[str],
                    llm_healthy: bool) -> int:
    cursor = (repo.poll_cursor or {}).get("updated_after")
    mrs = await client.list_merge_requests(repo.gitlab_project_id,
                                           updated_after=cursor, state="all")
    newest = cursor
    count = 0
    cursor_fetched_iids: set[int] = set()
    for head in mrs:
        iid = head["iid"]
        cursor_fetched_iids.add(iid)
        await _sync_and_reconcile_mr(session, client, repo, iid, bot_usernames, llm_healthy)
        count += 1
        if newest is None or (head.get("updated_at") or "") > newest:
            newest = head.get("updated_at")
    if newest:
        repo.poll_cursor = {**(repo.poll_cursor or {}), "updated_after": newest}

    if reconciliation_due(repo, datetime.now(timezone.utc)):
        pending_iids = await _pending_reconciliation_mr_iids(session, repo.id)
        for iid in pending_iids:
            if iid in cursor_fetched_iids:
                continue
            await _sync_and_reconcile_mr(session, client, repo, iid,
                                         bot_usernames, llm_healthy)
            count += 1
        mark_reconciliation_ran(repo, datetime.now(timezone.utc))

    if outcome_resolution_due(repo, datetime.now(timezone.utc)):
        mr_ids = (await session.execute(
            select(MergeRequest.id).where(MergeRequest.repo_id == repo.id)
        )).scalars().all()
        for mr_id in mr_ids:
            try:
                await resolve_outcomes_for_mr(session, mr_id)
            except Exception:
                logger.exception("outcome resolution failed for mr %s", mr_id)
        mark_outcome_resolution_ran(repo, datetime.now(timezone.utc))

    await session.commit()
    return count


async def run_poller_forever(sf: async_sessionmaker, client_factory,
                             stop: asyncio.Event, interval_s: int = 120,
                             get_interval=None) -> None:
    client: GitLabClient = client_factory()
    bot_usernames = set()
    try:
        me = await client.get_current_user()
        bot_usernames = {me["username"]}
    except Exception as e:
        logger.warning("could not resolve bot username: %s", e)
    while not stop.is_set():
        try:
            async with sf() as session:
                llm_healthy = True
                try:
                    cfg = await resolve_llm_config(session, None, None)
                    llm_healthy = await check_llm_health(cfg)
                    if not llm_healthy:
                        logger.warning(
                            "LLM endpoint unhealthy -- auto-review scheduling "
                            "paused this tick; MR sync/reconciliation continues")
                except ValueError:
                    pass  # no endpoint configured -- _maybe_auto_review handles this
                repos = (await session.execute(
                    select(Repository).where(Repository.enabled == True)  # noqa: E712
                )).scalars().all()
                for repo in repos:
                    try:
                        n = await poll_repo(session, client, repo, bot_usernames, llm_healthy)
                        logger.info("polled %s: %d MRs", repo.project_path, n)
                    except Exception:
                        logger.exception("poll failed for %s", repo.project_path)
                        await session.rollback()
        except Exception:
            logger.exception("poller tick failed")
        timeout = interval_s
        if get_interval:
            try:
                timeout = await get_interval()
            except Exception:
                logger.warning("get_interval failed; falling back to %ss",
                               interval_s, exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=timeout)
        except TimeoutError:
            pass
    await client.aclose()

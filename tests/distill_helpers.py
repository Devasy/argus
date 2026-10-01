"""Shared fixtures-as-functions for distillation thread tests."""
import uuid
from datetime import datetime, timedelta, timezone

from argus.domain.models import Discussion, MergeRequest, Note, Repository


async def _mr_disc(db):
    repo = Repository(provider="gitlab", project_path=f"g/dt-{uuid.uuid4().hex[:8]}",
                      gitlab_project_id=int(uuid.uuid4().int % 10**8))
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="s", target_branch="m", head_sha="abc", web_url="u")
    db.add(mr)
    await db.flush()
    disc = Discussion(mr_id=mr.id, provider_discussion_id=uuid.uuid4().hex)
    db.add(disc)
    await db.flush()
    return repo, mr, disc


async def _note(db, mr, disc, author_type, body, minutes, kind="inline"):
    n = Note(mr_id=mr.id, discussion_id=disc.id, provider_note_id=int(uuid.uuid4().int % 10**9),
             author_type=author_type, kind=kind, body=body,
             file_path="a.py" if kind == "inline" else None, line=3 if kind == "inline" else None,
             note_created_at=datetime.now(timezone.utc) + timedelta(minutes=minutes))
    db.add(n)
    await db.flush()
    return n


async def _resolved_bot_thread(db, mr):
    disc = Discussion(mr_id=mr.id, provider_discussion_id=uuid.uuid4().hex, resolved=True)
    db.add(disc)
    await db.flush()
    await _note(db, mr, disc, "bot", "use a context manager", 0)
    await _note(db, mr, disc, "human", "intentional, closed in finally", 5)
    return disc


async def purge_repos(engine, repo_ids) -> None:
    """Delete everything committed under these repos; later tests assert over whole tables."""
    from sqlalchemy import delete, select

    from argus.domain.models import (AgentComplaint, AuditRun, AuditVerdict,
                                         DistillationRun, DistillThread, Feedback, Job,
                                         Learning)
    if not repo_ids:
        return
    async with engine.begin() as conn:
        mr_ids = select(MergeRequest.id).where(MergeRequest.repo_id.in_(repo_ids))
        run_ids = select(DistillationRun.id).where(DistillationRun.mr_id.in_(mr_ids))
        audit_run_ids = select(AuditRun.id).where(AuditRun.repo_id.in_(repo_ids))
        learning_ids = select(Learning.id).where(Learning.repo_id.in_(repo_ids))
        await conn.execute(delete(AgentComplaint).where(
            AgentComplaint.distillation_run_id.in_(run_ids)))
        await conn.execute(delete(AgentComplaint).where(
            AgentComplaint.audit_run_id.in_(audit_run_ids)))
        await conn.execute(delete(AuditVerdict).where(
            AuditVerdict.audit_run_id.in_(audit_run_ids)
            | AuditVerdict.learning_id.in_(learning_ids)))
        await conn.execute(delete(Job).where(
            Job.id.in_(select(AuditRun.job_id).where(AuditRun.repo_id.in_(repo_ids),
                                                     AuditRun.job_id.isnot(None)))))
        await conn.execute(delete(AuditRun).where(AuditRun.repo_id.in_(repo_ids)))
        await conn.execute(delete(Learning).where(Learning.repo_id.in_(repo_ids)))
        await conn.execute(delete(DistillThread).where(DistillThread.mr_id.in_(mr_ids)))
        await conn.execute(delete(DistillationRun).where(DistillationRun.mr_id.in_(mr_ids)))
        await conn.execute(delete(Feedback).where(Feedback.note_id.in_(
            select(Note.id).where(Note.mr_id.in_(mr_ids)))))
        await conn.execute(delete(Note).where(Note.mr_id.in_(mr_ids)))
        await conn.execute(delete(Discussion).where(Discussion.mr_id.in_(mr_ids)))
        await conn.execute(delete(MergeRequest).where(MergeRequest.repo_id.in_(repo_ids)))
        await conn.execute(delete(Repository).where(Repository.id.in_(repo_ids)))

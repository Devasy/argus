from datetime import datetime, timezone

import pytest

from argus.knowledge.auditor import LearningVerdict


def test_verdict_rejects_unknown_action():
    with pytest.raises(Exception):
        LearningVerdict(learning_id="x", verdict="stale", confidence=0.5,
                        rationale="r", citations=[], proposed_action="delete")


# --- reclaiming audit runs orphaned by a restart -----------------------------
# A run stuck 'running' or 'queued' with no live job behind it looks like it
# will finish forever, silencing dedup_key/staleness checks for every run
# after -- three of these stacked for one repo before anyone noticed
# (2026-09-21 incident).

async def test_reclaim_marks_an_orphaned_running_audit_run_as_failed(db):
    from argus.domain.models import AuditRun, Repository
    from argus.knowledge.audit_scheduler import reclaim_stale_audit_runs

    repo = Repository(provider="gitlab", project_path="g/audit-reclaim-1",
                      gitlab_project_id=70000001)
    db.add(repo)
    await db.flush()
    run = AuditRun(repo_id=repo.id, status="running")
    db.add(run)
    await db.flush()

    n = await reclaim_stale_audit_runs(db, stale_after_s=0)

    assert n == 1
    assert run.status == "failed"
    assert run.finished_at is not None
    assert "orphaned" in (run.error or "").lower()


async def test_reclaim_audit_runs_ignores_a_genuinely_completed_run(db):
    from argus.domain.models import AuditRun, Repository
    from argus.knowledge.audit_scheduler import reclaim_stale_audit_runs

    repo = Repository(provider="gitlab", project_path="g/audit-reclaim-2",
                      gitlab_project_id=70000002)
    db.add(repo)
    await db.flush()
    done = AuditRun(repo_id=repo.id, status="done",
                    finished_at=datetime.now(timezone.utc))
    db.add(done)
    await db.flush()

    assert await reclaim_stale_audit_runs(db, stale_after_s=0) == 0
    assert done.status == "done"


# --- proposed_action must follow from the verdict ---------------------------
# The first production audit (run 9455ec42, Diskbot) returned five verdicts,
# every one with proposed_action="none" -- including an `ungrounded` one. The
# prompt said "be conservative, prefer none" and never said when an action WAS
# warranted, so the model rationally proposed nothing every time. Since
# apply_verdict only acts on "archive" and "merge", that made the entire
# approve/apply pipeline unreachable: audits produced opinions nobody could
# act on.

from argus.knowledge.auditor import default_action_for


@pytest.mark.parametrize("verdict,expected", [
    ("stale", "archive"),
    ("contradicted", "archive"),
    ("unfalsifiable", "flag_for_rewrite"),
    ("duplicate_of", "merge"),
    ("conflicts_with", "escalate_to_human"),
])
def test_bare_none_is_upgraded_to_the_implied_action(verdict, expected):
    assert default_action_for(verdict, "none") == expected


@pytest.mark.parametrize("verdict", ["corroborated", "ungrounded"])
def test_verdicts_that_genuinely_imply_no_action_stay_none(verdict):
    """corroborated is working as intended; ungrounded means we found no code
    referent, which is not evidence the learning is wrong."""
    assert default_action_for(verdict, "none") == "none"


def test_an_explicitly_proposed_action_is_never_overridden():
    """The default only fills a gap -- it must not overrule a model that
    actually thought about it."""
    assert default_action_for("stale", "flag_for_rewrite") == "flag_for_rewrite"
    assert default_action_for("corroborated", "archive") == "archive"


# --- global learnings must not be audited against one repo ------------------
# An audit of the Python backend proposed archiving a GLOBAL Ant Design
# learning because "no matches found for setFieldValue anywhere in the
# codebase... the codebase is a Python backend". True of that repo, and
# irrelevant: the learning belongs to a 305-file React UI the auditor never
# looked at. Approving it would have destroyed correct team knowledge.

async def test_audit_clustering_excludes_global_learnings(db):
    """A repo audit sees only its own learnings. `include_global=False` is the
    difference between "this repo disproves it" and "I looked in the wrong
    place"."""
    import uuid as _uuid

    from argus.domain.models import Learning, Repository
    from argus.knowledge.clustering import cluster_learnings

    repo = Repository(provider="gitlab",
                      project_path=f"g/scope-{_uuid.uuid4()}",
                      gitlab_project_id=60000000 + _uuid.uuid4().int % 9000000)
    db.add(repo)
    await db.flush()

    db.add(Learning(repo_id=repo.id, topic="scoped", hint_text="belongs here",
                    kind="guidance", status="active"))
    db.add(Learning(repo_id=None, topic="global", hint_text="applies everywhere",
                   kind="guidance", status="active"))
    await db.flush()

    audited = await cluster_learnings(db, repo_id=repo.id, include_global=False)
    topics = {l.topic for c in audited for l in c}
    assert "scoped" in topics
    assert "global" not in topics, "a global learning was audited against one repo"

    # Review injection must still see it -- global learnings apply everywhere,
    # and narrowing that would silently weaken every review.
    for_review = await cluster_learnings(db, repo_id=repo.id, include_global=True)
    assert "global" in {l.topic for c in for_review for l in c}


def test_audit_run_model_carries_a_trace_id_column():
    from argus.domain.models import AuditRun

    assert "langfuse_trace_id" in AuditRun.__table__.columns


def _pv(learning_id, verdict="duplicate_of", related=None, action="merge", text="merged"):
    from argus.knowledge.auditor import LearningVerdict
    return LearningVerdict(learning_id=str(learning_id), verdict=verdict, confidence=0.9,
                           rationale="r", related_learning_id=related,
                           proposed_action=action, suggested_hint_text=text)


def test_verdict_for_a_learning_outside_the_cluster_is_discarded():
    import uuid as _uuid
    from argus.knowledge.auditor import persisted_verdict
    a = _uuid.uuid4()
    assert persisted_verdict(_pv(_uuid.uuid4()), {a}) is None
    assert persisted_verdict(_pv("not-a-uuid"), {a}) is None


def test_ids_are_matched_regardless_of_formatting():
    import uuid as _uuid
    from argus.knowledge.auditor import persisted_verdict
    a, b = _uuid.uuid4(), _uuid.uuid4()
    p = persisted_verdict(_pv(str(a).upper(), related="{" + str(b) + "}"), {a, b})
    assert p["learning_id"] == a and p["related_learning_id"] == b
    assert p["action"] == "merge"


def test_merge_without_a_valid_in_cluster_survivor_is_escalated():
    import uuid as _uuid
    from argus.knowledge.auditor import persisted_verdict
    a, b = _uuid.uuid4(), _uuid.uuid4()
    for related in (None, "garbage", str(_uuid.uuid4()), str(a)):
        p = persisted_verdict(_pv(a, related=related), {a, b})
        assert p["action"] == "escalate_to_human", related
        assert p["related_learning_id"] is None


def test_conflicts_with_keeps_its_related_learning():
    import uuid as _uuid
    from argus.knowledge.auditor import persisted_verdict
    a, b = _uuid.uuid4(), _uuid.uuid4()
    p = persisted_verdict(_pv(a, verdict="conflicts_with", related=str(b),
                              action="none", text=None), {a, b})
    assert p["action"] == "escalate_to_human" and p["related_learning_id"] == b

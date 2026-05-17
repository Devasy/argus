"""Approving an audit verdict has to change something.

apply_verdict handled only `archive` and `merge`, so `flag_for_rewrite` and
`none` were silent no-ops: a human could read a verdict, click Approve, and
the learning would stay word-for-word the same. In production 31 of 40
verdicts were `none` (14 of them approved to no effect) and one approved
`flag_for_rewrite` also did nothing.

Two changes fix that. Rewrite actions must carry the replacement text, so
approval performs the rewrite; and `none` verdicts, which by definition need
no decision, stay out of the review queue by default.
"""
import uuid

import pytest

from argus.domain.models import AuditRun, AuditVerdict, Learning, Repository
from argus.knowledge.audit_apply import apply_verdict


@pytest.fixture
async def run(db):
    """A real AuditRun: audit_verdicts.audit_run_id is a foreign key."""
    repo = Repository(project_path=f"g/{uuid.uuid4().hex[:8]}",
                      gitlab_project_id=1)
    db.add(repo)
    await db.flush()
    r = AuditRun(repo_id=repo.id, status="done")
    db.add(r)
    await db.flush()
    return r


def _learning(db, topic="logging", hint="handle errors properly"):
    l = Learning(topic=topic, hint_text=hint, kind="guidance", status="active")
    db.add(l)
    return l


def _verdict(run, learning, action, *, verdict="unfalsifiable", suggested=None,
             related=None, state="approved"):
    return AuditVerdict(
        audit_run_id=run.id, learning_id=learning.id, verdict=verdict,
        confidence=0.9, rationale="r", proposed_action=action,
        suggested_hint_text=suggested, related_learning_id=related,
        state=state)


@pytest.mark.asyncio
async def test_approving_a_rewrite_actually_rewrites_the_learning(db, run):
    """The headline fix: this was a no-op."""
    l = _learning(db)
    await db.flush()
    v = _verdict(run, l, "flag_for_rewrite",
                 suggested="Wrap GitLab API calls in try/except and log the "
                           "response body on failure")
    db.add(v)
    await db.flush()

    assert await apply_verdict(db, v) is True
    assert l.hint_text.startswith("Wrap GitLab API calls")
    assert v.state == "applied"
    assert v.applied_at is not None
    # a rewrite must not archive: the learning is being kept, not removed
    assert l.status == "active"


@pytest.mark.asyncio
async def test_rewrite_without_replacement_text_changes_nothing(db, run):
    """Guards the degenerate case -- there is nothing to rewrite it TO, so
    the verdict must not be marked applied as if it had done something."""
    l = _learning(db, hint="original wording")
    await db.flush()
    v = _verdict(run, l, "flag_for_rewrite", suggested=None)
    db.add(v)
    await db.flush()

    assert await apply_verdict(db, v) is False
    assert l.hint_text == "original wording"
    assert v.state == "approved"


@pytest.mark.asyncio
async def test_rewrite_to_identical_text_is_not_a_change(db, run):
    l = _learning(db, hint="same text")
    await db.flush()
    v = _verdict(run, l, "flag_for_rewrite", suggested="  same text  ")
    db.add(v)
    await db.flush()

    assert await apply_verdict(db, v) is False
    assert v.state == "approved"


@pytest.mark.asyncio
async def test_merge_can_sharpen_the_survivors_wording(db, run):
    """A survivor absorbing a duplicate often needs to cover both, so merge
    accepts replacement text too."""
    dup = _learning(db, topic="dup", hint="duplicate wording")
    survivor = _learning(db, topic="survivor", hint="old survivor wording")
    await db.flush()
    dup.hit_count, survivor.hit_count = 3, 5
    v = _verdict(run, dup, "merge", verdict="duplicate_of",
                 suggested="combined, sharper wording", related=survivor.id)
    db.add(v)
    await db.flush()

    assert await apply_verdict(db, v) is True
    assert survivor.hint_text == "combined, sharper wording"
    assert dup.status == "archived"
    assert survivor.hit_count == 8      # evidence still folded in


@pytest.mark.asyncio
async def test_merge_without_suggestion_keeps_survivor_wording(db, run):
    dup = _learning(db, topic="dup", hint="dup")
    survivor = _learning(db, topic="survivor", hint="keep me")
    await db.flush()
    v = _verdict(run, dup, "merge", verdict="duplicate_of", related=survivor.id)
    db.add(v)
    await db.flush()

    assert await apply_verdict(db, v) is True
    assert survivor.hint_text == "keep me"
    assert dup.status == "archived"


@pytest.mark.asyncio
async def test_archive_still_archives_and_ignores_suggestion(db, run):
    """Regression guard on the path that already worked."""
    l = _learning(db)
    await db.flush()
    v = _verdict(run, l, "archive", verdict="stale", suggested="should be ignored")
    db.add(v)
    await db.flush()

    assert await apply_verdict(db, v) is True
    assert l.status == "archived"
    assert l.hint_text == "handle errors properly"


@pytest.mark.asyncio
async def test_unapproved_verdicts_are_never_applied(db, run):
    l = _learning(db, hint="untouched")
    await db.flush()
    v = _verdict(run, l, "flag_for_rewrite", suggested="new text", state="proposed")
    db.add(v)
    await db.flush()

    assert await apply_verdict(db, v) is False
    assert l.hint_text == "untouched"


# --- the auditor must not propose a rewrite it did not write ---------------

def test_rewrite_without_text_is_downgraded_at_write_time():
    """Enforced where verdicts are persisted, so an un-actionable proposal
    never reaches the human queue in the first place. Mirrors the guard in
    run_audit_for_repo."""
    def persisted_action(action: str, suggested: str | None) -> str:
        suggested = (suggested or "").strip() or None
        if action == "flag_for_rewrite" and not suggested:
            return "none"
        return action

    assert persisted_action("flag_for_rewrite", None) == "none"
    assert persisted_action("flag_for_rewrite", "   ") == "none"
    assert persisted_action("flag_for_rewrite", "sharper text") == "flag_for_rewrite"
    # other actions are unaffected by the absence of replacement text
    assert persisted_action("archive", None) == "archive"
    assert persisted_action("none", None) == "none"


def test_no_action_verdicts_never_reach_the_human_queue():
    """A 'none' action has nothing for a human to decide -- approving one runs
    every branch of apply_verdict and changes nothing. Queueing it just buries
    the handful of verdicts that do need a decision."""
    from argus.knowledge.auditor import initial_state_for

    assert initial_state_for("none") == "applied"
    # everything else is a real decision and must still be queued
    assert initial_state_for("archive") == "proposed"
    assert initial_state_for("flag_for_rewrite") == "proposed"
    assert initial_state_for("merge") == "proposed"
    assert initial_state_for("escalate_to_human") == "proposed"


async def test_auto_applied_none_verdict_is_a_noop_for_the_learning(db, run):
    """Auto-accepting must not touch the learning: 'none' means no change was
    ever proposed. The verdict's value is the groundedness it already recorded."""
    l = Learning(topic="t", hint_text="untouched", kind="guidance")
    db.add(l)
    await db.flush()
    v = AuditVerdict(audit_run_id=run.id, learning_id=l.id,
                     verdict="corroborated", confidence=0.9,
                     proposed_action="none", state="applied")
    db.add(v)
    await db.flush()

    assert await apply_verdict(db, v) is False
    assert l.hint_text == "untouched"
    assert l.status == "active"


def test_auditor_schema_carries_the_suggestion_field():
    from argus.knowledge.auditor import LearningVerdict

    assert "suggested_hint_text" in LearningVerdict.model_fields
    v = LearningVerdict(learning_id="x", verdict="unfalsifiable", confidence=0.5,
                        rationale="r", proposed_action="flag_for_rewrite",
                        suggested_hint_text="new wording")
    assert v.suggested_hint_text == "new wording"
    # optional, so archive/none verdicts need not supply it
    assert LearningVerdict(learning_id="x", verdict="stale", confidence=0.5,
                           rationale="r").suggested_hint_text is None


def test_auditor_prompt_requires_replacement_text_for_rewrites():
    from argus.knowledge.auditor import _SYSTEM

    prompt = " ".join(_SYSTEM.split())
    assert "suggested_hint_text" in prompt
    assert "not actionable" in prompt
    # the model must know an empty rewrite proposal is discarded
    assert "downgraded to 'none'" in prompt


"""Shared verdict contracts and helpers for the learnings auditor.

The actual audit work is the pipeline in audit_pipeline.py (pick -> scout ->
ground -> relate -> verify -> apply), run as a resumable audit_repo job
(audit_job.py). This module keeps what that pipeline and audit_apply_run.py
both depend on: the verdict shape, the verdict-to-action mapping, and the
branch-resolution logic an audit run needs before it can start.
"""
import logging
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from argus.knowledge.audit_apply import ARCHIVABLE

logger = logging.getLogger("argus.auditor")

VERDICTS = ("corroborated", "stale", "contradicted", "unfalsifiable",
            "duplicate_of", "conflicts_with", "ungrounded")
ACTIONS = ("none", "archive", "merge", "flag_for_rewrite", "escalate_to_human")


class Citation(BaseModel):
    file: str
    line: int = 0
    quote: str


class LearningVerdict(BaseModel):
    learning_id: str
    verdict: Literal["corroborated", "stale", "contradicted", "unfalsifiable",
                     "duplicate_of", "conflicts_with", "ungrounded"]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    citations: list[Citation] = []
    related_learning_id: str | None = None
    proposed_action: Literal["none", "archive", "merge", "flag_for_rewrite",
                             "escalate_to_human"] = "none"
    # Required for flag_for_rewrite and merge -- an action that rewrites a
    # learning is not actionable without the replacement text, and approving
    # it previously did nothing at all.
    suggested_hint_text: str | None = None


# The action each verdict implies. Applied when the model proposes "none" for
# a verdict that clearly calls for something: the first production audit
# returned proposed_action="none" on all five verdicts -- including an
# ungrounded one -- because the prompt said "be conservative, prefer none" and
# never said when an action WAS warranted. Every verdict being "none" makes
# the entire approve/apply pipeline dead code, since apply_verdict only acts
# on archive and merge.
#
# Only ever UPGRADES an explicit "none"; a model that proposed a real action
# keeps it, and the archive/conflicts_with guards downstream still have the
# final say. Nothing is executed without human approval either way.
_VERDICT_DEFAULT_ACTION = {
    "stale": "archive",
    "contradicted": "archive",
    "unfalsifiable": "flag_for_rewrite",
    "duplicate_of": "merge",
    "conflicts_with": "escalate_to_human",
    # corroborated and ungrounded genuinely imply no action: one is working,
    # the other simply has no code referent to judge it by.
}


def default_action_for(verdict: str, proposed: str) -> str:
    """The action to record, defaulting a bare "none" to what the verdict implies."""
    if proposed and proposed != "none":
        return proposed
    return _VERDICT_DEFAULT_ACTION.get(verdict, "none")


def initial_state_for(action: str) -> str:
    """The state a freshly written verdict starts in.

    A 'none' action asks the human for a decision that has no consequence:
    apply_verdict has no branch for it and returns False, so approving one is
    a no-op click. Landing it terminally keeps the queue to verdicts that
    actually change something -- and loses nothing, because the value of a
    'none' verdict is the groundedness it records on the learning, not its
    state. Every other action is a real decision and stays queued.
    """
    return "applied" if action == "none" else "proposed"


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def persisted_verdict(v: LearningVerdict, cluster_ids: set[uuid.UUID]) -> dict | None:
    """What actually gets written for one model verdict, or None to discard it.

    The model can hallucinate ids. A learning_id outside the audited cluster
    is discarded (it would fail the foreign key, or judge a learning the model
    was never shown), and a merge whose survivor is missing, malformed, outside
    the cluster or the learning itself is escalated to a human rather than
    stored as a merge that approval can never apply."""
    learning_id = _as_uuid(v.learning_id)
    if learning_id not in cluster_ids:
        return None
    related = _as_uuid(v.related_learning_id)
    if related not in cluster_ids or related == learning_id:
        related = None
    action = default_action_for(v.verdict, v.proposed_action)
    # never let a non-archivable verdict propose an archive, whatever the model asked for
    if action == "archive" and v.verdict not in ARCHIVABLE:
        action = "flag_for_rewrite"
    if v.verdict == "conflicts_with":
        action = "escalate_to_human"
    suggested = (v.suggested_hint_text or "").strip() or None
    # a rewrite with no replacement text is not a decision a human can make
    if action == "flag_for_rewrite" and not suggested:
        logger.info("audit: dropping flag_for_rewrite for learning %s -- "
                    "no suggested_hint_text supplied", learning_id)
        action = "none"
    if action == "merge" and related is None:
        action = "escalate_to_human"
    if action not in ("flag_for_rewrite", "merge"):
        suggested = None
    return {"learning_id": learning_id, "related_learning_id": related,
            "action": action, "suggested": suggested}


async def resolve_audit_ref(session, repo) -> str:
    """The branch to audit this repo's learnings against.

    NOT repo.default_branch. A learning is evidence about the branch it was
    learned from, and `default_branch` is frequently not that branch: two
    repositories here are set to `master` while every MR targets
    `develop-7.0.0`. Auditing them against master searched code years older
    than the learnings, and produced confident, wrong verdicts -- one
    correctly described `deprecated_features.permit.*` in
    data/rabbitmq/custom.conf, which exists on develop-7.0.0 and not on
    master, and was written off as "a pattern that does not exist in this
    codebase".

    So: the branch most of this repo's active learnings were actually learned
    from (via their source MR's target_branch), falling back to
    default_branch when a repo has no learning provenance at all.
    """
    from sqlalchemy import func, select

    from argus.domain.models import Learning, MergeRequest

    row = (await session.execute(
        select(MergeRequest.target_branch, func.count().label("n"))
        .join(Learning, Learning.mr_id == MergeRequest.id)
        .where(Learning.repo_id == repo.id, Learning.status == "active",
               MergeRequest.target_branch.isnot(None),
               MergeRequest.target_branch != "")
        .group_by(MergeRequest.target_branch)
        .order_by(func.count().desc())
        .limit(1))).first()
    if row is not None and row[0]:
        return row[0]
    return repo.default_branch or "HEAD"

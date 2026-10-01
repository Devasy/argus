"""Turn one audit run's ground/relate/verify output into verdict rows and learning updates."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import update

from argus.domain.models import AuditRun, AuditVerdict, Learning
from argus.knowledge.audit_apply import (apply_verdict, groundedness_from_verdict,
                                             validate_verdicts)
from argus.knowledge.auditor import LearningVerdict, initial_state_for, persisted_verdict

_DESTRUCTIVE_GROUND = ("stale", "contradicted")


async def apply_audit_results(deps, state) -> dict:
    s_ = deps.settings
    now = datetime.now(timezone.utc)
    due_ids = [d["id"] for d in state.due]
    raw = [v.model_dump() for v in state.grounded]
    kept, _ = validate_verdicts(raw, deps.workspace)
    ground = {v["learning_id"]: v for v in kept}
    rel = {r.learning_id: r for r in state.relations}
    checks = {c.learning_id: c for c in state.checks}
    allowed = {uuid.UUID(i) for i in due_ids} | {uuid.UUID(r.related_learning_id)
                                                 for r in state.relations}
    counts = {"verdicts": 0, "auto_archived": 0, "queued_for_humans": 0,
              "not_reached": len([i for i in due_ids if i not in {v.learning_id for v in state.grounded}])}
    async with deps.sf() as s:
        for lid, g in ground.items():
            r = rel.get(lid) if g["verdict"] not in _DESTRUCTIVE_GROUND else None
            lv = LearningVerdict(
                learning_id=lid, verdict=r.relation if r else g["verdict"],
                confidence=r.confidence if r else g["confidence"],
                rationale=r.rationale if r else g["rationale"], citations=g["citations"],
                related_learning_id=r.related_learning_id if r else None,
                suggested_hint_text=(r.suggested_hint_text if r else g.get("suggested_hint_text")))
            p = persisted_verdict(lv, allowed)
            if p is None:
                continue
            action, rationale = p["action"], lv.rationale
            check = checks.get(lid)
            if action in ("archive", "merge") and check is not None and not check.confirmed:
                action, rationale = "escalate_to_human", f"{rationale}\n\nverify disagreed: {check.reason}"
            auto = (s_.audit_auto_apply and action == "archive" and check is not None
                    and check.confirmed and lv.confidence * 100 >= s_.audit_auto_archive_min_pct)
            row = AuditVerdict(
                audit_run_id=deps.run_id, learning_id=p["learning_id"], verdict=lv.verdict,
                confidence=lv.confidence, rationale=rationale,
                citations=[c if isinstance(c, dict) else c.model_dump() for c in g["citations"]],
                related_learning_id=p["related_learning_id"] if action != "escalate_to_human"
                or lv.verdict == "conflicts_with" else None,
                proposed_action=action,
                suggested_hint_text=p["suggested"] if action in ("merge", "flag_for_rewrite") else None,
                state="approved" if auto else initial_state_for(action))
            s.add(row)
            await s.flush()
            if auto and await apply_verdict(s, row, actor="auditor"):
                counts["auto_archived"] += 1
            elif row.state == "proposed":
                counts["queued_for_humans"] += 1
            learning = await s.get(Learning, p["learning_id"])
            if learning is not None:
                learning.groundedness = groundedness_from_verdict(g["verdict"], g["confidence"])
                learning.last_audited_at = now
                learning.audited_at_sha = deps.commit_sha
            counts["verdicts"] += 1
        # A verdict validate_verdicts discarded (no citation resolved -- typical
        # for code that's since been deleted) still means the ground agent
        # examined this learning. It must not stay in tier 0 (last_audited_at
        # IS NULL) forever, which would make select_due_learnings put it first
        # in every future run and starve everything else out. Only the
        # timestamp is stamped -- the discarded verdict's evidence isn't
        # trusted, so groundedness is left untouched.
        for v in state.grounded:
            if v.learning_id in ground:
                continue
            learning = await s.get(Learning, uuid.UUID(v.learning_id))
            if learning is not None:
                learning.last_audited_at = now
                learning.audited_at_sha = deps.commit_sha
        await s.execute(update(AuditRun).where(AuditRun.id == deps.run_id).values(
            verdicts_written=counts["verdicts"], clusters_examined=len(state.groups)))
        await s.commit()
    return counts

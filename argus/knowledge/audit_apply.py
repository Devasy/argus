"""Validation and safe application of auditor verdicts.

The auditor is an LLM, so its confident-sounding claims are exactly the failure
mode this whole effort exists to remove. Nothing it says is trusted on its own:
every verdict that could destroy knowledge must carry a citation that we can
resolve against the real file, in code. A verdict whose quote is not literally
present in the cited file is discarded as a hallucination.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from argus.domain.models import AuditVerdict, Learning

logger = logging.getLogger("argus.audit_apply")

# Verdicts that may lead to archiving. 'ungrounded' is deliberately absent:
# absence of a code referent is not evidence a learning is wrong (process and
# deployment learnings have no code to point at). 'corroborated' is absent
# because it is the good case.
ARCHIVABLE = frozenset({"stale", "contradicted", "unfalsifiable", "duplicate_of"})

# Verdicts that make no claim about code and therefore need no citation.
_CITATION_EXEMPT = frozenset({"ungrounded", "unfalsifiable"})

_DIRECTION = {
    "corroborated": +1.0,
    "stale": -1.0,
    "contradicted": -1.0,
    "duplicate_of": -0.5,
    "unfalsifiable": -0.5,
    "conflicts_with": 0.0,
    "ungrounded": 0.0,
}


def citation_is_resolvable(citation: dict, workspace: Path) -> bool:
    """True when the quoted text actually appears in the cited file."""
    rel = (citation or {}).get("file")
    quote = (citation or {}).get("quote") or ""
    if not rel or not quote.strip():
        return False
    target = (Path(workspace) / rel).resolve()
    try:
        # Refuse to read outside the workspace.
        target.relative_to(Path(workspace).resolve())
    except ValueError:
        return False
    if not target.is_file():
        return False
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return quote.strip() in content


def validate_verdicts(verdicts: list[dict],
                       workspace: Path) -> tuple[list[dict], list[str]]:
    """Keep only verdicts backed by at least one resolvable citation.

    Returns (kept, rejection_reasons) so the run can log precisely what the
    model claimed and why it was thrown away.
    """
    kept, reasons = [], []
    for v in verdicts:
        verdict = v.get("verdict")
        if verdict not in _DIRECTION:
            reasons.append(f"unknown verdict {verdict!r}")
            continue
        if verdict in _CITATION_EXEMPT:
            kept.append(v)
            continue
        citations = v.get("citations") or []
        if not citations:
            reasons.append(f"{verdict} for {v.get('learning_id')}: no citation")
            continue
        if not any(citation_is_resolvable(c, workspace) for c in citations):
            reasons.append(
                f"{verdict} for {v.get('learning_id')}: no citation resolved "
                "against the audited tree")
            continue
        kept.append(v)
    if reasons:
        logger.warning("discarded %d unverifiable verdict(s): %s",
                        len(reasons), "; ".join(reasons[:5]))
    return kept, reasons


def groundedness_from_verdict(verdict: str, confidence: float) -> float:
    """Map a verdict to a [0,1] groundedness score, 0.5 = neutral/no signal.

    Confidence scales how far from neutral the verdict moves the score, so a
    hedged claim cannot swing a learning as hard as a certain one.
    """
    direction = _DIRECTION.get(verdict, 0.0)
    c = max(0.0, min(1.0, confidence))
    return max(0.0, min(1.0, 0.5 + 0.5 * direction * c))


async def apply_verdict(session: AsyncSession, verdict: AuditVerdict, *,
                         actor: str = "auditor") -> bool:
    """Apply an approved verdict. Returns True when something changed.

    Only ever archives -- never deletes -- and always leaves the justifying
    citation on the verdict row for audit and undo.
    """
    if verdict.state not in ("approved",):
        return False
    learning = await session.get(Learning, verdict.learning_id)
    if learning is None:
        return False

    changed = False
    if verdict.proposed_action == "archive" and verdict.verdict in ARCHIVABLE:
        if learning.status != "archived":
            learning.status = "archived"
            changed = True
    elif verdict.proposed_action == "flag_for_rewrite":
        # Previously a no-op: apply_verdict handled only archive and merge, so
        # a human could approve "this learning needs rewriting" and watch it
        # stay word-for-word the same. With replacement text supplied by the
        # auditor, approval now performs the rewrite.
        new_text = (verdict.suggested_hint_text or "").strip()
        if new_text and new_text != (learning.hint_text or "").strip():
            learning.hint_text = new_text
            changed = True
    elif verdict.proposed_action == "merge" and verdict.related_learning_id:
        survivor = await session.get(Learning, verdict.related_learning_id)
        if survivor is not None and learning.id != survivor.id:
            # Fold evidence into the survivor so the merge does not throw away
            # the duplicate's accumulated verdicts.
            survivor.hit_count += learning.hit_count
            survivor.miss_count += learning.miss_count
            survivor.harmful_count += learning.harmful_count
            survivor.ignored_count += learning.ignored_count
            survivor.inconclusive_count += learning.inconclusive_count
            # A merge may also sharpen the survivor's wording, since it now
            # has to cover what the duplicate said too.
            merged_text = (verdict.suggested_hint_text or "").strip()
            if merged_text:
                survivor.hint_text = merged_text
            learning.status = "archived"
            changed = True

    if changed:
        verdict.state = "applied"
        verdict.applied_at = datetime.now(timezone.utc)
        logger.info("applied %s (%s) to learning %s by %s",
                    verdict.proposed_action, verdict.verdict, learning.id, actor)
        await session.flush()
    return changed

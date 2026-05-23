import logging
import re
from pathlib import Path

from pydantic import BaseModel

from argus.review.artifacts import CandidateFinding

logger = logging.getLogger("argus.grounding")

# Mechanical citation decoration observed on real evidence_quote values --
# NOT an attempt at fuzzy matching. Measured across the golden set's dry
# runs: 94% of all historical ungrounded findings carry one of these two
# shapes. get_file_lines/get_hunk render "{line_no} | {text}" (tools.py:219)
# and reviewer agents sometimes copy that straight into evidence_quote
# instead of the bare source line; others write their own "N-M: " citation
# range. Stripping this recovered 26% of a real sample of ungrounded
# findings against the actual reference source -- the rest were the design
# agent quoting its own paraphrase instead of code, which correctly stays
# ungrounded; stripping decoration must never turn this into a fuzzy match.
_LINE_PREFIX_RE = re.compile(r"^\d+\s*\|\s*")
_RANGE_PREFIX_RE = re.compile(r"^\d+(-\d+)?:\s*")


def _clean_needle(line: str) -> str:
    line = _LINE_PREFIX_RE.sub("", line)
    line = _RANGE_PREFIX_RE.sub("", line)
    if len(line) >= 2 and line[0] == line[-1] == '"':
        line = line[1:-1]
    return line.strip()


class GroundingResult(BaseModel):
    finding_id: str
    grounded: bool
    corrected_line: int | None = None


def _first_line(quote: str) -> str:
    lines = quote.strip().splitlines()
    return lines[0].strip() if lines else ""


def ground_finding(f: CandidateFinding, workspace: Path) -> GroundingResult:
    target = (workspace / f.file_path)
    if not target.exists():
        return GroundingResult(finding_id=f.finding_id, grounded=False)
    raw_needle = _first_line(f.evidence_quote or "")
    if not raw_needle:
        return GroundingResult(finding_id=f.finding_id, grounded=False)
    lines = target.read_text(errors="replace").splitlines()
    cleaned_needle = _clean_needle(raw_needle)
    # The exact quote first (unchanged from before this decoration handling
    # existed), then the decoration-stripped one only if it differs -- so an
    # already-working exact match is never affected by the new pass.
    needles = [raw_needle]
    if cleaned_needle and cleaned_needle != raw_needle:
        needles.append(cleaned_needle)
    for needle in needles:
        if 1 <= f.line <= len(lines) and needle in lines[f.line - 1]:
            return GroundingResult(finding_id=f.finding_id, grounded=True)
        for i, line in enumerate(lines, start=1):
            if needle in line:
                return GroundingResult(finding_id=f.finding_id, grounded=True,
                                       corrected_line=i)
    return GroundingResult(finding_id=f.finding_id, grounded=False)


def apply_grounding(findings: list[CandidateFinding],
                    workspace: Path) -> tuple[list[CandidateFinding], list[str]]:
    kept, dropped = [], []
    for f in findings:
        r = ground_finding(f, workspace)
        if not r.grounded:
            dropped.append(f.finding_id)
            continue
        if r.corrected_line is not None:
            f.line = r.corrected_line
        kept.append(f)
    if dropped:
        logger.warning("grounding dropped %d/%d findings: %s",
                       len(dropped), len(findings), dropped)
    return kept, dropped


def ground_results(findings: list[CandidateFinding],
                   workspace: Path) -> list[GroundingResult]:
    """Like `apply_grounding` but returns the raw per-finding results instead
    of mutating/filtering `findings`, so callers can propagate drops and line
    corrections explicitly (e.g. via graph state) without relying on
    `apply_grounding`'s in-place mutation as the propagation mechanism."""
    results = [ground_finding(f, workspace) for f in findings]
    dropped = [r.finding_id for r in results if not r.grounded]
    if dropped:
        logger.warning("grounding dropped %d/%d findings: %s",
                       len(dropped), len(findings), dropped)
    return results

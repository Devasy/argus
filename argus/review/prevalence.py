"""Mechanical "is this already the convention?" evidence for candidate findings.

VERIFY_STATIC already tells the verifier to reject a finding whose pattern is
already used elsewhere. It does not reliably do it: verify makes ~0.8
search_code calls per verdict, so most findings never get the check, and
adjudicating nine disputed findings against real source gave 6 human-right, 3
trivial, 0 bot-right -- the misses being a convention in 24 other modals, a
pattern with 50+ instances, and similar. Asking a weak model again, in a
longer prompt, is not a fix. So the search is run for it and the count is put
next to the claim.

Presented as data, never as a verdict: a bad pattern repeated fifty times is
still bad, it just is not this MR's doing, and that is verify's call to make.
"""
import re
from pathlib import Path

from argus.review import navigation

# Below this, a quote matches everywhere and the count is noise, not evidence.
MIN_PROBE_CHARS = 12
MAX_PREVALENCE_MATCHES = 60


def quote_probe(evidence_quote: str | None) -> str | None:
    """The longest line of the quote, if it is distinctive enough to search."""
    lines = [l.strip() for l in (evidence_quote or "").splitlines()]
    usable = [l for l in lines if len(l) >= MIN_PROBE_CHARS]
    return max(usable, key=len) if usable else None


def _hit_path(hit: str) -> str | None:
    """`./src/a.py:12:text` -> `src/a.py`."""
    parts = hit.split(":", 2)
    if len(parts) < 3:
        return None
    return parts[0].removeprefix("./")


async def count_other_files(workspace: Path, probe: str, own_path: str) -> int:
    """How many files OTHER than own_path already contain this line."""
    hits = await navigation.search(workspace, re.escape(probe), "", 0,
                                   MAX_PREVALENCE_MATCHES)
    paths = {p for p in (_hit_path(h) for h in hits) if p}
    paths.discard(own_path.removeprefix("./"))
    return len(paths)


def prevalence_note(count: int) -> str:
    """Rendered inline beside the finding, or empty when there is nothing to say."""
    if count < 2:
        return ""
    if count >= MAX_PREVALENCE_MATCHES:
        return (f"NOTE: this exact line already appears in "
                f"{MAX_PREVALENCE_MATCHES}+ other file(s)")
    return f"NOTE: this exact line already appears in {count} other file(s)"


async def prevalence_notes(workspace: Path, findings: list) -> dict[str, str]:
    """finding_id -> prevalence note, omitting findings with nothing to report."""
    notes: dict[str, str] = {}
    for f in findings:
        probe = quote_probe(f.evidence_quote)
        if probe is None:
            continue
        try:
            count = await count_other_files(workspace, probe, f.file_path)
        except Exception:
            continue
        note = prevalence_note(count)
        if note:
            notes[f.finding_id] = note
    return notes

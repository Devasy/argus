"""Flatten a review's stage artifacts into one candidate list per graph node.

Lives here rather than in the API because the benchmark runner needs it too and
should not import the web app.
"""
from argus.domain.models import ReviewStage


def review_candidates(stages: list[ReviewStage]) -> list[dict]:
    """Every candidate finding, tagged with the graph node that produced it and
    with verify's verdict where one exists. valid=None means never verified,
    which is not the same as rejected."""
    verdicts: dict[str, dict] = {}
    # Grounding flags verify, not gates it; "ungrounded" below is only a fallback label for when verify never returned a verdict at all.
    ungrounded: set[str] = set()
    for s in stages:
        if s.stage_name != "verify" or not isinstance(s.artifact, dict):
            continue
        for v in s.artifact.get("verdicts") or []:
            if isinstance(v, dict) and v.get("finding_id"):
                verdicts[v["finding_id"]] = v
        for fid in s.artifact.get("ungrounded") or []:
            if isinstance(fid, str):
                ungrounded.add(fid)

    # There is no bare "analyze" node -- the graph has per-chunk nodes and
    # per-agent nodes -- so the node comes from the finding's own `stage`,
    # which every finding carries. Reading it off the artifact it appeared in
    # stranded agent findings whose own stage row failed to write one.
    stage_names = {s.stage_name for s in stages}

    def node_for(f: dict) -> str:
        fstage, chunk = f.get("stage"), f.get("chunk_id")
        if fstage and fstage != "analysis":
            scoped = f"{fstage}:{chunk}" if chunk else None
            return scoped if scoped in stage_names else fstage
        if chunk:
            return f"analyze:{chunk}"
        return "verify"

    best: dict[str, dict] = {}
    for s in stages:
        if not isinstance(s.artifact, dict):
            continue
        items = s.artifact.get("findings")
        if not isinstance(items, list):
            continue
        for f in items:
            if not isinstance(f, dict) or not f.get("finding_id"):
                continue
            fid = f["finding_id"]
            if fid in best:
                continue  # same node either way now, so first is fine
            if fid in verdicts:
                valid, gate = verdicts[fid].get("valid"), "verify"
                reason = verdicts[fid].get("reason")
            elif fid in ungrounded:
                valid, gate = False, "grounding"
                reason = ("evidence quote not found in the source; verify "
                          "did not return a verdict for it")
            else:
                valid, gate, reason = None, None, None
            best[fid] = {
                "finding_id": fid, "node": node_for(f), "title": f.get("title"),
                "file_path": f.get("file_path"), "line": f.get("line"),
                "severity": f.get("severity"), "type": f.get("type"),
                "valid": valid, "gate": gate, "reason": reason}
    return list(best.values())

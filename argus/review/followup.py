"""Follow up on argus's own earlier inline comments during an incremental re-review.

Checks open bot threads that are either silent (and whose file changed) or whose human replies only
claim a fix, then acts per the plan's table (docs/superpowers/plans/2026-09-24-followup-and-sister-repos.md).
At most one follow-up reply per thread, ever; every check is recorded as feedback kind 'followup'.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select

from argus.domain.models import Feedback, Note
from argus.ingest.reconciler import _reply_looks_like_acceptance as reply_claims_fix
from argus.llm.factory import build_chat_model

logger = logging.getLogger("argus.followup")

MODES = ("off", "reply_only", "resolve")
POSITIVE = ("addressed", "code_removed")
MAX_CANDIDATES = 25
SNIPPET_SPAN = 30
MAX_DIFF_LINES = 200
CHECK_TIMEOUT_S = 180
CHECK_REASONING_TOKENS = 2048
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FOOTER_RE = re.compile(r"\n*<sub>argus.*?</sub>\s*$", re.S)

SYSTEM = """You check whether a code review comment has been addressed by later commits.
You get the review comment, the code as it was when the comment was posted, the diff since then,
and the code now. Decide about THIS comment's specific concern only.

Answer with ONLY a JSON object: {"verdict": "...", "evidence": "..."}
- "addressed": the exact problem the comment describes is gone in the new code, and you can name
  the change that fixed it. The fix may differ from any suggestion in the comment.
- "code_removed": the code the comment is about was deleted, so the concern no longer applies.
- "not_addressed": the problem is still there. Renames, reformatting, moved code and unrelated
  edits near the line do NOT count as addressing it.
- "unsure": you cannot tell from what you were given.
"evidence" is one neutral, factual sentence of at most 25 words naming the concrete change
(or what is still unchanged). No judgement words, no advice, no pressure.
When in doubt, answer "not_addressed" or "unsure" -- a wrong "addressed" closes a real issue."""


@dataclass
class FollowupStats:
    candidates: int = 0
    checked: int = 0
    verdicts: dict = field(default_factory=dict)
    replied: int = 0
    resolved: int = 0
    errors: int = 0


def reviewed_sha_for(position: dict, versions: list, posted_at: str) -> str | None:
    """Commit the comment was posted on; GitLab may have already tracked the position forward."""
    sha = (position or {}).get("head_sha")
    born = next((v["created_at"] for v in versions if v.get("head_commit_sha") == sha), None)
    if sha and born and born <= posted_at:
        return sha
    before = [v for v in versions if v.get("created_at", "") <= posted_at]
    return max(before, key=lambda v: v["created_at"])["head_commit_sha"] if before else None


def map_line(diff_text: str, old_line: int) -> int:
    """Where old_line of the old file sits in the new file, following the diff's hunks."""
    offset = 0
    for raw in (diff_text or "").splitlines():
        m = HUNK_RE.match(raw)
        if not m:
            continue
        o_start, o_len = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        n_start, n_len = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
        if old_line < o_start:
            break
        if old_line < o_start + max(o_len, 1):
            return n_start
        offset = (n_start + n_len) - (o_start + o_len)
    return max(1, old_line + offset)


def numbered(text: str | None, line: int, span: int = SNIPPET_SPAN) -> str:
    if text is None:
        return "(file does not exist at this commit)"
    rows = text.splitlines()
    lo, hi = max(1, line - span), min(len(rows), line + span)
    return "\n".join(f"{i:>5}{'>' if i == line else ' '} {rows[i - 1]}" for i in range(lo, hi + 1))


def _text_of(content) -> str:
    """A langchain message's .content, normalized to plain text.

    content is a list of blocks -- not a plain string -- whenever the model
    ran with reasoning enabled: langchain_litellm's
    _inject_reasoning_content_into_content prepends a {"type": "thinking"}
    block ahead of the real {"type": "text"} answer. Only the text blocks are
    kept, so a stray {...} inside the model's own reasoning is never mistaken
    for its actual verdict."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_verdict(text) -> tuple[str, str]:
    body = re.sub(r"<think>.*?</think>", "", _text_of(text), flags=re.S)
    for m in re.finditer(r"\{[^{}]*\}", body, re.S):
        try:
            data = json.loads(m.group(0))
        except ValueError:
            continue
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict in ("addressed", "code_removed", "not_addressed", "unsure"):
            return verdict, str(data.get("evidence", "")).strip()[:400]
    return "unsure", "could not parse the model's answer"


def reply_body(kind: str, verdict: str, evidence: str, head_sha: str, mode: str,
               path: str) -> str | None:
    """The one reply this thread gets, or None to stay silent (see the plan's action table)."""
    sha = f"`{head_sha[:8]}`"
    resolved_tail = "<sub>argus follow-up · resolved automatically; reopen if this isn't right</sub>"
    if verdict in POSITIVE:
        if kind == "claimed":
            return f"Thanks, confirmed in {sha}.\n\n{resolved_tail}" if mode == "resolve" else None
        what = "this was addressed" if verdict == "addressed" else "the flagged code was removed"
        tail = resolved_tail if mode == "resolve" else (
            "<sub>argus follow-up · leaving this open for you to resolve</sub>")
        return f"✅ Looks like {what} in {sha}: {evidence}\n\n{tail}"
    if verdict == "not_addressed" and kind == "claimed":
        return (f"Thanks for the update! I couldn't spot this change in `{path}` at {sha} — it may "
                f"live in another file or be handled differently. No action needed if it's covered."
                f"\n\n<sub>argus follow-up · I won't comment on this thread again</sub>")
    return None


def eligible(discussion: dict, bot: str, bot_note_ids: set[int], history: dict,
             head_sha: str) -> tuple[dict, str] | None:
    """(bot's first note, "silent" | "claimed") for an open, unsettled bot diff thread, else None."""
    notes = discussion.get("notes") or []
    first = notes[0] if notes else None
    if not first or first.get("id") not in bot_note_ids or first.get("type") != "DiffNote":
        return None
    if first.get("resolved") or not first.get("position"):
        return None
    human = [n for n in notes[1:] if not n.get("system") and n["author"]["username"] != bot]
    if human and not all(reply_claims_fix(n.get("body") or "") for n in human):
        return None
    past = history.get(first["id"], [])
    if any(p.get("action") in ("replied", "resolved") or p.get("verdict") in POSITIVE
           or p.get("head_sha") == head_sha for p in past):
        return None
    return first, ("claimed" if human else "silent")


async def follow_up_prior_comments(sf, gitlab, project_id: int, mr_iid: int, mr_db_id,
                                   head_sha: str, workspace: Path, llm_cfg, mode: str
                                   ) -> FollowupStats:
    stats = FollowupStats()
    if mode not in MODES or mode == "off":
        return stats
    async with sf() as s:
        bot_notes = (await s.execute(select(Note).where(
            Note.mr_id == mr_db_id, Note.author_type == "bot", Note.kind == "inline"))).scalars().all()
        by_provider = {n.provider_note_id: n for n in bot_notes}
        rows = (await s.execute(select(Feedback).where(
            Feedback.kind == "followup",
            Feedback.note_id.in_([n.id for n in bot_notes])))).scalars().all() if bot_notes else []
    if not by_provider:
        return stats
    note_by_id = {n.id: n for n in bot_notes}
    history: dict = {}
    for f in rows:
        history.setdefault(note_by_id[f.note_id].provider_note_id, []).append(f.payload or {})

    bot = (await gitlab.get_current_user())["username"]
    discussions = await gitlab.list_discussions(project_id, mr_iid)
    versions = await gitlab.list_versions(project_id, mr_iid)
    candidates = [(d, *hit) for d in discussions
                  if (hit := eligible(d, bot, set(by_provider), history, head_sha))]
    stats.candidates = len(candidates)

    compares: dict = {}
    old_files: dict = {}
    check_cfg = llm_cfg.model_copy(update={"timeout": CHECK_TIMEOUT_S,
                                           "reasoning_budget_tokens": CHECK_REASONING_TOKENS})
    model = build_chat_model(check_cfg)
    for d, first, kind in candidates[:MAX_CANDIDATES]:
        pos = first["position"]
        path = pos.get("new_path") or pos.get("old_path")
        line = pos.get("new_line") or pos.get("old_line")
        reviewed = reviewed_sha_for(pos, versions, first.get("created_at", ""))
        if not path or not line or not reviewed or reviewed == head_sha:
            continue
        try:
            if reviewed not in compares:
                compares[reviewed] = await gitlab._get(
                    f"/projects/{project_id}/repository/compare",
                    **{"from": reviewed, "to": head_sha, "straight": "true"})
            entry = next((x for x in compares[reviewed].get("diffs", [])
                          if path in (x.get("old_path"), x.get("new_path"))), None)
            if entry is None and kind == "silent":
                continue
            entry = entry or {"new_path": path, "diff": ""}
            new_path = entry.get("new_path") or path
            if (reviewed, path) not in old_files:
                try:
                    old_files[(reviewed, path)] = await gitlab.get_file_raw(project_id, path, reviewed)
                except Exception:
                    old_files[(reviewed, path)] = None
            new_file = workspace / new_path
            new_text = (None if entry.get("deleted_file") or not new_file.is_file()
                        else new_file.read_text(errors="replace"))
            diff = "\n".join((entry.get("diff") or "").splitlines()[:MAX_DIFF_LINES]) or (
                "(this file is unchanged since the comment)")
            new_line = map_line(entry.get("diff") or "", int(line))
            comment = FOOTER_RE.sub("", first.get("body") or "")
            prompt = (f"## Review comment (on {path}, line {line})\n{comment}\n\n"
                      f"## Code when the comment was posted ({reviewed[:8]})\n"
                      f"{numbered(old_files[(reviewed, path)], int(line))}\n\n"
                      f"## Diff of {path} since then\n{diff}\n\n"
                      f"## Code now ({head_sha[:8]}, {new_path})\n{numbered(new_text, new_line)}")
            answer = await model.ainvoke([SystemMessage(content=SYSTEM), HumanMessage(content=prompt)])
            verdict, evidence = parse_verdict(getattr(answer, "content", "") or "")
        except Exception as e:
            logger.warning("follow-up check failed for note %s: %s", first.get("id"), e)
            stats.errors += 1
            continue
        stats.checked += 1
        stats.verdicts[verdict] = stats.verdicts.get(verdict, 0) + 1
        action = "none"
        body = reply_body(kind, verdict, evidence, head_sha, mode, path)
        if body:
            try:
                await gitlab.create_note(project_id, mr_iid, d["id"], body)
                action, stats.replied = "replied", stats.replied + 1
                if mode == "resolve" and verdict in POSITIVE:
                    await gitlab.resolve_discussion(project_id, mr_iid, d["id"], True)
                    action, stats.resolved = "resolved", stats.resolved + 1
            except Exception as e:
                logger.warning("follow-up action failed for note %s: %s", first.get("id"), e)
                stats.errors += 1
        async with sf() as s:
            s.add(Feedback(note_id=by_provider[first["id"]].id, kind="followup",
                           payload={"verdict": verdict, "evidence": evidence, "mode": mode,
                                    "thread": kind,
                                    "action": action, "head_sha": head_sha,
                                    "reviewed_sha": reviewed}))
            await s.commit()
    logger.info("follow-up on MR %s: %s", mr_iid, stats)
    return stats

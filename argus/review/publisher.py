"""The only component that posts review output to GitLab. Post + record atomically."""
import logging
import re
import uuid

from sqlalchemy import select

from argus.domain.models import Finding, Note, Review
from argus.review.artifacts import (CandidateFinding, CompiledReview,
                                        ReviewState, Verdict)

logger = logging.getLogger("argus.publisher")

SEVERITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}
_SEV_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
# A question is not graded by severity -- it is an ask, not a claim.
_QUESTION_EMOJI = "❓"


def diff_line_ranges(files_by_id: dict, hunks: dict) -> dict[str, list[tuple[int, int]]]:
    """Map each changed file path to the NEW-file line ranges its hunks cover.

    GitLab will only accept an inline comment anchored to a line inside the
    diff. Knowing the ranges up front lets us route a finding about untouched
    code to the summary deliberately, instead of attempting an inline post
    that fails and falls back to an un-anchored comment that reads like a bug.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    for h in hunks.values():
        fc = files_by_id.get(h.file_id)
        if fc is None:
            continue
        start = h.new_start
        ranges.setdefault(fc.path, []).append(
            (start, start + max(h.new_lines, 1) - 1))
    return ranges


def is_outside_diff(f: CandidateFinding,
                    ranges: dict[str, list[tuple[int, int]]]) -> bool:
    """True when this finding's line is not covered by any hunk.

    A file with no entry in `ranges` is outside the diff by definition --
    that includes files GitLab collapsed, whose hunks we may have recovered
    but which can still legitimately have none.

    When `ranges` is entirely empty we have NO diff information at all (an
    alternate publish path, or hunks that never reached the publisher). Fall
    back to treating everything as anchorable and let GitLab decide: the
    existing un-anchored retry handles a rejection, whereas assuming
    "everything is outside the diff" would silently stop posting inline
    comments altogether."""
    if not ranges:
        return False
    for lo, hi in ranges.get(f.file_path, ()):
        if lo <= f.line <= hi:
            return False
    return True


def rank_findings(findings: list[CandidateFinding],
                  verdicts: list[Verdict],
                  line_corrections: dict[str, int] | None = None,
                  ) -> list[CandidateFinding]:
    """Filter to valid findings and sort by severity*confidence.

    Grounding no longer gates here: a finding whose evidence_quote failed the
    mechanical check still reaches verify (flagged, not dropped), and verify's
    own verdict -- made with real tool access to the file -- is what decides
    valid vs. rejected. `line_corrections` is applied via `model_copy` (never
    in-place) so the original CandidateFinding instances in
    `findings`/`state.findings` are left untouched. When a verdict carries a
    `corrected` finding (verify's inline fix-up for a real but malformed
    finding, including one it relocated after a grounding flag), that replaces
    the original entirely before line_corrections is applied on top.
    """
    line_corrections = line_corrections or {}
    verdict_by_id = {v.finding_id: v for v in verdicts}
    valid_ids = {v.finding_id for v in verdicts if v.valid}
    kept = [f for f in findings if f.finding_id in valid_ids]
    fixed_up = [
        verdict_by_id[f.finding_id].corrected
        if verdict_by_id[f.finding_id].corrected is not None else f
        for f in kept]
    corrected = [
        f.model_copy(update={"line": line_corrections[f.finding_id]})
        if f.finding_id in line_corrections else f
        for f in fixed_up]
    return sorted(corrected, key=lambda f: SEVERITY_WEIGHT[f.severity] * f.confidence,
                  reverse=True)


def dedupe(ranked: list[CandidateFinding]) -> list[CandidateFinding]:
    """Drop repeats of a finding already kept: same line, or same file with a
    matching title AND matching evidence. Fan-out means several agents describe
    one defect, and keying on the exact line published "Undefined variable
    `displayRecord`" twice, on lines 720 and 721."""
    seen: set[tuple[str, int]] = set()
    out: list[CandidateFinding] = []
    for f in ranked:
        key = (f.file_path, f.line)
        if key in seen:
            continue
        twin = next((k for k in out
                     if _is_same_finding(f.file_path, f.title, f.evidence_quote,
                                         k.file_path, k.title, k.evidence_quote)),
                    None)
        if twin is not None:
            logger.info("skipping %s: same as %s in this review",
                        f.finding_id, twin.finding_id)
            continue
        seen.add(key)
        out.append(f)
    return out


# Measured on 405 real published findings: fires on 4 of all same-file pairs.
DUPLICATE_TITLE_SIMILARITY = 0.6
# Titles alone are not enough: dropping short tokens makes "Missing null check
# on X" and "...on Y" score 1.0, so the quoted code has to agree as well.
DUPLICATE_EVIDENCE_SIMILARITY = 0.5


def _title_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", (text or "").lower())
            if len(t) > 2}


def title_similarity(a: str, b: str) -> float:
    """Jaccard overlap of significant title tokens, 0.0 when either is empty."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _is_same_finding(file_a: str, title_a: str, quote_a: str | None,
                     file_b: str, title_b: str, quote_b: str | None) -> bool:
    """Same file, matching title, and matching evidence where both have it."""
    if file_a != file_b:
        return False
    if title_similarity(title_a, title_b) < DUPLICATE_TITLE_SIMILARITY:
        return False
    qa, qb = _title_tokens(quote_a or ""), _title_tokens(quote_b or "")
    if not qa or not qb:
        return True  # no usable evidence on one side: the title decides
    return len(qa & qb) / len(qa | qb) >= DUPLICATE_EVIDENCE_SIMILARITY


def drop_already_published(candidates: list[CandidateFinding],
                           prior: list[tuple[str, str, str | None]]
                           ) -> list[CandidateFinding]:
    """Remove candidates an earlier review of this MR already posted. Ignores
    the line number on purpose: dedupe() keys on the exact line, which is why
    a one-line shift let the same finding through again."""
    if not prior:
        return list(candidates)
    out = []
    for c in candidates:
        match = next(
            ((t, q) for path, t, q in prior
             if _is_same_finding(c.file_path, c.title, c.evidence_quote,
                                 path, t, q)), None)
        if match is not None:
            logger.info("skipping %s: already posted on this MR as %r "
                        "(title sim %.2f)", c.finding_id, match[0],
                        title_similarity(c.title, match[0]))
            continue
        out.append(c)
    return out


async def already_published_in_review(sf, review_id
                                       ) -> list[tuple[str, str, str | None]]:
    """(file_path, title, evidence_quote) for what THIS review already
    posted. A resumed publish replays the whole node, and the cross-review
    dedup cannot help because it excludes the current review by design.
    Matched the same way as drop_already_published (_is_same_finding), fixing
    an earlier title-only key that collided across files: two distinct
    findings sharing a title in different files would have made the second
    look like a repeat of the first."""
    async with sf() as s:
        rows = (await s.execute(
            select(Finding.file_path, Finding.title, Finding.evidence_quote)
            .where(Finding.review_id == review_id,
                   Finding.published_note_id.isnot(None)))).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def published_titles_for_mr(sf, mr_db_id, exclude_review_id
                                  ) -> list[tuple[str, str, str | None]]:
    """(file_path, title, evidence_quote) already published on this MR."""
    async with sf() as s:
        rows = (await s.execute(
            select(Finding.file_path, Finding.title, Finding.evidence_quote)
            .join(Review, Review.id == Finding.review_id)
            .where(Review.mr_id == mr_db_id,
                   Finding.published_note_id.isnot(None),
                   Finding.review_id != exclude_review_id))).all()
    return [(r[0], r[1], r[2]) for r in rows]


def suggestion_block(f: CandidateFinding) -> str | None:
    if not (f.suggestion_old and f.suggestion_new):
        return None
    span = len(f.suggestion_old.splitlines()) - 1
    return f"```suggestion:-0+{span}\n{f.suggestion_new}\n```"


def format_comment(f: CandidateFinding) -> str:
    if f.type == "question":
        # No severity badge and no suggestion block: this is an ask, and
        # dressing it as a graded defect invites the author to argue with the
        # grade instead of answering the question.
        return "\n".join([
            f"{_QUESTION_EMOJI} **{f.title}**", "", f.body, "",
            f"<sub>argus · question · `{f.finding_id}`</sub>"])
    parts = [f"{_SEV_EMOJI[f.severity]} **{f.title}**", "", f.body]
    block = suggestion_block(f)
    if block:
        parts += ["", block]
    parts += ["", f"<sub>argus · {f.severity} · confidence "
                  f"{f.confidence:.0%} · `{f.finding_id}`</sub>"]
    return "\n".join(parts)


def build_position(f: CandidateFinding, diff_refs: dict) -> dict:
    return {"base_sha": diff_refs["base_sha"], "start_sha": diff_refs["start_sha"],
            "head_sha": diff_refs["head_sha"], "position_type": "text",
            "new_path": f.file_path, "new_line": f.line}


def format_qa_summary(state: ReviewState) -> str:
    lines = ["## argus QA scenarios", "",
            f"**{state.plan.intent}** — {state.plan.mr_summary}", ""]
    if not state.test_scenarios:
        lines.append("No human-testable scenarios found for this MR.")
        return "\n".join(lines)
    lines += ["### 🧪 Test Scenarios", ""]
    for s in state.test_scenarios:
        lines.append(f"- [ ] **{s.title}** _{s.area}_")
        for step in s.steps:
            lines.append(f"  {step}")
        lines.append(f"  **Expected:** {s.expected_result}")
        if s.relevant_files:
            lines.append(f"  _Files: {', '.join(s.relevant_files)}_")
    return "\n".join(lines)


async def publish_qa_review(state: ReviewState, deps, *,
                            publish: bool = True) -> CompiledReview:
    """Dedicated publish path for review_mode="qa_scenarios": posts ONLY the
    test-scenario checklist -- no inline comments, no finding DB rows, since
    that graph path never populates state.findings/verdicts."""
    summary = format_qa_summary(state)
    if publish:
        try:
            await deps.gitlab.create_discussion(deps.project_id, deps.mr_iid, summary)
        except Exception:
            logger.exception("QA summary post failed")
    async with deps.sf() as s:
        review = await s.get(Review, uuid.UUID(state.review_id))
        review.summary = summary
        await s.commit()
    return CompiledReview(published_finding_ids=[], summary_markdown=summary)


async def _mr_still_open(deps) -> bool:
    """Whether the MR is open now, not when the review started. Fails OPEN so
    a transient GitLab error cannot silently discard a review's output."""
    try:
        payload = await deps.gitlab.get_merge_request(deps.project_id, deps.mr_iid)
    except Exception as e:
        logger.warning("could not confirm MR !%s state (%s); publishing anyway",
                       deps.mr_iid, e)
        return True
    state = (payload or {}).get("state")
    if state != "opened":
        logger.info("MR !%s is %s; recording findings without commenting",
                    deps.mr_iid, state)
        return False
    return True


async def publish_review(state: ReviewState, deps) -> CompiledReview:
    # Short-circuits, so a dry run never pays for the MR-state call.
    should_publish = deps.publish and await _mr_still_open(deps)
    if deps.review_mode == "qa_scenarios":
        return await publish_qa_review(state, deps, publish=should_publish)
    verdict_by_id = {v.finding_id: v for v in state.verdicts}
    ranked = dedupe(rank_findings(state.findings, state.verdicts,
                                  state.line_corrections))
    ranked = drop_already_published(
        ranked, await published_titles_for_mr(
            deps.sf, deps.mr_db_id, uuid.UUID(state.review_id)))
    posted_before = await already_published_in_review(
        deps.sf, uuid.UUID(state.review_id))
    # Route findings about untouched code away from the inline path before the
    # budget is applied: GitLab cannot anchor them, and letting them consume
    # inline slots would push anchorable findings into the overflow list.
    ranges = diff_line_ranges(deps.tool_ctx.files_by_id, deps.tool_ctx.hunks)
    outside = [f.model_copy(update={"outside_diff": True})
               for f in ranked if is_outside_diff(f, ranges)]
    anchorable = [f for f in ranked if not is_outside_diff(f, ranges)]
    budget = deps.settings.max_inline_comments
    inline, overflow = anchorable[:budget], anchorable[budget:]
    published: list[str] = []

    for f in inline:
        if any(_is_same_finding(f.file_path, f.title, f.evidence_quote, p, t, q)
               for p, t, q in posted_before):
            logger.info("skipping %s: this review already posted it",
                        f.finding_id)
            published.append(f.finding_id)
            continue
        note_payload = None
        if should_publish:
            try:
                note_payload = await deps.gitlab.create_discussion(
                    deps.project_id, deps.mr_iid, format_comment(f),
                    position=build_position(f, deps.diff_refs))
            except Exception as e:
                logger.warning("inline post failed for %s (%s); posting un-anchored",
                               f.finding_id, e)
                try:
                    note_payload = await deps.gitlab.create_discussion(
                        deps.project_id, deps.mr_iid,
                        f"`{f.file_path}:{f.line}`\n\n" + format_comment(f))
                except Exception:
                    logger.exception("post failed entirely for %s", f.finding_id)
                    continue
        # One session, one commit: a Note whose Finding write then failed left
        # a comment on GitLab with no way to tell already_published_in_review
        # it existed, so a resume would re-post it. NULL note id on a dry run
        # keeps the Finding out of dedup and the metrics either way.
        async with deps.sf() as s:
            note_id = None
            if note_payload is not None:
                note = Note(mr_id=deps.mr_db_id,
                            review_id=uuid.UUID(state.review_id),
                            provider_note_id=(note_payload.get("notes") or [{}])[0]
                            .get("id", 0),
                            author_type="bot", kind="inline",
                            body=format_comment(f),
                            file_path=f.file_path, line=f.line, raw=note_payload)
                s.add(note)
                await s.flush()
                note_id = note.id
            verdict = verdict_by_id.get(f.finding_id)
            s.add(Finding(review_id=uuid.UUID(state.review_id), stage=f.stage,
                          type=f.type, severity=f.severity, confidence=f.confidence,
                          file_path=f.file_path, line=f.line, title=f.title,
                          body=f.body, evidence_quote=f.evidence_quote,
                          suggestion_old=f.suggestion_old,
                          suggestion_new=f.suggestion_new,
                          contributing_learning_ids=f.contributing_learning_ids or None,
                          verdict_valid=True,
                          verdict_reason=verdict.reason if verdict else None,
                          published_note_id=note_id))
            await s.commit()
        published.append(f.finding_id)

    summary_lines = [f"## argus review", "",
                     f"**{state.plan.intent}** — {state.plan.mr_summary}", "",
                     f"{len(published)} inline comment(s) posted."]
    if outside:
        # Rendered in full rather than collapsed: a broken caller outside the
        # diff is often the most important thing in the review, and it is the
        # one class of finding the author cannot discover by reading their own
        # changes.
        summary_lines += ["", "### Outside this change", "",
                          "Code this MR does not modify, but is affected by "
                          "it. Not shown inline because GitLab can only "
                          "anchor comments to changed lines.", ""]
        for f in outside:
            label = ("question" if f.type == "question"
                     else f"{f.severity}, confidence {f.confidence:.0%}")
            summary_lines.append(f"- `{f.file_path}:{f.line}` — **{f.title}** "
                                 f"({label})")
            summary_lines.append(f"  {f.body}")
    if overflow:
        summary_lines += ["", f"<details><summary>{len(overflow)} lower-priority "
                              f"finding(s)</summary>", ""]
        summary_lines += [f"- `{f.file_path}:{f.line}` {f.title} ({f.severity})"
                          for f in overflow]
        summary_lines += ["", "</details>"]
    summary = "\n".join(summary_lines)
    if should_publish:
        try:
            await deps.gitlab.create_discussion(deps.project_id, deps.mr_iid, summary)
        except Exception:
            logger.exception("summary post failed")

    # unpublished-but-valid findings recorded too (for analytics/incremental).
    # `outside` is included so a finding reported in the summary is queryable
    # like any other, and distinguishable via outside_diff from one that
    # merely lost the inline budget.
    async with deps.sf() as s:
        for f in overflow + outside:
            verdict = verdict_by_id.get(f.finding_id)
            s.add(Finding(review_id=uuid.UUID(state.review_id), stage=f.stage,
                          type=f.type, severity=f.severity, confidence=f.confidence,
                          file_path=f.file_path, line=f.line, title=f.title,
                          body=f.body, evidence_quote=f.evidence_quote,
                          contributing_learning_ids=f.contributing_learning_ids or None,
                          verdict_valid=True,
                          outside_diff=f.outside_diff,
                          verdict_reason=verdict.reason if verdict else None))
        review = await s.get(Review, uuid.UUID(state.review_id))
        review.summary = summary
        await s.commit()

    return CompiledReview(published_finding_ids=published, summary_markdown=summary)

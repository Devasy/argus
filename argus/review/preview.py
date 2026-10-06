"""Compile once and publish the saved GitHub review without invoking a model."""
import hashlib
import json
import uuid

import httpx
from sqlalchemy import select, text

from argus.domain.models import Finding, MergeRequest, Repository, Review
from argus.providers import create_provider
from argus.review.artifacts import CompiledReview


def diff_fingerprint(diffs):
    files = sorted((d["old_path"], d["new_path"], d.get("diff", "")) for d in diffs)
    return hashlib.sha256(json.dumps(files).encode()).hexdigest()


async def save_preview(state, deps):
    from argus.review.publisher import (dedupe, rank_findings, diff_line_ranges,
        format_comment, format_qa_summary, summary_marker, drop_already_published,
        published_titles_for_mr)
    ranked = dedupe(rank_findings(state.findings, state.verdicts, state.line_corrections))
    ranked = drop_already_published(ranked, await published_titles_for_mr(
        deps.sf, deps.mr_db_id, uuid.UUID(state.review_id)))
    finding_ids = {f.finding_id: uuid.uuid4() for f in ranked}
    verdicts = {v.finding_id: v for v in state.verdicts}
    ranges = diff_line_ranges(deps.tool_ctx.files_by_id, deps.tool_ctx.hunks)
    comments, summarized = [], []
    for finding in ranked:
        anchorable = any(a <= finding.line <= b for a, b in ranges.get(finding.file_path, []))
        if anchorable and len(comments) < deps.settings.max_inline_comments:
            # GitHub uses the suggestion's selected line range, not GitLab offsets.
            body = format_comment(finding)
            if finding.suggestion_old and finding.suggestion_new:
                span = len(finding.suggestion_old.splitlines()) - 1
                body = body.replace(f"```suggestion:-0+{span}", "```suggestion") if span == 0 else body
                if span:
                    body = body.replace(f"```suggestion:-0+{span}\n{finding.suggestion_new}\n```", "")
            comments.append({"path": finding.file_path, "line": finding.line,
                             "side": "RIGHT", "body": body + "\n\n" + summary_marker(state.review_id),
                             "finding_db_id": str(finding_ids[finding.finding_id])})
        else:
            summarized.append(f"### `{finding.file_path}:{finding.line}`\n\n{format_comment(finding)}")
    summary = (format_qa_summary(state) if deps.review_mode == "qa_scenarios" else
        f"## Argus review\n\n**{state.plan.intent}** — {state.plan.mr_summary}\n\n"
        f"{len(comments)} inline finding(s) prepared.\n\n" + "\n\n".join(summarized))
    diffs = await deps.gitlab.list_diffs(deps.project_id, deps.mr_iid)
    current = await deps.gitlab.get_merge_request(deps.project_id, deps.mr_iid)
    if current["diff_refs"] != deps.diff_refs:
        raise ValueError("PR changed during review; start a fresh preview")
    artifact = {"version": 1, "diff_refs": deps.diff_refs,
                "diff_fingerprint": diff_fingerprint(diffs), "comments": comments,
                "body": summary + "\n\n" + summary_marker(state.review_id)}
    async with deps.sf() as session:
        review = await session.get(Review, uuid.UUID(state.review_id))
        # Publish nodes can replay after a restart. Replace only their saved preview rows.
        from sqlalchemy import delete
        await session.execute(delete(Finding).where(Finding.review_id == review.id,
                                                    Finding.published_note_id.is_(None)))
        for finding in ranked:
            session.add(Finding(id=finding_ids[finding.finding_id], review_id=review.id, stage=finding.stage, type=finding.type,
                severity=finding.severity, confidence=finding.confidence,
                file_path=finding.file_path, line=finding.line, title=finding.title,
                body=finding.body, evidence_quote=finding.evidence_quote,
                suggestion_old=finding.suggestion_old, suggestion_new=finding.suggestion_new,
                outside_diff=not any(a <= finding.line <= b for a, b in ranges.get(finding.file_path, [])),
                verdict_reason=verdicts[finding.finding_id].reason,
                contributing_learning_ids=finding.contributing_learning_ids or None,
                verdict_valid=True))
        review.summary = summary
        review.publication_artifact = artifact
        review.publication_status = "preview"
        await session.commit()
    return CompiledReview(published_finding_ids=[], summary_markdown=summary)


async def publish_saved_review(sf, settings, review_id):
    """Serialize delivery; ambiguous POSTs require reconciliation, never blind retry."""
    from argus.review.publisher import summary_marker
    async with sf() as session:
        review = await session.get(Review, review_id)
        if review is None:
            raise LookupError("review not found")
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                              {"key": f"publish-mr:{review.mr_id}"})
        await session.refresh(review)
        if review.publication_status == "published":
            return review.publication_result
        artifact = review.publication_artifact
        if review.status != "done" or not artifact:
            raise ValueError("only a completed saved preview can be published")
        mr = await session.get(MergeRequest, review.mr_id)
        repo = await session.get(Repository, mr.repo_id)
        if repo.provider != "github":
            raise ValueError("saved publication is currently supported for GitHub")
        provider = create_provider(settings, repo)
        try:
            await provider.get_project(repo.project_path)
            found = await provider.find_review(repo.project_path, mr.mr_iid, summary_marker(str(review_id)))
            if found:
                result = {"id": found["id"], "url": found.get("html_url")}
            else:
                if review.publication_status == "uncertain":
                    raise ValueError("previous delivery is uncertain and has not appeared on GitHub; reconcile before retrying")
                previous = (await session.execute(select(Review).where(
                    Review.mr_id == review.mr_id, Review.publication_status == "published",
                    Review.id != review_id))).scalars().all()
                if any((r.publication_artifact or {}).get("diff_refs") == artifact["diff_refs"] for r in previous):
                    raise ValueError("another saved review was already published for this PR snapshot")
                current = await provider.get_merge_request(repo.project_path, mr.mr_iid)
                if current["state"] != "opened" or current["draft"]:
                    raise ValueError("PR must be open and ready for review")
                if current["diff_refs"] != artifact["diff_refs"] or diff_fingerprint(
                        await provider.list_diffs(repo.project_path, mr.mr_iid)) != artifact["diff_fingerprint"]:
                    raise ValueError("PR changed since this preview; start a fresh review")
                # Commit the intent separately. A crash after this point is ambiguous.
                async with sf() as intent_session:
                    intent = await intent_session.get(Review, review_id)
                    intent.publication_status = "uncertain"
                    await intent_session.commit()
                try:
                    delivered = await provider.submit_review(repo.project_path, mr.mr_iid, artifact)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code < 500:
                        review.publication_status = "preview"
                        await session.commit()
                    raise
                result = {"id": delivered["id"], "url": delivered.get("html_url")}
            review.publication_result, review.publication_status = result, "published"
            await session.commit()
            return result
        finally:
            await provider.aclose()

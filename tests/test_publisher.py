import uuid

import pytest

from argus.review.artifacts import (CandidateFinding, ReviewPlan, ReviewState,
                                        TestScenario, Verdict)
from argus.review.publisher import (build_position, dedupe, format_comment,
                                        publish_review, rank_findings,
                                        suggestion_block)


def _f(fid, sev="high", conf=0.9, line=10, path="a.py", old=None, new=None):
    return CandidateFinding(finding_id=fid, stage="analysis", type="suggestion",
                            severity=sev, confidence=conf, file_path=path,
                            line=line, title=f"t-{fid}", body="b",
                            evidence_quote="q", suggestion_old=old,
                            suggestion_new=new)


def test_rank_filters_invalid_and_sorts():
    findings = [_f("a", "low", 0.9), _f("b", "critical", 0.8), _f("c", "high", 0.9)]
    verdicts = [Verdict(finding_id="a", valid=True, reason=""),
                Verdict(finding_id="b", valid=True, reason=""),
                Verdict(finding_id="c", valid=False, reason="speculative")]
    ranked = rank_findings(findings, verdicts)
    assert [f.finding_id for f in ranked] == ["b", "a"]


def test_rank_trusts_verifys_verdict_on_a_flagged_finding():
    """Grounding is a flag verify sees, not a gate in front of it: rank_findings
    itself no longer knows or cares which findings failed the mechanical
    check, so a valid verdict publishes regardless -- verify already had the
    chance to reject it as hallucinated and chose not to."""
    findings = [_f("a", "high", 0.9), _f("b", "critical", 0.9)]
    verdicts = [Verdict(finding_id="a", valid=True, reason=""),
                Verdict(finding_id="b", valid=True, reason="confirmed via tools")]
    ranked = rank_findings(findings, verdicts)
    assert {f.finding_id for f in ranked} == {"a", "b"}


def test_rank_applies_line_corrections_without_mutating_input():
    """A line-corrected finding's corrected line must be what flows through
    rank_findings' output, via a copy (not in-place mutation) — this must
    still pass even if CandidateFinding were frozen/immutable."""
    original = _f("a", line=99)
    findings = [original]
    verdicts = [Verdict(finding_id="a", valid=True, reason="")]
    ranked = rank_findings(findings, verdicts, line_corrections={"a": 5})
    assert ranked[0].line == 5
    # the original object passed in must be untouched
    assert original.line == 99
    assert findings[0].line == 99


def test_rank_uses_verdict_corrected_finding_over_original():
    """When verify's inline fix-up sets Verdict.corrected, rank_findings must
    use that corrected finding entirely (not the original) — this is how a
    real-but-malformed finding gets published in fixed form."""
    original = _f("a", line=10, path="a.py")
    fixed = original.model_copy(update={"body": "corrected body",
                                        "evidence_quote": "corrected quote"})
    findings = [original]
    verdicts = [Verdict(finding_id="a", valid=True, reason="fixed formatting",
                        corrected=fixed)]
    ranked = rank_findings(findings, verdicts)
    assert ranked[0].body == "corrected body"
    assert ranked[0].evidence_quote == "corrected quote"
    # original object passed in is untouched
    assert original.body == "b"


def test_rank_without_corrected_keeps_original_finding():
    findings = [_f("a")]
    verdicts = [Verdict(finding_id="a", valid=True, reason="ok")]
    ranked = rank_findings(findings, verdicts)
    assert ranked[0].body == "b"


def test_rank_applies_line_corrections_on_top_of_verdict_corrected():
    """line_corrections (from grounding) and verdict.corrected (from verify's
    fix-up) are independent mechanisms and must compose: the corrected
    finding's line still gets grounding's line correction applied."""
    original = _f("a", line=10)
    fixed = original.model_copy(update={"body": "corrected body"})
    findings = [original]
    verdicts = [Verdict(finding_id="a", valid=True, reason="fixed", corrected=fixed)]
    ranked = rank_findings(findings, verdicts, line_corrections={"a": 5})
    assert ranked[0].body == "corrected body"
    assert ranked[0].line == 5


def test_build_position_uses_corrected_line_from_rank_findings():
    """End-to-end check that the corrected line (not the original) is what
    ends up in the GitLab comment position, flowing through rank_findings
    rather than relying on in-place mutation aliasing."""
    original = _f("a", line=99, path="src/m.py")
    ranked = rank_findings([original], [Verdict(finding_id="a", valid=True, reason="")],
                           line_corrections={"a": 5})
    pos = build_position(ranked[0], {"base_sha": "B", "start_sha": "S", "head_sha": "H"})
    assert pos["new_line"] == 5
    assert original.line == 99  # input untouched


def test_dedupe_same_location():
    ranked = [_f("a", "critical"), _f("b", "low")]
    ranked[1].line = ranked[0].line
    assert [f.finding_id for f in dedupe(ranked)] == ["a"]


def test_suggestion_block_multiline():
    f = _f("a", old="x = 1\ny = 2", new="x = 1\ny = 3")
    block = suggestion_block(f)
    assert block.startswith("```suggestion:-0+1\n")
    assert block.endswith("\n```")
    assert suggestion_block(_f("b")) is None


def test_format_comment_contains_parts():
    f = _f("a", old="x", new="y")
    text = format_comment(f)
    assert "t-a" in text and "```suggestion" in text and "argus" in text


def test_build_position():
    f = _f("a", line=42, path="src/m.py")
    pos = build_position(f, {"base_sha": "B", "start_sha": "S", "head_sha": "H"})
    assert pos == {"base_sha": "B", "start_sha": "S", "head_sha": "H",
                   "position_type": "text", "new_path": "src/m.py",
                   "new_line": 42}


# ---------------------------------------------------------------------------
# publish_review: real DB writes against a fake GitLab client (no network)
# ---------------------------------------------------------------------------


class FakeGitLabClient:
    """Records every create_discussion call and returns a realistic-shaped
    GitLab discussion payload. `fail_on_position_for` lets a test simulate a
    positioned post failing while the un-anchored fallback succeeds."""

    def __init__(self, fail_on_position_for: set[str] | None = None):
        self.calls: list[dict] = []
        self._next_note_id = 1000
        self._fail_on_position_for = fail_on_position_for or set()

    async def get_merge_request(self, project_id, mr_iid):
        """Open by default, so publish-path tests exercise the real branch
        rather than _mr_still_open's fail-open error handling."""
        return {"iid": mr_iid, "state": "opened"}

    async def create_discussion(self, project_id, mr_iid, body, position=None):
        self.calls.append({"project_id": project_id, "mr_iid": mr_iid,
                           "body": body, "position": position})
        if position is not None:
            for fid in self._fail_on_position_for:
                if fid in body:
                    raise RuntimeError(f"simulated positioned-post failure for {fid}")
        note_id = self._next_note_id
        self._next_note_id += 1
        return {"notes": [{"id": note_id, "body": body}]}


def _finding(fid, sev="high", conf=0.9, line=10, path="a.py"):
    return CandidateFinding(finding_id=fid, stage="analysis", type="issue",
                            severity=sev, confidence=conf, file_path=path,
                            line=line, title=f"title-{fid}", body="body text",
                            evidence_quote="q")


async def _make_deps(sf, settings, gitlab, mr_db_id, tmp_path, max_inline_comments=5):
    from argus.review.pipeline import PipelineDeps
    from argus.llm.config import LLMConfig
    from argus.review.tools import ToolContext

    settings = settings.model_copy(update={"max_inline_comments": max_inline_comments})
    return PipelineDeps(
        sf=sf, settings=settings,
        llm_cfg=LLMConfig(provider="claude_cli_proxy", model="openai/x",
                          api_base="http://x/v1", api_key="k"),
        tool_ctx=ToolContext(workspace=tmp_path, files_by_id={}, hunks={}),
        profile_static="You are a reviewer.", mr_context="MR !1: t",
        gitlab=gitlab, project_id=848, mr_iid=1,
        diff_refs={"base_sha": "B", "start_sha": "S", "head_sha": "H"},
        mr_db_id=mr_db_id)


async def _make_review_and_mr(sf):
    from argus.domain.models import MergeRequest, Repository, Review

    async with sf() as s:
        repo = Repository(provider="gitlab",
                         project_path=f"g/publisher-test-{uuid.uuid4()}",
                         gitlab_project_id=80000000 + uuid.uuid4().int % 9000000)
        s.add(repo); await s.flush()
        mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                          source_branch="a", target_branch="b", head_sha="s",
                          web_url="u")
        s.add(mr); await s.flush()
        review = Review(mr_id=mr.id, status="running")
        s.add(review); await s.commit()
        return review.id, mr.id


async def test_publish_review_happy_path_under_budget(db, engine, settings, tmp_path):
    from argus.db import session_factory
    from argus.domain.models import Finding, Note, Review
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    findings = [_finding("f1", sev="critical", line=10, path="a.py"),
               _finding("f2", sev="high", line=20, path="b.py"),
               _finding("f3", sev="medium", line=30, path="c.py")]
    verdicts = [Verdict(finding_id=f.finding_id, valid=True, reason="ok")
               for f in findings]
    plan = ReviewPlan(intent="fix bug", mr_summary="fixes X", files=[])
    state = ReviewState(review_id=str(review_id), plan=plan, findings=findings,
                        verdicts=verdicts)

    gitlab = FakeGitLabClient()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path,
                            max_inline_comments=5)

    compiled = await publish_review(state, deps)

    # one create_discussion per finding (positioned) + one for the summary
    assert len(gitlab.calls) == 4
    positioned_calls = [c for c in gitlab.calls if c["position"] is not None]
    assert len(positioned_calls) == 3
    for f, call in zip(findings, positioned_calls):
        assert call["project_id"] == 848
        assert call["mr_iid"] == 1
        assert call["position"] == build_position(f, deps.diff_refs)

    assert set(compiled.published_finding_ids) == {"f1", "f2", "f3"}
    assert "3 inline comment(s) posted" in compiled.summary_markdown

    async with sf() as s:
        notes = (await s.execute(
            select(Note).where(Note.mr_id == mr_db_id).order_by(Note.line)
        )).scalars().all()
        assert [n.line for n in notes] == [10, 20, 30]
        assert [n.file_path for n in notes] == ["a.py", "b.py", "c.py"]
        assert {n.provider_note_id for n in notes} == {1000, 1001, 1002}
        assert all(n.review_id == uuid.UUID(state.review_id) for n in notes)

        findings_db = (await s.execute(
            select(Finding).where(Finding.review_id == review_id)
            .order_by(Finding.line))).scalars().all()
        assert len(findings_db) == 3
        note_by_line = {n.line: n.id for n in notes}
        for fdb in findings_db:
            assert fdb.published_note_id == note_by_line[fdb.line]
            assert fdb.verdict_reason == "ok"

        review = await s.get(Review, review_id)
        assert review.summary == compiled.summary_markdown


async def test_publish_persists_contributing_learning_ids(db, engine, settings, tmp_path):
    """The citation the agent made must survive onto the Finding row, or
    outcome attribution in Task 4 has nothing to join on."""
    from argus.db import session_factory
    from argus.domain.models import Finding
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    findings = [_finding("f1", sev="critical", line=10, path="a.py")]
    findings[0].contributing_learning_ids = ["abc12345", "def67890"]
    verdicts = [Verdict(finding_id="f1", valid=True, reason="ok")]
    plan = ReviewPlan(intent="fix bug", mr_summary="fixes X", files=[])
    state = ReviewState(review_id=str(review_id), plan=plan, findings=findings,
                        verdicts=verdicts)

    gitlab = FakeGitLabClient()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path,
                            max_inline_comments=5)

    await publish_review(state, deps)

    async with sf() as s:
        row = (await s.execute(
            select(Finding).where(Finding.review_id == review_id)
        )).scalars().first()
        assert row.contributing_learning_ids == ["abc12345", "def67890"]


async def test_publish_review_overflow_beyond_budget(db, engine, settings, tmp_path):
    from argus.db import session_factory
    from argus.domain.models import Finding, Note
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    # 4 valid findings of descending severity/confidence rank, budget of 2.
    findings = [_finding("f1", sev="critical", conf=0.95, line=10, path="a.py"),
               _finding("f2", sev="critical", conf=0.9, line=20, path="b.py"),
               _finding("f3", sev="high", conf=0.9, line=30, path="c.py"),
               _finding("f4", sev="low", conf=0.5, line=40, path="d.py")]
    verdicts = [Verdict(finding_id=f.finding_id, valid=True, reason="ok")
               for f in findings]
    plan = ReviewPlan(intent="fix bug", mr_summary="fixes X", files=[])
    state = ReviewState(review_id=str(review_id), plan=plan, findings=findings,
                        verdicts=verdicts)

    gitlab = FakeGitLabClient()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path,
                            max_inline_comments=2)

    compiled = await publish_review(state, deps)

    # only top-2 ranked findings get posted inline
    assert set(compiled.published_finding_ids) == {"f1", "f2"}
    positioned_calls = [c for c in gitlab.calls if c["position"] is not None]
    assert len(positioned_calls) == 2

    # overflow findings appear in the summary's collapsible section
    assert "2 lower-priority finding(s)" in compiled.summary_markdown
    assert "c.py:30" in compiled.summary_markdown
    assert "d.py:40" in compiled.summary_markdown
    assert "a.py:10" not in compiled.summary_markdown  # inline ones aren't listed there

    async with sf() as s:
        findings_db = (await s.execute(
            select(Finding).where(Finding.review_id == review_id)
            .order_by(Finding.line))).scalars().all()
        assert len(findings_db) == 4  # all 4 valid findings persisted

        by_line = {f.line: f for f in findings_db}
        # inline findings have a published_note_id
        assert by_line[10].published_note_id is not None
        assert by_line[20].published_note_id is not None
        # overflow findings have no published_note_id and no Note row
        assert by_line[30].published_note_id is None
        assert by_line[40].published_note_id is None
        # verdict_reason is persisted for both inline and overflow findings
        for f in findings_db:
            assert f.verdict_reason == "ok"

        notes = (await s.execute(
            select(Note).where(Note.mr_id == mr_db_id))).scalars().all()
        assert len(notes) == 2
        assert {n.line for n in notes} == {10, 20}


async def test_publish_review_falls_back_to_unanchored_on_positioned_failure(
        db, engine, settings, tmp_path, caplog):
    from argus.db import session_factory
    from argus.domain.models import Finding, Note
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    findings = [_finding("f1", sev="critical", line=10, path="a.py")]
    verdicts = [Verdict(finding_id="f1", valid=True, reason="ok")]
    plan = ReviewPlan(intent="fix bug", mr_summary="fixes X", files=[])
    state = ReviewState(review_id=str(review_id), plan=plan, findings=findings,
                        verdicts=verdicts)

    # positioned call for f1 fails; un-anchored fallback (position=None) succeeds
    gitlab = FakeGitLabClient(fail_on_position_for={"f1"})
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path,
                            max_inline_comments=5)

    import logging
    caplog.set_level(logging.WARNING, logger="argus.publisher")

    compiled = await publish_review(state, deps)

    # first call attempted with position and failed, second succeeded without
    assert len(gitlab.calls) == 3  # positioned (fails), fallback, summary
    assert gitlab.calls[0]["position"] is not None
    assert gitlab.calls[1]["position"] is None
    assert "a.py:10" in gitlab.calls[1]["body"]

    assert any("inline post failed for f1" in rec.message for rec in caplog.records)
    assert compiled.published_finding_ids == ["f1"]

    async with sf() as s:
        notes = (await s.execute(
            select(Note).where(Note.mr_id == mr_db_id))).scalars().all()
        assert len(notes) == 1
        assert notes[0].line == 10

        findings_db = (await s.execute(
            select(Finding).where(Finding.review_id == review_id))).scalars().all()
        assert len(findings_db) == 1
        assert findings_db[0].published_note_id == notes[0].id
        assert findings_db[0].verdict_reason == "ok"


async def test_publish_review_dispatches_to_qa_only_comment_in_qa_mode(
        db, engine, settings, tmp_path):
    """deps.review_mode="qa_scenarios" must dispatch to publish_qa_review:
    exactly one comment containing ONLY the checklist (no "N inline
    comment(s) posted" line, no finding/Note DB rows), regardless of
    state.findings/verdicts (which this graph path never populates anyway)."""
    from argus.db import session_factory
    from argus.domain.models import Finding, Note
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    plan = ReviewPlan(intent="add login flow", mr_summary="adds OAuth login", files=[])
    scenarios = [TestScenario(
        scenario_id="c1-qa1", chunk_id="c1",
        title="Login with valid credentials", area="happy_path",
        steps=["Go to /login", "Enter valid credentials", "Submit"],
        expected_result="User is redirected to the dashboard",
        relevant_files=["auth/login.py"])]
    state = ReviewState(review_id=str(review_id), plan=plan, findings=[],
                        verdicts=[], test_scenarios=scenarios)

    gitlab = FakeGitLabClient()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)
    deps = deps.model_copy(update={"review_mode": "qa_scenarios"})

    compiled = await publish_review(state, deps)

    assert len(gitlab.calls) == 1
    summary_body = gitlab.calls[0]["body"]
    assert "## argus QA scenarios" in summary_body
    assert "### 🧪 Test Scenarios" in summary_body
    assert "Login with valid credentials" in summary_body
    assert "Go to /login" in summary_body
    assert "Expected:** User is redirected to the dashboard" in summary_body
    assert "auth/login.py" in summary_body
    assert "inline comment" not in summary_body
    assert "### 🧪 Test Scenarios" in compiled.summary_markdown
    assert compiled.published_finding_ids == []

    async with sf() as s:
        notes = (await s.execute(select(Note).where(Note.mr_id == mr_db_id))).scalars().all()
        findings = (await s.execute(
            select(Finding).where(Finding.review_id == review_id))).scalars().all()
    assert notes == []
    assert findings == []


async def test_publish_review_qa_mode_reports_no_scenarios_found(
        db, engine, settings, tmp_path):
    """No test_scenarios in qa_scenarios mode still posts one comment, saying
    so explicitly rather than an empty/misleading checklist section."""
    from argus.db import session_factory

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    plan = ReviewPlan(intent="fix bug", mr_summary="fixes X", files=[])
    state = ReviewState(review_id=str(review_id), plan=plan, findings=[],
                        verdicts=[], test_scenarios=[])

    gitlab = FakeGitLabClient()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)
    deps = deps.model_copy(update={"review_mode": "qa_scenarios"})

    await publish_review(state, deps)

    summary_body = gitlab.calls[0]["body"]
    assert "Test Scenarios" not in summary_body
    assert "No human-testable scenarios found" in summary_body


# --- cross-review dedup: do not re-post what an earlier review already said ---

def _titled(fid, title, path="a.py", line=10):
    return CandidateFinding(finding_id=fid, stage="analysis", type="issue",
                            severity="high", confidence=0.9, file_path=path,
                            line=line, title=title, body="b", evidence_quote="q")


def test_title_similarity_scores_rewordings_high_and_distinct_titles_low():
    from argus.review.publisher import title_similarity

    a = "Undefined variable `dest_status` causes NameError"
    assert title_similarity(a, a) == 1.0
    # the bot rewords between rounds; a human called these "duplicate comment"
    assert title_similarity(a, "Undefined variable dest_status causes a NameError") > 0.6
    assert title_similarity(a, "Missing error_code in error log violates policy") < 0.3
    assert title_similarity("", "anything") == 0.0


def test_already_published_finding_is_not_reposted():
    """A human called this out three times in a row -- "duplicate comment",
    "updated", "duplicate comment but updated" -- because every re-review
    re-posted findings the previous review had already published."""
    from argus.review.publisher import drop_already_published

    prior = [("api/views.py", "Undefined variable `dest_status` causes NameError", None)]
    cands = [_titled("a", "Undefined variable dest_status causes a NameError",
                     path="api/views.py")]
    assert drop_already_published(cands, prior) == []


def test_line_drift_does_not_defeat_the_dedup():
    """The old dedupe keyed on exact (file, line), so a push that shifted the
    code by one line let the same finding through again."""
    from argus.review.publisher import drop_already_published

    prior = [("api/views.py", "Undefined variable `dest_status` causes NameError", None)]
    cands = [_titled("a", "Undefined variable `dest_status` causes NameError",
                     path="api/views.py", line=812)]
    assert drop_already_published(cands, prior) == []


def test_unrelated_finding_in_the_same_file_survives():
    from argus.review.publisher import drop_already_published

    prior = [("api/views.py", "Undefined variable `dest_status` causes NameError", None)]
    cands = [_titled("a", "Missing timeout on outbound HTTP request",
                     path="api/views.py")]
    assert [f.finding_id for f in drop_already_published(cands, prior)] == ["a"]


def test_same_title_in_a_different_file_survives():
    """Genuinely the same class of bug in another file is a new finding."""
    from argus.review.publisher import drop_already_published

    prior = [("api/views.py", "Undefined variable `dest_status` causes NameError", None)]
    cands = [_titled("a", "Undefined variable `dest_status` causes NameError",
                     path="api/other.py")]
    assert [f.finding_id for f in drop_already_published(cands, prior)] == ["a"]


def test_no_prior_findings_keeps_everything():
    from argus.review.publisher import drop_already_published

    cands = [_titled("a", "x"), _titled("b", "y")]
    assert len(drop_already_published(cands, [])) == 2


async def test_second_review_does_not_repost_the_first_reviews_finding(
        db, engine, settings, tmp_path):
    """End-to-end proof of the cross-review dedup: review 1 publishes a
    finding, review 2 on the same MR rediscovers it (reworded, on a drifted
    line) and must stay silent, while a genuinely new finding still posts."""
    from argus.db import session_factory
    from argus.domain.models import Review

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    first = CandidateFinding(
        finding_id="f1", stage="analysis", type="issue", severity="high",
        confidence=0.9, file_path="api/views.py", line=810,
        title="Undefined variable `dest_status` causes NameError",
        body="b", evidence_quote="q")
    state = ReviewState(review_id=str(review_id),
                        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
                        findings=[first],
                        verdicts=[Verdict(finding_id="f1", valid=True, reason="ok")])
    gitlab = FakeGitLabClient()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)
    await publish_review(state, deps)
    assert len([c for c in gitlab.calls if c["position"] is not None]) == 1

    # A second review of the same MR, as a re-review after a push.
    async with sf() as s:
        second_review = Review(mr_id=mr_db_id, status="running")
        s.add(second_review)
        await s.commit()
        second_id = second_review.id

    repeat = CandidateFinding(
        finding_id="f2", stage="analysis", type="issue", severity="high",
        confidence=0.9, file_path="api/views.py", line=814,  # line drifted
        title="Undefined variable dest_status causes a NameError",  # reworded
        body="b", evidence_quote="q")
    fresh = CandidateFinding(
        finding_id="f3", stage="analysis", type="issue", severity="high",
        confidence=0.9, file_path="api/views.py", line=42,
        title="Missing timeout on outbound HTTP request",
        body="b", evidence_quote="q")
    state2 = ReviewState(
        review_id=str(second_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=[repeat, fresh],
        verdicts=[Verdict(finding_id="f2", valid=True, reason="ok"),
                  Verdict(finding_id="f3", valid=True, reason="ok")])
    gitlab2 = FakeGitLabClient()
    # Distinct provider note ids: notes are unique on (mr_id, provider_note_id)
    # and both fake clients otherwise start numbering at 1000 on the same MR.
    gitlab2._next_note_id = 2000
    deps2 = await _make_deps(sf, settings, gitlab2, mr_db_id, tmp_path)

    compiled = await publish_review(state2, deps2)

    assert compiled.published_finding_ids == ["f3"], "the repeat must not re-post"
    posted = [c for c in gitlab2.calls if c["position"] is not None]
    assert len(posted) == 1
    assert "Missing timeout" in posted[0]["body"]


# --- no-publish (dry run): compute everything, post nothing ---------------

async def test_dry_run_persists_findings_without_posting(
        db, engine, settings, tmp_path):
    """The golden-set harness: run the real pipeline over a fixed set of MRs,
    repeatedly and across models, without touching GitLab. Findings must still
    land in the DB (that IS the result) with published_note_id left NULL."""
    from argus.db import session_factory
    from argus.domain.models import Finding, Note, Review
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    findings = [_finding("f1", line=10, path="a.py"),
                _finding("f2", line=20, path="b.py")]
    state = ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=findings,
        verdicts=[Verdict(finding_id=f.finding_id, valid=True, reason="ok")
                  for f in findings])
    gitlab = FakeGitLabClient()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)
    deps = deps.model_copy(update={"publish": False})

    compiled = await publish_review(state, deps)

    assert gitlab.calls == [], "a dry run must not talk to GitLab at all"
    assert set(compiled.published_finding_ids) == {"f1", "f2"}

    async with sf() as s:
        rows = (await s.execute(select(Finding).where(
            Finding.review_id == review_id))).scalars().all()
        assert len(rows) == 2
        assert all(f.published_note_id is None for f in rows)
        notes = (await s.execute(select(Note).where(
            Note.mr_id == mr_db_id))).scalars().all()
        assert notes == []
        review = await s.get(Review, review_id)
        assert review.summary, "the summary is still the human-readable result"


async def test_dry_run_findings_do_not_suppress_a_later_real_review(
        db, engine, settings, tmp_path):
    """Cross-review dedup keys on PUBLISHED findings, so benchmarking runs
    must not silence the next genuine review of the same MR."""
    from argus.db import session_factory
    from argus.domain.models import Review

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)

    f = _finding("f1", line=10, path="a.py")
    state = ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=[f],
        verdicts=[Verdict(finding_id="f1", valid=True, reason="ok")])
    deps = await _make_deps(sf, settings, FakeGitLabClient(), mr_db_id, tmp_path)
    await publish_review(state, deps.model_copy(update={"publish": False}))

    async with sf() as s:
        second = Review(mr_id=mr_db_id, status="running")
        s.add(second)
        await s.commit()
        second_id = second.id

    state2 = ReviewState(
        review_id=str(second_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=[_finding("f2", line=10, path="a.py")],
        verdicts=[Verdict(finding_id="f2", valid=True, reason="ok")])
    gitlab2 = FakeGitLabClient()
    deps2 = await _make_deps(sf, settings, gitlab2, mr_db_id, tmp_path)

    compiled = await publish_review(state2, deps2)

    assert compiled.published_finding_ids == ["f2"]
    assert len([c for c in gitlab2.calls if c["position"] is not None]) == 1


# --- post-merge guard: don't comment on an MR that merged mid-review --------

class FakeGitLabWithState(FakeGitLabClient):
    def __init__(self, state="opened"):
        super().__init__()
        self.state = state
        self.state_queries = 0

    async def get_merge_request(self, project_id, mr_iid):
        self.state_queries += 1
        return {"iid": mr_iid, "state": self.state}


async def test_no_comments_posted_when_the_mr_merged_mid_review(
        db, engine, settings, tmp_path):
    """35 of 142 never-touched comments (25%) were posted after their MR had
    already merged -- MR 104 got all 8 comments 15.4h post-merge. The queue-time
    guard in _maybe_auto_review cannot help, because the merge happens while
    the review is running. Findings are still recorded; only the comments are
    skipped."""
    from argus.db import session_factory
    from argus.domain.models import Finding, Note
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)
    f = _finding("f1", line=10, path="a.py")
    state = ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=[f],
        verdicts=[Verdict(finding_id="f1", valid=True, reason="ok")])
    gitlab = FakeGitLabWithState(state="merged")
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)

    compiled = await publish_review(state, deps)

    assert gitlab.calls == [], "nobody will ever read a comment on a merged MR"
    assert compiled.published_finding_ids == ["f1"]
    async with sf() as s:
        rows = (await s.execute(select(Finding).where(
            Finding.review_id == review_id))).scalars().all()
        assert len(rows) == 1 and rows[0].published_note_id is None
        assert (await s.execute(select(Note).where(
            Note.mr_id == mr_db_id))).scalars().all() == []


async def test_open_mr_still_gets_comments(db, engine, settings, tmp_path):
    from argus.db import session_factory

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)
    state = ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=[_finding("f1", line=10, path="a.py")],
        verdicts=[Verdict(finding_id="f1", valid=True, reason="ok")])
    gitlab = FakeGitLabWithState(state="opened")
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)

    await publish_review(state, deps)

    assert len([c for c in gitlab.calls if c["position"] is not None]) == 1
    assert gitlab.state_queries == 1, "state checked once, not per finding"


async def test_unknown_mr_state_fails_open_and_still_posts(
        db, engine, settings, tmp_path):
    """A transient GitLab error must not silently discard a whole review's
    output -- publishing is the status quo, so an undeterminable state posts."""
    from argus.db import session_factory

    class Broken(FakeGitLabClient):
        async def get_merge_request(self, project_id, mr_iid):
            raise RuntimeError("GitLab 502")

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)
    state = ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=[_finding("f1", line=10, path="a.py")],
        verdicts=[Verdict(finding_id="f1", valid=True, reason="ok")])
    gitlab = Broken()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)

    await publish_review(state, deps)

    assert len([c for c in gitlab.calls if c["position"] is not None]) == 1


async def test_dry_run_does_not_bother_checking_mr_state(
        db, engine, settings, tmp_path):
    """Already not posting, so the extra API call would be pure waste."""
    from argus.db import session_factory

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)
    state = ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=[_finding("f1", line=10, path="a.py")],
        verdicts=[Verdict(finding_id="f1", valid=True, reason="ok")])
    gitlab = FakeGitLabWithState(state="opened")
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)

    await publish_review(state, deps.model_copy(update={"publish": False}))

    assert gitlab.state_queries == 0
    assert gitlab.calls == []


def test_dedupe_catches_the_same_finding_on_a_nearby_line():
    """Real case: "Undefined variable `displayRecord`" was published twice in
    one review, on lines 720 and 721. Keying on the exact line let it through."""
    a = _titled("a", "Undefined variable `displayRecord` causes ReferenceError",
                path="src/x.jsx", line=720)
    b = _titled("b", "Undefined variable displayRecord causes a ReferenceError",
                path="src/x.jsx", line=721)
    assert [f.finding_id for f in dedupe([a, b])] == ["a"]


def test_dedupe_keeps_distinct_findings_that_happen_to_be_adjacent():
    """Most same-file near-line pairs are genuinely different concerns."""
    a = _titled("a", "subprocess.run return code not checked",
                path="src/h.py", line=131)
    b = _titled("b", "Silent exception swallowing in health check",
                path="src/h.py", line=136)
    assert [f.finding_id for f in dedupe([a, b])] == ["a", "b"]


def test_dedupe_keeps_the_same_concern_in_a_different_file():
    a = _titled("a", "Missing timeout on outbound HTTP request",
                path="src/one.py", line=10)
    b = _titled("b", "Missing timeout on outbound HTTP request",
                path="src/two.py", line=10)
    assert [f.finding_id for f in dedupe([a, b])] == ["a", "b"]


# --- false-suppression guard (raised by argus on MR !3) ----------------

def test_similar_titles_about_different_code_are_both_kept():
    """argus flagged this on its own MR: "Missing null check on X" and
    "...on Y" tokenize identically once short tokens are dropped, so title
    overlap alone scored them 1.0 and suppressed a genuine finding. The
    evidence quote is what distinguishes them."""
    from argus.review.publisher import drop_already_published

    prior = [("api/views.py", "Missing null check on X", "if x.name:")]
    cands = [CandidateFinding(
        finding_id="a", stage="analysis", type="issue", severity="high",
        confidence=0.9, file_path="api/views.py", line=50,
        title="Missing null check on Y", body="b", evidence_quote="if y.label:")]
    assert [f.finding_id for f in drop_already_published(cands, prior)] == ["a"]


def test_same_finding_with_the_same_evidence_is_still_suppressed():
    from argus.review.publisher import drop_already_published

    prior = [("api/views.py", "Undefined variable `dest_status` causes NameError",
              "return dest_status")]
    cands = [CandidateFinding(
        finding_id="a", stage="analysis", type="issue", severity="high",
        confidence=0.9, file_path="api/views.py", line=814,
        title="Undefined variable dest_status causes a NameError", body="b",
        evidence_quote="return dest_status")]
    assert drop_already_published(cands, prior) == []


def test_missing_evidence_falls_back_to_the_title_alone():
    """Older findings predate evidence capture; refusing to dedup them would
    reintroduce the duplicate re-posting this whole mechanism exists to stop."""
    from argus.review.publisher import drop_already_published

    prior = [("api/views.py", "Undefined variable dest_status causes NameError", None)]
    cands = [CandidateFinding(
        finding_id="a", stage="analysis", type="issue", severity="high",
        confidence=0.9, file_path="api/views.py", line=814,
        title="Undefined variable `dest_status` causes NameError", body="b",
        evidence_quote="return dest_status")]
    assert drop_already_published(cands, prior) == []


def test_within_review_dedupe_also_respects_the_evidence():
    a = CandidateFinding(finding_id="a", stage="s", type="issue", severity="high",
                         confidence=0.9, file_path="v.py", line=10,
                         title="Missing null check on X", body="b",
                         evidence_quote="if x.name:")
    b = CandidateFinding(finding_id="b", stage="s", type="issue", severity="high",
                         confidence=0.9, file_path="v.py", line=40,
                         title="Missing null check on Y", body="b",
                         evidence_quote="if y.label:")
    assert [f.finding_id for f in dedupe([a, b])] == ["a", "b"]


async def test_a_resumed_publish_does_not_repost_its_own_comments(
        db, engine, settings, tmp_path):
    """If the worker dies part-way through publishing, LangGraph replays the
    whole publish node. The cross-review dedup cannot help: it deliberately
    excludes the current review. Without this, a resume re-posts the comments
    it already posted -- the exact duplicate-comment failure the dedup work
    exists to stop."""
    from argus.db import session_factory
    from argus.domain.models import Finding, Note
    from sqlalchemy import select

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)
    findings = [_finding("f1", line=10, path="a.py"),
                _finding("f2", line=20, path="b.py")]
    verdicts = [Verdict(finding_id=f.finding_id, valid=True, reason="ok")
                for f in findings]
    state = ReviewState(review_id=str(review_id),
                        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
                        findings=findings, verdicts=verdicts)

    first = FakeGitLabClient()
    deps = await _make_deps(sf, settings, first, mr_db_id, tmp_path)
    await publish_review(state, deps)
    assert len([c for c in first.calls if c["position"] is not None]) == 2

    # The replay: same review id, same findings.
    second = FakeGitLabClient()
    second._next_note_id = 5000
    deps2 = await _make_deps(sf, settings, second, mr_db_id, tmp_path)
    compiled = await publish_review(state, deps2)

    assert [c for c in second.calls if c["position"] is not None] == [], \
        "already-published findings must not be posted again"
    assert set(compiled.published_finding_ids) == {"f1", "f2"}, \
        "they still count as published for the summary"

    async with sf() as s:
        notes = (await s.execute(select(Note).where(
            Note.mr_id == mr_db_id))).scalars().all()
        assert len(notes) == 2, "one note per finding, not four"
        rows = (await s.execute(select(Finding).where(
            Finding.review_id == review_id,
            Finding.published_note_id.isnot(None)))).scalars().all()
        assert len(rows) == 2


async def test_resume_dedup_does_not_collide_across_files_with_the_same_title(
        db, engine, settings, tmp_path):
    """argus flagged this on MR !3, correctly: already_published_in_review
    keyed its lookup on title alone, unlike drop_already_published (the
    cross-review path) which requires file_path + title + evidence via
    _is_same_finding. Simulates the real scenario: this review already
    published finding "a" (title X, file a.py) on an earlier pass; a resumed
    replay then produces a genuinely different finding "c" that happens to
    share the title but is in a different file. Title-only keying would
    silently drop "c" as if it were a repeat of "a"."""
    from argus.db import session_factory

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)
    a = CandidateFinding(finding_id="a", stage="analysis", type="issue",
                        severity="high", confidence=0.9, file_path="a.py",
                        line=10, title="Missing null check on config",
                        body="b", evidence_quote="if a.value:")
    gitlab = FakeGitLabClient()
    deps = await _make_deps(sf, settings, gitlab, mr_db_id, tmp_path)
    await publish_review(ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]), findings=[a],
        verdicts=[Verdict(finding_id="a", valid=True, reason="ok")]), deps)
    assert len([c for c in gitlab.calls if c["position"] is not None]) == 1

    # The resumed replay: "a" again (must stay suppressed) plus a genuinely
    # different finding "c" that happens to share the exact title.
    c = CandidateFinding(finding_id="c", stage="analysis", type="issue",
                        severity="high", confidence=0.9, file_path="c.py",
                        line=30, title="Missing null check on config",
                        body="b", evidence_quote="if c.value:")
    state2 = ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]), findings=[a, c],
        verdicts=[Verdict(finding_id="a", valid=True, reason="ok"),
                 Verdict(finding_id="c", valid=True, reason="ok")])
    gitlab2 = FakeGitLabClient()
    gitlab2._next_note_id = 5000
    deps2 = await _make_deps(sf, settings, gitlab2, mr_db_id, tmp_path)

    compiled = await publish_review(state2, deps2)

    assert set(compiled.published_finding_ids) == {"a", "c"}
    posted = [call["body"] for call in gitlab2.calls if call["position"] is not None]
    assert len(posted) == 1, "only the genuinely new finding must post"
    assert "`c`" in posted[0], "the new finding (id=c), not a repeat of a"


async def test_note_and_finding_are_written_in_one_commit(
        db, engine, settings, tmp_path, monkeypatch):
    """argus flagged this on MR !3: the Note and Finding used to be
    written in two separate sessions with two separate commits. If the first
    commit (the Note) failed after a successful GitLab post, the comment
    would exist on GitLab with neither row recorded; if the second (the
    Finding) failed, the Note would exist without the Finding, breaking
    already_published_in_review on the next resume. Now both are added to one
    session with one commit, so a failure rolls back together: simulate that
    commit failing and confirm neither row survives (rather than the Note
    having been separately committed already)."""
    from argus.db import session_factory
    from argus.domain.models import Finding, Note
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    sf = session_factory(engine)
    review_id, mr_db_id = await _make_review_and_mr(sf)
    state = ReviewState(
        review_id=str(review_id),
        plan=ReviewPlan(intent="i", mr_summary="s", files=[]),
        findings=[_finding("f1", line=10, path="a.py")],
        verdicts=[Verdict(finding_id="f1", valid=True, reason="ok")])
    deps = await _make_deps(sf, settings, FakeGitLabClient(), mr_db_id, tmp_path)

    async def flaky_commit(self):
        raise RuntimeError("simulated DB outage")
    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)

    with pytest.raises(RuntimeError, match="simulated DB outage"):
        await publish_review(state, deps)

    monkeypatch.undo()
    async with sf() as s:
        notes = (await s.execute(select(Note).where(
            Note.mr_id == mr_db_id))).scalars().all()
        findings = (await s.execute(select(Finding).where(
            Finding.review_id == review_id))).scalars().all()
        assert len(notes) == 0, (
            "the Note must not survive when its Finding's write in the same "
            "commit fails, or a resume would think this was already posted")
        assert len(findings) == 0

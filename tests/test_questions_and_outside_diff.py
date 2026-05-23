"""Three behaviours a human reviewer has and the bot did not:

1. Asking a question when a change looks unrelated to the MR's intent,
   instead of inventing a defect or staying silent.
2. Reporting a consequence in code the MR did not touch -- a broken caller is
   often the most important thing in a review, and GitLab cannot anchor an
   inline comment there.
3. Consolidating instead of repeating. Agents are told to report what they
   find; the verifier is the only stage that sees everything at once, so
   volume control is a verdict, not a truncation.
"""
import pytest

from argus.review import stages
from argus.review.artifacts import CandidateFinding, FileChange, Hunk
from argus.review.publisher import (diff_line_ranges, format_comment,
                                        is_outside_diff, rank_findings)


def _finding(fid="F1", line=10, path="src/app.py", type="issue",
             severity="high", confidence=0.9, **kw):
    return CandidateFinding(
        finding_id=fid, stage="analyze", type=type, severity=severity,
        confidence=confidence, file_path=path, line=line, title=f"title {fid}",
        body=f"body {fid}", evidence_quote="x = 1", **kw)


# --- question findings -----------------------------------------------------

def test_question_is_an_accepted_finding_type():
    f = _finding(type="question")
    assert f.type == "question"


def test_question_comment_omits_severity_and_confidence():
    """A question is an ask, not a graded claim. Showing a severity badge
    invites the author to argue the grade instead of answering."""
    out = format_comment(_finding(type="question", severity="low"))
    assert "❓" in out
    assert "question" in out
    assert "confidence" not in out
    for emoji in ("🔴", "🟠", "🟡", "⚪"):
        assert emoji not in out


def test_defect_comment_still_shows_severity_and_confidence():
    out = format_comment(_finding(type="issue", severity="critical",
                                  confidence=0.8))
    assert "🔴" in out and "critical" in out and "80%" in out


def test_question_never_renders_a_suggestion_block():
    """A code suggestion asserts you know the fix; a question asserts you do
    not know the intent. Emitting both is incoherent."""
    out = format_comment(_finding(type="question", suggestion_old="a = 1",
                                  suggestion_new="a = 2"))
    assert "```suggestion" not in out


@pytest.mark.parametrize("prompt_name", ["ANALYSIS_STATIC", "DESIGN_STATIC"])
def test_prompts_tell_agents_when_to_ask(prompt_name):
    prompt = " ".join(getattr(stages, prompt_name).split())
    assert 'type="question"' in prompt
    assert "unrelated to the stated intent" in prompt
    # the crucial permission: uncertainty becomes a question, not a weak issue
    assert "ask a question instead of filing a weak issue" in prompt


def test_verifier_judges_questions_on_a_different_axis():
    """Applying the trace test to a question would reject every one of them,
    since a question makes no defect claim to trace."""
    verify = " ".join(stages.VERIFY_STATIC.split())
    assert "VERDICTS ON QUESTIONS" in verify
    assert "nothing to trace" in verify


# --- out-of-diff findings --------------------------------------------------

@pytest.fixture
def diff_ctx():
    files = {"f1": FileChange(file_id="f1", path="src/app.py",
                              change_kind="modified", hunk_ids=["h1"])}
    hunks = {"h1": Hunk(hunk_id="h1", file_id="f1", old_start=10, old_lines=5,
                        new_start=10, new_lines=5, diff_text="@@ -10,5 +10,5 @@")}
    return files, hunks


def test_diff_ranges_cover_exactly_the_changed_lines(diff_ctx):
    ranges = diff_line_ranges(*diff_ctx)
    assert ranges == {"src/app.py": [(10, 14)]}


def test_finding_inside_the_diff_is_anchorable(diff_ctx):
    ranges = diff_line_ranges(*diff_ctx)
    for line in (10, 12, 14):
        assert is_outside_diff(_finding(line=line), ranges) is False


def test_finding_outside_the_hunk_is_not_anchorable(diff_ctx):
    """GitLab rejects these. Detecting it up front is what lets us route them
    to the summary deliberately instead of after a failed post."""
    ranges = diff_line_ranges(*diff_ctx)
    for line in (9, 15, 200):
        assert is_outside_diff(_finding(line=line), ranges) is True


def test_finding_in_an_untouched_file_is_not_anchorable(diff_ctx):
    """The broken-caller case: the whole reason this feature exists."""
    ranges = diff_line_ranges(*diff_ctx)
    assert is_outside_diff(_finding(path="src/caller.py", line=88), ranges) is True


def test_collapsed_file_among_normal_ones_is_outside_the_diff(diff_ctx):
    """A file GitLab collapsed and we could not reconstruct contributes no
    ranges, so nothing in it can be anchored -- while its anchorable
    neighbours in the same MR still are.

    Note this is only decidable when SOME file in the MR has hunks. When the
    whole MR yields no ranges we cannot distinguish "all collapsed" from "no
    diff data reached the publisher", and
    test_no_diff_information_at_all_falls_back_to_anchorable covers that."""
    files, hunks = diff_ctx
    files["f2"] = FileChange(file_id="f2", path="src/big.js",
                             change_kind="modified", diff_available=False)
    ranges = diff_line_ranges(files, hunks)
    assert is_outside_diff(_finding(path="src/big.js", line=5), ranges) is True
    assert is_outside_diff(_finding(path="src/app.py", line=12), ranges) is False


def test_outside_diff_defaults_false_so_existing_findings_are_unaffected():
    assert _finding().outside_diff is False


def test_no_diff_information_at_all_falls_back_to_anchorable():
    """Fail open, not closed. With no hunk data (an alternate publish path,
    or hunks that never reached the publisher) treating everything as
    "outside the diff" would silently stop posting inline comments entirely;
    attempting the post lets GitLab decide, and the existing un-anchored
    retry already covers a rejection."""
    assert is_outside_diff(_finding(line=999, path="anything.py"), {}) is False


def test_ranking_is_unchanged_by_the_new_fields():
    """Regression guard: severity*confidence ordering must not shift."""
    findings = [_finding("A", severity="low", confidence=0.4),
                _finding("B", severity="critical", confidence=0.9),
                _finding("C", severity="medium", confidence=0.8)]
    verdicts = [type("V", (), {"finding_id": f.finding_id, "valid": True,
                               "corrected": None})() for f in findings]
    ranked = rank_findings(findings, verdicts)
    assert [f.finding_id for f in ranked] == ["B", "C", "A"]


# --- verifier as volume control -------------------------------------------

def test_verifier_is_told_to_consolidate_not_truncate():
    """Findings are kept, not capped: the verifier merges and rejects with a
    reason, so nothing disappears unjudged."""
    p = " ".join(stages.VERIFY_STATIC.split())
    assert "YOU ARE THE VOLUME CONTROL" in p
    assert "MERGE duplicates" in p
    assert "one comment per line" in p


def test_analysis_prompt_does_not_impose_a_numeric_cap():
    """Capping upstream would drop findings nobody judged. Design keeps its
    own cap because it is scoped to cross-cutting concerns."""
    assert "Max " not in stages.ANALYSIS_STATIC


def test_prompts_encourage_reporting_outside_diff_consequences():
    for name in ("ANALYSIS_STATIC", "DESIGN_STATIC"):
        # Prompts are hard-wrapped, so collapse whitespace before matching
        # phrases -- otherwise these assertions break on rewrapping alone.
        prompt = " ".join(getattr(stages, name).split())
        assert "Outside this change" in prompt
        assert "Do not suppress a real consequence just because it lands " \
               "outside the diff" in prompt

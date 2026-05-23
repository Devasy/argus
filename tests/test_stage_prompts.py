"""Stage prompts carry two things the traces showed were missing: how to use
the tools without looping, and how to actually review code.

The old prompts were a dense paragraph of tool names. Agents pattern-matched
on risky-looking constructs instead of tracing consequences, and when a tool
answered unhelpfully they abandoned it (Langfuse trace 1238b9e4: the scout
dropped the call-graph tool at round 3 and never returned to it).

These tests assert structure and the specific guidance those failures
motivated -- never exact wording, which should stay free to improve.
"""
import pytest

from argus.review import stages

JUDGING_PROMPTS = {
    "ANALYSIS_STATIC": stages.ANALYSIS_STATIC,
    "DESIGN_STATIC": stages.DESIGN_STATIC,
    "VERIFY_STATIC": stages.VERIFY_STATIC,
}
ALL_PROMPTS = {
    **JUDGING_PROMPTS,
    "SCOUT_STATIC": stages.SCOUT_STATIC,
    "QA_SCENARIOS_STATIC": stages.QA_SCENARIOS_STATIC,
}


@pytest.mark.parametrize("name,prompt", sorted(ALL_PROMPTS.items()))
def test_prompt_has_the_five_part_structure(name, prompt):
    for section in ("ROLE", "WHAT YOU ARE DEALING WITH", "TOOL PLAYBOOK",
                    "ANTI-PATTERNS", "WHEN TO STOP"):
        assert section in prompt, f"{name} is missing {section}"


@pytest.mark.parametrize("name,prompt", sorted(ALL_PROMPTS.items()))
def test_prompt_warns_about_collapsed_diffs(name, prompt):
    """The single most expensive misunderstanding in the trace record: an
    agent cannot know a file's hunks will never arrive unless told."""
    assert "COLLAPSED" in prompt
    assert "get_file_lines" in prompt


@pytest.mark.parametrize("name,prompt", sorted(ALL_PROMPTS.items()))
def test_prompt_names_the_cheap_tool_before_the_expensive_one(name, prompt):
    """get_file_lines was called 1449 times across the recursion failures.
    outline_file exists to absorb most of that; the prompt has to say so."""
    assert "outline_file" in prompt
    assert "search_code" in prompt


@pytest.mark.parametrize("name,prompt", sorted(ALL_PROMPTS.items()))
def test_prompt_forbids_repeating_a_tool_call(name, prompt):
    assert "Repeating a tool call" in prompt


@pytest.mark.parametrize("name,prompt", sorted(ALL_PROMPTS.items()))
def test_prompt_says_stopping_early_is_allowed(name, prompt):
    """Agents that never called their output tool had usually finished
    thinking. Each prompt must make the terminal call explicit."""
    assert "WHEN TO STOP" in prompt
    assert any(tool in prompt for tool in
               ("ScoutOutput", "FindingList", "VerdictList", "TestScenarioList"))


# --- reviewing methodology (judging stages only) ---------------------------

@pytest.mark.parametrize("name,prompt", sorted(JUDGING_PROMPTS.items()))
def test_judging_prompt_carries_review_methodology(name, prompt):
    assert "HOW TO REVIEW" in prompt
    assert "MENTALITY" in prompt


@pytest.mark.parametrize("name,prompt", sorted(JUDGING_PROMPTS.items()))
def test_methodology_demands_tracing_over_pattern_matching(name, prompt):
    """The core discipline: a construct that looks dangerous is not a
    finding until traced to a concrete bad outcome."""
    assert "trace, do not pattern-match" in prompt
    assert "specific" in prompt


@pytest.mark.parametrize("name,prompt", sorted(JUDGING_PROMPTS.items()))
def test_methodology_prioritises_effects_outside_the_diff(name, prompt):
    """Where real defects hide, and what a diff-only reader cannot see."""
    assert "NOT in the diff" in prompt
    assert "graph_diff_impact" in prompt


@pytest.mark.parametrize("name,prompt", sorted(JUDGING_PROMPTS.items()))
def test_methodology_states_what_is_not_a_finding(name, prompt):
    assert "WHAT IS NOT A FINDING" in prompt
    for excluded in ("Style", "Pre-existing"):
        assert excluded in prompt


@pytest.mark.parametrize("name,prompt", sorted(JUDGING_PROMPTS.items()))
def test_methodology_makes_reporting_nothing_respectable(name, prompt):
    """Precision beats recall: a clean MR should come back clean, and agents
    need permission to say so or they invent filler."""
    assert "reports nothing on a clean MR is doing the job correctly" in prompt


@pytest.mark.parametrize("name,prompt", sorted(JUDGING_PROMPTS.items()))
def test_methodology_calibrates_severity_and_confidence(name, prompt):
    assert "CALIBRATING SEVERITY AND CONFIDENCE" in prompt
    assert "consequence in production" in prompt


@pytest.mark.parametrize("name,prompt", sorted(JUDGING_PROMPTS.items()))
def test_methodology_has_a_pre_report_checklist(name, prompt):
    assert "BEFORE YOU REPORT ANYTHING" in prompt
    assert "NEW-file line number" in prompt


def test_scout_does_not_get_finding_methodology():
    """Scout describes; it must not start judging, or downstream agents
    inherit its opinions as fact."""
    assert "HOW TO REVIEW" not in stages.SCOUT_STATIC
    assert "do not review" in stages.SCOUT_STATIC


def test_verifier_methodology_is_framed_as_rejection_criteria():
    """The verifier applies the same method inverted -- checking the author
    did the tracing, not redoing the review."""
    assert "APPLYING THAT METHOD AS A VERIFIER" in stages.VERIFY_STATIC
    assert "Reject when" in stages.VERIFY_STATIC
    assert "pre-existing" in stages.VERIFY_STATIC.lower()


def test_prompts_render_literal_braces():
    """These are f-strings; an unescaped brace would silently mangle the
    tool-call example agents copy."""
    assert "{path, start, end}" in stages.ANALYSIS_STATIC


# --- pre-existing-convention findings (2026-08-10) --------------------------
# A verifier accepted "Deprecated logger.warn() calls" against a file this MR
# touched, citing an accurate line and quote -- but logger.warn() turned out
# to be used 39 times across 20 files in the codebase, a pre-existing
# convention the MR did not introduce. The rejection criterion for this
# already existed ("the same pattern is already used elsewhere"), but nothing
# connected it explicitly to code-quality/deprecation findings, which read as
# "technically confirmed" rather than triggering the pre-existing-pattern
# check. An accurate evidence_quote is not the same test as "this MR caused
# it," and that distinction needs to be explicit, not inferred.

@pytest.mark.parametrize("name,prompt", sorted(JUDGING_PROMPTS.items()))
def test_technical_sounding_findings_still_get_the_convention_check(name, prompt):
    assert "deprecated" in prompt.lower()
    assert "search_code for the flagged pattern" in prompt


def test_verifier_explicitly_rejects_accurate_but_preexisting_findings():
    """The exact failure: the finding was factually correct about the line it
    cited, and still should have been rejected as a pre-existing convention."""
    p = " ".join(stages.VERIFY_STATIC.split())
    assert "regardless of how the finding is worded" in p
    assert "does not mean this MR caused the problem" in p


# --- knowledge-recalled claims (2026-09-09) ---------------------------------
# A traced failure: a model asserted a library config option "is not a
# standard" one, purely from its own training-time knowledge, explicitly told
# itself to stop deliberating and commit -- and was wrong; a human replied
# that the option is real. Verify's own tools (get_file_lines, search_code)
# only ever confirm what THIS repo's code does; they cannot confirm or deny a
# claim about a library's behavior in general, so a false claim like this
# passes tracing (the cited line is real) while still being wrong.

def test_review_method_flags_claims_about_external_library_behavior():
    """The upstream fix: catch this at the source, before an issue is even
    filed, not only at verify after the fact."""
    p = " ".join(stages.ANALYSIS_STATIC.split())
    assert "recall" in p.lower() or "recalling" in p.lower()
    assert 'type="question"' in p


def test_verify_rejects_issues_that_are_really_claims_about_a_library():
    p = " ".join(stages.VERIFY_STATIC.split())
    assert "does not exist, is not standard, or is not supported" in p
    assert "outside this repo" in p


# --- grounding flags verify instead of gating it (2026-09-09) ---------------
# A mechanical substring check cannot tell a hallucination from an agent's own
# citation decoration or a quote that just moved a few lines, and it was
# dropping findings before verify -- which has the tools and the file -- ever
# got a look. So it no longer gates: a failed check reaches verify as a FLAG
# note instead, and verify's own reject/`corrected` call is what decides.

def test_verify_is_told_what_a_grounding_flag_means_and_how_to_resolve_it():
    p = " ".join(stages.VERIFY_STATIC.split())
    assert "FLAG: mechanical check did not find this evidence_quote" in p
    assert "reject as hallucinated" in p
    assert "`corrected`" in p


def test_verify_may_correct_the_line_when_it_resolves_a_flag():
    """The pre-existing `corrected` contract pinned file_path AND line, which
    would have blocked verify from ever relocating a flagged finding to the
    real line it confirmed -- that constraint had to relax for this to work."""
    p = " ".join(stages.VERIFY_STATIC.split())
    assert "correct `line` too" in p

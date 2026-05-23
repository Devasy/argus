import logging
from pathlib import Path

from argus.review.artifacts import CandidateFinding
from argus.review.grounding import (apply_grounding, ground_finding,
                                        ground_results)


def _f(fid, quote, line=2, path="m.py"):
    return CandidateFinding(finding_id=fid, stage="analysis", type="issue",
                            severity="high", confidence=0.9, file_path=path,
                            line=line, title="t", body="b", evidence_quote=quote)


def _ws(tmp_path: Path) -> Path:
    (tmp_path / "m.py").write_text("import os\nx = eval(user_input)\ny = 2\n")
    return tmp_path


def test_grounded_exact(tmp_path):
    r = ground_finding(_f("a", "x = eval(user_input)", line=2), _ws(tmp_path))
    assert r.grounded and r.corrected_line is None


def test_grounded_wrong_line_corrected(tmp_path):
    r = ground_finding(_f("a", "x = eval(user_input)", line=7), _ws(tmp_path))
    assert r.grounded and r.corrected_line == 2


def test_hallucinated_quote_dropped(tmp_path):
    ws = _ws(tmp_path)
    kept, dropped = apply_grounding(
        [_f("a", "x = eval(user_input)"), _f("b", "this line does not exist")], ws)
    assert [f.finding_id for f in kept] == ["a"] and dropped == ["b"]


def test_missing_file_dropped(tmp_path):
    r = ground_finding(_f("a", "anything", path="nope.py"), _ws(tmp_path))
    assert not r.grounded


def test_ground_results_logs_warning_on_drop(tmp_path, caplog):
    ws = _ws(tmp_path)
    caplog.set_level(logging.WARNING, logger="argus.grounding")
    results = ground_results(
        [_f("a", "x = eval(user_input)"), _f("b", "this line does not exist")], ws)
    assert [r.finding_id for r in results if not r.grounded] == ["b"]
    assert any("grounding dropped" in rec.message for rec in caplog.records)


def test_ground_results_no_warning_when_all_grounded(tmp_path, caplog):
    ws = _ws(tmp_path)
    caplog.set_level(logging.WARNING, logger="argus.grounding")
    ground_results([_f("a", "x = eval(user_input)")], ws)
    assert not any("grounding dropped" in rec.message for rec in caplog.records)


# --- decorated-quote recovery -----------------------------------------
#
# Measured against production dry-run data (see the review-quality ledger):
# the large majority of historical ungrounded findings carry a literal "\n"
# in evidence_quote. get_file_lines/get_hunk render "{line_no} | {text}"
# (tools.py:219), and reviewer agents sometimes copy that formatting
# straight into evidence_quote instead of the bare source text, so the exact
# substring check never had a chance. Separately, some agents write their
# own "N-M: " citation-range prefix around a real quote. Validated against
# real source before writing this fix: cleaning recovers a real minority of
# ungrounded findings; the rest are an agent quoting its own paraphrase
# instead of code, which must stay dropped -- shapes reproduced below with
# synthetic code, not the real client source they were found in.

def test_recovers_the_tool_output_line_number_prefix(tmp_path):
    """evidence_quote copied straight from get_file_lines' own
    "{line_no} | {text}" rendering, decoration and all."""
    ws = _ws(tmp_path)
    r = ground_finding(_f("a", "2 | x = eval(user_input)", line=2), ws)
    assert r.grounded


def test_recovers_a_citation_style_line_range_prefix(tmp_path):
    """A citation habit: range prefix plus wrapping quote marks around
    genuine source code."""
    ws = _ws(tmp_path)
    r = ground_finding(
        _f("a", '2-3: "x = eval(user_input)"', line=2), ws)
    assert r.grounded


def test_does_not_rescue_a_genuine_paraphrase(tmp_path):
    """The other real failure mode, same decoration: some "evidence" is the
    agent's own natural-language description, not a copy of the code.
    Cleaning must not turn grounding into a fuzzy matcher -- a
    prefix-stripped paraphrase still has to fail, the same as it should have
    before this fix."""
    ws = _ws(tmp_path)
    r = ground_finding(
        _f("a", '2-3: "This assigns the result of a dangerous eval call"',
           line=2), ws)
    assert not r.grounded


def test_recovers_regardless_of_line_hint_via_the_existing_fallback_scan(tmp_path):
    """Decoration-stripping composes with the pre-existing wrong-line
    fallback: a decorated quote pointed at the wrong line must still be found
    (and the line corrected) by the full-file scan, not just at f.line."""
    ws = _ws(tmp_path)
    r = ground_finding(_f("a", "2 | x = eval(user_input)", line=99), ws)
    assert r.grounded and r.corrected_line == 2


def test_still_grounds_a_plain_undecorated_quote(tmp_path):
    """The common, already-working case must be untouched: no decoration to
    strip, matches exactly as before."""
    r = ground_finding(_f("a", "x = eval(user_input)", line=2), _ws(tmp_path))
    assert r.grounded and r.corrected_line is None

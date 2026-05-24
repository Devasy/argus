from argus.review.artifacts import CandidateFinding
from argus.review.prevalence import (count_other_files, prevalence_note,
                                         quote_probe)


def _f(fid="f1", path="src/a.py", quote="x"):
    return CandidateFinding(finding_id=fid, stage="analysis", type="issue",
                            severity="high", confidence=0.9, file_path=path,
                            line=1, title="t", body="b", evidence_quote=quote)


def test_probe_picks_the_most_distinctive_line():
    assert quote_probe("  enforceFocus={false}  ") == "enforceFocus={false}"
    quote = "if x:\n    connector.collection('audit').find_one({'id': i})\n"
    assert quote_probe(quote) == "connector.collection('audit').find_one({'id': i})"


def test_probe_refuses_generic_or_missing_quotes():
    """A short quote matches everywhere, so its count would be noise, not evidence."""
    assert quote_probe("return None") is None
    assert quote_probe("") is None
    assert quote_probe(None) is None
    assert quote_probe("   \n  \n") is None


async def test_counts_other_files_containing_the_same_line(tmp_path):
    """The real case: enforceFocus={false} appears in 24 other modals, which is
    what makes it the codebase's convention rather than this MR's regression."""
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / "src" / name).write_text(
            "def render():\n    return dict(enforceFocus_is_false=1)\n")
    (tmp_path / "src" / "target.py").write_text(
        "def render():\n    return dict(enforceFocus_is_false=1)\n")

    n = await count_other_files(tmp_path, "enforceFocus_is_false",
                                own_path="src/target.py")
    assert n == 3, "its own file must not be counted as corroboration"


async def test_counts_zero_when_the_pattern_is_unique(tmp_path):
    (tmp_path / "only.py").write_text("uniquely_spelled_thing = 1\n")
    n = await count_other_files(tmp_path, "uniquely_spelled_thing",
                                own_path="only.py")
    assert n == 0


async def test_counts_zero_for_absent_pattern(tmp_path):
    (tmp_path / "only.py").write_text("something_else = 1\n")
    assert await count_other_files(tmp_path, "not_present_anywhere",
                                   own_path="only.py") == 0


def test_note_renders_only_when_the_pattern_is_widespread():
    assert prevalence_note(0) == ""
    assert prevalence_note(1) == ""
    assert "3 other file(s)" in prevalence_note(3)
    assert "already appears" in prevalence_note(24)


def test_note_marks_the_cap_as_at_least():
    """A capped count must not read as an exact one."""
    from argus.review.prevalence import MAX_PREVALENCE_MATCHES
    assert "+" in prevalence_note(MAX_PREVALENCE_MATCHES)

from pathlib import Path

import pytest

from argus.knowledge.audit_apply import (ARCHIVABLE, citation_is_resolvable,
                                             groundedness_from_verdict,
                                             validate_verdicts)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "a.py").write_text("import os\nDEBUG = True\nprint(DEBUG)\n")
    return tmp_path


def test_citation_resolves_when_quote_present(workspace):
    assert citation_is_resolvable(
        {"file": "a.py", "line": 2, "quote": "DEBUG = True"}, workspace)


def test_citation_fails_when_quote_absent(workspace):
    assert not citation_is_resolvable(
        {"file": "a.py", "line": 2, "quote": "TOTALLY NOT THERE"}, workspace)


def test_citation_fails_for_missing_file(workspace):
    assert not citation_is_resolvable(
        {"file": "nope.py", "line": 1, "quote": "x"}, workspace)


def test_uncited_verdict_is_discarded(workspace):
    kept, reasons = validate_verdicts(
        [{"learning_id": "x", "verdict": "contradicted", "confidence": 0.9,
          "citations": []}], workspace)
    assert kept == []
    assert any("citation" in r for r in reasons)


def test_verdict_with_bogus_citation_is_discarded(workspace):
    kept, _ = validate_verdicts(
        [{"learning_id": "x", "verdict": "stale", "confidence": 0.9,
          "citations": [{"file": "a.py", "line": 1, "quote": "NOT PRESENT"}]}],
        workspace)
    assert kept == []


def test_ungrounded_verdict_survives_without_citation(workspace):
    """'I could not find a referent' is legitimately uncitable."""
    kept, _ = validate_verdicts(
        [{"learning_id": "x", "verdict": "ungrounded", "confidence": 0.5,
          "citations": []}], workspace)
    assert len(kept) == 1


def test_ungrounded_is_never_archivable():
    assert "ungrounded" not in ARCHIVABLE
    assert "corroborated" not in ARCHIVABLE
    assert "stale" in ARCHIVABLE
    assert "contradicted" in ARCHIVABLE


def test_groundedness_rewards_corroborated_and_punishes_contradicted():
    assert groundedness_from_verdict("corroborated", 0.9) > 0.5
    assert groundedness_from_verdict("contradicted", 0.9) < 0.5
    assert groundedness_from_verdict("stale", 0.9) < 0.5


def test_groundedness_of_ungrounded_is_neutral():
    """No referent found is not evidence of badness."""
    assert groundedness_from_verdict("ungrounded", 0.9) == 0.5


def test_low_confidence_pulls_toward_neutral():
    strong = groundedness_from_verdict("contradicted", 1.0)
    weak = groundedness_from_verdict("contradicted", 0.1)
    assert weak > strong  # weaker evidence -> closer to 0.5

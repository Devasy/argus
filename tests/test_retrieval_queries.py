"""Query construction for learning retrieval."""
from argus.knowledge.queries import (identifier_query, learnings_query_text,
                                         scout_query)


class TestIdentifierQuery:
    def test_keeps_camel_case_identifier(self):
        assert "computeTotals" in identifier_query(["const x = computeTotals(rows)"])

    def test_keeps_screaming_snake_constant(self):
        assert "MAX_RETRY_COUNT" in identifier_query(["MAX_RETRY_COUNT = 5"])

    def test_keeps_snake_case_function(self):
        assert "render_widget" in identifier_query(["def render_widget(cfg):"])

    def test_drops_language_keywords(self):
        out = identifier_query(["return const if else def class import for while"])
        assert out.strip() == ""

    def test_drops_bare_numbers(self):
        assert identifier_query(["x = 42"]).strip() == ""

    def test_deduplicates_repeated_identifiers(self):
        out = identifier_query(["computeTotals()"] * 5)
        assert out.split().count("computeTotals") == 1

    def test_reads_past_the_first_2000_characters(self):
        filler = ["padding_value_%d = fillerExpression(alpha, beta, gamma)" % n
                  for n in range(40)]
        lines = filler + ["lateIdentifier = 1"]
        assert sum(len(l) for l in filler) > 2000
        assert "lateIdentifier" in identifier_query(lines)

    def test_caps_the_number_of_terms(self):
        lines = ["identifierNumber%d = 1" % n for n in range(500)]
        assert len(identifier_query(lines, max_terms=30).split()) == 30

    def test_rarest_terms_come_first_when_frequencies_differ(self):
        lines = ["commonThing"] * 20 + ["rareThing"]
        terms = identifier_query(lines).split()
        assert terms.index("rareThing") < terms.index("commonThing")

    def test_no_added_lines_yields_empty(self):
        assert identifier_query([]).strip() == ""


class TestScoutQuery:
    def test_includes_mr_summary_and_file_summaries(self):
        out = scout_query(
            mr_summary="Adds a retry toggle to the settings panel",
            file_summaries={"ui/panel.jsx": "Adds retryEnabled state and a switch"},
            changed_paths=["ui/panel.jsx"])
        assert "retry toggle" in out
        assert "retryEnabled" in out

    def test_includes_changed_paths(self):
        out = scout_query(mr_summary="", file_summaries={},
                          changed_paths=["widgets/render_widget.py"])
        assert "widgets/render_widget.py" in out

    def test_tolerates_missing_summaries(self):
        assert scout_query(mr_summary=None, file_summaries={},
                           changed_paths=[]).strip() == ""

    def test_skips_files_without_a_summary(self):
        out = scout_query(mr_summary="x", file_summaries={"a.py": None},
                          changed_paths=["a.py"])
        assert "None" not in out


class TestLearningsQueryText:
    def test_combines_title_paths_and_added_lines(self):
        out = learnings_query_text("Add retry toggle", ["ui/panel.jsx"],
                                   ["const retryEnabled = true"])
        assert "Add retry toggle" in out
        assert "ui/panel.jsx" in out
        assert "retryEnabled" in out

    def test_truncates_added_lines_at_the_configured_budget(self):
        lines = ["x" * 100 for _ in range(100)]
        out = learnings_query_text("t", [], lines, max_chars=500)
        assert len(out) < 700

    def test_caps_the_joined_paths_matching_runner_py(self):
        """Mirrors runner.py::_learnings_query_text's fix: a 54-file MR built
        a 5,390-char query from paths alone and crashed the embedding call in
        production. This module's baseline strategy has to reproduce that fix,
        not the bug."""
        paths = [f"very/long/nested/path/number_{i}/file.txt" for i in range(200)]
        out = learnings_query_text("t", paths, [])
        assert len(out) < 4000

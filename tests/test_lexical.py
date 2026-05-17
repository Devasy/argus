"""Lexical retrieval: identifier-aware tokenization, BM25, rank fusion."""
import pytest

from argus.knowledge.lexical import Bm25Index, rrf, tokenize


class TestTokenize:
    def test_splits_camel_case_keeping_whole_and_parts(self):
        out = tokenize("computeTotals")
        assert "computetotals" in out
        assert "compute" in out
        assert "totals" in out

    def test_splits_snake_case_keeping_whole_and_parts(self):
        out = tokenize("max_retry_count")
        assert "max_retry_count" in out
        assert {"max", "retry", "count"} <= set(out)

    def test_splits_screaming_snake_constant(self):
        out = tokenize("MAX_RETRY_COUNT = 5")
        assert "max_retry_count" in out
        assert "retry" in out

    def test_drops_punctuation_and_lowercases(self):
        assert set(tokenize("Foo.bar(baz);")) >= {"foo", "bar", "baz"}

    def test_keeps_dotted_path_parts(self):
        out = tokenize("widgets/render_widget.py")
        assert "render_widget" in out
        assert "widget" in out

    def test_single_char_fragments_dropped(self):
        assert "a" not in tokenize("aFoo")

    def test_empty_text_yields_no_tokens(self):
        assert tokenize("") == []


class TestBm25Index:
    def test_rare_identifier_beats_common_word(self):
        idx = Bm25Index({
            "rare": "call computeTotals before saving the record",
            "common": "the record is saved when the user saves the record",
            "filler1": "the user saves the record",
            "filler2": "saving the record is fine",
        })
        ranked = [i for i, _ in idx.rank("computeTotals", limit=4)]
        assert ranked[0] == "rare"

    def test_camel_query_matches_snake_document(self):
        idx = Bm25Index({
            "snake": "set max_retry_count on the client",
            "other": "unrelated guidance about logging",
        })
        ranked = [i for i, _ in idx.rank("maxRetryCount", limit=2)]
        assert ranked[0] == "snake"

    def test_scores_every_document_not_a_prefiltered_pool(self):
        docs = {f"d{n}": "unrelated filler text" for n in range(200)}
        docs["needle"] = "guidance about renderWidget defaults"
        idx = Bm25Index(docs)
        ranked = [i for i, _ in idx.rank("renderWidget", limit=5)]
        assert ranked[0] == "needle"

    def test_limit_caps_returned_results(self):
        idx = Bm25Index({f"d{n}": "shared token here" for n in range(10)})
        assert len(idx.rank("shared", limit=3)) == 3

    def test_query_with_no_overlap_returns_nothing(self):
        idx = Bm25Index({"a": "alpha beta", "b": "gamma delta"})
        assert idx.rank("completelyUnrelatedIdentifier", limit=5) == []

    def test_empty_corpus_returns_nothing(self):
        assert Bm25Index({}).rank("anything", limit=5) == []

    def test_scores_are_descending(self):
        idx = Bm25Index({
            "a": "renderWidget renderWidget defaults",
            "b": "renderWidget defaults",
            "c": "defaults only",
        })
        scores = [s for _, s in idx.rank("renderWidget defaults", limit=3)]
        assert scores == sorted(scores, reverse=True)


class TestRrf:
    def test_appearing_in_both_lists_beats_top_of_one(self):
        fused = rrf([["solo", "both"], ["other", "both"]])
        assert fused["both"] > fused["solo"]

    def test_absent_from_a_list_still_scores(self):
        fused = rrf([["a"], ["b"]])
        assert fused["a"] > 0 and fused["b"] > 0

    def test_earlier_rank_scores_higher_within_one_list(self):
        fused = rrf([["first", "second"]])
        assert fused["first"] > fused["second"]

    def test_weights_shift_the_balance(self):
        lists = [["x", "y"], ["y", "x"]]
        vector_heavy = rrf(lists, weights=[10.0, 1.0])
        assert vector_heavy["x"] > vector_heavy["y"]

    def test_no_lists_yields_no_scores(self):
        assert rrf([]) == {}

    def test_duplicate_id_within_a_list_uses_best_rank(self):
        fused = rrf([["a", "b", "a"]])
        assert fused["a"] == pytest.approx(rrf([["a"]])["a"])

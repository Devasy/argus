"""Candidate ranking for learning retrieval: bonuses, demotion, exploration."""
import uuid

from argus.domain.models import Learning
from argus.knowledge.learnings import hybrid_base_scores, rank_learnings


def make(topic: str, *, file_pattern: str | None = None, hit: int = 0,
         harmful: int = 0, ignored: int = 0, miss: int = 0) -> Learning:
    l = Learning(id=uuid.uuid4(), topic=topic, hint_text="h",
                 kind="guidance", file_pattern=file_pattern,
                 hit_count=hit, harmful_count=harmful,
                 ignored_count=ignored, miss_count=miss, groundedness=None)
    return l


class TestRankLearnings:
    def test_higher_base_score_ranks_first(self):
        a, b = make("a"), make("b")
        out = rank_learnings([a, b], {a.id: 0.2, b.id: 0.9},
                             file_paths=[], top=2, explore=False)
        assert out == [b, a]

    def test_pattern_match_promotes_a_weaker_candidate(self):
        plain = make("plain")
        matching = make("matching", file_pattern="*.py")
        out = rank_learnings([plain, matching],
                             {plain.id: 0.5, matching.id: 0.4},
                             file_paths=["widgets/render_widget.py"],
                             top=2, explore=False)
        assert out[0] is matching

    def test_pattern_that_does_not_match_gives_no_bonus(self):
        plain = make("plain")
        other = make("other", file_pattern="*.js")
        out = rank_learnings([plain, other],
                             {plain.id: 0.5, other.id: 0.4},
                             file_paths=["widgets/render_widget.py"],
                             top=2, explore=False)
        assert out[0] is plain

    def test_harmful_learning_falls_below_a_weaker_untainted_one(self):
        untainted = make("untainted")
        harmful = make("harmful", harmful=5)
        out = rank_learnings([untainted, harmful],
                             {untainted.id: 0.5, harmful.id: 0.6},
                             file_paths=[], top=2, explore=False)
        assert out[0] is untainted

    def test_returns_all_candidates_when_fewer_than_top(self):
        a = make("a")
        out = rank_learnings([a], {a.id: 0.5}, file_paths=[], top=8)
        assert out == [a]

    def test_top_caps_the_result_size(self):
        cands = [make(f"l{n}") for n in range(10)]
        base = {l.id: 1.0 - n / 100 for n, l in enumerate(cands)}
        out = rank_learnings(cands, base, file_paths=[], top=3, explore=False)
        assert len(out) == 3


class TestExplorationSlot:
    def test_zero_evidence_candidate_outside_the_cut_takes_the_last_slot(self):
        cands = [make(f"l{n}", hit=3) for n in range(3)]
        newcomer = make("newcomer")
        base = {l.id: 0.9 - n / 100 for n, l in enumerate(cands)}
        base[newcomer.id] = 0.1
        out = rank_learnings(cands + [newcomer], base,
                             file_paths=[], top=3, explore=True)
        assert newcomer in out
        assert len(out) == 3

    def test_disabled_exploration_keeps_the_pure_ranking(self):
        cands = [make(f"l{n}", hit=3) for n in range(3)]
        newcomer = make("newcomer")
        base = {l.id: 0.9 - n / 100 for n, l in enumerate(cands)}
        base[newcomer.id] = 0.1
        out = rank_learnings(cands + [newcomer], base,
                             file_paths=[], top=3, explore=False)
        assert newcomer not in out

    def test_no_slot_spent_when_every_outsider_has_evidence(self):
        cands = [make(f"l{n}", hit=3) for n in range(4)]
        base = {l.id: 0.9 - n / 100 for n, l in enumerate(cands)}
        out = rank_learnings(cands, base, file_paths=[], top=3, explore=True)
        assert out == cands[:3]


class TestHybridBaseScores:
    def test_candidate_in_both_arms_outranks_one_arms_leader(self):
        both, solo = uuid.uuid4(), uuid.uuid4()
        other = uuid.uuid4()
        scores = hybrid_base_scores([solo, both], [other, both])
        assert scores[both] > scores[solo]

    def test_lexical_only_candidate_still_scores(self):
        vec, lex = uuid.uuid4(), uuid.uuid4()
        scores = hybrid_base_scores([vec], [lex])
        assert scores[lex] > 0

    def test_weights_can_favour_one_arm(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        scores = hybrid_base_scores([a, b], [b, a], weights=(10.0, 1.0))
        assert scores[a] > scores[b]

import math

from argus.knowledge.reputation import (HARMFUL_WEIGHT, combined_strength,
                                            decay_weight, reputation,
                                            wilson_lower_bound)


def test_no_evidence_is_neutral_not_zero():
    # A brand-new learning must not be indistinguishable from a proven-bad one.
    assert reputation(hit=0, harmful=0, ignored=0, miss=0) == 0.5


def test_wilson_penalises_small_samples():
    # 5/5 must not outrank 200/220 — that is the whole point of Wilson.
    small = wilson_lower_bound(5, 0)
    large = wilson_lower_bound(200, 20)
    assert small < large


def test_wilson_bounds():
    assert 0.0 <= wilson_lower_bound(0, 10) <= 1.0
    assert 0.0 <= wilson_lower_bound(10, 0) <= 1.0
    assert wilson_lower_bound(0, 0) == 0.5


def test_harmful_counts_double_against_a_learning():
    only_ignored = reputation(hit=10, harmful=0, ignored=4, miss=0)
    with_harmful = reputation(hit=10, harmful=4, ignored=0, miss=0)
    assert with_harmful < only_ignored


def test_harmful_weight_is_two():
    assert HARMFUL_WEIGHT == 2.0


def test_good_learning_scores_above_bad_learning():
    good = reputation(hit=20, harmful=0, ignored=2, miss=1)
    bad = reputation(hit=1, harmful=8, ignored=10, miss=4)
    assert good > 0.5 > bad


def test_decay_weight_halves_at_half_life():
    assert math.isclose(decay_weight(90.0, half_life_days=90.0), 0.5, rel_tol=1e-9)
    assert math.isclose(decay_weight(0.0), 1.0, rel_tol=1e-9)
    assert decay_weight(365.0) < decay_weight(30.0)


def test_decay_weight_never_negative():
    assert decay_weight(10_000.0) >= 0.0


def test_combined_falls_back_to_outcome_when_never_audited():
    assert combined_strength(0.8, None) == 0.8


def test_groundedness_pulls_combined_score():
    high = combined_strength(0.5, 0.9)
    low = combined_strength(0.5, 0.1)
    assert high > 0.5 > low


def test_stale_audit_loses_influence():
    fresh = combined_strength(0.5, 0.9, audit_age_days=0.0)
    stale = combined_strength(0.5, 0.9, audit_age_days=365.0)
    assert fresh > stale  # an old audit should not dominate forever

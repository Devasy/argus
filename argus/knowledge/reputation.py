"""Pure scoring maths for learning strength.

Two ideas, no I/O:

1. Wilson lower bound — a pessimistic estimate of true success rate given the
   evidence so far. Stops "5/5" from outranking "200/220", and gives a
   zero-evidence learning a neutral prior instead of 0.0 (the old `_hit_rate`
   returned 0.0 for both "never tried" and "always wrong", which made new
   learnings unrankable and therefore untriable).
2. Exponential time decay — recent verdicts count more, so a learning that was
   right about since-refactored code fades instead of coasting.
"""
import math

# A harmful verdict (the learning drove a comment a human explicitly rejected)
# costs twice a plain miss: a wrong comment burns reviewer trust, silence does not.
HARMFUL_WEIGHT = 2.0

NEUTRAL = 0.5


def wilson_lower_bound(successes: float, failures: float, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for a Bernoulli proportion.

    Returns NEUTRAL when there is no evidence at all, so untried learnings sort
    above demonstrably bad ones rather than tying with them at zero.
    """
    n = successes + failures
    if n <= 0:
        return NEUTRAL
    phat = successes / n
    denom = 1.0 + (z * z) / n
    centre = phat + (z * z) / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + (z * z) / (4 * n)) / n)
    return max(0.0, min(1.0, (centre - margin) / denom))


def decay_weight(age_days: float, half_life_days: float = 90.0) -> float:
    """Exponential decay: a verdict `half_life_days` old counts half as much."""
    if age_days <= 0:
        return 1.0
    if half_life_days <= 0:
        return 1.0
    return max(0.0, 0.5 ** (age_days / half_life_days))


def reputation(hit: float, harmful: float, ignored: float, miss: float) -> float:
    """Collapse weighted verdict counts into a [0,1] strength score.

    `hit` is the only success. `harmful` counts double; `ignored` and `miss` are
    weak negatives (the learning was present and did not help, which is evidence
    but not damning).
    """
    successes = hit
    failures = HARMFUL_WEIGHT * harmful + ignored + miss
    return wilson_lower_bound(successes, failures)


def combined_strength(outcome_rep: float, groundedness: float | None,
                      audit_age_days: float = 0.0,
                      audit_half_life_days: float = 180.0) -> float:
    """Blend revealed preference with groundedness.

    Kept as two axes rather than one number because they fail differently and
    the interesting diagnostic is the disagreement: high groundedness with low
    outcome reputation means the learning is TRUE but nobody acts on it — a
    rewrite candidate, not a delete candidate. Collapsing them hides that.

    An audit's influence decays with age, since code moves on.
    """
    if groundedness is None:
        return outcome_rep
    w = 0.4 * decay_weight(audit_age_days, half_life_days=audit_half_life_days)
    return max(0.0, min(1.0, (1.0 - w) * outcome_rep + w * groundedness))

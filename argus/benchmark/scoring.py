"""Score a benchmark run against the adjudicated golden set.

The question is not "what fraction of comments did a human accept" -- that
number turned out to measure the reconciler's keyword list more than the bot.
It is: of the things we have independently established to be false, how many
did the run raise again, and of the things established true, how many did it
find.
"""
from dataclasses import dataclass, field

from argus.benchmark.spec import Item
from argus.review.publisher import title_similarity

# Looser than the dedup threshold: this matches a paraphrase of one concern
# across two different runs, not one finding against its own restatement.
MATCH_SIMILARITY = 0.4


def matches(item: Item, finding: dict) -> bool:
    """Same file and the same concern. Line numbers are ignored on purpose --
    they drift between the reviewed revision and any later one."""
    if (finding.get("file_path") or "") != item.file:
        return False
    return title_similarity(item.claim, finding.get("title") or "") >= MATCH_SIMILARITY


@dataclass
class MRScore:
    hits: int = 0
    misses: list[str] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)
    regressions: int = 0
    trivia: list[str] = field(default_factory=list)
    unadjudicated: int = 0


def score_mr(items: list[Item], findings: list[dict]) -> MRScore:
    """`findings` is every candidate the run produced for this MR; only the
    ones it kept are judged, since a dropped candidate never reached a human."""
    kept = [f for f in findings if f.get("valid") is True]
    score = MRScore()
    matched_findings: set[int] = set()

    for item in items:
        hit = next((i for i, f in enumerate(kept) if matches(item, f)), None)
        if hit is not None:
            matched_findings.add(hit)
        if item.verdict == "human_right":
            if hit is not None:
                score.false_positives.append(item.id)
                score.regressions += 1
        elif item.verdict == "bot_right":
            if hit is not None:
                score.hits += 1
            else:
                score.misses.append(item.id)
        else:
            if hit is not None:
                score.trivia.append(item.id)

    score.unadjudicated = len(kept) - len(matched_findings)
    return score


@dataclass
class RunReport:
    mrs_scored: int
    hits: int
    misses: int
    regressions: int
    trivia: int
    unadjudicated: int
    recall: float | None
    worst_mr: tuple[str, int] | None


def aggregate(per_mr: dict[tuple[str, int], MRScore]) -> RunReport:
    hits = sum(s.hits for s in per_mr.values())
    misses = sum(len(s.misses) for s in per_mr.values())
    real = hits + misses
    worst = max(per_mr.items(), key=lambda kv: kv[1].regressions,
                default=(None, None))
    return RunReport(
        mrs_scored=len(per_mr),
        hits=hits,
        misses=misses,
        regressions=sum(s.regressions for s in per_mr.values()),
        trivia=sum(len(s.trivia) for s in per_mr.values()),
        unadjudicated=sum(s.unadjudicated for s in per_mr.values()),
        # None, not 0.0: nothing adjudicated true means recall is undefined.
        recall=(hits / real) if real else None,
        worst_mr=worst[0] if worst[1] and worst[1].regressions else None,
    )

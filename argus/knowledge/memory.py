"""Renders indexed/full-text learning blocks for injection into review prompts."""
from argus.domain.models import Learning


def learnings_index_block(learnings: list[Learning]) -> str:
    """Renders each learning in full.

    The hint used to be cut to 100 chars, which is shorter than 334 of the 335
    active learnings (median 409) — so the rule arrived mid-word and the agent
    had to spend a get_learning() call to find out what it actually said.
    Traces showed it making that call 11 times in 1,554 tool calls, i.e. it was
    reasoning from a mangled fragment. Showing all eight in full measures at
    ~1.75k tokens against a median review of 594k, so the cap was protecting
    0.3% of the prompt at the cost of the rule being legible at all.
    """
    if not learnings:
        return ""
    lines = ["## Team learnings",
             "If a learning below leads you to a finding, put its [id] in that "
             "finding's `contributing_learning_ids`. Cite only genuine "
             "influence — do not cite a learning you did not actually use."]
    for l in learnings:
        lines.append(f"- [{str(l.id)[:8]}] {l.topic}: {l.hint_text or ''}")
    return "\n".join(lines)


def do_not_suggest_block(learnings: list[Learning]) -> str:
    """Rendered with short ids, like learnings_index_block. Without an id a
    suppression can never be attributed: a do_not_suggest learning is only
    ever 'used' by causing a finding to be DROPPED, and if the verifier cannot
    name which one it applied, the learning can never be credited a hit and is
    instead charged 'ignored' on every review forever."""
    if not learnings:
        return ""
    lines = ["## Previously rejected suggestions — do NOT re-suggest these",
             "If one of these is why you reject a finding, put its [id] in "
             "that verdict's `applied_learning_ids`. Cite only genuine "
             "influence — do not cite one you did not actually apply."]
    for l in learnings:
        lines.append(f"- [{str(l.id)[:8]}] {l.topic}: {l.hint_text}")
    return "\n".join(lines)

import uuid

from argus.domain.models import Learning
from argus.knowledge.memory import do_not_suggest_block, learnings_index_block


def _l(topic, hint, kind="guidance"):
    l = Learning(topic=topic, hint_text=hint, kind=kind)
    l.id = uuid.uuid4()
    return l


def test_index_block_shows_the_whole_rule():
    """This deliberately reverses the old 100-char-per-learning budget.

    That budget was set to protect the prompt, but the protection was never
    measured and cost the feature its usefulness: 334 of 335 active learnings
    are longer than 100 chars (median 409), so the cut landed mid-word and the
    agent was reasoning from half a sentence. Recovering the rest needed a
    get_learning() call, which traces show happening 11 times in 1,554 tool
    calls. Measured cost of showing all 8 in full is ~1.75k tokens against a
    median review of 594k -- 0.3%, which does not buy a mangled rule.
    """
    hint = ("When a feature depends on another module being enabled, check "
            "that dependency first and return None if it is disabled.")
    l = _l("dependency guards", hint)
    block = learnings_index_block([l])

    assert str(l.id)[:8] in block
    assert "dependency guards" in block
    assert hint in block, "the rule must arrive whole, not as a teaser"


def test_index_block_does_not_truncate_mid_word():
    long_hint = "alpha bravo charlie delta echo foxtrot " * 20
    block = learnings_index_block([_l("t", long_hint)])
    assert long_hint.strip() in block


def test_do_not_suggest_full_text():
    l = _l("log wrapping", "get_logs_in_window does not need <log_data> tags",
           kind="do_not_suggest")
    block = do_not_suggest_block([l])
    assert "do NOT re-suggest" in block
    assert "get_logs_in_window" in block


def test_empty_blocks():
    assert learnings_index_block([]) == ""
    assert do_not_suggest_block([]) == ""

"""Every reply string here is real, taken from argus.notes on the dev DB.

The old classifier keyword-matched ~14 acceptance phrases and stamped
everything else `rejected_with_rationale`, so "rejected" was the bucket for
"we did not recognise this reply" rather than a verdict. That made the
acceptance rate meaningless: on MR 12, 9 of 10 "rejections" were the bot being
right.
"""
from argus.ingest.reconciler import classify_suggestion_disposition as classify


def _c(replies, resolved=True, suggestions=None, diff=""):
    return classify(suggestions or [], diff, resolved, replies)


# --- replies that were stamped "rejected" but are plainly acceptances -------

def test_doing_verbs_are_acceptances():
    assert _c(["Added the .warn log"]) == "accepted_manually"
    assert _c(["Added toast message for better UX"]) == "accepted_manually"
    assert _c(["added shild for task, however still the task cancellation "
               "is not guarented."]) == "accepted_manually"


def test_confirmed_and_fixed_is_an_acceptance():
    assert _c(["Confirmed and fixed, using the suggested shape — "
               "`_read_json_body` now coerces a non-object body to `{}`"]
              ) == "accepted_manually"
    assert _c(["Confirmed and fixed, and this comment's enumeration is the "
               "most accurate of the set"]) == "accepted_manually"


# --- a human saying they fixed it counts even if they never clicked resolve --

def test_fixed_reply_on_an_unresolved_thread_is_still_an_acceptance():
    """These sat in `open` forever, so genuine wins were never counted."""
    assert _c(["Fixed in 2f6b3be. Added `_cleanup_all_healthcheck_files()`"],
              resolved=False) == "accepted_manually"
    assert _c(["Fixed in 84563f8. Confirmed the path was exactly as described"],
              resolved=False) == "accepted_manually"


def test_silence_on_an_unresolved_thread_is_still_open():
    assert _c([], resolved=False) == "open"
    assert _c(["   "], resolved=False) == "open"


# --- genuine disagreement stays a rejection --------------------------------

def test_explicit_declines_are_rejections():
    assert _c(["Declined — no observable effect"]) == "rejected_with_rationale"
    assert _c(["Action is a required field. We do not need to do \".?\" for it"]
              ) == "rejected_with_rationale"
    assert _c(["Unrelated"]) == "rejected_with_rationale"
    assert _c(["intentional"]) == "rejected_with_rationale"
    assert _c(["Not reachable."]) == "rejected_with_rationale"
    assert _c(["This feature flag is itself added in this PR, so not possible"]
              ) == "rejected_with_rationale"
    assert _c(["No fix required — it's the codebase's established, "
               "intentional pattern."]) == "rejected_with_rationale"


def test_the_humans_words_beat_a_coincidental_diff_match():
    """A real row was stamped accepted_manually while the human said
    "Unrelated": the suggested lines happened to appear in the final diff, and
    that outranked what the reviewer actually wrote."""
    got = _c(["Unrelated"], suggestions=[{"to_content": "x = 1"}], diff="x = 1")
    assert got == "rejected_with_rationale"


# --- the fallback no longer masquerades as a verdict -----------------------

def test_unrecognised_replies_are_not_counted_as_rejections():
    got = _c(["fast api handles the def <name> methods to execute them in its "
              "own threadpool executor."])
    assert got == "replied_unclassified"


def test_unclassified_is_excluded_from_the_acceptance_rate():
    from argus.knowledge.acceptance import ACCEPTED, REJECTED
    assert "replied_unclassified" not in ACCEPTED
    assert "replied_unclassified" not in REJECTED


def test_unclassified_is_terminal_for_learning_outcomes():
    """It must not leave an injection pending forever: the human has spoken,
    we just cannot tell what they meant, which is 'inconclusive'."""
    from argus.knowledge.outcomes import _TERMINAL
    assert "replied_unclassified" in _TERMINAL


# --- unchanged behaviour ---------------------------------------------------

def test_applied_suggestion_still_wins_outright():
    assert _c([], suggestions=[{"applied": True}]) == "accepted"


def test_resolved_with_no_reply_is_still_dismissed():
    assert _c([], resolved=True) == "dismissed_ambiguous"


def test_suggestion_in_final_diff_is_acceptance_when_nobody_replied():
    got = _c([], suggestions=[{"to_content": "y = 2"}], diff="y = 2")
    assert got == "accepted_manually"

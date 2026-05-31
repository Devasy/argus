import pytest

from argus.benchmark import scoring, spec


def _finding(file_path, title, line=10, valid=True):
    return {"file_path": file_path, "title": title, "line": line,
            "valid": valid}


# --- the set definition ---------------------------------------------------

def test_golden_set_loads_and_is_keyed_by_repo_and_iid():
    """mr_iid alone is ambiguous: MR 12 exists in six repositories with
    unrelated titles, so a bare iid would benchmark the wrong MR."""
    gs = spec.load()
    assert gs.version == 1
    assert len(gs.merge_requests) >= 16
    keys = [(m.project_path, m.mr_iid) for m in gs.merge_requests]
    assert len(keys) == len(set(keys)), "duplicate (repo, iid) in the set"
    assert all(m.project_path and m.mr_iid for m in gs.merge_requests)


def test_every_item_points_at_an_mr_that_is_in_the_set():
    gs = spec.load()
    mr_keys = {(m.project_path, m.mr_iid) for m in gs.merge_requests}
    for item in gs.items:
        assert (item.project_path, item.mr_iid) in mr_keys, item.id


def test_items_only_hang_off_mrs_marked_scored():
    """A scored MR is one we can compute precision/recall for. An item on an
    unscored MR would be silently ignored."""
    gs = spec.load()
    scored = {(m.project_path, m.mr_iid) for m in gs.merge_requests if m.scored}
    for item in gs.items:
        assert (item.project_path, item.mr_iid) in scored, item.id


def test_every_scored_mr_actually_has_at_least_one_item():
    gs = spec.load()
    with_items = {(i.project_path, i.mr_iid) for i in gs.items}
    for m in gs.merge_requests:
        if m.scored:
            assert (m.project_path, m.mr_iid) in with_items, \
                f"{m.project_path}!{m.mr_iid} is marked scored but has no items"


def test_verdicts_are_from_the_known_vocabulary():
    gs = spec.load()
    assert {i.verdict for i in gs.items} <= set(spec.VERDICTS)


def test_item_ids_are_unique():
    gs = spec.load()
    ids = [i.id for i in gs.items]
    assert len(ids) == len(set(ids))


# --- matching a run's finding to a ground-truth item ----------------------

def test_matches_a_reworded_finding_in_the_same_file():
    gs = spec.load()
    item = next(i for i in gs.items if i.id == "mr49-enforcefocus")
    got = _finding(item.file, "Modal focus trap is disabled which is an accessibility regression")
    assert scoring.matches(item, got)


def test_line_drift_does_not_prevent_a_match():
    """Lines move between the reviewed revision and any later one, so the
    match deliberately ignores them."""
    gs = spec.load()
    item = next(i for i in gs.items if i.id == "mr49-enforcefocus")
    got = _finding(item.file, item.claim, line=item.line + 400)
    assert scoring.matches(item, got)


def test_a_different_concern_in_the_same_file_does_not_match():
    gs = spec.load()
    item = next(i for i in gs.items if i.id == "mr49-enforcefocus")
    assert not scoring.matches(item, _finding(item.file, "Missing key prop in list render"))


def test_the_same_concern_in_another_file_does_not_match():
    gs = spec.load()
    item = next(i for i in gs.items if i.id == "mr49-enforcefocus")
    assert not scoring.matches(item, _finding("src/other.jsx", item.claim))


# --- scoring one MR -------------------------------------------------------

def test_reraising_an_adjudicated_false_finding_is_a_regression():
    items = [spec.Item(id="x", project_path="p", mr_iid=1, file="a.py", line=1,
                       verdict="human_right", claim="Missing null guard on config",
                       why="")]
    r = scoring.score_mr(items, [_finding("a.py", "Missing null guard on config")])
    assert r.false_positives == ["x"]
    assert r.regressions == 1 and r.hits == 0


def test_finding_a_real_issue_counts_as_a_hit():
    items = [spec.Item(id="y", project_path="p", mr_iid=1, file="a.py", line=1,
                       verdict="bot_right", claim="Silent PASS when pattern absent",
                       why="")]
    r = scoring.score_mr(items, [_finding("a.py", "Silent PASS when pattern absent")])
    assert r.hits == 1 and r.misses == []


def test_missing_a_real_issue_is_recorded():
    items = [spec.Item(id="y", project_path="p", mr_iid=1, file="a.py", line=1,
                       verdict="bot_right", claim="Silent PASS", why="")]
    r = scoring.score_mr(items, [])
    assert r.misses == ["y"] and r.hits == 0


def test_trivia_is_neither_a_hit_nor_a_regression():
    items = [spec.Item(id="t", project_path="p", mr_iid=1, file="a.py", line=1,
                       verdict="bot_right_trivial", claim="Cross-module import added",
                       why="")]
    r = scoring.score_mr(items, [_finding("a.py", "Cross-module import added")])
    assert r.hits == 0 and r.regressions == 0
    assert r.trivia == ["t"]


def test_only_findings_the_pipeline_kept_are_scored():
    """A candidate verify dropped was never shown to a human, so re-raising an
    adjudicated-false concern internally is not a regression."""
    items = [spec.Item(id="x", project_path="p", mr_iid=1, file="a.py", line=1,
                       verdict="human_right", claim="Missing null guard", why="")]
    dropped = _finding("a.py", "Missing null guard", valid=False)
    assert scoring.score_mr(items, [dropped]).regressions == 0


def test_unmatched_kept_findings_are_counted_but_not_judged():
    """Findings outside the adjudicated set are not evidence either way -- the
    set covers 10 items, not everything these MRs contain."""
    r = scoring.score_mr([], [_finding("a.py", "Something we never adjudicated")])
    assert r.unadjudicated == 1


# --- aggregating a run ----------------------------------------------------

def test_run_report_aggregates_across_mrs():
    per_mr = {
        ("p", 1): scoring.MRScore(hits=1, misses=[], false_positives=[],
                                  regressions=0, trivia=[], unadjudicated=2),
        ("p", 2): scoring.MRScore(hits=0, misses=["m"], false_positives=["f"],
                                  regressions=1, trivia=[], unadjudicated=0),
    }
    rep = scoring.aggregate(per_mr)
    assert rep.hits == 1 and rep.regressions == 1 and rep.misses == 1
    assert rep.recall == pytest.approx(0.5)
    assert rep.mrs_scored == 2


def test_recall_is_none_when_nothing_was_adjudicated_as_real():
    rep = scoring.aggregate({("p", 1): scoring.MRScore(
        hits=0, misses=[], false_positives=[], regressions=0, trivia=[],
        unadjudicated=3)})
    assert rep.recall is None, "0/0 is not 0%"


def test_selection_can_be_narrowed_to_the_scored_mrs():
    """The 8 behavioural-only MRs cost ~26h40m of serial worker time against
    ~3h08m for the 7 scored ones, and only the scored ones can be judged for
    correctness. Running everything blocks real reviews for over a day."""
    gs = spec.load()
    everything = spec.select(gs, scored_only=False)
    scored = spec.select(gs, scored_only=True)

    assert len(everything) == len(gs.merge_requests)
    assert len(scored) == 7
    assert all(m.scored for m in scored)

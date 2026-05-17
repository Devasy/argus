import pytest
from sqlalchemy import func, select

import argus.knowledge.learnings as L
from argus.domain.models import InjectionEvent, Learning, Repository, Review


def _vec(i: int) -> list[float]:
    v = [0.0] * 768
    v[i % 768] = 1.0
    return v


@pytest.fixture
def fake_embed(monkeypatch):
    state = {"i": 0}

    async def fake(text, settings, is_query=False):
        state["i"] += 1
        return _vec(state["i"])

    monkeypatch.setattr(L, "embed_text", fake)
    return state


@pytest.fixture
def same_embed(monkeypatch):
    async def fake(text, settings, is_query=False):
        return _vec(1)
    monkeypatch.setattr(L, "embed_text", fake)


async def _repo(db):
    r = Repository(provider="gitlab", project_path="g/p", gitlab_project_id=1)
    db.add(r)
    await db.flush()
    return r


async def test_upsert_and_dedup(db, settings, same_embed):
    repo = await _repo(db)
    l1 = await L.upsert_learning(db, settings, repo_id=repo.id,
                                 topic="logging", hint_text="use lazy formatting")
    l2 = await L.upsert_learning(db, settings, repo_id=repo.id,
                                 topic="logging", hint_text="use lazy fmt")
    assert l1.id == l2.id  # deduped by cosine distance


async def test_relevant_reranks_by_file_pattern(db, settings, fake_embed, monkeypatch):
    repo = await _repo(db)
    a = await L.upsert_learning(db, settings, repo_id=repo.id, topic="a",
                                hint_text="x", file_pattern="*.py")
    b = await L.upsert_learning(db, settings, repo_id=repo.id, topic="b",
                                hint_text="y", file_pattern="*.tsx")

    async def query_embed(text, settings, is_query=False):
        return _vec(999)  # equidistant from both
    monkeypatch.setattr(L, "embed_text", query_embed)
    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything",
                                     file_paths=["src/main.py"])
    assert got and got[0].id == a.id  # *.py pattern bonus wins


async def test_injection_lifecycle(db, settings, same_embed):
    repo = await _repo(db)
    lrn = await L.upsert_learning(db, settings, repo_id=repo.id,
                                  topic="t", hint_text="h")
    from argus.domain.models import MergeRequest
    mr = MergeRequest(repo_id=repo.id, mr_iid=1, title="t", state="opened",
                      source_branch="a", target_branch="b", head_sha="s", web_url="u")
    db.add(mr); await db.flush()
    review = Review(mr_id=mr.id, status="running")
    db.add(review); await db.flush()
    await L.record_injections(db, review.id, [lrn])
    await L.record_hit(db, lrn.id)
    await db.refresh(lrn)
    assert lrn.hit_count == 1


async def test_list_learnings_filters_by_kind_and_status(db, settings):
    from argus.domain.models import Learning
    from argus.knowledge.learnings import list_learnings

    db.add_all([
        Learning(repo_id=None, topic="a", hint_text="h", kind="guidance", status="active"),
        Learning(repo_id=None, topic="b", hint_text="h", kind="do_not_suggest", status="active"),
        Learning(repo_id=None, topic="c", hint_text="h", kind="guidance", status="archived"),
    ])
    await db.flush()

    rows, total = await list_learnings(db, kind="guidance")
    assert total == 2 and {l.topic for l, mr, actor in rows} == {"a", "c"}

    rows, total = await list_learnings(db, kind="guidance", status="active")
    assert total == 1 and rows[0][0].topic == "a"


async def test_list_learnings_repo_scope_includes_global(db, settings):
    from argus.domain.models import Learning, Repository
    from argus.knowledge.learnings import list_learnings

    repo = Repository(provider="gitlab", project_path="grp/list-learnings-test",
                      gitlab_project_id=90000300)
    db.add(repo)
    await db.flush()
    db.add_all([
        Learning(repo_id=repo.id, topic="repo-specific", hint_text="h"),
        Learning(repo_id=None, topic="global", hint_text="h"),
        Learning(repo_id=None, topic="other-repo-global-noise", hint_text="h"),
    ])
    await db.flush()

    rows, total = await list_learnings(db, repo_id=repo.id)
    assert total == 3  # repo-specific + both globals
    assert {l.topic for l, mr, actor in rows} == \
        {"repo-specific", "global", "other-repo-global-noise"}


async def test_list_learnings_resolves_source_mr_and_author(db, settings):
    from argus.domain.models import Actor, MergeRequest, Repository
    from argus.knowledge.learnings import list_learnings

    repo = Repository(provider="gitlab", project_path="grp/source-trace-test",
                      gitlab_project_id=90000310)
    db.add(repo)
    await db.flush()
    mr = MergeRequest(repo_id=repo.id, mr_iid=41, title="Adds threat_intel notifiers tasks flow",
                      state="opened", source_branch="a", target_branch="b",
                      head_sha="s", web_url="https://gitlab.example/repo/-/merge_requests/41")
    db.add(mr)
    await db.flush()
    actor = Actor(username="lead.dev", provider_user_id=99)
    db.add(actor)
    await db.flush()
    traced = Learning(repo_id=None, topic="traced", hint_text="h",
                      mr_id=mr.id, learned_from_actor_id=actor.id)
    untraced = Learning(repo_id=None, topic="untraced", hint_text="h")
    db.add_all([traced, untraced])
    await db.flush()

    rows, total = await list_learnings(db, kind=None)
    by_topic = {l.topic: (l, m, a) for l, m, a in rows}
    traced_l, traced_mr, traced_actor = by_topic["traced"]
    assert traced_mr is not None and traced_mr.mr_iid == 41
    assert traced_mr.title == "Adds threat_intel notifiers tasks flow"
    assert traced_actor is not None and traced_actor.username == "lead.dev"
    untraced_l, untraced_mr, untraced_actor = by_topic["untraced"]
    assert untraced_mr is None and untraced_actor is None


async def test_list_learnings_pagination(db, settings):
    from argus.domain.models import Learning
    from argus.knowledge.learnings import list_learnings

    db.add_all([Learning(repo_id=None, topic=f"t{i}", hint_text="h") for i in range(5)])
    await db.flush()

    rows, total = await list_learnings(db, page=1, per_page=2)
    assert total == 5 and len(rows) == 2
    rows2, total2 = await list_learnings(db, page=2, per_page=2)
    assert total2 == 5 and len(rows2) == 2
    assert {l.id for l, mr, actor in rows} & {l.id for l, mr, actor in rows2} == set()  # no overlap


async def test_search_learnings_returns_ranked_by_similarity(db, settings, monkeypatch):
    from argus.domain.models import Learning
    from argus.knowledge import learnings as learnings_mod

    async def fake_embed(text, settings_, is_query=False):
        # deterministic fake: embedding = [similarity-to-"auth" signal, 0, 0, ...]
        base = 1.0 if "auth" in text.lower() else 0.0
        return [base] + [0.0] * 767

    monkeypatch.setattr(learnings_mod, "embed_text", fake_embed)

    close = Learning(repo_id=None, topic="auth token refresh", hint_text="h",
                     embedding=[1.0] + [0.0] * 767)
    far = Learning(repo_id=None, topic="unrelated billing logic", hint_text="h",
                   embedding=[0.0, 1.0] + [0.0] * 766)
    db.add_all([close, far])
    await db.flush()

    rows, total = await learnings_mod.search_learnings(db, settings, query_text="auth")
    assert total >= 1
    assert rows[0][0].topic == "auth token refresh"


async def test_search_learnings_empty_query_returns_empty(db, settings, monkeypatch):
    from argus.knowledge import learnings as learnings_mod

    async def fake_embed(text, settings_, is_query=False):
        return None

    monkeypatch.setattr(learnings_mod, "embed_text", fake_embed)
    rows, total = await learnings_mod.search_learnings(db, settings, query_text="   ")
    assert rows == [] and total == 0


async def test_upsert_learning_with_learning_id_updates_in_place(db, settings, monkeypatch):
    from argus.domain.models import Learning
    from argus.knowledge.learnings import upsert_learning
    import argus.knowledge.learnings as L

    async def fake_embed(text, settings_, is_query=False):
        return [0.5] * 768
    monkeypatch.setattr(L, "embed_text", fake_embed)

    original = Learning(repo_id=None, topic="old topic", hint_text="old hint",
                       kind="guidance", embedding=[0.1] * 768)
    db.add(original)
    await db.flush()
    original_id = original.id

    updated = await upsert_learning(
        db, settings, repo_id=None, topic="new topic", hint_text="new hint",
        kind="do_not_suggest", file_paths=["a.py", "b.py"],
        metadata={"source_mr_id": "abc"}, learning_id=original_id)

    assert updated.id == original_id  # same row, not a new insert
    assert updated.topic == "new topic" and updated.hint_text == "new hint"
    assert updated.kind == "do_not_suggest"
    assert updated.file_paths == ["a.py", "b.py"]
    assert updated.metadata_ == {"source_mr_id": "abc"}

    total = (await db.execute(
        select(func.count(Learning.id)).where(Learning.topic.in_(["old topic", "new topic"]))
    )).scalar_one()
    assert total == 1  # no duplicate row was created


async def test_upsert_learning_without_learning_id_unchanged_behavior(db, settings, monkeypatch):
    """Regression guard: existing callers (e.g. distill_rejection) that never
    pass learning_id must see identical create/dedup behavior as before."""
    import argus.knowledge.learnings as L
    from argus.knowledge.learnings import upsert_learning

    async def fake_embed(text, settings_, is_query=False):
        return [0.9] * 768
    monkeypatch.setattr(L, "embed_text", fake_embed)

    first = await upsert_learning(db, settings, repo_id=None,
                                  topic="fresh topic", hint_text="fresh hint")
    second = await upsert_learning(db, settings, repo_id=None,
                                   topic="fresh topic again", hint_text="fresh hint again")
    assert second.id == first.id  # deduped onto the same row (distance 0 < 0.15 threshold)


async def test_harmful_learning_ranks_below_untried_learning(db, settings,
                                                             fake_embed, monkeypatch):
    """A learning that produced rejected comments must sort BELOW a fresh one.

    Under the old `_hit_rate` both scored 0.0 and tied — a bad learning was
    never actually penalised, only un-rewarded.
    """
    repo = await _repo(db)
    bad = await L.upsert_learning(db, settings, repo_id=repo.id, topic="bad",
                                  hint_text="x")
    fresh = await L.upsert_learning(db, settings, repo_id=repo.id, topic="fresh",
                                    hint_text="y")
    bad.harmful_count = 9
    bad.hit_count = 0
    await db.flush()

    async def query_embed(text, settings, is_query=False):
        return _vec(999)  # equidistant from both
    monkeypatch.setattr(L, "embed_text", query_embed)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[])
    ids = [l.id for l in got]
    assert ids.index(fresh.id) < ids.index(bad.id)


async def test_proven_learning_ranks_above_untried(db, settings, fake_embed,
                                                   monkeypatch):
    repo = await _repo(db)
    proven = await L.upsert_learning(db, settings, repo_id=repo.id, topic="proven",
                                     hint_text="x")
    fresh = await L.upsert_learning(db, settings, repo_id=repo.id, topic="fresh",
                                    hint_text="y")
    proven.hit_count = 25
    await db.flush()

    async def query_embed(text, settings, is_query=False):
        return _vec(999)
    monkeypatch.setattr(L, "embed_text", query_embed)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[])
    ids = [l.id for l in got]
    assert ids.index(proven.id) < ids.index(fresh.id)


async def test_learning_reputation_neutral_without_evidence(db, settings, same_embed):
    repo = await _repo(db)
    l = await L.upsert_learning(db, settings, repo_id=repo.id, topic="t",
                                hint_text="h")
    assert L.learning_reputation(l) == 0.5


async def test_exploration_slot_surfaces_an_unproven_learning(db, settings,
                                                              fake_embed, monkeypatch):
    """Without this, a learning that never cracks the top-N can never earn a
    verdict, so its reputation can never move — a rich-get-richer trap."""
    repo = await _repo(db)
    proven = []
    for i in range(8):
        l = await L.upsert_learning(db, settings, repo_id=repo.id,
                                    topic=f"proven{i}", hint_text=f"h{i}")
        l.hit_count = 50
        proven.append(l)
    newbie = await L.upsert_learning(db, settings, repo_id=repo.id,
                                     topic="newbie", hint_text="never tried")
    await db.flush()

    async def query_embed(text, settings, is_query=False):
        return _vec(999)
    monkeypatch.setattr(L, "embed_text", query_embed)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[],
                                     top=8, explore=True)
    assert newbie.id in {l.id for l in got}
    assert len(got) <= 8


async def test_inconclusive_outcomes_do_not_count_as_evidence(db, settings,
                                                              fake_embed, monkeypatch):
    """'inconclusive' means the review published nothing, so no judgement about
    the learning was possible -- outcomes.py's own comment says it is "not this
    learning's fault". Counting it as evidence therefore locked a learning out
    of the exploration slot on the strength of reviews that never judged it:
    7,132 of 16,279 injections came from reviews that failed outright, and
    every one was charged inconclusive."""
    repo = await _repo(db)
    for i in range(8):
        l = await L.upsert_learning(db, settings, repo_id=repo.id,
                                    topic=f"proven{i}", hint_text=f"h{i}")
        l.hit_count = 50
    starved = await L.upsert_learning(db, settings, repo_id=repo.id,
                                      topic="starved", hint_text="never judged")
    # Injected into many barren/failed reviews, never actually judged once.
    starved.inconclusive_count = 40
    await db.flush()

    async def query_embed(text, settings, is_query=False):
        return _vec(999)
    monkeypatch.setattr(L, "embed_text", query_embed)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[],
                                     top=8, explore=True)
    assert starved.id in {l.id for l in got}, (
        "a learning with only inconclusive outcomes has no evidence and must "
        "still be explorable")


async def test_real_verdicts_still_disqualify_the_explore_slot(db, settings,
                                                               fake_embed, monkeypatch):
    """The counterpart: a learning that HAS been judged must not keep taking
    the explore slot, or the slot stops rotating."""
    repo = await _repo(db)
    for i in range(8):
        l = await L.upsert_learning(db, settings, repo_id=repo.id,
                                    topic=f"proven{i}", hint_text=f"h{i}")
        l.hit_count = 50
    judged = await L.upsert_learning(db, settings, repo_id=repo.id,
                                     topic="judged", hint_text="was ignored once")
    judged.ignored_count = 1
    await db.flush()

    async def query_embed(text, settings, is_query=False):
        return _vec(999)
    monkeypatch.setattr(L, "embed_text", query_embed)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[],
                                     top=8, explore=True)
    assert judged.id not in {l.id for l in got}


async def test_suppressions_can_be_excluded_from_the_guidance_index(db, settings,
                                                                    fake_embed,
                                                                    monkeypatch):
    """do_not_suggest learnings have their own dedicated path to the verify
    stage. When they also win slots in relevant_learnings they land in
    mr_context, which every stage sees -- so scout and analyze were handed
    "do not flag X" rules at the moment they are supposed to be finding
    problems. Observed live: all 8 slots of a real review's "## Team
    learnings" block were do_not_suggest, and 6,390 of 9,198 injections were
    suppressions against 2,719 guidance."""
    repo = await _repo(db)
    for i in range(6):
        await L.upsert_learning(db, settings, repo_id=repo.id,
                                topic=f"dns{i}", hint_text=f"do not flag {i}",
                                kind="do_not_suggest")
    guide = await L.upsert_learning(db, settings, repo_id=repo.id,
                                    topic="real guidance", hint_text="prefer X",
                                    kind="guidance")
    await db.flush()

    async def query_embed(text, settings, is_query=False):
        return _vec(999)
    monkeypatch.setattr(L, "embed_text", query_embed)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[],
                                     top=4, explore=False,
                                     exclude_kinds=("do_not_suggest",))
    kinds = {l.kind for l in got}
    assert "do_not_suggest" not in kinds
    assert guide.id in {l.id for l in got}


async def test_search_path_still_sees_suppressions_by_default(db, settings,
                                                              fake_embed,
                                                              monkeypatch):
    """The distiller searches learnings before writing a new one; it must still
    see suppressions or it will happily create a duplicate of one."""
    repo = await _repo(db)
    dns = await L.upsert_learning(db, settings, repo_id=repo.id, topic="dns",
                                  hint_text="do not flag this",
                                  kind="do_not_suggest")
    await db.flush()

    async def query_embed(text, settings, is_query=False):
        return _vec(999)
    monkeypatch.setattr(L, "embed_text", query_embed)

    got = await L.relevant_learnings(db, settings, repo_id=repo.id,
                                     query_text="anything", file_paths=[])
    assert dns.id in {l.id for l in got}


async def test_record_injections_is_idempotent_across_a_resume(db, settings, fake_embed):
    """A resumed review re-runs everything before the graph, including this.
    There is no uniqueness constraint on (review_id, learning_id), so a second
    pass silently doubled the injection count and every counter derived from
    it."""
    from argus.domain.models import InjectionEvent, MergeRequest
    from sqlalchemy import func, select as sa_select

    repo = await _repo(db)
    a = await L.upsert_learning(db, settings, repo_id=repo.id, topic="a",
                                hint_text="hint a")
    b = await L.upsert_learning(db, settings, repo_id=repo.id, topic="b",
                                hint_text="hint b", kind="do_not_suggest")
    mr = MergeRequest(repo_id=repo.id, mr_iid=7, title="t", state="opened",
                      source_branch="s", target_branch="t", head_sha="h",
                      web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="running")
    db.add(review)
    await db.flush()

    await L.record_injections(db, review.id, [a, b])
    await L.record_injections(db, review.id, [a, b])

    n = (await db.execute(sa_select(func.count(InjectionEvent.id)).where(
        InjectionEvent.review_id == review.id))).scalar_one()
    assert n == 2, "a resume must not re-record the same injections"


async def test_record_injections_still_records_a_newly_added_learning(db, settings,
                                                                      fake_embed):
    from argus.domain.models import InjectionEvent, MergeRequest
    from sqlalchemy import func, select as sa_select

    repo = await _repo(db)
    a = await L.upsert_learning(db, settings, repo_id=repo.id, topic="a",
                                hint_text="hint a")
    b = await L.upsert_learning(db, settings, repo_id=repo.id, topic="b",
                                hint_text="totally different text here")
    mr = MergeRequest(repo_id=repo.id, mr_iid=8, title="t", state="opened",
                      source_branch="s", target_branch="t", head_sha="h",
                      web_url="u")
    db.add(mr)
    await db.flush()
    review = Review(mr_id=mr.id, status="running")
    db.add(review)
    await db.flush()

    await L.record_injections(db, review.id, [a])
    await L.record_injections(db, review.id, [a, b])

    n = (await db.execute(sa_select(func.count(InjectionEvent.id)).where(
        InjectionEvent.review_id == review.id))).scalar_one()
    assert n == 2

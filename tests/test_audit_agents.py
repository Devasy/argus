import uuid

from argus.knowledge.audit_agents import (AuditScoutPlan, GroundVerdict, Relation,
                                              ScoutGroup, keep_known, plan_groups,
                                              valid_relations)

IDS = [str(uuid.UUID(int=i)) for i in range(1, 8)]


def test_plan_groups_covers_every_due_learning_exactly_once():
    plan = AuditScoutPlan(groups=[
        ScoutGroup(group_id="x", learning_ids=[IDS[0], IDS[1], "bogus"], area="auth/"),
        ScoutGroup(group_id="y", learning_ids=[IDS[1], IDS[2]], area="db/"),
    ])
    groups = plan_groups(plan, IDS, max_group_size=5)
    flat = [i for g in groups for i in g.learning_ids]
    assert sorted(flat) == sorted(IDS)
    assert len(flat) == len(set(flat))
    assert [g.group_id for g in groups] == [f"g{i}" for i in range(1, len(groups) + 1)]


def test_plan_groups_splits_big_groups_and_survives_no_plan():
    big = AuditScoutPlan(groups=[ScoutGroup(group_id="a", learning_ids=IDS, area="all")])
    assert max(len(g.learning_ids) for g in plan_groups(big, IDS, 3)) == 3
    assert sorted(i for g in plan_groups(None, IDS, 3) for i in g.learning_ids) == sorted(IDS)


def test_keep_known_normalises_ids_and_dedups():
    a = IDS[0]
    vs = [GroundVerdict(learning_id=a.upper(), verdict="stale", confidence=0.9, rationale="r"),
          GroundVerdict(learning_id=a, verdict="corroborated", confidence=0.9, rationale="r"),
          GroundVerdict(learning_id=str(uuid.uuid4()), verdict="stale", confidence=0.9, rationale="r")]
    out = keep_known(vs, {a})
    assert [(v.learning_id, v.verdict) for v in out] == [(a, "stale")]


def test_valid_relations_requires_both_ids_in_cluster_and_distinct():
    a, b, c = IDS[:3]
    rels = [Relation(learning_id=a, relation="duplicate_of", related_learning_id=b,
                     confidence=0.9, rationale="r"),
            Relation(learning_id=a, relation="duplicate_of", related_learning_id=a,
                     confidence=0.9, rationale="self"),
            Relation(learning_id=c, relation="conflicts_with", related_learning_id=str(uuid.uuid4()),
                     confidence=0.9, rationale="outside")]
    assert [(r.learning_id, r.related_learning_id) for r in valid_relations(rels, {a, b, c})] == [(a, b)]


async def test_investigate_tool_runs_a_named_sub_agent_and_returns_its_answer():
    from argus.knowledge.audit_agents import InvestigationReport, build_investigate_tool
    calls = []

    async def run_agent(stage_name, system_prompt, user_msg, response_model, max_rounds, tools):
        calls.append((stage_name, user_msg, response_model))
        return InvestigationReport(answer="helper used in 3 files: a.py:4 'helper()'")

    counter = {"n": 0}
    tool = build_investigate_tool(run_agent, [], counter)
    out = await tool.ainvoke({"question": "where is helper used?"})
    out2 = await tool.ainvoke({"question": "does x exist?"})
    assert "3 files" in out
    assert [c[0] for c in calls] == ["investigate:1", "investigate:2"]
    assert calls[0][2] is InvestigationReport

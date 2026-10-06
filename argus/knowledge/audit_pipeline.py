"""The learnings audit as a LangGraph pipeline: pick → scout → ground (fan-out, with
investigate sub-agents) → relate (fan-out) → verify → apply. Checkpointed per run,
so a restarted job resumes; every node writes an AuditStage row for the UI."""
import asyncio
import operator
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from argus.domain.models import AuditRun, AuditStage
from argus.knowledge import audit_agents as aa
from argus.review import stages

MAX_GROUP_SIZE = 5
VERIFY_BATCH = 6
GROUND_MAX_ROUNDS = 14
SCOUT_MAX_ROUNDS = 16
RELATE_MAX_ROUNDS = 8
VERIFY_MAX_ROUNDS = 12
DESTRUCTIVE = ("stale", "contradicted")


@dataclass
class AuditDeps:
    sf: object
    settings: object
    llm_cfg: object
    repo_id: uuid.UUID
    workspace: Path
    commit_sha: str | None
    run_id: uuid.UUID
    graph_path: Path | None
    langfuse_metadata: dict
    gate: asyncio.Semaphore


class AuditState(BaseModel):
    audit_run_id: str
    due: list[dict] = []
    groups: list[aa.GroupSpec] = []
    notes: dict[str, str] = {}
    grounded: Annotated[list[aa.GroundVerdict], operator.add] = []
    relations: Annotated[list[aa.Relation], operator.add] = []
    checks: Annotated[list[aa.ProposalCheck], operator.add] = []
    result: dict | None = None


async def record_stage(sf, run_id, name: str, status: str, artifact=None, error=None) -> None:
    now = datetime.now(timezone.utc)
    async with sf() as s:
        await s.execute(insert(AuditStage).values(
            audit_run_id=run_id, stage_name=name, status=status, artifact=artifact, error=error,
            finished_at=None if status == "running" else now,
        ).on_conflict_do_update(index_elements=["audit_run_id", "stage_name"], set_={
            "status": status, "artifact": artifact, "error": error,
            "finished_at": None if status == "running" else now}))
        await s.commit()


def _cards(state: AuditState) -> dict[str, aa.LearningCard]:
    return {d["id"]: aa.LearningCard(**{**d, "file_paths": tuple(d["file_paths"])}) for d in state.due}


def _read_tools(deps: AuditDeps, stage_name: str) -> list:
    from argus.knowledge.graphify import build_graphify_tool
    from argus.review.tools import (ToolContext, build_file_knowledge_tool,
                                        build_learning_tool, build_read_tools,
                                        build_report_problem_tool)
    tools = build_read_tools(ToolContext(workspace=deps.workspace, files_by_id={}, hunks={}))
    tools += [build_file_knowledge_tool(deps.sf, deps.settings, deps.repo_id, deps.workspace),
              build_learning_tool(deps.sf, deps.repo_id if deps.settings.repository_knowledge_only else None),
              build_report_problem_tool(deps.sf, None, stage_name, audit_run_id=deps.run_id)]
    if deps.graph_path is not None:
        tools += build_graphify_tool(deps.graph_path, set())
    return tools


async def _run_agent(deps: AuditDeps, stage_name: str, system_prompt: str, user_msg: str,
                     response_model, max_rounds: int, tools: list, *, parent_stage_names=None):
    """One agent call with its own trace stage, the shared model-call gate, and a stage row."""
    from argus.llm.factory import build_chat_model
    from argus.llm.trace import DBTraceCallback
    callbacks = [DBTraceCallback(deps.sf, stage_name, audit_run_id=deps.run_id)]
    if deps.llm_cfg.langfuse_handler is not None:
        callbacks.append(deps.llm_cfg.langfuse_handler)
    lineage = ({"_parent_stage_names": parent_stage_names}
               if parent_stage_names is not None else {})
    await record_stage(deps.sf, deps.run_id, stage_name, "running", artifact=lineage or None)
    try:
        out = await stages.run_stage_agent(
            build_chat_model(deps.llm_cfg, callbacks=callbacks), tools, system_prompt, user_msg,
            response_model, max_rounds, callbacks=callbacks,
            metadata={**deps.langfuse_metadata, "stage_name": stage_name},
            context_window=deps.settings.model_context_window,
            output_margin=deps.settings.model_output_margin, model_gate=deps.gate)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        from argus.llm.quota import QuotaDeferred
        if isinstance(e, QuotaDeferred):
            raise
        # Any model-call failure -- not just the unrecoverable-stage kind --
        # must not take the whole run down with it: a transient provider
        # error (timeout, connection drop) on one ground group used to fail
        # the entire graph and lose every other group's finished work. Every
        # caller already treats a None return the same way an unrecoverable
        # error was treated: scout falls back to un-grouped due ids, ground
        # retries once and otherwise leaves the learning due, relate/verify
        # simply produce nothing for that branch.
        await record_stage(deps.sf, deps.run_id, stage_name, "failed",
                           artifact=lineage or None, error=str(e)[:1500])
        return None
    await record_stage(deps.sf, deps.run_id, stage_name, "done", artifact={**out.model_dump(), **lineage})
    return out


async def _apply(deps: AuditDeps, state: AuditState) -> dict:
    from argus.knowledge.audit_apply_run import apply_audit_results
    return await apply_audit_results(deps, state)


def build_audit_graph(deps: AuditDeps, checkpointer):
    async def pick(state: AuditState) -> dict:
        from argus.knowledge.audit_pick import select_due_learnings
        await record_stage(deps.sf, deps.run_id, "pick", "running")
        async with deps.sf() as s:
            due = await select_due_learnings(
                s, deps.repo_id, workspace=deps.workspace, now=datetime.now(timezone.utc),
                reaudit_after_days=deps.settings.audit_interval_days,
                limit=deps.settings.audit_learnings_per_run)
            cards = [aa.card_for(l) for l in due]
            await s.execute(update(AuditRun).where(AuditRun.id == deps.run_id)
                            .values(planned_count=len(cards)))
            await s.commit()
        await record_stage(deps.sf, deps.run_id, "pick", "done", artifact={"due": len(cards)})
        return {"due": [{**c.__dict__, "file_paths": list(c.file_paths)} for c in cards]}

    def after_pick(state: AuditState):
        return "scout" if state.due else "apply"

    async def scout(state: AuditState) -> dict:
        cards = list(_cards(state).values())
        plan = await _run_agent(deps, "scout", aa.SCOUT_PROMPT,
                                "LEARNINGS TO MAP:\n\n" + aa.format_cards(cards),
                                aa.AuditScoutPlan, SCOUT_MAX_ROUNDS, _read_tools(deps, "scout"))
        groups = aa.plan_groups(plan, [c.id for c in cards], MAX_GROUP_SIZE)
        return {"groups": groups, "notes": plan.notes if plan else {}}

    def fan_out_ground(state: AuditState):
        return [Send("ground", {"state": state, "group": g.model_dump()}) for g in state.groups]

    async def ground(payload: dict) -> dict:
        state: AuditState = payload["state"]
        group = aa.GroupSpec(**payload["group"])
        cards = _cards(state)
        name = f"ground:{group.group_id}"
        counter = {"n": 0}
        read_tools = _read_tools(deps, name)

        async def run_sub(stage_name, prompt, msg, model_cls, rounds, tools):
            return await _run_agent(deps, f"{name}:{stage_name}", prompt, msg, model_cls, rounds, tools)
        tools = read_tools + [aa.build_investigate_tool(run_sub, read_tools, counter)]

        async def attempt(ids: list[str], label: str):
            msg = (f"Code area: {group.area}\n\nLEARNINGS TO GROUND:\n\n"
                   + aa.format_cards([cards[i] for i in ids], state.notes))
            out = await _run_agent(deps, label, aa.GROUND_PROMPT, msg, aa.GroundList,
                                   GROUND_MAX_ROUNDS, tools)
            return aa.keep_known(out.verdicts if out else [], set(ids))

        got = await attempt(group.learning_ids, name)
        missing = [i for i in group.learning_ids if i not in {v.learning_id for v in got}]
        if missing:
            # coverage is structural: one focused retry on exactly what was skipped
            got += await attempt(missing, f"{name}:retry")
        async with deps.sf() as s:
            await s.execute(update(AuditRun).where(AuditRun.id == deps.run_id)
                            .values(grounded_count=AuditRun.grounded_count + len(got)))
            await s.commit()
        return {"grounded": got}

    async def gather(state: AuditState) -> dict:
        return {}

    async def _relate_clusters(state: AuditState) -> list[list[str]]:
        from argus.knowledge.clustering import cluster_learnings
        due = {d["id"] for d in state.due}
        async with deps.sf() as s:
            clusters = await cluster_learnings(s, repo_id=deps.repo_id, include_global=False)
        return [[str(l.id) for l in c] for c in clusters
                if len(c) > 1 and any(str(l.id) in due for l in c)]

    async def fan_out_relate(state: AuditState):
        clusters = await _relate_clusters(state)
        sends = [Send("relate", {"state": state, "cluster": c, "n": i})
                 for i, c in enumerate(clusters, start=1)]
        return sends or "verify"

    async def relate(payload: dict) -> dict:
        from argus.domain.models import Learning
        state: AuditState = payload["state"]
        ids = payload["cluster"]
        verdict_by = {v.learning_id: v for v in state.grounded}
        async with deps.sf() as s:
            rows = [await s.get(Learning, uuid.UUID(i)) for i in ids]
        cards = [aa.card_for(l) for l in rows if l is not None]
        notes = {c.id: f"this run's grounding: {verdict_by[c.id].verdict} — {verdict_by[c.id].rationale}"
                 for c in cards if c.id in verdict_by}
        out = await _run_agent(deps, f"relate:k{payload['n']}", aa.RELATE_PROMPT,
                               "SIMILAR LEARNINGS:\n\n" + aa.format_cards(cards, notes),
                               aa.RelationList, RELATE_MAX_ROUNDS,
                               _read_tools(deps, f"relate:k{payload['n']}"),
                               parent_stage_names=[f"ground:{g.group_id}" for g in state.groups
                                                   if set(g.learning_ids).intersection(notes)])
        return {"relations": aa.valid_relations(out.relations if out else [], set(ids))}

    async def verify(state: AuditState) -> dict:
        dup = {r.learning_id for r in state.relations if r.relation == "duplicate_of"}
        targets = [v for v in state.grounded if v.verdict in DESTRUCTIVE or v.learning_id in dup]
        if not targets:
            return {}
        cards = _cards(state)
        rel = {r.learning_id: r for r in state.relations}
        checks: list = []
        for k in range(0, len(targets), VERIFY_BATCH):
            batch = targets[k:k + VERIFY_BATCH]
            listing = "\n\n".join(
                f"learning_id={v.learning_id}\nlearning: {cards[v.learning_id].topic} — "
                f"{cards[v.learning_id].hint_text}\nproposal: "
                # Must mirror apply's own precedence (audit_apply_run.py's
                # _DESTRUCTIVE_GROUND check): a destructive ground verdict wins
                # over a duplicate_of relation there, so verify has to be asked
                # about the SAME claim apply will act on -- otherwise a
                # confirmed merge gets read as confirming an archive verify
                # never actually saw.
                + (f"merge into {rel[v.learning_id].related_learning_id}: {rel[v.learning_id].rationale}"
                   if v.learning_id in dup and v.verdict not in DESTRUCTIVE
                   else f"archive ({v.verdict}): {v.rationale}")
                + f"\ncitations: {[c.model_dump() for c in v.citations]}" for v in batch)
            name = "verify" if k == 0 else f"verify:{k // VERIFY_BATCH + 1}"
            out = await _run_agent(deps, name, aa.VERIFY_PROMPT, "PROPOSALS:\n\n" + listing,
                                   aa.CheckList, VERIFY_MAX_ROUNDS, _read_tools(deps, name))
            checks += aa.keep_known(out.checks if out else [], {v.learning_id for v in batch})
        return {"checks": checks}

    async def apply(state: AuditState) -> dict:
        await record_stage(deps.sf, deps.run_id, "apply", "running")
        result = await _apply(deps, state)
        await record_stage(deps.sf, deps.run_id, "apply", "done", artifact=result)
        return {"result": result}

    g = StateGraph(AuditState)
    for name, fn in (("pick", pick), ("scout", scout), ("ground", ground), ("gather", gather),
                     ("relate", relate), ("verify", verify), ("apply", apply)):
        g.add_node(name, fn)
    g.add_edge(START, "pick")
    g.add_conditional_edges("pick", after_pick, ["scout", "apply"])
    g.add_conditional_edges("scout", fan_out_ground, ["ground"])
    g.add_edge("ground", "gather")
    g.add_conditional_edges("gather", fan_out_relate, ["relate", "verify"])
    g.add_edge("relate", "verify")
    g.add_edge("verify", "apply")
    g.add_edge("apply", END)
    return g.compile(checkpointer=checkpointer)


async def run_audit_pipeline(deps: AuditDeps, checkpointer) -> AuditState:
    graph = build_audit_graph(deps, checkpointer)
    config = {"configurable": {"thread_id": f"audit:{deps.run_id}"}}
    fresh = AuditState(audit_run_id=str(deps.run_id))
    input_state = fresh
    if checkpointer is not None and await checkpointer.aget_tuple(config) is not None:
        input_state = None
    return AuditState.model_validate(await graph.ainvoke(input_state, config=config))

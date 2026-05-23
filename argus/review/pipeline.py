"""LangGraph pipeline: scout -> analyze(fan-out) -> [design] -> verify -> publish."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import async_sessionmaker

from argus.config import Settings
from argus.domain.models import Review, ReviewStage
from argus.llm.config import LLMConfig
from argus.llm.factory import build_chat_model
from argus.llm.prompts import PromptBlocks, assemble_system_prompt
from argus.llm.trace import DBTraceCallback
from argus.review import stages
from argus.review.artifacts import (Chunk, CompiledReview, ReviewPlan,
                                        ReviewState)
from argus.review.diffsvc import full_diff_text
from argus.review.grounding import ground_results
from argus.review.prevalence import prevalence_notes
from argus.review.router import apply_risk_flags, plan_chunks
from argus.knowledge.learnings import record_injections, relevant_learnings
from argus.knowledge.memory import learnings_index_block
from argus.knowledge.queries import scout_query
from argus.review.stages import (ANALYSIS_STATIC, LENS_PROMPTS,
                                     QA_SCENARIOS_STATIC, SCOUT_STATIC,
                                     VERIFY_STATIC, FindingList, ScoutOutput,
                                     TestScenarioList, VerdictList)
from argus.review.tools import (ToolContext, build_read_tools,
                                    build_report_problem_tool, summary_table)

logger = logging.getLogger("argus.pipeline")

# Read-only navigation aids that every agent gets regardless of its profile's
# tool_allowlist. Those allowlists enumerate tool names explicitly, so any
# tool added after a profile was written is invisible to it -- 13 reviewer
# agent versions in production predate these two. Exempting them keeps the
# allowlist meaningful for capability grants (writing knowledge, reading
# learnings) while ensuring a newer, cheaper way to read code is never
# withheld from an agent that would otherwise fall back to paging through
# files with get_file_lines.
# report_problem is exempt for a sharper reason than the navigation tools:
# the agents running custom, allowlist-bearing prompts are exactly the ones
# whose configuration is most likely to be wrong, so denying them the
# complaint channel would silence the reports that matter most.
ALWAYS_AVAILABLE_TOOLS = frozenset({"outline_file", "search_code",
                                    "report_problem"})


class ReviewCanceled(Exception):
    """Raised mid-pipeline when a review's status has been externally set to
    'canceled' (via POST /reviews/{id}/cancel) — checked cooperatively at the
    start of each graph node rather than via task cancellation, since a node
    may be mid-LLM-call when the cancel request arrives."""


async def _raise_if_canceled(sf: async_sessionmaker, review_id: str) -> None:
    async with sf() as s:
        review = await s.get(Review, uuid.UUID(review_id))
        if review is not None and review.status == "canceled":
            raise ReviewCanceled()


class PipelineDeps(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    sf: async_sessionmaker
    settings: Settings
    llm_cfg: LLMConfig
    tool_ctx: ToolContext
    # "full" (default) runs the normal scout/analyze/verify/publish graph.
    # "qa_scenarios" runs a dedicated, per-chunk QA test-scenario checklist
    # graph instead: scout -> qa_chunk (fan-out) -> gather_qa -> publish,
    # skipping analyze_chunk/run_reviewer_agent/verify entirely.
    review_mode: str = "full"
    # False: run and record everything, post nothing. Orthogonal to review_mode.
    publish: bool = True
    profile_static: str
    mr_context: str
    # Suppression context (linter output, do-not-suggest learnings) shown ONLY
    # to verify: it can only ever cause a candidate finding to be dropped, so
    # the earlier stages should not pay for it.
    verify_context: str = ""
    gitlab: object | None = None
    project_id: int = 0
    mr_iid: int = 0
    diff_refs: dict = {}
    mr_db_id: uuid.UUID | None = None
    extra_tools: list = []
    graphify_tools: list = []
    skill_tools: list = []
    tool_allowlist: list[str] | None = None
    # langfuse_session_id/langfuse_user_id/langfuse_tags keys, plus plain
    # review/repo/model context -- see runner.py for how this is built.
    # Merged into every stage's run_stage_agent call so every LLM call AND
    # every tool call in the review lands under one Langfuse session with
    # consistent tags, not just the ones a callback happens to be bound to.
    langfuse_metadata: dict = {}
    reviewer_agent_versions: dict[str, "ResolvedAgent"] = {}
    force_agents: list[str] = []
    # Hybrid learning retrieval runs after scout so the lexical arm can use
    # scout's file summaries as its query. These carry what that call needs;
    # when learnings_repo_id is None, runner already retrieved up front.
    learnings_repo_id: uuid.UUID | None = None
    learnings_query: str = ""
    learnings_paths: list[str] = []


class ResolvedAgent(BaseModel):
    name: str
    guidelines: str
    description: str | None = None
    model: str | None = None
    max_rounds: int = 10
    tool_allowlist: list[str] | None = None
    version_id: uuid.UUID


async def _record_stage(deps: PipelineDeps, review_id: str, name: str,
                        artifact: dict | None, error: str | None = None) -> None:
    from sqlalchemy.dialects.postgresql import insert
    async with deps.sf() as s:
        stmt = insert(ReviewStage).values(
            review_id=uuid.UUID(review_id), stage_name=name,
            status="failed" if error else "done", artifact=artifact, error=error,
            finished_at=datetime.now(timezone.utc),
        ).on_conflict_do_update(
            index_elements=["review_id", "stage_name"],
            set_={"status": "failed" if error else "done", "artifact": artifact,
                  "error": error, "finished_at": datetime.now(timezone.utc)})
        await s.execute(stmt)
        await s.commit()


async def _inject_learnings(deps: PipelineDeps, review_id: str,
                            plan: ReviewPlan) -> None:
    """Retrieve learnings now that scout has summarised each file, and append
    them to the context every later stage reads.

    Scout itself runs without them by design -- it only has to describe the
    diff, and its summaries are what makes the lexical arm work. Mutating
    deps.mr_context here is safe because the analyze fan-out has not started;
    the cost is one prompt-prefix divergence at the scout boundary."""
    if deps.learnings_repo_id is None:
        return
    lexical = scout_query(plan.mr_summary,
                          {f.path: f.summary for f in plan.files},
                          deps.learnings_paths)
    trace: list = []
    async with deps.sf() as s:
        relevant = await relevant_learnings(
            s, deps.settings, repo_id=deps.learnings_repo_id,
            query_text=deps.learnings_query, file_paths=deps.learnings_paths,
            exclude_kinds=("do_not_suggest",),
            lexical_query=lexical or None, trace=trace)
        await record_injections(s, uuid.UUID(review_id), relevant)
        await s.commit()
    if relevant:
        deps.mr_context = (deps.mr_context + "\n\n"
                           + learnings_index_block(relevant))
    # Why each candidate lost, not just which won: without it the next
    # evaluation of this ranking has to be done by hand, as the last one was.
    await _record_stage(deps, review_id, "retrieval", {"selection": trace})


def _model(deps: PipelineDeps, review_id: str, stage_name: str,
          model_override: str | None = None):
    """Returns (model, callbacks) — callbacks must ALSO be passed to the agent's
    own invocation, since a callback bound only to the chat model does not
    propagate to a create_agent graph's tool-execution nodes."""
    cb = DBTraceCallback(deps.sf, stage_name, review_id=uuid.UUID(review_id))
    llm_cfg = deps.llm_cfg
    if model_override:
        llm_cfg = llm_cfg.model_copy(update={"model": model_override})
    # llm_cfg.langfuse_handler is already bound onto the model itself (see
    # build_chat_model), which is enough for LLM generation spans -- but a
    # callback bound only to the chat model does not propagate to a
    # create_agent graph's tool-execution nodes, so it must ALSO ride along
    # in the callbacks list passed to the agent's own invocation for tool
    # calls to show up in Langfuse at all (see the 605c2161 incident: the
    # tool call that crashed that review was invisible in Langfuse because
    # of exactly this gap). Same handler instance both places -- LangChain
    # dedupes callbacks by identity, so this does not double-count events.
    callbacks = [cb]
    if llm_cfg.langfuse_handler is not None:
        callbacks.append(llm_cfg.langfuse_handler)
    return build_chat_model(llm_cfg, callbacks=[cb]), callbacks


def _metadata(deps: PipelineDeps, stage_name: str) -> dict:
    return {**deps.langfuse_metadata, "stage_name": stage_name}


def _prompt(deps: PipelineDeps, static: str, per_agent: str) -> str:
    return assemble_system_prompt(PromptBlocks(
        static=deps.profile_static + "\n\n" + static,
        per_mr=deps.mr_context, per_agent=per_agent))


def build_agent_tool(deps: PipelineDeps, review_id: str, agent: ResolvedAgent,
                     chunk_id: str, agent_tools: list, collected: list):
    """Wraps a custom reviewer agent as a callable tool for analyze_chunk --
    a full nested agent invocation (own model, own tools, own max_rounds
    budget), not a single-shot call. analyze_chunk's model decides whether to
    invoke it based on the chunk's real diff content; scout's risk-flag
    roster is only a hint in analyze_chunk's prompt, not a binding assignment
    (see docs/superpowers/specs/2026-08-02-chunk-scoped-specialist-agents-design.md).

    Stage name is f"{agent.name}:{chunk_id}", NOT just agent.name -- the same
    specialist can be invoked from multiple chunks in one review, and
    ReviewStage has a UNIQUE (review_id, stage_name) constraint, so a shared
    name would let one chunk's outcome silently clobber another's. The
    frontend's pipelineGraph.ts must recognize this "<agent>:<chunk>" shape
    (see discoverAgentNames/CHUNK_STAGE_RE there) to render it as its own node
    rather than a stray literally-named agent node.

    `collected` is appended to as a side effect with every CandidateFinding
    the specialist actually returns -- analyze_chunk's own model only sees
    the JSON text back and may summarize/drop/reword it in its own final
    answer, so `collected` is the source of truth used to tag these findings
    with the specialist's stage/finding_id scheme, independent of whatever
    analyze_chunk's top-level FindingList ends up containing."""
    from langchain_core.tools import tool

    stage_name = f"{agent.name}:{chunk_id}"

    @tool(f"invoke_{agent.name}_agent")
    async def _invoke(chunk_context: str) -> str:
        """Run the {agent.name} specialist reviewer over this chunk. Pass a
        description of the chunk (files, what changed, why it might warrant
        this specialist's lens) as chunk_context. The specialist has its own
        read tools and can look beyond this chunk's files if it needs to;
        returns its findings as JSON."""
        model, cbs = _model(deps, review_id, stage_name, agent.model)
        try:
            out: FindingList = await stages.run_stage_agent(
                model, agent_tools,
                _prompt(deps, agent.guidelines, chunk_context),
                "Investigate the chunk context above.", FindingList,
                agent.max_rounds, callbacks=cbs,
                metadata=_metadata(deps, stage_name),
                context_window=deps.settings.model_context_window,
                output_margin=deps.settings.model_output_margin)
        except stages.unrecoverable_stage_errors() as e:
            logger.error("specialist agent %s could not complete for chunk %s "
                         "(%s); returning no findings", agent.name, chunk_id,
                         type(e).__name__)
            await _record_stage(
                deps, review_id, stage_name, None,
                error=f"agent could not complete: {str(e)[:1500]}")
            return FindingList(findings=[]).model_dump_json()
        collected.extend(out.findings)
        await _record_stage(deps, review_id, stage_name,
                            {"findings": [f.model_dump() for f in out.findings]})
        return out.model_dump_json()

    return _invoke


def build_qa_scenario_tool(deps: PipelineDeps, review_id: str, chunk_id: str,
                           files_summary: str, qa_tools: list):
    """Wraps a scoped QA-scenario sub-agent as a callable tool for
    analyze_chunk (and, via agent_tools, the specialists it invokes) -- the
    agent-initiated counterpart to review_mode="qa_scenarios" above. That mode
    runs qa_chunk over EVERY chunk of a dedicated QA-only review, decided
    before any agent looks at the diff; this tool instead lets the
    finding-writing agent itself decide, chunk by chunk, finding by finding,
    that one specific change is risky enough to need a human to run it before
    merge -- no separate review, no upfront heuristic, and normally never
    called at all (most changes are not this kind of risky).

    Scoped to the SAME chunk's files as the caller, with its own get_hunk/
    get_file_lines/search_code calls -- it re-derives repro steps from the
    real diff rather than trusting the calling agent's own (already
    round-budget-constrained) description of what might break.

    Counted per-call in the stage name (qa_scenario:{chunk_id}:{n}) rather
    than reusing a fixed name: ReviewStage has a UNIQUE (review_id,
    stage_name) constraint, and unlike build_agent_tool's specialist -- one
    call per chunk in practice -- this tool may legitimately be called
    several times in one chunk, once per risky finding."""
    from langchain_core.tools import tool

    counter = {"n": 0}

    @tool
    async def generate_test_scenario(concern: str) -> str:
        """Call this when you find a change that might break EXISTING
        behavior and a human should manually verify it before merge, but you
        are not certain from reading the code alone. Pass `concern`: what you
        think might break and why, specific enough to focus the search (not
        "check this file works"). Returns 0-2 concrete manual test scenarios
        (steps + expected result) for that concern, or an empty list if it
        turns out not to be human-testable (e.g. a pure internal/type-level
        change). Quote the returned steps into your finding's body."""
        counter["n"] += 1
        stage_name = f"qa_scenario:{chunk_id}:{counter['n']}"
        model, cbs = _model(deps, review_id, stage_name)
        try:
            out: TestScenarioList = await stages.run_stage_agent(
                model, qa_tools,
                _prompt(deps, QA_SCENARIOS_STATIC,
                        f"Stage: qa_scenario (on-demand), chunk {chunk_id}.\n"
                        f"A reviewing agent raised this specific concern: "
                        f"{concern}\nFocus your scenario(s) on validating or "
                        "refuting THIS concern -- do not write a general test "
                        "plan for the chunk."),
                "Chunk files:\n" + files_summary,
                TestScenarioList, deps.settings.qa_scenario_max_rounds,
                callbacks=cbs, context_window=deps.settings.model_context_window,
                output_margin=deps.settings.model_output_margin)
        except stages.unrecoverable_stage_errors() as e:
            logger.error("qa_scenario tool could not complete for chunk %s "
                         "(%s); returning no scenarios", chunk_id,
                         type(e).__name__)
            await _record_stage(
                deps, review_id, stage_name, None,
                error=f"agent could not complete: {str(e)[:1500]}")
            return TestScenarioList(scenarios=[]).model_dump_json()
        for i, s in enumerate(out.scenarios, start=1):
            s.scenario_id = f"{stage_name}-s{i}"
            s.chunk_id = chunk_id
        await _record_stage(deps, review_id, stage_name,
                            {"concern": concern,
                             "scenarios": [s.model_dump() for s in out.scenarios]})
        return out.model_dump_json()

    return generate_test_scenario


# publisher hook — real implementation in publisher.py (Task 6); imported late
async def publish_compiled_review(state: ReviewState, deps: PipelineDeps) -> CompiledReview:
    from argus.review.publisher import publish_review
    return await publish_review(state, deps)


def build_graph(deps: PipelineDeps, checkpointer):
    files = list(deps.tool_ctx.files_by_id.values())

    def _filtered(tool_list: list) -> list:
        return [t for t in tool_list
                if deps.tool_allowlist is None
                or t.name in deps.tool_allowlist
                or t.name in ALWAYS_AVAILABLE_TOOLS]

    base_tools = _filtered(build_read_tools(deps.tool_ctx) + list(deps.extra_tools))
    graphify_tools = _filtered(list(deps.graphify_tools))
    scout_tools = graphify_tools + _filtered(list(deps.skill_tools))
    analyze_tools = _filtered(list(deps.skill_tools))

    def _with_complaints(tools: list, review_id: str, stage_name: str) -> list:
        """Append the complaint channel, bound to the stage reporting it.

        Built per stage rather than once into base_tools because a complaint is
        only actionable if we know which stage filed it -- "the prompt does not
        fit the task" means nothing without knowing whose prompt."""
        return tools + [build_report_problem_tool(deps.sf, review_id, stage_name)]

    async def scout(state: ReviewState) -> dict:
        await _raise_if_canceled(deps.sf, state.review_id)
        model, cbs = _model(deps, state.review_id, "scout")
        base_user_msg = "Analyze this merge request.\n\n" + summary_table(files)
        user_msg = base_user_msg
        diff_text = full_diff_text(files, deps.tool_ctx.hunks)
        if diff_text and len(diff_text) // 4 <= deps.settings.review_token_ceiling:
            user_msg += "\n\nFull diff:\n" + diff_text
        agent_roster = "\n".join(
            f"- {name}: {agent.description or agent.guidelines[:120]}"
            for name, agent in deps.reviewer_agent_versions.items())
        scout_instructions = "Stage: scout."
        if agent_roster:
            scout_instructions += (
                "\n\nAvailable specialist reviewers (assign any that apply to "
                "this PR by returning their exact name in assigned_agents; "
                "assigning none is valid if none apply):\n" + agent_roster)

        async def _run_scout(msg: str) -> ScoutOutput:
            return await stages.run_stage_agent(
                model, _with_complaints(base_tools + scout_tools,
                                        state.review_id, "scout"),
                _prompt(deps, SCOUT_STATIC, scout_instructions),
                msg,
                ScoutOutput, deps.settings.scout_max_rounds, callbacks=cbs,
                metadata=_metadata(deps, "scout"),
                context_window=deps.settings.model_context_window,
                output_margin=deps.settings.model_output_margin)

        import litellm

        def _fatal(e: BaseException) -> bool:
            # Only context overflow has a droppable input (the inlined diff)
            # worth retrying without. A recursion-limit error means the agent
            # got stuck in a tool-call loop -- dropping the diff does not fix
            # a stuck loop, so there is nothing to retry.
            return not isinstance(e, litellm.ContextWindowExceededError
                                  ) and not isinstance(e, stages.ContextBudgetExceeded)

        try:
            out: ScoutOutput = await _run_scout(user_msg)
        except stages.unrecoverable_stage_errors() as e:
            if _fatal(e) or user_msg == base_user_msg:
                logger.error("scout could not complete (%s); failing the review",
                            type(e).__name__)
                await _record_stage(
                    deps, state.review_id, "scout", None,
                    error=f"scout could not complete: {str(e)[:1500]}")
                raise
            # The inlined full diff is scout's one droppable input -- the agent
            # can still reach every hunk through get_hunk. Retry without it
            # before giving up on the whole review.
            logger.warning(
                "scout exceeded the model context window with the full diff "
                "inlined (%d chars); retrying without it", len(diff_text))
            try:
                out = await _run_scout(base_user_msg)
            except stages.unrecoverable_stage_errors() as retry_err:
                logger.error("scout still could not complete without the "
                             "inlined diff (%s); failing the review",
                             type(retry_err).__name__)
                await _record_stage(
                    deps, state.review_id, "scout", None,
                    error=f"scout could not complete: {str(retry_err)[:1500]}")
                raise
        flagged = apply_risk_flags(files)
        for f in flagged:
            f.summary = out.file_summaries.get(f.file_id, f.summary)
        chunks = plan_chunks(flagged, deps.settings.max_chunk_files)
        merged_agents = list(dict.fromkeys(out.assigned_agents + deps.force_agents))
        plan = ReviewPlan(intent=out.intent, mr_summary=out.mr_summary,
                          files=flagged, chunks=chunks,
                          assigned_agents=merged_agents,
                          notes_for_agents=out.notes_for_agents)
        await _record_stage(deps, state.review_id, "scout", plan.model_dump())
        await _inject_learnings(deps, state.review_id, plan)
        return {"plan": plan}

    def fan_out(state: ReviewState):
        sends = [Send("analyze_chunk", {"state": state, "chunk": c.model_dump()})
                 for c in state.plan.chunks]
        # DEPRECATED: run_reviewer_agent always runs a custom agent once over
        # the whole MR, which is wasteful on large PRs. Custom agents are now
        # also invocable per-chunk, on demand, as tools inside analyze_chunk
        # (see build_agent_tool / docs/superpowers/specs/
        # 2026-08-02-chunk-scoped-specialist-agents-design.md). Kept working
        # for now in case some agent genuinely wants whole-MR framing; slated
        # for removal once the chunk-scoped path is validated in production.
        sends += [Send("run_reviewer_agent", {"state": state, "agent_name": name})
                  for name in state.plan.assigned_agents
                  if name in deps.reviewer_agent_versions]
        return sends

    def fan_out_qa(state: ReviewState):
        return [Send("qa_chunk", {"state": state, "chunk": c.model_dump()})
               for c in state.plan.chunks]

    def _specialist_hint(chunk: Chunk, plan: ReviewPlan) -> str:
        """Scout's risk-flag roster is a HINT here, not a binding assignment
        (unlike the deprecated run_reviewer_agent path below, which still
        treats plan.assigned_agents as a fixed fan_out list). analyze_chunk's
        model decides for itself, against the chunk's real diff, whether to
        call a specialist tool -- catching cases scout's coarse upfront pass
        missed on a large/noisy diff, without forcing a call scout merely
        flagged as possibly relevant."""
        names = [n for n in plan.assigned_agents if n in deps.reviewer_agent_versions]
        if not names:
            return ""
        roster = "\n".join(f"- invoke_{n}_agent" for n in names)
        return ("\n\nScout flagged this MR as possibly needing these "
               "specialists (only call one for THIS chunk if its files "
               "actually warrant that lens -- calling none is valid). A "
               "specialist tool call returns its OWN findings already -- do "
               "not re-list a specialist's findings in your own final "
               "answer, only report what you found yourself:\n" + roster)

    async def _run_analyze_chunk(state: ReviewState, chunk: Chunk) -> list:
        """Runs the analysis agent for one chunk. Raises litellm.ContextWindowExceededError
        (provider-side) or ContextBudgetExceeded (pre-flight) unchanged if the
        chunk's files don't fit -- callers are responsible for splitting and
        retrying."""
        lens_text = "\n".join(LENS_PROMPTS[l] for l in chunk.lenses
                              if l in LENS_PROMPTS)
        member_files = [f for f in state.plan.files if f.file_id in chunk.file_ids]
        model, cbs = _model(deps, state.review_id, f"analyze:{chunk.chunk_id}")
        agent_findings: dict[str, list] = {}
        qa_tools = _with_complaints(base_tools + analyze_tools,
                                    state.review_id, f"analyze:{chunk.chunk_id}")
        # Available to analyze_chunk's own model AND every specialist it
        # invokes below -- whichever agent spots the risky change is the one
        # that should be able to reach for it.
        qa_scenario_tool = build_qa_scenario_tool(
            deps, state.review_id, chunk.chunk_id,
            summary_table(member_files), qa_tools)
        agent_tool_list = [
            build_agent_tool(deps, state.review_id, agent, chunk.chunk_id,
                            qa_tools + [qa_scenario_tool],
                            agent_findings.setdefault(agent.name, []))
            for agent in deps.reviewer_agent_versions.values()]
        out: FindingList = await stages.run_stage_agent(
            model, _with_complaints(
                base_tools + analyze_tools + agent_tool_list + [qa_scenario_tool],
                state.review_id, f"analyze:{chunk.chunk_id}"),
            _prompt(deps, ANALYSIS_STATIC,
                    f"Stage: analysis, chunk {chunk.chunk_id}.\n{lens_text}\n"
                    f"Scout notes: {state.plan.notes_for_agents}"
                    f"{_specialist_hint(chunk, state.plan)}"),
            "Your assigned files:\n" + summary_table(member_files),
            FindingList, deps.settings.analysis_max_rounds, callbacks=cbs,
            metadata=_metadata(deps, f"analyze:{chunk.chunk_id}"),
            context_window=deps.settings.model_context_window,
            output_margin=deps.settings.model_output_margin)
        found = []
        for i, f in enumerate(out.findings, start=1):
            f.finding_id = f"{chunk.chunk_id}-a{i}"
            f.stage, f.chunk_id = "analysis", chunk.chunk_id
            found.append(f)
        for agent_name, agent_out in agent_findings.items():
            for i, f in enumerate(agent_out, start=1):
                f.finding_id = f"{chunk.chunk_id}-{agent_name}{i}"
                f.stage, f.chunk_id = agent_name, chunk.chunk_id
                found.append(f)
        return found

    async def _analyze_chunk_with_split_retry(state: ReviewState, chunk: Chunk) -> list:
        try:
            return await _run_analyze_chunk(state, chunk)
        except stages.context_overflow_errors():
            if len(chunk.file_ids) <= 1:
                logger.error(
                    "chunk %s (single file %s) exceeds the model context "
                    "window; skipping analysis for this file",
                    chunk.chunk_id, chunk.file_ids)
                await _record_stage(
                    deps, state.review_id, f"analyze:{chunk.chunk_id}", None,
                    error="chunk exceeds model context window (single file, "
                         "cannot split further)")
                return []
            logger.warning(
                "chunk %s exceeded the model context window; splitting into "
                "two halves and retrying", chunk.chunk_id)
            mid = len(chunk.file_ids) // 2
            halves = [
                Chunk(chunk_id=f"{chunk.chunk_id}a", file_ids=chunk.file_ids[:mid],
                     lenses=chunk.lenses),
                Chunk(chunk_id=f"{chunk.chunk_id}b", file_ids=chunk.file_ids[mid:],
                     lenses=chunk.lenses),
            ]
            found = []
            for half in halves:
                found += await _analyze_chunk_with_split_retry(state, half)
            return found

    async def _analyze_chunk_resilient(state: ReviewState, chunk: Chunk) -> list:
        """One chunk failing (for any reason -- timeout, a GraphRecursionError
        from a stuck tool-call loop, provider error, etc, besides
        ReviewCanceled/CancelledError) must not fail the whole review: retry
        once unmodified, then record the chunk as failed and return no
        findings for it so the rest of the review (other chunks, verify,
        publish) still completes. The bare `except Exception` here already
        covers stages.unrecoverable_stage_errors() (context overflow escapes
        via the split-retry above first; a recursion error has no split lever
        and falls straight through to this retry-once path)."""
        for attempt in (1, 2):
            try:
                return await _analyze_chunk_with_split_retry(state, chunk)
            except (ReviewCanceled, asyncio.CancelledError):
                raise
            except Exception as e:
                if attempt == 2:
                    logger.exception("chunk %s failed twice; skipping",
                                     chunk.chunk_id)
                    await _record_stage(
                        deps, state.review_id, f"analyze:{chunk.chunk_id}", None,
                        error=f"chunk failed after retry: {str(e)[:2000]}")
                    return []
                logger.warning("chunk %s failed (%s); retrying once",
                               chunk.chunk_id, e)
        return []

    async def analyze_chunk(payload: dict) -> dict:
        state = payload["state"]
        await _raise_if_canceled(deps.sf, state.review_id)
        chunk = Chunk.model_validate(payload["chunk"])
        found = await _analyze_chunk_resilient(state, chunk)
        return {"findings": found}

    async def run_reviewer_agent(payload: dict) -> dict:
        """DEPRECATED whole-MR custom-agent path -- see the deprecation note
        on fan_out above. Prefer chunk-scoped dispatch via build_agent_tool."""
        state = payload["state"]
        await _raise_if_canceled(deps.sf, state.review_id)
        agent_name = payload["agent_name"]
        agent = deps.reviewer_agent_versions[agent_name]
        model, cbs = _model(deps, state.review_id, agent_name, agent.model)
        agent_tools = _filtered(list(deps.graphify_tools) + list(deps.skill_tools))
        try:
            out: FindingList = await stages.run_stage_agent(
                model, _with_complaints(base_tools + agent_tools,
                                        state.review_id, agent_name),
                _prompt(deps, agent.guidelines, f"Stage: {agent_name} (whole MR)."),
                "Whole-MR view:\n" + summary_table(state.plan.files),
                FindingList, agent.max_rounds, callbacks=cbs,
                metadata=_metadata(deps, agent_name),
                context_window=deps.settings.model_context_window,
                output_margin=deps.settings.model_output_margin)
        except stages.unrecoverable_stage_errors() as e:
            # A specialist agent's whole-MR view is not splittable by design
            # (that is the point of the stage). Losing one agent's findings
            # must not cost the whole review, so record it and move on -- the
            # same trade-off _analyze_chunk_resilient makes for a dead chunk.
            # Covers both context overflow AND a GraphRecursionError (the
            # agent stuck in a tool-call loop for max_rounds*2+4 steps without
            # ever reaching the FindingList tool call -- review b9df9951 died
            # this way in platform-reviewer with no stage recorded at all).
            logger.error("reviewer agent %s could not complete (%s); "
                         "skipping its findings", agent_name, type(e).__name__)
            await _record_stage(
                deps, state.review_id, agent_name, None,
                error=f"agent could not complete: {str(e)[:1500]}")
            return {"findings": []}
        from argus.domain.models import ReviewReviewerAgentVersion
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        async with deps.sf() as s:
            stmt = pg_insert(ReviewReviewerAgentVersion).values(
                review_id=uuid.UUID(state.review_id),
                agent_version_id=agent.version_id)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["review_id", "agent_version_id"])
            await s.execute(stmt)
            await s.commit()
        found = []
        for i, f in enumerate(out.findings, start=1):
            f.finding_id = f"{agent_name}{i}"
            f.stage = agent_name
            found.append(f)
        await _record_stage(deps, state.review_id, agent_name,
                            {"findings": [f.model_dump() for f in found]})
        return {"findings": found}

    async def _run_qa_chunk(state: ReviewState, chunk: Chunk) -> list:
        """Per-chunk QA scenario generation -- same file-scoping as
        _run_analyze_chunk, but the model looks for human-testable behavior
        instead of bugs, and returns TestScenarioList instead of FindingList.
        Only used in review_mode="qa_scenarios" (see build_graph branching)."""
        member_files = [f for f in state.plan.files if f.file_id in chunk.file_ids]
        model, cbs = _model(deps, state.review_id, f"qa_chunk:{chunk.chunk_id}")
        out: TestScenarioList = await stages.run_stage_agent(
            model,
            _with_complaints(base_tools, state.review_id,
                             f"qa_chunk:{chunk.chunk_id}"),
            _prompt(deps, QA_SCENARIOS_STATIC,
                    f"Stage: qa_scenarios, chunk {chunk.chunk_id}."),
            "Your assigned files:\n" + summary_table(member_files),
            TestScenarioList, deps.settings.qa_scenarios_max_rounds,
            callbacks=cbs,
            metadata=_metadata(deps, f"qa_chunk:{chunk.chunk_id}"),
            context_window=deps.settings.model_context_window,
            output_margin=deps.settings.model_output_margin)
        scenarios = []
        for i, s in enumerate(out.scenarios, start=1):
            s.scenario_id = f"{chunk.chunk_id}-qa{i}"
            s.chunk_id = chunk.chunk_id
            scenarios.append(s)
        return scenarios

    async def _qa_chunk_resilient(state: ReviewState, chunk: Chunk) -> list:
        """One chunk failing must not fail the whole QA run -- same
        retry-once-then-skip contract as _analyze_chunk_resilient. Records
        the ReviewStage itself (success or failure) since it is the only
        place that knows which outcome actually happened -- an unconditional
        record afterward in the caller would clobber a failed stage back to
        done with empty scenarios."""
        for attempt in (1, 2):
            try:
                scenarios = await _run_qa_chunk(state, chunk)
                await _record_stage(
                    deps, state.review_id, f"qa_chunk:{chunk.chunk_id}",
                    {"scenarios": [s.model_dump() for s in scenarios]})
                return scenarios
            except (ReviewCanceled, asyncio.CancelledError):
                raise
            except Exception as e:
                if attempt == 2:
                    logger.exception("qa_chunk %s failed twice; skipping",
                                     chunk.chunk_id)
                    await _record_stage(
                        deps, state.review_id, f"qa_chunk:{chunk.chunk_id}", None,
                        error=f"chunk failed after retry: {str(e)[:2000]}")
                    return []
                logger.warning("qa_chunk %s failed (%s); retrying once",
                               chunk.chunk_id, e)
        return []

    async def qa_chunk(payload: dict) -> dict:
        state = payload["state"]
        await _raise_if_canceled(deps.sf, state.review_id)
        chunk = Chunk.model_validate(payload["chunk"])
        scenarios = await _qa_chunk_resilient(state, chunk)
        return {"test_scenarios": scenarios}

    async def gather_qa(state: ReviewState) -> dict:
        return {}

    async def gather(state: ReviewState) -> dict:
        await _record_stage(deps, state.review_id, "analyze",
                            {"findings": [f.model_dump() for f in state.findings]})
        return {}

    async def verify(state: ReviewState) -> dict:
        await _raise_if_canceled(deps.sf, state.review_id)
        # Grounding flags verify instead of gating in front of it -- a mechanical check can't tell a hallucination from a decorated/relocated quote, so verify's own reject/`corrected` call decides.
        results = ground_results(state.findings, deps.tool_ctx.workspace)
        flagged_ids = {r.finding_id for r in results if not r.grounded}
        line_corrections = {r.finding_id: r.corrected_line for r in results
                            if r.grounded and r.corrected_line is not None}
        # build corrected copies for use in this stage's LLM listing only —
        # the graph-level state.findings are never mutated here; the
        # corrections are propagated explicitly via the returned dict.
        findings_for_verify = [
            f.model_copy(update={"line": line_corrections[f.finding_id]})
            if f.finding_id in line_corrections else f
            for f in state.findings]
        if not findings_for_verify:
            await _record_stage(deps, state.review_id, "verify",
                                {"verdicts": [], "ungrounded": list(flagged_ids)})
            return {"verdicts": [], "ungrounded_ids": list(flagged_ids),
                    "line_corrections": line_corrections}
        model, cbs = _model(deps, state.review_id, "verify")
        # Run the "is this already the convention?" search rather than asking
        # the model to remember to (see review/prevalence.py).
        notes = await prevalence_notes(deps.tool_ctx.workspace, findings_for_verify)
        flag_note = ("FLAG: mechanical check did not find this evidence_quote "
                     "verbatim at or near the cited line. Confirm for yourself "
                     "with get_file_lines/search_code before deciding -- reject "
                     "as hallucinated if you cannot find any real occurrence, "
                     "or accept with `corrected` set to the real "
                     "file_path/line/evidence_quote if you locate it.")

        async def _verify_batch(batch: list) -> list:
            """Verify one batch of findings, halving and recursing if the
            listing does not fit. verify is the one stage whose prompt grows
            with the NUMBER of findings, so a big review can overflow here even
            when every individual finding is small."""
            listing = "\n\n".join(
                f"finding_id={f.finding_id} file={f.file_path}:{f.line} "
                f"sev={f.severity}\n{f.title}\n{f.body}\n"
                f"evidence: {f.evidence_quote!r}"
                + (f"\n{notes[f.finding_id]}" if f.finding_id in notes else "")
                + (f"\n{flag_note}" if f.finding_id in flagged_ids else "")
                for f in batch)
            try:
                verify_instructions = "Stage: verify."
                if deps.verify_context:
                    verify_instructions += "\n\n" + deps.verify_context
                out: VerdictList = await stages.run_stage_agent(
                    model,
                    _with_complaints(base_tools + graphify_tools,
                                     state.review_id, "verify"),
                    _prompt(deps, VERIFY_STATIC, verify_instructions),
                    "Candidate findings:\n\n" + listing,
                    VerdictList,
                    deps.settings.verify_max_rounds * max(1, len(batch)),
                    callbacks=cbs,
                    metadata=_metadata(deps, "verify"),
                    context_window=deps.settings.model_context_window,
                    output_margin=deps.settings.model_output_margin)
                return list(out.verdicts)
            except stages.unrecoverable_stage_errors() as e:
                import litellm
                is_overflow = isinstance(
                    e, (litellm.ContextWindowExceededError, stages.ContextBudgetExceeded))
                if is_overflow and len(batch) > 1:
                    logger.warning(
                        "verify listing of %d findings exceeded the model "
                        "context window; splitting into halves", len(batch))
                    mid = len(batch) // 2
                    return (await _verify_batch(batch[:mid])
                            + await _verify_batch(batch[mid:]))
                # A single finding that cannot be verified is dropped rather
                # than silently published unverified: with no verdict it does
                # not reach the published set. A recursion-limit error means
                # the verifier is stuck in a tool-call loop -- a smaller batch
                # would not fix that, so drop straight to no verdicts instead
                # of recursing into halves that would hit the same limit.
                logger.error(
                    "verify could not resolve a batch of %d finding(s) (%s); "
                    "they will have no verdict", len(batch), type(e).__name__)
                return []

        verdicts = await _verify_batch(findings_for_verify)
        await _record_stage(deps, state.review_id, "verify",
                            {"verdicts": [v.model_dump() for v in verdicts],
                             "ungrounded": list(flagged_ids)})
        return {"verdicts": verdicts, "ungrounded_ids": list(flagged_ids),
                "line_corrections": line_corrections}

    async def publish(state: ReviewState) -> dict:
        await _raise_if_canceled(deps.sf, state.review_id)
        compiled = await publish_compiled_review(state, deps)
        await _record_stage(deps, state.review_id, "publish", compiled.model_dump())
        return {"compiled": compiled}

    class GraphState(ReviewState):
        pass

    g = StateGraph(GraphState)
    g.add_node("scout", scout)
    g.add_node("publish", publish)
    g.add_edge(START, "scout")

    if deps.review_mode == "qa_scenarios":
        # Dedicated QA-only run: scout still plans chunks/summarizes files,
        # but analyze_chunk/run_reviewer_agent/verify are skipped entirely --
        # scenarios describe MR-wide human-testable behavior, not a specific
        # file:line, so there is nothing for grounding/verify to check.
        g.add_node("qa_chunk", qa_chunk)
        g.add_node("gather_qa", gather_qa)
        g.add_conditional_edges("scout", fan_out_qa, ["qa_chunk"])
        g.add_edge("qa_chunk", "gather_qa")
        g.add_edge("gather_qa", "publish")
    else:
        g.add_node("analyze_chunk", analyze_chunk)
        g.add_node("run_reviewer_agent", run_reviewer_agent)
        g.add_node("gather", gather)
        g.add_node("verify", verify)
        g.add_conditional_edges(
            "scout", fan_out, ["analyze_chunk", "run_reviewer_agent"])
        g.add_edge("analyze_chunk", "gather")
        g.add_edge("run_reviewer_agent", "gather")
        g.add_edge("gather", "verify")
        g.add_edge("verify", "publish")

    g.add_edge("publish", END)
    return g.compile(checkpointer=checkpointer)


async def run_review_pipeline(review_id, deps: PipelineDeps, checkpointer) -> ReviewState:
    graph = build_graph(deps, checkpointer)
    config = {"configurable": {"thread_id": str(review_id)}}
    fresh = ReviewState(review_id=str(review_id))

    input_state = fresh
    if checkpointer is not None:
        existing = await checkpointer.aget_tuple(config)
        if existing is not None:
            # A checkpoint already exists for this thread (e.g. a retried job
            # for the same review_id) — resume from it instead of restarting.
            input_state = None

    result = await graph.ainvoke(input_state, config=config)
    return ReviewState.model_validate(result)

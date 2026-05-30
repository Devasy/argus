import type { ReviewStage, Trace, TraceLLMRound, TraceToolCall } from "../api/types";

export type NodeStatus = "pending" | "running" | "done" | "failed";
export type NodeKind = "scout" | "chunk" | "agent" | "verify" | "publish";

export interface GraphNodeSpec {
  id: string; // stage label: "scout" | "analyze:c1" | "<agent name>" | "verify" | "publish"
  label: string; // display: chunk id for chunks ("c1"), else the stage name
  kind: NodeKind;
  status: NodeStatus;
  layer: 0 | 1 | 2 | 3; // scout | fan-out | verify | publish
  durationMs: number | null; // from stage started_at/finished_at (null for chunks: no own stage row)
  tokens: number | null; // client-side sum of trace llm_rounds (prompt+completion) for this stage label
  produced: number | null; // candidates emitted by this stage
  allowed: number | null; // verify only: how many survived the gate
}

export const FIXED_STAGES = ["scout", "analyze", "verify", "publish"] as const;

const CHUNK_STAGE_RE = /^analyze:(.+)$/;

// Chunk-scoped specialist agent, e.g. "security:c1" -- the backend records
// ReviewStage.stage_name as f"{agent.name}:{chunk_id}" (not just agent.name)
// because the same specialist can be invoked from multiple chunks in one
// review, and ReviewStage has a UNIQUE (review_id, stage_name) constraint.
// "analyze:c1" itself must NOT match this -- it's excluded explicitly since
// CHUNK_STAGE_RE is checked first everywhere this matters.
const SPECIALIST_STAGE_RE = /^(?!analyze:)([^:]+):(.+)$/;

export function specialistLabel(stageName: string): { agent: string; chunkId: string } | null {
  const m = SPECIALIST_STAGE_RE.exec(stageName);
  return m ? { agent: m[1], chunkId: m[2] } : null;
}

export function stageStatusToNodeStatus(status: string): NodeStatus {
  if (status === "running" || status === "done" || status === "failed") return status;
  return "pending";
}

export function discoverChunkIds(trace: Trace | null): string[] {
  if (trace == null) return [];
  const ids = new Set<string>();
  for (const tc of trace.tool_calls) {
    const m = CHUNK_STAGE_RE.exec(tc.stage);
    if (m) ids.add(m[1]);
  }
  for (const lr of trace.llm_rounds) {
    const m = CHUNK_STAGE_RE.exec(lr.stage);
    if (m) ids.add(m[1]);
  }
  return Array.from(ids).sort();
}

export function discoverAgentNames(trace: Trace | null, stages: ReviewStage[]): string[] {
  // Agent stage labels are whatever is left over once the fixed pipeline
  // stages and chunk labels are excluded -- the backend only ever calls
  // _model(deps, review_id, stage_name, ...) with "scout", "analyze:<chunk>",
  // "verify", or a reviewer agent's own name, so anything else in the trace
  // IS an agent by construction. Included so an agent that produced real
  // llm_rounds/tool_calls activity but never got its own ReviewStage row
  // (e.g. review b9df9951: platform-reviewer hit LangGraph's recursion limit
  // before _record_stage ran) still shows up instead of silently vanishing
  // the way chunks never used to before discoverChunkIds existed.
  const names = new Set<string>();
  for (const s of stages) {
    if (!(FIXED_STAGES as readonly string[]).includes(s.name)) names.add(s.name);
  }
  if (trace != null) {
    for (const row of [...trace.tool_calls, ...trace.llm_rounds]) {
      if ((FIXED_STAGES as readonly string[]).includes(row.stage)) continue;
      if (CHUNK_STAGE_RE.test(row.stage)) continue;
      names.add(row.stage);
    }
  }
  return Array.from(names).sort();
}

export function chunkNodeStatus(
  chunkId: string,
  trace: Trace | null,
  analyzeStage: ReviewStage | undefined,
): NodeStatus {
  const stageLabel = `analyze:${chunkId}`;
  const hasTraceRows =
    trace != null &&
    (trace.tool_calls.some((tc) => tc.stage === stageLabel) ||
      trace.llm_rounds.some((lr) => lr.stage === stageLabel));

  if (!hasTraceRows) return "pending";
  if (analyzeStage == null) return "pending";
  if (analyzeStage.status === "done") return "done";
  if (analyzeStage.status === "failed") {
    const ownRoundFailed = trace!.llm_rounds.some(
      (lr) => lr.stage === stageLabel && lr.error != null,
    );
    return ownRoundFailed ? "failed" : "done";
  }
  return "running";
}

export function agentNodeStatus(agentName: string, trace: Trace | null,
                                agentStage: ReviewStage | undefined): NodeStatus {
  if (agentStage != null) return stageStatusToNodeStatus(agentStage.status);
  // No ReviewStage row at all -- the pipeline crashed/hit a limit before
  // _record_stage ran for this agent. Trust the agent's OWN llm_rounds: any
  // recorded error means it failed; otherwise it produced activity but we
  // never learned how it ended, so "running" is the honest status rather
  // than a silent "pending" that implies it never started.
  const ownRoundFailed = trace?.llm_rounds.some(
    (lr) => lr.stage === agentName && lr.error != null,
  );
  return ownRoundFailed ? "failed" : "running";
}

export function fmtDuration(ms: number | null): string | null {
  if (ms == null) return null;
  return ms >= 60_000
    ? `${Math.round(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
    : `${Math.round(ms / 1000)}s`;
}

export function fmtTokens(t: number | null): string | null {
  if (t == null) return null;
  return t >= 1000 ? `${(t / 1000).toFixed(1)}k tok` : `${t} tok`;
}

export function tokensForStage(label: string, trace: Trace | null): number | null {
  if (trace == null) return null;
  const rounds = trace.llm_rounds.filter((lr) => lr.stage === label);
  if (rounds.length === 0) return null;
  return rounds.reduce((sum, lr) => sum + (lr.prompt_tokens ?? 0) + (lr.completion_tokens ?? 0), 0);
}

function stageDurationMs(stage: ReviewStage | undefined): number | null {
  if (stage?.started_at == null || stage?.finished_at == null) return null;
  const start = new Date(stage.started_at).getTime();
  const finish = new Date(stage.finished_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(finish)) return null;
  return finish - start;
}

const counts = (s: ReviewStage | undefined) => ({
  produced: s?.produced ?? null,
  allowed: s?.allowed ?? null,
});

// A chunk has no stage row of its own, so its yield comes from analyze's
// per-chunk tally. Absent from that map once analyze finished means it found
// nothing, which is worth showing as 0 rather than leaving the node blank.
function chunkProduced(analyze: ReviewStage | undefined, chunkId: string): number | null {
  const tally = analyze?.produced_by_chunk;
  if (tally && chunkId in tally) return tally[chunkId];
  return analyze?.status === "done" ? 0 : null;
}

export function buildGraphNodes(stages: ReviewStage[], trace: Trace | null): GraphNodeSpec[] {
  const byName = new Map(stages.map((s) => [s.name, s]));
  const analyzeStage = byName.get("analyze");

  const nodes: GraphNodeSpec[] = [];

  const scoutStage = byName.get("scout");
  nodes.push({
    id: "scout",
    label: "scout",
    kind: "scout",
    status: stageStatusToNodeStatus(scoutStage?.status ?? "pending"),
    layer: 0,
    durationMs: stageDurationMs(scoutStage),
    tokens: tokensForStage("scout", trace),
    ...counts(scoutStage),
  });

  for (const chunkId of discoverChunkIds(trace)) {
    const id = `analyze:${chunkId}`;
    nodes.push({
      id,
      label: chunkId,
      kind: "chunk",
      status: chunkNodeStatus(chunkId, trace, analyzeStage),
      layer: 1,
      durationMs: null,
      tokens: tokensForStage(id, trace),
      ...counts(byName.get(id)),
      produced: byName.get(id)?.produced ?? chunkProduced(analyzeStage, chunkId),
    });
  }

  // Agent nodes: any stage not among the fixed stage names is a dynamic
  // reviewer-agent stage (e.g. "design", "security-reviewer") and becomes a
  // generic layer-1 agent node. This replaces the old hardcoded
  // byName.get("design") special case, which made any other dynamically
  // named agent stage invisible in the DAG. Discovered from trace rows as
  // well as review_stages (see discoverAgentNames) so an agent that crashed
  // before its own ReviewStage row was written still renders instead of
  // vanishing -- review b9df9951's platform-reviewer did exactly this.
  for (const agentName of discoverAgentNames(trace, stages)) {
    const agentStage = byName.get(agentName);
    const specialist = specialistLabel(agentName);
    nodes.push({
      id: agentName,
      label: specialist != null ? `${specialist.agent} (${specialist.chunkId})` : agentName,
      kind: "agent",
      status: agentNodeStatus(agentName, trace, agentStage),
      layer: 1,
      durationMs: stageDurationMs(agentStage),
      tokens: tokensForStage(agentName, trace),
      ...counts(agentStage),
    });
  }

  const verifyStage = byName.get("verify");
  nodes.push({
    id: "verify",
    label: "verify",
    kind: "verify",
    status: stageStatusToNodeStatus(verifyStage?.status ?? "pending"),
    layer: 2,
    durationMs: stageDurationMs(verifyStage),
    tokens: tokensForStage("verify", trace),
    ...counts(verifyStage),
  });

  const publishStage = byName.get("publish");
  nodes.push({
    id: "publish",
    label: "publish",
    kind: "publish",
    status: stageStatusToNodeStatus(publishStage?.status ?? "pending"),
    layer: 3,
    durationMs: stageDurationMs(publishStage),
    tokens: tokensForStage("publish", trace),
    ...counts(publishStage),
  });

  return nodes;
}

export interface FilteredTrace {
  toolCalls: Trace["tool_calls"];
  llmRounds: Trace["llm_rounds"];
}

export function filterTraceForNode(nodeId: string, trace: Trace | null): FilteredTrace {
  if (trace == null) return { toolCalls: [], llmRounds: [] };
  return {
    toolCalls: trace.tool_calls.filter((tc) => tc.stage === nodeId),
    llmRounds: trace.llm_rounds.filter((lr) => lr.stage === nodeId),
  };
}

export interface TimelineGroup {
  round: TraceLLMRound | null; // null only when tool calls exist but no round has completed yet
  toolCalls: TraceToolCall[];
}

export function buildTimeline(
  toolCalls: Trace["tool_calls"],
  llmRounds: Trace["llm_rounds"],
): TimelineGroup[] {
  // Handle the case where there are no rounds at all
  if (llmRounds.length === 0) {
    if (toolCalls.length === 0) return [];
    // All tool calls go into a single round:null group
    return [{ round: null, toolCalls: [...toolCalls] }];
  }

  // Sort rounds by seq to create groups
  const sortedRounds = [...llmRounds].sort((a, b) => a.seq - b.seq);
  const groups: TimelineGroup[] = sortedRounds.map((round) => ({
    round,
    toolCalls: [],
  }));

  // Sort tool calls by seq
  const sortedToolCalls = [...toolCalls].sort((a, b) => a.seq - b.seq);

  // For each tool call, find which group it belongs to
  for (const toolCall of sortedToolCalls) {
    // Find the group for the round that comes before this tool call
    // (or the first group if the tool call comes before all rounds)
    let groupIndex = 0;
    for (let i = groups.length - 1; i >= 0; i--) {
      const round = groups[i].round;
      if (round != null && round.seq < toolCall.seq) {
        groupIndex = i;
        break;
      }
    }
    groups[groupIndex].toolCalls.push(toolCall);
  }

  return groups;
}

export interface AggregateSummary {
  toolCallCount: number;
  llmRoundCount: number;
  statusCounts: Record<NodeStatus, number>;
}

export function aggregateSummary(stages: ReviewStage[], trace: Trace | null): AggregateSummary {
  const nodes = buildGraphNodes(stages, trace);
  const statusCounts: Record<NodeStatus, number> = { pending: 0, running: 0, done: 0, failed: 0 };
  for (const n of nodes) statusCounts[n.status]++;
  return {
    toolCallCount: trace?.tool_calls.length ?? 0,
    llmRoundCount: trace?.llm_rounds.length ?? 0,
    statusCounts,
  };
}

// ---- elk-input construction (pure; elkjs itself is not imported here) ----

export interface ElkNodeShape {
  id: string;
  width: number;
  height: number;
}

export interface ElkPoint {
  x: number;
  y: number;
}

export interface ElkEdgeSection {
  id: string;
  startPoint: ElkPoint;
  endPoint: ElkPoint;
  bendPoints?: ElkPoint[];
}

export interface ElkEdgeShape {
  id: string;
  sources: string[];
  targets: string[];
  sections?: ElkEdgeSection[]; // populated by elk.layout(), never set on input
}

export interface ElkGraphShape {
  id: string;
  layoutOptions: Record<string, string>;
  children: ElkNodeShape[];
  edges: ElkEdgeShape[];
}

const ELK_NODE_WIDTH = 200;
const ELK_NODE_HEIGHT = 82;

export function toElkGraph(nodes: GraphNodeSpec[]): ElkGraphShape {
  const scout = nodes.find((n) => n.layer === 0);
  const layer1 = nodes.filter((n) => n.layer === 1);
  const verify = nodes.find((n) => n.layer === 2);
  const publish = nodes.find((n) => n.layer === 3);

  const edges: ElkEdgeShape[] = [];

  if (scout != null) {
    if (layer1.length === 0) {
      if (verify != null) {
        edges.push({ id: `e:${scout.id}:${verify.id}`, sources: [scout.id], targets: [verify.id] });
      }
    } else {
      for (const mid of layer1) {
        edges.push({ id: `e:${scout.id}:${mid.id}`, sources: [scout.id], targets: [mid.id] });
        if (verify != null) {
          edges.push({ id: `e:${mid.id}:${verify.id}`, sources: [mid.id], targets: [verify.id] });
        }
      }
    }
  }

  if (verify != null && publish != null) {
    edges.push({ id: `e:${verify.id}:${publish.id}`, sources: [verify.id], targets: [publish.id] });
  }

  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "60",
      "elk.spacing.nodeNode": "18",
    },
    children: nodes.map((n) => ({ id: n.id, width: ELK_NODE_WIDTH, height: ELK_NODE_HEIGHT })),
    edges,
  };
}

// ---- group bounds (pure math; used to draw the analyze fan-out backdrop) ----

export interface PositionedNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Bounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

const GROUP_PADDING = 16;

export function groupBounds(positioned: PositionedNode[]): Bounds | null {
  if (positioned.length === 0) return null;

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const n of positioned) {
    minX = Math.min(minX, n.x);
    minY = Math.min(minY, n.y);
    maxX = Math.max(maxX, n.x + n.width);
    maxY = Math.max(maxY, n.y + n.height);
  }

  return {
    x: minX - GROUP_PADDING,
    y: minY - GROUP_PADDING,
    width: maxX - minX + GROUP_PADDING * 2,
    height: maxY - minY + GROUP_PADDING * 2,
  };
}

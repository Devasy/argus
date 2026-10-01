import type { ReviewStage, Trace, TraceLLMRound, TraceToolCall } from "../api/types";

export type NodeStatus = "pending" | "running" | "done" | "failed";
export type NodeKind =
  | "scout"
  | "retrieval"
  | "chunk"
  | "agent"
  | "verify"
  | "verify-branch"
  | "verify-delegate"
  | "publish"
  | "pick"
  | "ground"
  | "investigate"
  | "relate"
  | "apply";

// Mirrors the backend graph: scout -> retrieval -> analyze fan-out -> verify -> verify fan-out -> publish.
export const LAYER = { scout: 0, retrieval: 1, analyze: 2, verify: 3, verifyFanOut: 4, publish: 5 } as const;
export type Layer = (typeof LAYER)[keyof typeof LAYER];

export interface GraphNodeSpec {
  id: string; // stage label: "scout" | "analyze:c1" | "<agent name>" | "verify" | "verify:c1" | "publish"
  label: string; // display: chunk id for chunks ("c1"), else the stage name
  kind: NodeKind;
  status: NodeStatus;
  layer: number;
  durationMs: number | null; // from stage started_at/finished_at (null for chunks: no own stage row)
  tokens: number | null; // client-side sum of trace llm_rounds (prompt+completion) for this stage label
  produced: number | null; // candidates emitted by this stage
  allowed: number | null; // verify only: how many survived the gate
}

export const FIXED_STAGES = ["scout", "retrieval", "analyze", "verify", "publish"] as const;

const CHUNK_STAGE_RE = /^analyze:(.+)$/;
const VERIFY_BRANCH_RE = /^verify:(.+)$/;
const VERIFY_DELEGATE_RE = /^verify_delegate:(.+)$/;

function isAgentStage(name: string): boolean {
  if ((FIXED_STAGES as readonly string[]).includes(name)) return false;
  return !CHUNK_STAGE_RE.test(name) && !VERIFY_BRANCH_RE.test(name) && !VERIFY_DELEGATE_RE.test(name);
}

function hasActivity(label: string, trace: Trace | null): boolean {
  return (
    trace != null &&
    (trace.tool_calls.some((tc) => tc.stage === label) ||
      trace.llm_rounds.some((lr) => lr.stage === label))
  );
}

function ownRoundFailed(label: string, trace: Trace | null): boolean {
  return trace?.llm_rounds.some((lr) => lr.stage === label && lr.error != null) ?? false;
}

function stageLabels(stages: ReviewStage[], trace: Trace | null): string[] {
  const rows = trace == null ? [] : [...trace.tool_calls, ...trace.llm_rounds];
  return [...stages.map((s) => s.name), ...rows.map((r) => r.stage)];
}

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
  return Array.from(new Set(stageLabels(stages, trace).filter(isAgentStage))).sort();
}

// fan_out_verify buckets findings by chunk_id (untagged ones share "whole-mr"),
// so once analyze is done the branches are known and can render as pending.
export function discoverVerifyBranches(stages: ReviewStage[], trace: Trace | null): string[] {
  const ids = new Set<string>();
  for (const label of stageLabels(stages, trace)) {
    const m = VERIFY_BRANCH_RE.exec(label);
    if (m) ids.add(m[1]);
  }
  const analyze = stages.find((s) => s.name === "analyze");
  if (analyze?.status === "done") {
    let tagged = 0;
    for (const [chunkId, n] of Object.entries(analyze.produced_by_chunk ?? {})) {
      if (n > 0) ids.add(chunkId);
      tagged += n;
    }
    if ((analyze.produced ?? 0) > tagged) ids.add("whole-mr");
  }
  return Array.from(ids).sort();
}

export function discoverVerifyDelegates(stages: ReviewStage[], trace: Trace | null): string[] {
  const names = new Set(stageLabels(stages, trace).filter((l) => VERIFY_DELEGATE_RE.test(l)));
  const n = (s: string) => Number(VERIFY_DELEGATE_RE.exec(s)![1]);
  return Array.from(names).sort((a, b) => n(a) - n(b));
}

export function chunkNodeStatus(
  chunkId: string,
  trace: Trace | null,
  analyzeStage: ReviewStage | undefined,
  chunkStage?: ReviewStage,
): NodeStatus {
  if (chunkStage != null) return stageStatusToNodeStatus(chunkStage.status);
  const stageLabel = `analyze:${chunkId}`;
  if (!hasActivity(stageLabel, trace)) return "pending";
  if (analyzeStage?.status === "done") return "done";
  if (analyzeStage?.status === "failed") return ownRoundFailed(stageLabel, trace) ? "failed" : "done";
  return "running";
}

export function verifyBranchStatus(
  bucketId: string,
  trace: Trace | null,
  verifyStage: ReviewStage | undefined,
  branchStage?: ReviewStage,
): NodeStatus {
  if (branchStage != null) return stageStatusToNodeStatus(branchStage.status);
  const label = `verify:${bucketId}`;
  // gather_verify writes the aggregate row only after every branch returned.
  if (verifyStage?.status === "done") return "done";
  if (verifyStage?.status === "failed") return ownRoundFailed(label, trace) ? "failed" : "done";
  return hasActivity(label, trace) ? "running" : "pending";
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
    layer: LAYER.scout,
    durationMs: stageDurationMs(scoutStage),
    tokens: tokensForStage("scout", trace),
    ...counts(scoutStage),
  });

  // Only exists when learnings are enabled for the repo, so no pending placeholder.
  const retrievalStage = byName.get("retrieval");
  if (retrievalStage != null) {
    nodes.push({
      id: "retrieval",
      label: "retrieval",
      kind: "retrieval",
      status: stageStatusToNodeStatus(retrievalStage.status),
      layer: LAYER.retrieval,
      durationMs: stageDurationMs(retrievalStage),
      tokens: null,
      ...counts(retrievalStage),
    });
  }

  for (const chunkId of discoverChunkIds(trace)) {
    const id = `analyze:${chunkId}`;
    nodes.push({
      id,
      label: chunkId,
      kind: "chunk",
      status: chunkNodeStatus(chunkId, trace, analyzeStage, byName.get(id)),
      layer: LAYER.analyze,
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
      layer: LAYER.analyze,
      durationMs: stageDurationMs(agentStage),
      tokens: tokensForStage(agentName, trace),
      ...counts(agentStage),
    });
  }

  const verifyStage = byName.get("verify");
  const verifyFanOut: GraphNodeSpec[] = [];
  for (const bucketId of discoverVerifyBranches(stages, trace)) {
    const id = `verify:${bucketId}`;
    const branchStage = byName.get(id);
    verifyFanOut.push({
      id,
      label: `verify (${bucketId})`,
      kind: "verify-branch",
      status: verifyBranchStatus(bucketId, trace, verifyStage, branchStage),
      layer: LAYER.verifyFanOut,
      durationMs: stageDurationMs(branchStage),
      tokens: tokensForStage(id, trace),
      ...counts(branchStage),
    });
  }
  for (const id of discoverVerifyDelegates(stages, trace)) {
    const delegateStage = byName.get(id);
    verifyFanOut.push({
      id,
      label: `delegate ${VERIFY_DELEGATE_RE.exec(id)![1]}`,
      kind: "verify-delegate",
      status: agentNodeStatus(id, trace, delegateStage),
      layer: LAYER.verifyFanOut,
      durationMs: stageDurationMs(delegateStage),
      tokens: tokensForStage(id, trace),
      ...counts(delegateStage),
    });
  }

  // The overall MR verify state: the aggregate row only lands after every branch, so infer "running" from branch activity until then.
  const verifyStarted =
    hasActivity("verify", trace) || verifyFanOut.some((n) => n.status !== "pending");
  nodes.push({
    id: "verify",
    label: "verify",
    kind: "verify",
    status:
      verifyStage != null
        ? stageStatusToNodeStatus(verifyStage.status)
        : verifyStarted
          ? "running"
          : "pending",
    layer: LAYER.verify,
    durationMs: stageDurationMs(verifyStage),
    tokens: tokensForStage("verify", trace),
    ...counts(verifyStage),
  });
  nodes.push(...verifyFanOut);

  const publishStage = byName.get("publish");
  nodes.push({
    id: "publish",
    label: "publish",
    kind: "publish",
    status: stageStatusToNodeStatus(publishStage?.status ?? "pending"),
    layer: LAYER.publish,
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
  // Each non-empty layer feeds the next; an empty fan-out layer is skipped so
  // its neighbours connect directly. Layers come from the sorted distinct
  // values actually PRESENT in `nodes`, not the fixed LAYER map, so this
  // works for any graph (review or audit) built from its own layer numbers.
  // For a review graph this is equivalent to the old fixed-map iteration:
  // LAYER's own values are already 0..5 in ascending order, and a layer with
  // no nodes was always filtered out anyway.
  const layerValues = Array.from(new Set(nodes.map((n) => n.layer))).sort((a, b) => a - b);
  const layers = layerValues.map((layer) => nodes.filter((n) => n.layer === layer));

  const edges: ElkEdgeShape[] = [];
  for (let i = 0; i + 1 < layers.length; i++) {
    for (const src of layers[i]) {
      for (const dst of layers[i + 1]) {
        edges.push({ id: `e:${src.id}:${dst.id}`, sources: [src.id], targets: [dst.id] });
      }
    }
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

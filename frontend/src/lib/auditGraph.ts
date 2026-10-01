import type { AuditStage } from "../api/types";
import type { GraphNodeSpec, NodeStatus } from "./pipelineGraph";

// Mirrors the backend graph: pick -> scout -> ground fan-out (each with its own
// investigate sub-agents) -> relate fan-out -> verify -> apply.
export const AUDIT_LAYER = {
  pick: 0,
  scout: 1,
  ground: 2,
  investigate: 3,
  relate: 4,
  verify: 5,
  apply: 6,
} as const;

const INVESTIGATE_RE = /^ground:([^:]+):investigate:(\d+)$/;
const GROUND_RETRY_RE = /^ground:([^:]+):retry$/;
const GROUND_RE = /^ground:([^:]+)$/;
const RELATE_RE = /^relate:(.+)$/;
const VERIFY_RE = /^verify(:.*)?$/;

function stageStatusToNodeStatus(status: string): NodeStatus {
  if (status === "running" || status === "done" || status === "failed") return status;
  return "pending";
}

function stageDurationMs(stage: AuditStage): number | null {
  if (stage.finished_at == null) return null;
  const start = new Date(stage.started_at).getTime();
  const finish = new Date(stage.finished_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(finish)) return null;
  return finish - start;
}

export function buildAuditGraphNodes(stages: AuditStage[]): GraphNodeSpec[] {
  const retryFor = new Map<string, AuditStage>();
  for (const s of stages) {
    const m = GROUND_RETRY_RE.exec(s.stage_name);
    if (m) retryFor.set(`ground:${m[1]}`, s);
  }

  // Pre-index stages to construct accurate, non-conflicting dependency edges
  const rawNames = new Set(stages.map((s) => s.stage_name));
  const groundGroups = new Set<string>();
  const investigationsByGroup = new Map<string, string[]>();
  const relateNodes: string[] = [];

  for (const s of stages) {
    const { stage_name } = s;
    const invMatch = INVESTIGATE_RE.exec(stage_name);
    if (invMatch) {
      const group = invMatch[1];
      const list = investigationsByGroup.get(group) ?? [];
      list.push(stage_name);
      investigationsByGroup.set(group, list);
    } else if (GROUND_RE.test(stage_name)) {
      const [, group] = GROUND_RE.exec(stage_name)!;
      groundGroups.add(group);
    } else if (RELATE_RE.test(stage_name)) {
      relateNodes.push(stage_name);
    }
  }

  // Determine terminal nodes for each ground group:
  // If the ground group spawned investigations, those investigation stages are the terminal nodes.
  // Otherwise, the ground node itself is terminal.
  const terminalNodesForGroup = (group: string): string[] => {
    const invs = investigationsByGroup.get(group);
    return invs && invs.length > 0 ? invs : [`ground:${group}`];
  };

  const allGroundTerminals: string[] = [];
  for (const group of groundGroups) {
    allGroundTerminals.push(...terminalNodesForGroup(group));
  }

  const nodes: GraphNodeSpec[] = [];

  for (const s of stages) {
    const { stage_name } = s;
    if (GROUND_RETRY_RE.test(stage_name)) continue; // folded into its parent ground node below

    if (stage_name === "pick") {
      nodes.push(specFor(s, "pick", "pick", AUDIT_LAYER.pick, []));
    } else if (stage_name === "scout") {
      const parentIds = rawNames.has("pick") ? ["pick"] : [];
      nodes.push(specFor(s, "scout", "scout", AUDIT_LAYER.scout, parentIds));
    } else if (INVESTIGATE_RE.test(stage_name)) {
      const [, group, n] = INVESTIGATE_RE.exec(stage_name)!;
      // Connects ONLY to its parent ground group
      const parentIds = rawNames.has(`ground:${group}`) ? [`ground:${group}`] : (rawNames.has("scout") ? ["scout"] : []);
      nodes.push(specFor(s, `investigate (${group}:${n})`, "investigate", AUDIT_LAYER.investigate, parentIds));
    } else if (GROUND_RE.test(stage_name)) {
      const [, group] = GROUND_RE.exec(stage_name)!;
      const retry = retryFor.get(stage_name);
      const authoritative = retry ?? s;
      const parentIds = rawNames.has("scout") ? ["scout"] : (rawNames.has("pick") ? ["pick"] : []);
      nodes.push({
        id: stage_name,
        label: `ground (${group})`,
        kind: "ground",
        status: stageStatusToNodeStatus(authoritative.status),
        layer: AUDIT_LAYER.ground,
        durationMs: stageDurationMs(authoritative),
        tokens: null,
        produced: null,
        allowed: null,
        parentIds,
      });
    } else if (RELATE_RE.test(stage_name)) {
      const [, group] = RELATE_RE.exec(stage_name)!;
      // If group corresponds to a known ground group, connect from that group's terminal nodes.
      // Otherwise (cluster relate), connect from all ground terminals.
      const parentIds = groundGroups.has(group)
        ? terminalNodesForGroup(group)
        : (allGroundTerminals.length > 0 ? allGroundTerminals : (rawNames.has("scout") ? ["scout"] : []));
      nodes.push(specFor(s, `relate (${group})`, "relate", AUDIT_LAYER.relate, parentIds));
    } else if (VERIFY_RE.test(stage_name)) {
      // Verify runs after all relate nodes (or ground terminals if relate is absent)
      const parentIds = relateNodes.length > 0
        ? relateNodes
        : (allGroundTerminals.length > 0 ? allGroundTerminals : (rawNames.has("scout") ? ["scout"] : []));
      nodes.push(specFor(s, stage_name, "verify", AUDIT_LAYER.verify, parentIds));
    } else if (stage_name === "apply") {
      const verifyNodes = stages.filter((st) => VERIFY_RE.test(st.stage_name)).map((st) => st.stage_name);
      const parentIds = verifyNodes.length > 0
        ? verifyNodes
        : (rawNames.has("pick") ? ["pick"] : []);
      nodes.push(specFor(s, "apply", "apply", AUDIT_LAYER.apply, parentIds));
    }
  }

  return nodes;

  function specFor(
    s: AuditStage,
    label: string,
    kind: GraphNodeSpec["kind"],
    layer: number,
    parentIds: string[] = [],
  ): GraphNodeSpec {
    return {
      id: s.stage_name,
      label,
      kind,
      status: stageStatusToNodeStatus(s.status),
      layer,
      durationMs: stageDurationMs(s),
      tokens: null,
      produced: null,
      allowed: null,
      parentIds,
    };
  }
}

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

  const nodes: GraphNodeSpec[] = [];

  for (const s of stages) {
    const { stage_name } = s;
    if (GROUND_RETRY_RE.test(stage_name)) continue; // folded into its parent ground node below

    if (stage_name === "pick") {
      nodes.push(specFor(s, "pick", "pick", AUDIT_LAYER.pick));
    } else if (stage_name === "scout") {
      nodes.push(specFor(s, "scout", "scout", AUDIT_LAYER.scout));
    } else if (INVESTIGATE_RE.test(stage_name)) {
      const [, group, n] = INVESTIGATE_RE.exec(stage_name)!;
      nodes.push(specFor(s, `investigate (${group}:${n})`, "investigate", AUDIT_LAYER.investigate));
    } else if (GROUND_RE.test(stage_name)) {
      const [, group] = GROUND_RE.exec(stage_name)!;
      // The retry, when it exists, is the true final word on this group: it
      // ran because the first attempt skipped learnings, and its outcome is
      // what coverage actually settled on.
      const retry = retryFor.get(stage_name);
      const authoritative = retry ?? s;
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
      });
    } else if (RELATE_RE.test(stage_name)) {
      const [, group] = RELATE_RE.exec(stage_name)!;
      nodes.push(specFor(s, `relate (${group})`, "relate", AUDIT_LAYER.relate));
    } else if (VERIFY_RE.test(stage_name)) {
      nodes.push(specFor(s, stage_name, "verify", AUDIT_LAYER.verify));
    } else if (stage_name === "apply") {
      nodes.push(specFor(s, "apply", "apply", AUDIT_LAYER.apply));
    }
  }

  return nodes;

  function specFor(s: AuditStage, label: string, kind: GraphNodeSpec["kind"], layer: number): GraphNodeSpec {
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
    };
  }
}

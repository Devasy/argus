import { Handle, Position } from "reactflow";
import { fmtDuration, fmtTokens, type GraphNodeSpec } from "../lib/pipelineGraph";

const KIND_META: Record<GraphNodeSpec["kind"], { icon: string; tag: string }> = {
  scout: { icon: "⌖", tag: "context" },
  chunk: { icon: "▤", tag: "analysis" },
  agent: { icon: "✦", tag: "reviewer agent" },
  verify: { icon: "✓", tag: "gate" },
  publish: { icon: "↗", tag: "output" },
};

export default function PipelineNode({
  data,
}: {
  data: { spec: GraphNodeSpec; selected: boolean };
}) {
  const { spec, selected } = data;
  const meta = KIND_META[spec.kind];
  const metrics = [fmtDuration(spec.durationMs), fmtTokens(spec.tokens)].filter(Boolean);
  const yieldLabel =
    spec.produced == null
      ? null
      : spec.allowed == null
        ? `${spec.produced} found`
        : `${spec.allowed}/${spec.produced} kept`;
  return (
    <div
      className={`pl-node pl-${spec.status}${selected ? " sel" : ""}${spec.status === "running" ? " nw-pulse" : ""}`}
    >
      <Handle type="target" position={Position.Left} className="pl-handle" />
      <div className="pl-head">
        <span className="pl-icon">{meta.icon}</span>
        <span className="pl-name mono">{spec.label}</span>
        <span className={`pl-dot ${spec.status}`} />
      </div>
      <div className="pl-meta">
        <span className="stage-tag">{meta.tag}</span>
        {metrics.length > 0 && <span className="pl-metrics">{metrics.join(" · ")}</span>}
      </div>
      {yieldLabel && (
        <div className="pl-yield" title="candidate findings produced by this stage">
          {yieldLabel}
        </div>
      )}
      <Handle type="source" position={Position.Right} className="pl-handle" />
    </div>
  );
}

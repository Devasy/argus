import { BaseEdge, type EdgeProps } from "reactflow";
import type { ElkPoint } from "../lib/pipelineGraph";

const CORNER_RADIUS = 10;

// Renders ELK's own computed route (edge.sections[].startPoint/bendPoints/endPoint)
// instead of letting react-flow re-derive a path from node positions. React-flow's
// built-in smoothstep/bezier edges pick their own bend heuristics from source/target
// handle positions alone, which produces large detour loops for fan-out/fan-in edges
// (one source or target shared by nodes at very different vertical offsets). ELK
// already solved that routing problem as part of layout, so we just draw its answer.
function pointsToPath(points: ElkPoint[]): string {
  if (points.length < 2) return "";
  let d = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1];
    const curr = points[i];
    const next = points[i + 1];
    if (next == null) {
      d += ` L ${curr.x} ${curr.y}`;
      continue;
    }
    // Round the corner at `curr` by stopping short on both sides and
    // quadratic-curving through the corner point, mirroring react-flow's
    // own getBend() rounding for smoothstep edges.
    const toPrev = { x: prev.x - curr.x, y: prev.y - curr.y };
    const toNext = { x: next.x - curr.x, y: next.y - curr.y };
    const prevLen = Math.hypot(toPrev.x, toPrev.y);
    const nextLen = Math.hypot(toNext.x, toNext.y);
    const r = Math.min(CORNER_RADIUS, prevLen / 2, nextLen / 2);
    const inPoint = {
      x: curr.x + (toPrev.x / prevLen) * r,
      y: curr.y + (toPrev.y / prevLen) * r,
    };
    const outPoint = {
      x: curr.x + (toNext.x / nextLen) * r,
      y: curr.y + (toNext.y / nextLen) * r,
    };
    d += ` L ${inPoint.x} ${inPoint.y} Q ${curr.x} ${curr.y} ${outPoint.x} ${outPoint.y}`;
  }
  return d;
}

export default function ElkEdge({ data, style, markerEnd, markerStart }: EdgeProps) {
  const points: ElkPoint[] | undefined = data?.points;
  if (points == null || points.length < 2) return null;
  return (
    <BaseEdge path={pointsToPath(points)} style={style} markerEnd={markerEnd} markerStart={markerStart} />
  );
}

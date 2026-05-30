import ELK from "elkjs/lib/elk.bundled.js";
import type { ElkGraphShape, ElkPoint } from "./pipelineGraph";

const elk = new ELK();

export interface ElkEdgeRoute {
  points: ElkPoint[]; // startPoint, ...bendPoints, endPoint, in order
}

export interface ElkLayoutResult {
  positions: Map<string, { x: number; y: number }>;
  edgeRoutes: Map<string, ElkEdgeRoute>;
}

export async function layoutWithElk(graph: ElkGraphShape): Promise<ElkLayoutResult> {
  const result = await elk.layout(graph);
  const positions = new Map<string, { x: number; y: number }>();
  for (const child of result.children ?? []) {
    positions.set(child.id!, { x: child.x ?? 0, y: child.y ?? 0 });
  }

  const edgeRoutes = new Map<string, ElkEdgeRoute>();
  for (const edge of result.edges ?? []) {
    const section = edge.sections?.[0];
    if (section == null) continue;
    const points: ElkPoint[] = [
      section.startPoint,
      ...(section.bendPoints ?? []),
      section.endPoint,
    ];
    edgeRoutes.set(edge.id!, { points });
  }

  return { positions, edgeRoutes };
}

import { useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  type Edge,
  type Node,
} from "reactflow";
import "reactflow/dist/style.css";
import { useReviewerGraph } from "../../api/queries";
import type { ReviewerGraphEdge, ReviewerGraphNode } from "../../api/types";
import ErrorBox from "../../components/ErrorBox";
import ElkEdge from "../../components/ElkEdge";
import { layoutWithElk, type ElkEdgeRoute } from "../../lib/elkLayout";
import type { ElkGraphShape } from "../../lib/pipelineGraph";

const NODE_WIDTH = 160;
const NODE_HEIGHT = 44;

function toElkGraph(nodes: ReviewerGraphNode[], edges: ReviewerGraphEdge[]): ElkGraphShape {
  return {
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.layered.spacing.nodeNodeBetweenLayers": "70",
      "elk.spacing.nodeNode": "40",
    },
    children: nodes.map((n) => ({ id: n.actor_id, width: NODE_WIDTH, height: NODE_HEIGHT })),
    edges: edges.map((e) => ({
      id: `e:${e.reviewer_id}:${e.author_id}`,
      sources: [e.reviewer_id],
      targets: [e.author_id],
    })),
  };
}

function edgeColor(e: ReviewerGraphEdge): string {
  const decided = e.resolved + e.rejected;
  if (decided === 0) return "var(--border-default)";
  const hitRate = e.resolved / decided;
  if (hitRate >= 0.66) return "var(--state-success-text)";
  if (hitRate >= 0.33) return "var(--state-warning-text, orange)";
  return "var(--state-error-text, crimson)";
}

export default function ReviewerGraphView() {
  // Only ever rendered by PeoplePage once useMe() confirms an admin role.
  const graph = useReviewerGraph(true);
  const nodes = useMemo(() => graph.data?.nodes ?? [], [graph.data]);
  const edges = useMemo(() => graph.data?.edges ?? [], [graph.data]);

  const elkGraph = useMemo(() => toElkGraph(nodes, edges), [nodes, edges]);
  const [positions, setPositions] = useState<Map<string, { x: number; y: number }> | null>(null);
  const [edgeRoutes, setEdgeRoutes] = useState<Map<string, ElkEdgeRoute>>(new Map());

  useEffect(() => {
    if (nodes.length === 0) return;
    let alive = true;
    layoutWithElk(elkGraph).then(({ positions, edgeRoutes }) => {
      if (!alive) return;
      setPositions(positions);
      setEdgeRoutes(edgeRoutes);
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [elkGraph]);

  if (graph.error) return <ErrorBox message={graph.error.message} onRetry={graph.refetch} />;
  if (graph.isPending) return <div className="spinner">Loading reviewer graph…</div>;
  if (nodes.length === 0) {
    return <div className="muted dash-empty">No human review comments recorded yet.</div>;
  }
  if (positions == null) {
    return <div className="spinner">Laying out graph…</div>;
  }

  const flowNodes: Node[] = nodes.map((n) => ({
    id: n.actor_id,
    position: positions.get(n.actor_id) ?? { x: 0, y: 0 },
    data: { label: n.display_name || n.username },
    style: {
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      borderRadius: 8,
      fontSize: 13,
    },
    draggable: false,
  }));
  const flowEdges: Edge[] = edges.map((e) => {
    const id = `e:${e.reviewer_id}:${e.author_id}`;
    const width = Math.max(1, Math.min(6, Math.round(Math.log2(e.comments + 1) * 2)));
    return {
      id,
      source: e.reviewer_id,
      target: e.author_id,
      type: "elkEdge",
      data: { points: edgeRoutes.get(id)?.points },
      label: `${e.comments}`,
      style: { stroke: edgeColor(e), strokeWidth: width },
      markerEnd: { type: MarkerType.ArrowClosed, color: edgeColor(e) },
    };
  });

  return (
    <div>
      <p className="muted">
        Arrow points reviewer → PR author. Thicker = more comments; green = mostly resolved, red =
        mostly rejected.
      </p>
      <div className="pipeline-graph-canvas" style={{ height: 560 }}>
        <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
          edgeTypes={{ elkEdge: ElkEdge }}
          nodesDraggable={false}
          nodesConnectable={false}
          fitView
        >
          <Background />
          <Controls showInteractive={false} />
          {nodes.length > 8 && <MiniMap />}
        </ReactFlow>
      </div>
      <div className="muted" style={{ marginTop: 8 }}>
        {nodes.length} people · {edges.length} reviewer relationships
      </div>
    </div>
  );
}

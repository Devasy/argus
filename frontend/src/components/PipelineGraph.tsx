import { useEffect, useMemo, useState } from "react";
import ReactFlow, { Background, Controls, MiniMap, type Edge, type Node } from "reactflow";
import "reactflow/dist/style.css";
import type { ReviewStage, Trace } from "../api/types";
import { layoutWithElk, type ElkEdgeRoute } from "../lib/elkLayout";
import { buildGraphNodes, groupBounds, toElkGraph, type GraphNodeSpec } from "../lib/pipelineGraph";
import ElkEdge from "./ElkEdge";
import PipelineNode from "./PipelineNode";

interface Props {
  stages: ReviewStage[];
  trace: Trace | null;
  selectedNode: string | null;
  onSelectNode: (nodeId: string | null) => void;
}

function GroupBackdrop({ data }: { data: { label: string } }) {
  return <div className="pl-group-label">{data.label}</div>;
}

const nodeTypes = { pipelineCard: PipelineNode, groupBackdrop: GroupBackdrop };
const edgeTypes = { elkEdge: ElkEdge };

const NODE_WIDTH = 200;
const NODE_HEIGHT = 82;

function toFlowNodes(
  specs: GraphNodeSpec[],
  positions: Map<string, { x: number; y: number }>,
  selectedNode: string | null,
): Node[] {
  const cardNodes: Node[] = specs.map((spec) => ({
    id: spec.id,
    type: "pipelineCard",
    position: positions.get(spec.id) ?? { x: 0, y: 0 },
    data: { spec, selected: spec.id === selectedNode },
    draggable: false,
  }));

  const chunkSpecs = specs.filter((s) => s.kind === "chunk");
  if (chunkSpecs.length === 0) return cardNodes;

  const bounds = groupBounds(
    chunkSpecs.map((s) => {
      const pos = positions.get(s.id) ?? { x: 0, y: 0 };
      return { id: s.id, x: pos.x, y: pos.y, width: NODE_WIDTH, height: NODE_HEIGHT };
    }),
  );
  if (bounds == null) return cardNodes;

  const groupNode: Node = {
    id: "__group:analyze",
    type: "groupBackdrop",
    position: { x: bounds.x, y: bounds.y },
    style: {
      width: bounds.width,
      height: bounds.height,
    },
    className: "pl-group",
    data: { label: `Analyze · fan-out × ${chunkSpecs.length}` },
    selectable: false,
    draggable: false,
    zIndex: -1,
  };

  return [groupNode, ...cardNodes];
}

function toFlowEdges(specs: GraphNodeSpec[], edgeRoutes: Map<string, ElkEdgeRoute>): Edge[] {
  const elk = toElkGraph(specs);
  const byId = new Map(specs.map((s) => [s.id, s]));

  return elk.edges.map((e): Edge => {
    const source = e.sources[0];
    const target = e.targets[0];
    const sourceSpec = byId.get(source);
    const targetSpec = byId.get(target);
    const animated = sourceSpec?.status === "running" || targetSpec?.status === "running";
    const bothDone = sourceSpec?.status === "done" && targetSpec?.status === "done";
    return {
      id: e.id,
      source,
      target,
      // Render ELK's own computed route rather than letting react-flow
      // re-derive a path from node positions: react-flow's built-in
      // smoothstep/bezier edges pick bend heuristics from source/target
      // handle positions alone, which draws large detour loops for fan-out/
      // fan-in edges (one source or target shared by nodes at very different
      // vertical offsets -- exactly the scout -> design/c1 and
      // design/c1 -> verify cases). ELK already solved that routing problem
      // as part of layout, so we just draw its answer.
      type: "elkEdge",
      data: { points: edgeRoutes.get(e.id)?.points },
      animated,
      style: bothDone
        ? { stroke: "var(--state-success-text)", opacity: 0.55 }
        : { stroke: "var(--border-default)" },
    };
  });
}

export default function PipelineGraph({ stages, trace, selectedNode, onSelectNode }: Props) {
  const specs = useMemo(() => buildGraphNodes(stages, trace), [stages, trace]);

  // ELK layout only depends on which nodes exist and how they're connected
  // (id/kind/layer), never on status/duration/tokens. Keying the layout
  // effect off this structural signature -- instead of `specs` itself, which
  // is a fresh array on every poll -- means status-only updates (the vast
  // majority of polls) don't trigger a relayout, don't reset `positions` to
  // null, and don't unmount/remount the <ReactFlow> tree. That remount was
  // what caused `fitView` to refire (visible zoom reset) and produced
  // transient node/edge misalignment on every refresh.
  const structureKey = useMemo(
    () => specs.map((s) => `${s.id}:${s.kind}:${s.layer}`).join("|"),
    [specs],
  );
  const elkGraph = useMemo(() => toElkGraph(specs), [structureKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const [positions, setPositions] = useState<Map<string, { x: number; y: number }> | null>(null);
  const [edgeRoutes, setEdgeRoutes] = useState<Map<string, ElkEdgeRoute>>(new Map());

  useEffect(() => {
    let alive = true;
    layoutWithElk(elkGraph).then(({ positions, edgeRoutes }) => {
      if (!alive) return;
      setPositions(positions);
      setEdgeRoutes(edgeRoutes);
    });
    return () => {
      alive = false;
    };
  }, [elkGraph]);

  // Only render specs whose real coordinates have already come back from ELK.
  // `specs` updates the instant a new chunk/agent stage is discovered, but
  // `positions` only catches up once the async layout resolves; without this
  // filter, a brand-new id would render its node at a {x:0,y:0} fallback (and
  // its edges fully formed) for that one tick -- a dangling line pointing at
  // the canvas origin, exactly like the floating/misaligned segments seen in
  // production.
  const positionedSpecs = useMemo(
    () => (positions == null ? [] : specs.filter((s) => positions.has(s.id))),
    [specs, positions],
  );
  const nodes = useMemo(
    () => (positions == null ? [] : toFlowNodes(positionedSpecs, positions, selectedNode)),
    [positionedSpecs, positions, selectedNode],
  );
  const edges = useMemo(
    () => toFlowEdges(positionedSpecs, edgeRoutes),
    [positionedSpecs, edgeRoutes],
  );

  if (positions == null) {
    return (
      <div className="pipeline-graph-canvas">
        <div className="spinner">Laying out graph…</div>
      </div>
    );
  }

  return (
    <div className="pipeline-graph-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodeClick={(_, node) => {
          if (node.id.startsWith("__group:")) return;
          onSelectNode(node.id);
        }}
        onPaneClick={() => onSelectNode(null)}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={true}
        fitView
      >
        <Background />
        <Controls showInteractive={false} />
        {specs.length > 8 && <MiniMap />}
      </ReactFlow>
    </div>
  );
}

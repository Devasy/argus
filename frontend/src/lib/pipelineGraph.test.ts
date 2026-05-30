import { describe, expect, it } from "vitest";
import type { ReviewStage, Trace, TraceLLMRound, TraceToolCall } from "../api/types";
import {
  agentNodeStatus,
  aggregateSummary,
  buildGraphNodes,
  buildTimeline,
  chunkNodeStatus,
  discoverAgentNames,
  discoverChunkIds,
  filterTraceForNode,
  groupBounds,
  specialistLabel,
  stageStatusToNodeStatus,
  toElkGraph,
  tokensForStage,
  type PositionedNode,
} from "./pipelineGraph";

function trace(overrides: Partial<Trace> = {}): Trace {
  return { tool_calls: [], llm_rounds: [], ...overrides };
}

function stage(
  name: string,
  status: ReviewStage["status"],
  overrides: Partial<ReviewStage> = {},
): ReviewStage {
  return {
    name,
    status,
    started_at: null,
    finished_at: null,
    produced: null,
    allowed: null,
    produced_by_chunk: {},
    ...overrides,
  };
}

describe("stageStatusToNodeStatus", () => {
  it("maps known statuses through", () => {
    expect(stageStatusToNodeStatus("running")).toBe("running");
    expect(stageStatusToNodeStatus("done")).toBe("done");
    expect(stageStatusToNodeStatus("failed")).toBe("failed");
  });

  it("defaults unknown statuses to pending", () => {
    expect(stageStatusToNodeStatus("queued")).toBe("pending");
    expect(stageStatusToNodeStatus("")).toBe("pending");
  });
});

describe("discoverChunkIds", () => {
  it("returns empty list for null trace", () => {
    expect(discoverChunkIds(null)).toEqual([]);
  });

  it("returns empty list when no analyze: stage labels present", () => {
    const t = trace({
      tool_calls: [{ stage: "scout", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 }],
    });
    expect(discoverChunkIds(t)).toEqual([]);
  });

  it("extracts and dedupes chunk ids from tool_calls and llm_rounds", () => {
    const t = trace({
      tool_calls: [
        { stage: "analyze:c1", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
        { stage: "analyze:c2", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
      ],
      llm_rounds: [
        {
          stage: "analyze:c1",
          seq: 1,
          prompt_tokens: 10,
          completion_tokens: 5,
          latency_ms: 100,
          error: null,
        },
      ],
    });
    expect(discoverChunkIds(t)).toEqual(["c1", "c2"]);
  });

  it("sorts chunk ids", () => {
    const t = trace({
      tool_calls: [
        { stage: "analyze:c10", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
        { stage: "analyze:c2", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
      ],
    });
    expect(discoverChunkIds(t)).toEqual(["c10", "c2"]);
  });
});

describe("specialistLabel", () => {
  it("parses a chunk-scoped specialist stage name", () => {
    expect(specialistLabel("security:c1")).toEqual({ agent: "security", chunkId: "c1" });
  });

  it("does not match analyze:<chunk> chunk stages", () => {
    expect(specialistLabel("analyze:c1")).toBeNull();
  });

  it("does not match fixed or whole-MR agent stage names without a colon", () => {
    expect(specialistLabel("scout")).toBeNull();
    expect(specialistLabel("design")).toBeNull();
  });

  it("splits only on the first colon (agent names could theoretically contain none, chunk ids can)", () => {
    expect(specialistLabel("security:c1a")).toEqual({ agent: "security", chunkId: "c1a" });
  });
});

describe("discoverAgentNames", () => {
  it("returns empty when there are no agent stages or trace rows", () => {
    const stages: ReviewStage[] = [stage("scout", "done"), stage("verify", "pending")];
    expect(discoverAgentNames(trace(), stages)).toEqual([]);
  });

  it("finds an agent name from review_stages alone", () => {
    const stages: ReviewStage[] = [stage("scout", "done"), stage("design", "done")];
    expect(discoverAgentNames(trace(), stages)).toEqual(["design"]);
  });

  it("finds an agent name from trace rows alone (the bug fix: no ReviewStage row exists)", () => {
    // Regression for review b9df9951: platform-reviewer hit LangGraph's
    // recursion limit before _record_stage ever ran, so it has llm_rounds
    // but zero review_stages rows.
    const stages: ReviewStage[] = [stage("scout", "done")];
    const t = trace({
      llm_rounds: [
        {
          stage: "platform-reviewer",
          seq: 1,
          prompt_tokens: 1000,
          completion_tokens: 10,
          latency_ms: 1,
          error: null,
        },
      ],
    });
    expect(discoverAgentNames(t, stages)).toEqual(["platform-reviewer"]);
  });

  it("excludes fixed stage names and chunk labels", () => {
    const stages: ReviewStage[] = [stage("scout", "done"), stage("analyze", "done")];
    const t = trace({
      tool_calls: [
        { stage: "analyze:c1", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
      ],
    });
    expect(discoverAgentNames(t, stages)).toEqual([]);
  });

  it("dedupes an agent name present in both review_stages and trace", () => {
    const stages: ReviewStage[] = [stage("design", "done")];
    const t = trace({
      llm_rounds: [
        { stage: "design", seq: 1, prompt_tokens: 1, completion_tokens: 1, latency_ms: 1, error: null },
      ],
    });
    expect(discoverAgentNames(t, stages)).toEqual(["design"]);
  });

  it("sorts multiple agent names", () => {
    const stages: ReviewStage[] = [stage("security-reviewer", "done"), stage("design", "done")];
    expect(discoverAgentNames(trace(), stages)).toEqual(["design", "security-reviewer"]);
  });

  it("does not crash on a null trace", () => {
    const stages: ReviewStage[] = [stage("design", "done")];
    expect(discoverAgentNames(null, stages)).toEqual(["design"]);
  });

  it("finds a chunk-scoped specialist stage name (e.g. security:c1) as an agent", () => {
    const stages: ReviewStage[] = [stage("scout", "done"), stage("security:c1", "done")];
    expect(discoverAgentNames(trace(), stages)).toEqual(["security:c1"]);
  });
});

describe("agentNodeStatus", () => {
  it("uses the ReviewStage row's status when one exists", () => {
    expect(agentNodeStatus("design", trace(), stage("design", "done"))).toBe("done");
  });

  it("is 'running' when no ReviewStage row exists but the agent has trace activity with no error", () => {
    // The honest state for platform-reviewer mid-recursion-loop: it started
    // and produced rounds, but nothing ever recorded how it ended.
    const t = trace({
      llm_rounds: [
        {
          stage: "platform-reviewer",
          seq: 1,
          prompt_tokens: 1,
          completion_tokens: 1,
          latency_ms: 1,
          error: null,
        },
      ],
    });
    expect(agentNodeStatus("platform-reviewer", t, undefined)).toBe("running");
  });

  it("is 'failed' when no ReviewStage row exists but the agent's own round recorded an error", () => {
    const t = trace({
      llm_rounds: [
        {
          stage: "platform-reviewer",
          seq: 1,
          prompt_tokens: 1,
          completion_tokens: 1,
          latency_ms: 1,
          error: "Recursion limit of 24 reached",
        },
      ],
    });
    expect(agentNodeStatus("platform-reviewer", t, undefined)).toBe("failed");
  });

  it("is 'running' (not 'pending') when neither a stage row nor trace rows exist yet", () => {
    expect(agentNodeStatus("platform-reviewer", trace(), undefined)).toBe("running");
  });

  it("does not crash on a null trace with no stage row", () => {
    expect(agentNodeStatus("platform-reviewer", null, undefined)).toBe("running");
  });
});

describe("chunkNodeStatus", () => {
  const t = trace({
    tool_calls: [
      { stage: "analyze:c1", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
    ],
  });

  it("is pending when no trace rows exist for the chunk", () => {
    expect(chunkNodeStatus("c9", t, stage("analyze", "running"))).toBe("pending");
  });

  it("is running when trace rows exist and aggregate analyze stage is running", () => {
    expect(chunkNodeStatus("c1", t, stage("analyze", "running"))).toBe("running");
  });

  it("is done when aggregate analyze stage is done", () => {
    expect(chunkNodeStatus("c1", t, stage("analyze", "done"))).toBe("done");
  });

  it("is done when aggregate analyze stage failed but this chunk's own round has no error", () => {
    const withRound = trace({
      tool_calls: t.tool_calls,
      llm_rounds: [
        {
          stage: "analyze:c1",
          seq: 1,
          prompt_tokens: 1,
          completion_tokens: 1,
          latency_ms: 1,
          error: null,
        },
      ],
    });
    expect(chunkNodeStatus("c1", withRound, stage("analyze", "failed"))).toBe("done");
  });

  it("is failed when aggregate analyze stage failed and this chunk's own round has an error", () => {
    const withError = trace({
      tool_calls: t.tool_calls,
      llm_rounds: [
        {
          stage: "analyze:c1",
          seq: 1,
          prompt_tokens: 1,
          completion_tokens: 1,
          latency_ms: 1,
          error: "boom",
        },
      ],
    });
    expect(chunkNodeStatus("c1", withError, stage("analyze", "failed"))).toBe("failed");
  });

  it("is pending when aggregate analyze stage record is missing entirely", () => {
    expect(chunkNodeStatus("c1", t, undefined)).toBe("pending");
  });
});

describe("tokensForStage", () => {
  it("sums prompt+completion tokens across llm_rounds matching the stage label", () => {
    const t = trace({
      llm_rounds: [
        {
          stage: "scout",
          seq: 1,
          prompt_tokens: 10,
          completion_tokens: 5,
          latency_ms: 1,
          error: null,
        },
        {
          stage: "scout",
          seq: 2,
          prompt_tokens: 7,
          completion_tokens: 3,
          latency_ms: 1,
          error: null,
        },
        {
          stage: "verify",
          seq: 1,
          prompt_tokens: 100,
          completion_tokens: 100,
          latency_ms: 1,
          error: null,
        },
      ],
    });
    expect(tokensForStage("scout", t)).toBe(25);
  });

  it("treats null prompt/completion tokens as 0", () => {
    const t = trace({
      llm_rounds: [
        {
          stage: "scout",
          seq: 1,
          prompt_tokens: null,
          completion_tokens: 5,
          latency_ms: 1,
          error: null,
        },
      ],
    });
    expect(tokensForStage("scout", t)).toBe(5);
  });

  it("returns null when the trace has no rows for the label", () => {
    const t = trace({
      llm_rounds: [
        {
          stage: "verify",
          seq: 1,
          prompt_tokens: 1,
          completion_tokens: 1,
          latency_ms: 1,
          error: null,
        },
      ],
    });
    expect(tokensForStage("scout", t)).toBeNull();
  });

  it("returns null for a null trace", () => {
    expect(tokensForStage("scout", null)).toBeNull();
  });
});

describe("buildGraphNodes", () => {
  const stages: ReviewStage[] = [
    stage("scout", "done"),
    stage("analyze", "running"),
    stage("verify", "pending"),
  ];
  const t = trace({
    tool_calls: [
      { stage: "analyze:c1", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
      { stage: "analyze:c2", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
    ],
  });

  it("builds scout, chunk nodes, verify, publish in layer order", () => {
    const nodes = buildGraphNodes(stages, t);
    expect(nodes.map((n) => n.id)).toEqual([
      "scout",
      "analyze:c1",
      "analyze:c2",
      "verify",
      "publish",
    ]);
    expect(nodes.find((n) => n.id === "scout")!.layer).toBe(0);
    expect(nodes.find((n) => n.id === "analyze:c1")!.layer).toBe(1);
    expect(nodes.find((n) => n.id === "verify")!.layer).toBe(2);
    expect(nodes.find((n) => n.id === "publish")!.layer).toBe(3);
  });

  it("chunk nodes have kind 'chunk', label as bare chunk id, and null durationMs", () => {
    const nodes = buildGraphNodes(stages, t);
    const chunk = nodes.find((n) => n.id === "analyze:c1")!;
    expect(chunk.kind).toBe("chunk");
    expect(chunk.label).toBe("c1");
    expect(chunk.durationMs).toBeNull();
  });

  it("defaults verify/publish to pending when absent from stages", () => {
    const noVerify: ReviewStage[] = [stage("scout", "done")];
    const nodes = buildGraphNodes(noVerify, trace());
    expect(nodes.find((n) => n.id === "verify")!.status).toBe("pending");
    expect(nodes.find((n) => n.id === "publish")!.status).toBe("pending");
  });

  it("uses stage status when verify/publish are present in stages", () => {
    const withPublish: ReviewStage[] = [...stages, stage("publish", "done")];
    const nodes = buildGraphNodes(withPublish, t);
    expect(nodes.find((n) => n.id === "publish")!.status).toBe("done");
  });

  it("derives a dynamically-named agent stage (e.g. security-reviewer) as an agent node in layer 1 (the bug fix)", () => {
    const withAgent: ReviewStage[] = [
      ...stages,
      stage("security-reviewer", "done", {
        started_at: "2024-01-01T00:00:00Z",
        finished_at: "2024-01-01T00:00:05Z",
      }),
    ];
    const nodes = buildGraphNodes(withAgent, t);
    const agent = nodes.find((n) => n.id === "security-reviewer");
    expect(agent).toBeDefined();
    expect(agent!.kind).toBe("agent");
    expect(agent!.layer).toBe(1);
    expect(agent!.status).toBe("done");
    expect(agent!.durationMs).toBe(5000);
  });

  it("treats a 'design' stage as an ordinary agent node, not special-cased", () => {
    const withDesign: ReviewStage[] = [...stages, stage("design", "done")];
    const nodes = buildGraphNodes(withDesign, t);
    const design = nodes.find((n) => n.id === "design");
    expect(design).toBeDefined();
    expect(design!.kind).toBe("agent");
    expect(design!.layer).toBe(1);
    expect(design!.status).toBe("done");
  });

  it("renders a chunk-scoped specialist stage (e.g. security:c1) as an agent node with a friendly label", () => {
    const withSpecialist: ReviewStage[] = [
      ...stages,
      stage("security:c1", "done", {
        started_at: "2024-01-01T00:00:00Z",
        finished_at: "2024-01-01T00:00:03Z",
      }),
    ];
    const nodes = buildGraphNodes(withSpecialist, t);
    const specialist = nodes.find((n) => n.id === "security:c1");
    expect(specialist).toBeDefined();
    expect(specialist!.kind).toBe("agent");
    expect(specialist!.label).toBe("security (c1)");
    expect(specialist!.layer).toBe(1);
    expect(specialist!.status).toBe("done");
    expect(specialist!.durationMs).toBe(3000);
  });

  it("still renders an agent node when it has trace activity but no ReviewStage row " +
    "(regression for review b9df9951: platform-reviewer hit the recursion " +
    "limit before _record_stage ran, and was invisible on the UI)", () => {
    const noAgentRow: ReviewStage[] = [...stages]; // no "platform-reviewer" row
    const withAgentTrace = trace({
      tool_calls: t.tool_calls,
      llm_rounds: [
        {
          stage: "platform-reviewer",
          seq: 1,
          prompt_tokens: 5000,
          completion_tokens: 100,
          latency_ms: 10,
          error: "Recursion limit of 24 reached",
        },
      ],
    });
    const nodes = buildGraphNodes(noAgentRow, withAgentTrace);
    const agent = nodes.find((n) => n.id === "platform-reviewer");
    expect(agent).toBeDefined();
    expect(agent!.kind).toBe("agent");
    expect(agent!.layer).toBe(1);
    expect(agent!.status).toBe("failed");
    expect(agent!.durationMs).toBeNull(); // no stage row -> no started_at/finished_at
    expect(agent!.tokens).toBe(5100);
  });

  it("derives agent duration as null when the stage is running with no finished_at", () => {
    const withRunningAgent: ReviewStage[] = [
      ...stages,
      stage("design", "running", { started_at: "2024-01-01T00:00:00Z", finished_at: null }),
    ];
    const nodes = buildGraphNodes(withRunningAgent, t);
    expect(nodes.find((n) => n.id === "design")!.durationMs).toBeNull();
  });

  it("derives scout duration from started_at/finished_at", () => {
    const withTimes: ReviewStage[] = [
      stage("scout", "done", {
        started_at: "2024-01-01T00:00:00Z",
        finished_at: "2024-01-01T00:00:02Z",
      }),
      stage("verify", "pending"),
    ];
    const nodes = buildGraphNodes(withTimes, trace());
    expect(nodes.find((n) => n.id === "scout")!.durationMs).toBe(2000);
  });

  it("computes tokens per node from llm_rounds matching the node's stage label", () => {
    const withTokens: ReviewStage[] = [stage("scout", "done"), stage("verify", "pending")];
    const tt = trace({
      llm_rounds: [
        {
          stage: "scout",
          seq: 1,
          prompt_tokens: 4,
          completion_tokens: 6,
          latency_ms: 1,
          error: null,
        },
      ],
    });
    const nodes = buildGraphNodes(withTokens, tt);
    expect(nodes.find((n) => n.id === "scout")!.tokens).toBe(10);
    expect(nodes.find((n) => n.id === "verify")!.tokens).toBeNull();
  });
});

describe("filterTraceForNode", () => {
  it("returns only rows matching the exact stage", () => {
    const t: Trace = {
      tool_calls: [
        { stage: "analyze:c1", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
        { stage: "analyze:c2", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
      ],
      llm_rounds: [
        {
          stage: "analyze:c1",
          seq: 1,
          prompt_tokens: 1,
          completion_tokens: 1,
          latency_ms: 1,
          error: null,
        },
      ],
    };
    const filtered = filterTraceForNode("analyze:c1", t);
    expect(filtered.toolCalls).toHaveLength(1);
    expect(filtered.toolCalls[0].stage).toBe("analyze:c1");
    expect(filtered.llmRounds).toHaveLength(1);
  });

  it("returns empty arrays for null trace", () => {
    expect(filterTraceForNode("scout", null)).toEqual({ toolCalls: [], llmRounds: [] });
  });
});

describe("buildTimeline", () => {
  it("returns an empty array when there are no rounds and no tool calls", () => {
    expect(buildTimeline([], [])).toEqual([]);
  });

  it("groups the exact bug-report interleaving: round(seq1) -> tools(seq2,3,4) -> round(seq5)", () => {
    const rounds: TraceLLMRound[] = [
      { stage: "analyze:c2", seq: 1, prompt_tokens: 10, completion_tokens: 5, latency_ms: 100, error: null },
      { stage: "analyze:c2", seq: 5, prompt_tokens: 20, completion_tokens: 8, latency_ms: 200, error: null },
    ];
    const toolCalls: TraceToolCall[] = [
      { stage: "analyze:c2", seq: 2, tool: "read_file", status: "done", duration_ms: 10 },
      { stage: "analyze:c2", seq: 3, tool: "grep_symbol", status: "done", duration_ms: 20 },
      { stage: "analyze:c2", seq: 4, tool: "run_lint", status: "error", duration_ms: 30 },
    ];
    const groups = buildTimeline(toolCalls, rounds);
    expect(groups).toHaveLength(2);
    expect(groups[0].round).toEqual(rounds[0]);
    expect(groups[0].toolCalls.map((tc) => tc.seq)).toEqual([2, 3, 4]);
    expect(groups[1].round).toEqual(rounds[1]);
    expect(groups[1].toolCalls).toEqual([]);
  });

  it("attaches tool calls with a lower seq than the first round to that first round", () => {
    const rounds: TraceLLMRound[] = [
      { stage: "scout", seq: 4, prompt_tokens: 1, completion_tokens: 1, latency_ms: 1, error: null },
    ];
    const toolCalls: TraceToolCall[] = [
      { stage: "scout", seq: 1, tool: "list_files", status: "done", duration_ms: 5 },
      { stage: "scout", seq: 2, tool: "read_file", status: "done", duration_ms: 5 },
    ];
    const groups = buildTimeline(toolCalls, rounds);
    expect(groups).toHaveLength(1);
    expect(groups[0].round).toEqual(rounds[0]);
    expect(groups[0].toolCalls.map((tc) => tc.seq)).toEqual([1, 2]);
  });

  it("emits a single round:null group when there are tool calls but no rounds at all yet", () => {
    const toolCalls: TraceToolCall[] = [
      { stage: "scout", seq: 1, tool: "list_files", status: "done", duration_ms: 5 },
      { stage: "scout", seq: 2, tool: "read_file", status: "done", duration_ms: 5 },
    ];
    const groups = buildTimeline(toolCalls, []);
    expect(groups).toHaveLength(1);
    expect(groups[0].round).toBeNull();
    expect(groups[0].toolCalls.map((tc) => tc.seq)).toEqual([1, 2]);
  });

  it("gives a round with an error and no tool calls an empty toolCalls array", () => {
    const rounds: TraceLLMRound[] = [
      { stage: "analyze:c2", seq: 8, prompt_tokens: null, completion_tokens: null, latency_ms: null, error: "rate_limit_error" },
    ];
    const groups = buildTimeline([], rounds);
    expect(groups).toHaveLength(1);
    expect(groups[0].round!.error).toBe("rate_limit_error");
    expect(groups[0].toolCalls).toEqual([]);
  });

  it("orders multiple rounds by seq regardless of input array order", () => {
    const rounds: TraceLLMRound[] = [
      { stage: "s", seq: 5, prompt_tokens: 1, completion_tokens: 1, latency_ms: 1, error: null },
      { stage: "s", seq: 1, prompt_tokens: 1, completion_tokens: 1, latency_ms: 1, error: null },
    ];
    const groups = buildTimeline([], rounds);
    expect(groups.map((g) => g.round!.seq)).toEqual([1, 5]);
  });
});

describe("aggregateSummary", () => {
  it("counts total tool calls and llm rounds and status breakdown", () => {
    const stages: ReviewStage[] = [
      stage("scout", "done"),
      stage("analyze", "done"),
      stage("verify", "running"),
    ];
    const t: Trace = {
      tool_calls: [
        { stage: "analyze:c1", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
      ],
      llm_rounds: [
        {
          stage: "scout",
          seq: 1,
          prompt_tokens: 1,
          completion_tokens: 1,
          latency_ms: 1,
          error: null,
        },
      ],
    };
    const summary = aggregateSummary(stages, t);
    expect(summary.toolCallCount).toBe(1);
    expect(summary.llmRoundCount).toBe(1);
    expect(summary.statusCounts.done).toBe(2); // scout, analyze:c1
    expect(summary.statusCounts.running).toBe(1); // verify
    expect(summary.statusCounts.pending).toBe(1); // publish
    expect(summary.statusCounts.failed).toBe(0);
  });

  it("counts agent nodes too (bug fix: previously uncounted)", () => {
    const stages: ReviewStage[] = [
      stage("scout", "done"),
      stage("design", "done"),
      stage("verify", "pending"),
    ];
    const summary = aggregateSummary(stages, trace());
    // scout(done) + design(done) + verify(pending) + publish(pending) = 4 nodes total
    expect(summary.statusCounts.done).toBe(2);
    expect(summary.statusCounts.pending).toBe(2);
  });
});

describe("toElkGraph", () => {
  const stages: ReviewStage[] = [stage("scout", "done"), stage("verify", "pending")];

  it("produces the expected root shape with layoutOptions", () => {
    const nodes = buildGraphNodes(stages, trace());
    const elk = toElkGraph(nodes);
    expect(elk.id).toBe("root");
    expect(elk.layoutOptions).toEqual({
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "60",
      "elk.spacing.nodeNode": "18",
    });
  });

  it("produces one elk child per node with fixed width/height", () => {
    // These must stay in step with .pl-node in styles.css and NODE_HEIGHT in
    // PipelineGraph.tsx: the node has overflow:hidden, so a layout box shorter
    // than the rendered content silently clips it (which is how the candidate
    // yield line came to be invisible).
    const nodes = buildGraphNodes(stages, trace());
    const elk = toElkGraph(nodes);
    expect(elk.children).toHaveLength(nodes.length);
    for (const child of elk.children) {
      expect(child.width).toBe(200);
      expect(child.height).toBe(82);
    }
    expect(elk.children.map((c) => c.id)).toEqual(nodes.map((n) => n.id));
  });

  it("connects scout directly to verify when layer 1 is empty", () => {
    const nodes = buildGraphNodes(stages, trace());
    const elk = toElkGraph(nodes);
    expect(elk.edges).toContainEqual({
      id: "e:scout:verify",
      sources: ["scout"],
      targets: ["verify"],
    });
    expect(elk.edges).toContainEqual({
      id: "e:verify:publish",
      sources: ["verify"],
      targets: ["publish"],
    });
  });

  it("connects scout to each layer-1 node and each layer-1 node to verify when layer 1 is non-empty", () => {
    const withAgent: ReviewStage[] = [...stages, stage("design", "done")];
    const nodes = buildGraphNodes(withAgent, trace());
    const elk = toElkGraph(nodes);
    expect(elk.edges).toContainEqual({
      id: "e:scout:design",
      sources: ["scout"],
      targets: ["design"],
    });
    expect(elk.edges).toContainEqual({
      id: "e:design:verify",
      sources: ["design"],
      targets: ["verify"],
    });
    // no direct scout->verify edge when layer 1 is non-empty
    expect(elk.edges).not.toContainEqual({
      id: "e:scout:verify",
      sources: ["scout"],
      targets: ["verify"],
    });
  });

  it("connects scout to each chunk node and each chunk node to verify (multiple layer-1 nodes)", () => {
    const t = trace({
      tool_calls: [
        { stage: "analyze:c1", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
        { stage: "analyze:c2", seq: 1, tool: "get_hunk", status: "done", duration_ms: 10 },
      ],
    });
    const nodes = buildGraphNodes(stages, t);
    const elk = toElkGraph(nodes);
    expect(elk.edges).toContainEqual({
      id: "e:scout:analyze:c1",
      sources: ["scout"],
      targets: ["analyze:c1"],
    });
    expect(elk.edges).toContainEqual({
      id: "e:scout:analyze:c2",
      sources: ["scout"],
      targets: ["analyze:c2"],
    });
    expect(elk.edges).toContainEqual({
      id: "e:analyze:c1:verify",
      sources: ["analyze:c1"],
      targets: ["verify"],
    });
    expect(elk.edges).toContainEqual({
      id: "e:analyze:c2:verify",
      sources: ["analyze:c2"],
      targets: ["verify"],
    });
  });
});

describe("groupBounds", () => {
  it("returns null for zero chunk nodes", () => {
    expect(groupBounds([])).toBeNull();
  });

  it("returns the padded bounding box of chunk node positions+dimensions", () => {
    const positioned: PositionedNode[] = [
      { id: "analyze:c1", x: 100, y: 100, width: 200, height: 64 },
      { id: "analyze:c2", x: 100, y: 200, width: 200, height: 64 },
    ];
    const bounds = groupBounds(positioned);
    // raw bbox: x in [100, 300] (width 200), y in [100, 264] (height 164)
    // padded by 16px on every side: origin shifts by -16, each dimension grows by 32
    expect(bounds).toEqual({ x: 84, y: 84, width: 232, height: 196 });
  });
});

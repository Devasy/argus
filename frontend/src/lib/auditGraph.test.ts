import { describe, expect, it } from "vitest";
import type { AuditStage } from "../api/types";
import { buildAuditGraphNodes } from "./auditGraph";
import { toElkGraph } from "./pipelineGraph";

const st = (stage_name: string, status: AuditStage["status"] = "done"): AuditStage =>
  ({ stage_name, status, artifact: null, error: null, started_at: "2026-09-29T00:00:00Z",
     finished_at: status === "running" ? null : "2026-09-29T00:01:00Z" });

describe("buildAuditGraphNodes", () => {
  it("lays out pick → scout → ground fan-out → investigate → relate → verify → apply", () => {
    const nodes = buildAuditGraphNodes([
      st("pick"), st("scout"), st("ground:g1"), st("ground:g2", "running"),
      st("ground:g1:investigate:1"), st("relate:k1"), st("verify"), st("apply"),
    ]);
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
    expect(byId["ground:g2"].status).toBe("running");
    expect(byId["ground:g1:investigate:1"].kind).toBe("investigate");
    expect(byId["apply"].layer).toBeGreaterThan(byId["verify"].layer);
    const edges = toElkGraph(nodes).edges.map((e) => e.id);
    expect(edges).toContain("e:scout:ground:g1");
    expect(edges).toContain("e:scout:ground:g2");
    expect(edges).toContain("e:ground:g1:ground:g1:investigate:1");
    expect(edges).toContain("e:ground:g1:investigate:1:relate:k1");
    expect(edges).toContain("e:ground:g2:relate:k1");
    expect(edges).toContain("e:relate:k1:verify");
    expect(edges).toContain("e:verify:apply");
    // Ensure no false cross-group dependency: ground:g2 does NOT connect to g1's investigation
    expect(edges).not.toContain("e:ground:g2:ground:g1:investigate:1");
    expect(edges).not.toContain("e:pick:apply");
  });

  it("folds a retry into its parent ground node", () => {
    const nodes = buildAuditGraphNodes([st("ground:g1"), st("ground:g1:retry", "failed")]);
    expect(nodes.filter((n) => n.id.startsWith("ground:g1")).map((n) => n.id)).toEqual(["ground:g1"]);
  });
});

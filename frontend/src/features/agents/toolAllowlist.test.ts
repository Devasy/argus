import { describe, expect, it } from "vitest";
import { addCustomTool, normalizeAllowlist, toggleTool } from "./toolAllowlist";

describe("toggleTool", () => {
  it("adds a tool that is not yet selected", () => {
    expect(toggleTool(["get_hunk"], "file_knowledge")).toEqual(["get_hunk", "file_knowledge"]);
  });

  it("removes a tool that is already selected", () => {
    expect(toggleTool(["get_hunk", "file_knowledge"], "get_hunk")).toEqual(["file_knowledge"]);
  });

  it("adding to an empty list yields a single-item list", () => {
    expect(toggleTool([], "graph_impact")).toEqual(["graph_impact"]);
  });

  it("removing the last item yields an empty list", () => {
    expect(toggleTool(["graph_impact"], "graph_impact")).toEqual([]);
  });
});

describe("addCustomTool", () => {
  it("appends a trimmed, non-empty custom name not already present", () => {
    expect(addCustomTool(["get_hunk"], "  my_custom_tool  ")).toEqual([
      "get_hunk",
      "my_custom_tool",
    ]);
  });

  it("ignores blank input", () => {
    expect(addCustomTool(["get_hunk"], "   ")).toEqual(["get_hunk"]);
  });

  it("does not duplicate a name already in the list", () => {
    expect(addCustomTool(["get_hunk"], "get_hunk")).toEqual(["get_hunk"]);
  });
});

describe("normalizeAllowlist", () => {
  it("trims whitespace-padded entries", () => {
    expect(normalizeAllowlist(["  get_hunk ", "graph_impact"])).toEqual([
      "get_hunk",
      "graph_impact",
    ]);
  });

  it("drops duplicates, including ones that only match after trimming", () => {
    expect(normalizeAllowlist(["get_hunk", " get_hunk", "get_hunk"])).toEqual(["get_hunk"]);
  });

  it("drops empty and whitespace-only entries", () => {
    expect(normalizeAllowlist(["", "   ", "file_knowledge"])).toEqual(["file_knowledge"]);
  });
});

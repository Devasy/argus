import { describe, expect, it } from "vitest";
import { diffLines } from "./textDiff";

describe("diffLines", () => {
  it("returns all same lines when inputs are identical", () => {
    const result = diffLines("a\nb\nc", "a\nb\nc");
    expect(result).toEqual([
      { kind: "same", text: "a" },
      { kind: "same", text: "b" },
      { kind: "same", text: "c" },
    ]);
  });

  it("marks an appended line as added", () => {
    const result = diffLines("a\nb", "a\nb\nc");
    expect(result).toEqual([
      { kind: "same", text: "a" },
      { kind: "same", text: "b" },
      { kind: "added", text: "c" },
    ]);
  });

  it("marks a removed line as removed", () => {
    const result = diffLines("a\nb\nc", "a\nc");
    expect(result).toEqual([
      { kind: "same", text: "a" },
      { kind: "removed", text: "b" },
      { kind: "same", text: "c" },
    ]);
  });

  it("marks a changed line as a removal followed by an addition", () => {
    const result = diffLines("a\nb\nc", "a\nx\nc");
    expect(result).toEqual([
      { kind: "same", text: "a" },
      { kind: "removed", text: "b" },
      { kind: "added", text: "x" },
      { kind: "same", text: "c" },
    ]);
  });

  it("handles empty-to-nonempty", () => {
    const result = diffLines("", "a\nb");
    expect(result).toEqual([
      { kind: "removed", text: "" },
      { kind: "added", text: "a" },
      { kind: "added", text: "b" },
    ]);
  });
});

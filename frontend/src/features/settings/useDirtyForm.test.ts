import { describe, expect, it } from "vitest";
import { computeDirty } from "./useDirtyForm";

describe("computeDirty", () => {
  it("reports keys whose draft differs from initial", () => {
    expect(computeDirty({ a: 1, b: "x" }, { a: 2, b: "x" })).toEqual(["a"]);
  });
  it("is empty when drafts match", () => {
    expect(computeDirty({ a: 1 }, { a: 1 })).toEqual([]);
  });
  it("treats type-preserving numeric edits correctly", () => {
    expect(computeDirty({ n: 500000 }, { n: 100000 })).toEqual(["n"]);
  });
});

import { describe, expect, it } from "vitest";
import {
  barHeights,
  fmtCompact,
  fmtDayLabel,
  fmtPct,
  groupConsecutiveActivity,
  isErrorActivity,
} from "./format";

describe("dashboard format helpers", () => {
  it("formats percentages with null as em-dash", () => {
    expect(fmtPct(null)).toBe("—");
    expect(fmtPct(0.714)).toBe("71%");
    expect(fmtPct(0)).toBe("0%");
    expect(fmtPct(1)).toBe("100%");
  });

  it("compacts large numbers", () => {
    expect(fmtCompact(0)).toBe("0");
    expect(fmtCompact(950)).toBe("950");
    expect(fmtCompact(33_400)).toBe("33.4k");
    expect(fmtCompact(4_200_000)).toBe("4.2M");
  });

  it("scales bars to the max day with a 4% floor", () => {
    expect(barHeights([{ count: 0 }, { count: 5 }, { count: 10 }])).toEqual([4, 50, 100]);
  });

  it("scales bars to a 4% floor even when every day is zero", () => {
    expect(barHeights([{ count: 0 }, { count: 0 }])).toEqual([4, 4]);
  });

  it("handles an empty day list without dividing by zero", () => {
    expect(barHeights([])).toEqual([]);
  });

  it("formats an ISO date as a short weekday+day label", () => {
    expect(fmtDayLabel("2026-07-24")).toBe(fmtDayLabel("2026-07-24"));
    expect(fmtDayLabel("2026-07-24")).toMatch(/\d+/);
  });

  it("falls back to the raw string for an unparsable date", () => {
    expect(fmtDayLabel("not-a-date")).toBe("not-a-date");
  });

  it("flags failure-shaped activity by title or detail text", () => {
    expect(isErrorActivity({ title: "Learning run failed", detail: "litellm.InternalServerError" })).toBe(true);
    expect(isErrorActivity({ title: "Review completed", detail: null })).toBe(false);
    expect(isErrorActivity({ title: "Review errored", detail: null })).toBe(true);
    expect(isErrorActivity({ title: "Something", detail: "Connection exception occurred" })).toBe(true);
  });

  it("collapses consecutive identical type+title activity entries with a count", () => {
    const mk = (title: string, at: string) => ({
      type: "distillation",
      title,
      detail: "litellm.InternalServerError",
      at,
      href_id: null,
    });
    const items = [
      mk("Learning run failed", "2026-07-30T03:00:00Z"),
      mk("Learning run failed", "2026-07-30T02:00:00Z"),
      mk("Learning run failed", "2026-07-30T01:00:00Z"),
      { type: "verdict", title: "agent: accepted", detail: null, at: "2026-07-29T23:00:00Z", href_id: "1" },
      mk("Learning run failed", "2026-07-29T20:00:00Z"),
    ];
    const grouped = groupConsecutiveActivity(items);
    expect(grouped).toHaveLength(3);
    expect(grouped[0].count).toBe(3);
    expect(grouped[0].item.at).toBe("2026-07-30T03:00:00Z");
    expect(grouped[1].count).toBe(1);
    expect(grouped[2].count).toBe(1);
  });

  it("leaves distinct activity entries ungrouped", () => {
    const items = [
      { type: "verdict", title: "a", detail: null, at: "t1", href_id: null },
      { type: "verdict", title: "b", detail: null, at: "t2", href_id: null },
    ];
    expect(groupConsecutiveActivity(items)).toHaveLength(2);
  });
});

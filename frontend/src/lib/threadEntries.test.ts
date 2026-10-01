import { describe, expect, it } from "vitest";
import type { Thread } from "../api/types";
import { threadEntries } from "./threadEntries";

const thread: Thread = {
  discussion_id: "d1",
  resolved: false,
  anchor: "a.py:3",
  has_bot_comment: true,
  disposition: "open",
  verdict: null,
  verdict_reason: null,
  gitlab_url: null,
  notes: [
    {
      id: "n1",
      author_username: "bot",
      author_type: "bot",
      kind: "inline",
      body: "x",
      created_at: "2026-01-01T12:00:00Z",
      depth: 0,
      disposition: "open",
    },
    {
      id: "n2",
      author_username: "dev",
      author_type: "human",
      kind: "inline",
      body: "y",
      created_at: "2026-01-01T12:40:00Z",
      depth: 1,
      disposition: null,
    },
  ],
  events: [
    { at: "2026-01-01T12:20:00Z", kind: "line_changed", text: "the commented line changed" },
  ],
};

describe("threadEntries", () => {
  it("interleaves code events between notes by time", () => {
    const kinds = threadEntries(thread).map((e) => ("note" in e ? e.note.id : e.event.kind));
    expect(kinds).toEqual(["n1", "line_changed", "n2"]);
  });
});

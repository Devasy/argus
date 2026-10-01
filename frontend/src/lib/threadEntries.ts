import type { CodeEvent, Thread, ThreadNote } from "../api/types";

export type ThreadEntry = { at: string; note: ThreadNote } | { at: string; event: CodeEvent };

// Notes and code events share one timeline; the sort is stable so ties keep API order.
export function threadEntries(thread: Thread): ThreadEntry[] {
  const entries: ThreadEntry[] = [
    ...thread.notes.map((note) => ({ at: note.created_at ?? "", note })),
    ...thread.events.map((event) => ({ at: event.at, event })),
  ];
  return entries.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
}

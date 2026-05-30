/** Format a 0..1 rate as a rounded percentage, or an em-dash when there's no data yet. */
export function fmtPct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate * 100)}%`;
}

/** Compact large counts (1,284 -> "1.3k", 4,200,000 -> "4.2M"); small numbers pass through. */
export function fmtCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** Scale per-day counts to bar heights (0..100), with a 4% floor so zero-count days stay visible. */
export function barHeights(days: { count: number }[]): number[] {
  const max = Math.max(1, ...days.map((d) => d.count));
  return days.map((d) => Math.max(4, Math.round((d.count / max) * 100)));
}

/** Format an ISO date string as a short weekday+day label for chart axes/tooltips ("Wed 24"). */
export function fmtDayLabel(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
}

interface ActivityLike {
  type: string;
  title: string;
  detail: string | null;
  at: string;
  href_id: string | null;
}

/** An activity entry that reads as a failure/error (e.g. a failed distillation run). */
export function isErrorActivity(item: Pick<ActivityLike, "title" | "detail">): boolean {
  const haystack = `${item.title} ${item.detail ?? ""}`.toLowerCase();
  return /\bfail|\berror|exception/.test(haystack);
}

export interface GroupedActivity<T extends ActivityLike = ActivityLike> {
  item: T;
  count: number;
}

/**
 * Collapse consecutive activity entries that share the same type+title (e.g. repeated
 * "Learning run failed" rows differing only by timestamp) into one row carrying a count,
 * keeping the most recent occurrence's timestamp/detail. Non-consecutive repeats (with
 * other activity interleaved) are left distinct, since collapsing across the whole list
 * would misrepresent when each burst happened.
 */
export function groupConsecutiveActivity<T extends ActivityLike>(items: T[]): GroupedActivity<T>[] {
  const grouped: GroupedActivity<T>[] = [];
  for (const item of items) {
    const prev = grouped[grouped.length - 1];
    if (prev && prev.item.type === item.type && prev.item.title === item.title) {
      prev.count += 1;
    } else {
      grouped.push({ item, count: 1 });
    }
  }
  return grouped;
}

// The canonical tool list used to be hardcoded here and went stale the
// moment a new tool (outline_file, search_code) was added to the pipeline
// without a matching entry -- it had no chip in the editor and was invisible
// in the UI even though every agent received it anyway. The list now comes
// from GET /agents/available-tools (useAvailableTools in api/queries.ts),
// generated server-side from the pipeline's own registered tools, so adding
// a tool there needs no frontend change to become visible here.

/** Trim, drop empties, and de-duplicate allowlist entries loaded from the
 * API — legacy data written via the old comma-separated input or direct API
 * calls may contain padded or duplicate names. */
export function normalizeAllowlist(entries: string[]): string[] {
  return Array.from(new Set(entries.map((e) => e.trim()).filter(Boolean)));
}

/** Toggle membership of `tool` in `selected`, returning a new array. */
export function toggleTool(selected: string[], tool: string): string[] {
  return selected.includes(tool) ? selected.filter((t) => t !== tool) : [...selected, tool];
}

/** Append a trimmed custom tool name if non-empty and not already present. */
export function addCustomTool(selected: string[], name: string): string[] {
  const trimmed = name.trim();
  if (!trimmed || selected.includes(trimmed)) return selected;
  return [...selected, trimmed];
}

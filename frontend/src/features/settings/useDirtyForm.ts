import { useCallback, useState } from "react";

type V = string | number | boolean;

export function computeDirty(initial: Record<string, V>, draft: Record<string, V>): string[] {
  return Object.keys(draft)
    .filter((k) => draft[k] !== initial[k])
    .sort();
}

export function useDirtyForm(initial: Record<string, V>) {
  const [base, setBase] = useState(initial);
  const [draft, setDraft] = useState(initial);
  const set = useCallback((key: string, value: V) => setDraft((d) => ({ ...d, [key]: value })), []);
  const reset = useCallback(() => setDraft(base), [base]);
  const rebase = useCallback((next: Record<string, V>) => {
    setBase(next);
    setDraft(next);
  }, []);
  return { draft, set, dirtyKeys: computeDirty(base, draft), reset, rebase };
}

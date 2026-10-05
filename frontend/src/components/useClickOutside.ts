import { useEffect, useRef } from "react";

export function useClickOutside(
  ref: React.RefObject<HTMLElement | null>, onOutside: () => void, enabled = true,
) {
  const callback = useRef(onOutside);
  useEffect(() => { callback.current = onOutside; }, [onOutside]);
  useEffect(() => {
    if (!enabled) return;
    function handle(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) callback.current();
    }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, [ref, enabled]);
}

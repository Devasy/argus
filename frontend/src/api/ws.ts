import { getToken } from "./client";
import type { ReviewStage, ReviewStatus } from "./types";

export interface ActiveLlmCall {
  stage: string | null;
  elapsed_s: number | null;
}

export interface ReviewSnapshot {
  status: ReviewStatus;
  stages: ReviewStage[];
  tool_calls: number;
  llm_rounds: number;
  active_llm_call: ActiveLlmCall | null;
}

export const TERMINAL_STATUSES: ReviewStatus[] = ["done", "failed", "canceled"];

/**
 * Opens the live review stream. Calls onSnapshot for each server snapshot.
 * If the socket errors or closes BEFORE a terminal status was seen, calls
 * onFallback once — the caller should switch to polling. Returns close().
 */
export function openReviewStream(
  reviewId: string,
  onSnapshot: (snap: ReviewSnapshot) => void,
  onFallback: () => void,
): () => void {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const token = getToken();
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";
  const url = `${proto}://${window.location.host}/ws/reviews/${reviewId}${qs}`;

  let closedByCaller = false;
  let sawTerminal = false;
  let fellBack = false;
  const sock = new WebSocket(url);

  const fallBack = () => {
    if (!closedByCaller && !sawTerminal && !fellBack) {
      fellBack = true;
      onFallback();
    }
  };

  sock.onmessage = (ev) => {
    let snap: ReviewSnapshot;
    try {
      snap = JSON.parse(ev.data as string) as ReviewSnapshot;
    } catch {
      return;
    }
    if (TERMINAL_STATUSES.includes(snap.status)) sawTerminal = true;
    onSnapshot(snap);
  };
  sock.onerror = fallBack;
  sock.onclose = fallBack;

  return () => {
    closedByCaller = true;
    sock.close();
  };
}

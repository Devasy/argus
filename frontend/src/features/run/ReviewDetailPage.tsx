import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { queryKeys, useCancelReview, useReview, useTrace } from "../../api/queries";
import type { Review } from "../../api/types";
import {
  openReviewStream,
  TERMINAL_STATUSES,
  type ActiveLlmCall,
  type ReviewSnapshot,
} from "../../api/ws";
import BenchmarkBadge from "../../components/BenchmarkBadge";
import ErrorBox from "../../components/ErrorBox";
import NodeFindings from "../../components/NodeFindings";
import PipelineGraph from "../../components/PipelineGraph";
import StatusBadge from "../../components/StatusBadge";
import TraceTimeline from "../../components/TraceTimeline";
import {
  aggregateSummary,
  buildGraphNodes,
  filterTraceForNode,
  fmtDuration,
  fmtTokens,
} from "../../lib/pipelineGraph";

type ReviewTab = "pipeline" | "summary";

export default function ReviewDetail() {
  const { reviewId } = useParams<{ reviewId: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const review = useReview(reviewId!);
  const trace = useTrace(reviewId!);
  const cancelReview = useCancelReview();
  const [live, setLive] = useState<Review | null>(null);
  const [activeLlmCall, setActiveLlmCall] = useState<ActiveLlmCall | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [rerunSubmitting, setRerunSubmitting] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<ReviewTab>("pipeline");
  const countsRef = useRef({ tools: -1, rounds: -1 });

  const current = live ?? review.data ?? null;
  const terminal = current != null && TERMINAL_STATUSES.includes(current.status);

  // Live updates: WS with polling fallback, until terminal.
  const hasReview = review.data != null;
  useEffect(() => {
    if (!reviewId || !hasReview || terminal) return;

    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let disposed = false;

    const refreshAll = async () => {
      try {
        const r = await api.review(reviewId);
        if (disposed) return;
        setLive(r);
        void qc.invalidateQueries({ queryKey: queryKeys.review(reviewId) });
        void qc.invalidateQueries({ queryKey: queryKeys.reviewTrace(reviewId) });
        if (TERMINAL_STATUSES.includes(r.status) && pollTimer) {
          clearInterval(pollTimer);
          pollTimer = null;
        }
      } catch {
        /* transient poll failure — keep trying until terminal */
      }
    };

    const onSnapshot = (snap: ReviewSnapshot) => {
      const changed =
        snap.tool_calls !== countsRef.current.tools || snap.llm_rounds !== countsRef.current.rounds;
      countsRef.current = { tools: snap.tool_calls, rounds: snap.llm_rounds };

      if (TERMINAL_STATUSES.includes(snap.status)) {
        // Don't publish the interim status-only snapshot here: doing so would flip
        // `terminal` to true on the next render, tearing down this effect (and setting
        // `disposed = true`) before refreshAll()'s network round-trip resolves, which
        // would discard the real summary/error. Instead, go straight to the full
        // refetch; its own setLive(r) carries the terminal status AND the real
        // summary/error together, atomically.
        setActiveLlmCall(null);
        void refreshAll();
        return;
      }

      setActiveLlmCall(snap.active_llm_call);
      setLive((prev) => ({
        ...(prev ?? review.data!),
        id: reviewId,
        status: snap.status,
        summary: prev?.summary ?? review.data!.summary,
        error: prev?.error ?? review.data!.error,
        stages: snap.stages,
      }));
      if (changed) {
        void refreshAll();
      }
    };

    const close = openReviewStream(reviewId, onSnapshot, () => {
      if (!disposed && pollTimer == null) {
        pollTimer = setInterval(refreshAll, 5000);
      }
    });

    return () => {
      disposed = true;
      close();
      if (pollTimer) clearInterval(pollTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reviewId, hasReview, terminal]);

  // Elapsed-time ticker: re-renders once a second while non-terminal so the
  // toolbar's elapsed display keeps advancing; stops (and is cleaned up) once
  // terminal, since `finished_at` freezes the elapsed value from then on.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (terminal) return;
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [terminal]);

  const nodes = useMemo(
    () => buildGraphNodes(current?.stages ?? [], trace.data ?? null),
    [current, trace.data],
  );

  const doRerun = async () => {
    if (current == null) return;
    setRerunSubmitting(true);
    setRerunError(null);
    try {
      const result = await api.triggerReview(current.mr_id, {});
      navigate(`/reviews/${result.review_id}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setRerunError("a review for this MR is already in flight");
      } else {
        setRerunError(String(err instanceof Error ? err.message : err));
      }
      setRerunSubmitting(false);
    }
  };

  const doCancel = async () => {
    if (reviewId == null) return;
    if (!window.confirm("Cancel this review?")) return;
    await cancelReview.mutateAsync([reviewId]);
  };

  const elapsedMs =
    current?.started_at != null
      ? new Date(current.finished_at ?? Date.now()).getTime() -
        new Date(current.started_at).getTime()
      : null;
  const totalTokens = current != null ? current.prompt_tokens + current.completion_tokens : null;

  const summary = aggregateSummary(current?.stages ?? [], trace.data ?? null);
  const selectedSpec = selectedNode != null ? nodes.find((n) => n.id === selectedNode) : undefined;
  const selectedTrace =
    selectedNode != null ? filterTraceForNode(selectedNode, trace.data ?? null) : null;
  const selectedCandidates =
    selectedNode == null
      ? []
      : (current?.candidates ?? []).filter((c) => c.node === selectedNode);
  const selectedMetrics = selectedSpec
    ? [fmtDuration(selectedSpec.durationMs), fmtTokens(selectedSpec.tokens)].filter(Boolean)
    : [];

  return (
    <div className="nw-fade">
      {review.error && <ErrorBox message={review.error.message} onRetry={() => review.refetch()} />}
      {review.isPending && <div className="spinner">Loading review…</div>}

      {current && (
        <>
          <div className="page-head">
            <div>
              <div className="muted">
                <Link to="/repos">Repositories</Link> / Review
              </div>
              <h1 className="row">
                Review <StatusBadge status={current.status} />
                {!current.publish && <BenchmarkBadge />}
              </h1>
            </div>
          </div>

          <div className="run-toolbar">
            {elapsedMs != null && (
              <span className="muted">Elapsed {Math.max(0, Math.round(elapsedMs / 1000))}s</span>
            )}
            {activeLlmCall != null && (
              <span className="muted" title="the model is actively generating a response">
                Model running{activeLlmCall.stage ? ` (${activeLlmCall.stage})` : ""}
                {activeLlmCall.elapsed_s != null
                  ? ` — ${Math.round(activeLlmCall.elapsed_s)}s`
                  : ""}
              </span>
            )}
            {totalTokens != null && totalTokens > 0 && (
              <span className="muted run-toolbar-tokens">{fmtTokens(totalTokens)}</span>
            )}
            <span className="muted">{summary.toolCallCount} tool calls</span>
            <span className="muted">{summary.llmRoundCount} LLM rounds</span>
            <span className="row" style={{ gap: 6 }}>
              <StatusBadge status="done" label={`${summary.statusCounts.done} done`} />
              <StatusBadge status="running" label={`${summary.statusCounts.running} running`} />
              <StatusBadge status="failed" label={`${summary.statusCounts.failed} failed`} />
              <StatusBadge status="canceled" label={`${summary.statusCounts.pending} pending`} />
            </span>
            {!terminal && (
              <button className="btn-o" onClick={doCancel} disabled={cancelReview.isPending}>
                {cancelReview.isPending ? "Canceling…" : "Cancel run"}
              </button>
            )}
            {terminal && (
              <button className="btn-o" onClick={doRerun} disabled={rerunSubmitting}>
                {rerunSubmitting ? "Starting…" : "Re-run"}
              </button>
            )}
          </div>
          {rerunError && <ErrorBox message={rerunError} />}
          {cancelReview.error && <ErrorBox message={cancelReview.error.message} />}

          <div className="tabs section">
            <button
              className={activeTab === "pipeline" ? "active" : ""}
              onClick={() => setActiveTab("pipeline")}
            >
              Pipeline
            </button>
            <button
              className={activeTab === "summary" ? "active" : ""}
              onClick={() => setActiveTab("summary")}
            >
              Summary
            </button>
          </div>

          {current.error && <ErrorBox message={current.error} />}

          {activeTab === "pipeline" && (
            <div className="section">
              <PipelineGraph
                stages={current.stages}
                trace={trace.data ?? null}
                selectedNode={selectedNode}
                onSelectNode={setSelectedNode}
              />

              <div className="nw-card section" style={{ marginTop: 16 }}>
                {selectedNode == null || selectedTrace == null ? (
                  <p className="muted">Select a node above to see its timeline.</p>
                ) : (
                  <>
                    {selectedSpec ? (
                      <>
                        <h3 className="mono">{selectedSpec.label}</h3>
                        <div className="row" style={{ gap: 8, alignItems: "center", marginTop: 4, marginBottom: 12 }}>
                          <StatusBadge
                            status={selectedSpec.status === "pending" ? "queued" : selectedSpec.status}
                            label={selectedSpec.status === "pending" ? "pending" : undefined}
                          />
                          {selectedMetrics.length > 0 && (
                            <span className="muted">{selectedMetrics.join(" · ")}</span>
                          )}
                        </div>
                      </>
                    ) : (
                      <h3 style={{ marginBottom: 12 }}>{selectedNode}</h3>
                    )}
                    <NodeFindings candidates={selectedCandidates} />
                    <TraceTimeline
                      toolCalls={selectedTrace.toolCalls}
                      llmRounds={selectedTrace.llmRounds}
                    />
                  </>
                )}
              </div>
            </div>
          )}

          {activeTab === "summary" && (
            <div className="nw-card section">
              <h3>Summary</h3>
              {current.summary ? (
                <div className="md-body">
                  <ReactMarkdown>{current.summary}</ReactMarkdown>
                </div>
              ) : (
                <p className="muted">No summary available for this review.</p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

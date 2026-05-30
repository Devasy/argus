import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Square } from "lucide-react";
import { useCancelReview, useReviewQueueSummary, useReviews } from "../../api/queries";
import type { ReviewListItem, ReviewStatus } from "../../api/types";
import BenchmarkBadge from "../../components/BenchmarkBadge";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";
import StatusBadge from "../../components/StatusBadge";
import { fmtDuration, fmtTokens } from "../../lib/pipelineGraph";

const FILTERS: { key: "all" | ReviewStatus; label: string }[] = [
  { key: "all", label: "All" },
  { key: "queued", label: "Queued" },
  { key: "running", label: "Running" },
  { key: "done", label: "Done" },
  { key: "failed", label: "Failed" },
];

const LIVE_STATUSES: ReviewStatus[] = ["queued", "running"];
const POLL_MS = 5000;

function fmtEta(seconds: number | null): string | null {
  if (seconds == null) return null;
  const m = Math.round(seconds / 60);
  return m < 1 ? "<1m" : `~${m}m`;
}

function fmtAvg(seconds: number | null): string | null {
  if (seconds == null) return null;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}

// eslint-disable-next-line @typescript-eslint/no-unused-vars
function DurationCell({ item, tick: _tick }: { item: ReviewListItem; tick: number }) {
  if (item.status === "queued") {
    const pos = item.queue_position != null ? `#${item.queue_position} in queue` : "queued";
    const eta = fmtEta(item.eta_seconds);
    return (
      <span className="muted">
        {pos}
        {eta && ` · est. ${eta}`}
      </span>
    );
  }
  if (item.status === "running" && item.started_at) {
    const elapsedMs = Date.now() - new Date(item.started_at).getTime();
    return <span className="num">{fmtDuration(Math.max(0, elapsedMs))}</span>;
  }
  if (item.started_at && item.finished_at) {
    const ms = new Date(item.finished_at).getTime() - new Date(item.started_at).getTime();
    return <span className="num">{fmtDuration(ms)}</span>;
  }
  return <span className="muted">—</span>;
}

export default function ReviewsPage() {
  const [filter, setFilter] = useState<"all" | ReviewStatus>("all");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const [tick, setTick] = useState(0);
  const navigate = useNavigate();
  const cancelReview = useCancelReview();

  const statusParam = filter === "all" ? undefined : filter;
  const reviews = useReviews(
    { status: statusParam, page, per_page: perPage },
    {
      refetchInterval: (query) => {
        const items = query.state.data?.items ?? [];
        return items.some((r) => LIVE_STATUSES.includes(r.status)) ? POLL_MS : false;
      },
    },
  );
  const summary = useReviewQueueSummary({
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && (data.queued > 0 || data.running > 0) ? POLL_MS : false;
    },
  });

  // Lightweight per-chip counts: per_page=1 keeps the response tiny since we
  // only read `total`, not `items`. Queued/running counts reuse the
  // already-fetched queue summary instead of firing extra requests.
  const allCount = useReviews({ per_page: 1 });
  const doneCount = useReviews({ status: "done", per_page: 1 });
  const failedCount = useReviews({ status: "failed", per_page: 1 });

  const chipCounts: Record<"all" | ReviewStatus, number | undefined> = {
    all: allCount.data?.total,
    queued: summary.data?.queued,
    running: summary.data?.running,
    done: doneCount.data?.total,
    failed: failedCount.data?.total,
    canceled: undefined,
  };

  const hasLive = (reviews.data?.items ?? []).some((r) => LIVE_STATUSES.includes(r.status));
  useEffect(() => {
    if (!hasLive) return;
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [hasLive]);

  const onCancel = async (e: React.MouseEvent, reviewId: string) => {
    e.stopPropagation();
    if (!window.confirm("Cancel this review?")) return;
    await cancelReview.mutateAsync([reviewId]);
  };

  const s = summary.data;

  return (
    <div className="nw-fade">
      <div className="page-head">
        <div>
          <h1>Reviews</h1>
          <div className="muted">Every review run across all repositories — live and historical</div>
        </div>
      </div>

      {s && s.queued > 0 && (
        <div className="nw-card section" style={{ padding: "10px 16px" }}>
          {s.queued} queued · {s.running} running
          {s.avg_duration_s != null && <> · avg review ~{fmtAvg(s.avg_duration_s)}</>}
        </div>
      )}

      <div className="row" style={{ gap: 8, flexWrap: "wrap", margin: "14px 0" }}>
        {FILTERS.map((f) => {
          const count = chipCounts[f.key];
          return (
            <button
              key={f.key}
              type="button"
              className={filter === f.key ? "btn-p" : "btn-o"}
              onClick={() => {
                setFilter(f.key);
                setPage(1);
              }}
            >
              {f.label}
              {count != null && <span className="muted"> ({count})</span>}
            </button>
          );
        })}
      </div>

      {reviews.error && <ErrorBox message={reviews.error.message} onRetry={reviews.refetch} />}
      {cancelReview.error && <ErrorBox message={cancelReview.error.message} />}
      {reviews.isPending && <div className="spinner">Loading reviews…</div>}

      {reviews.data && (
        <div className="table-wrap">
          <table className="nw-table">
            <thead>
              <tr>
                <th>Merge request</th>
                <th>Repository</th>
                <th>Profile</th>
                <th>Trigger</th>
                <th>Status</th>
                <th className="num">Duration</th>
                <th className="num">Tokens</th>
                <th>Started</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {reviews.data.items.length === 0 && (
                <tr>
                  <td colSpan={9} className="muted">
                    No reviews match this filter.
                  </td>
                </tr>
              )}
              {reviews.data.items.map((item) => {
                const canCancel = LIVE_STATUSES.includes(item.status);
                return (
                  <tr
                    key={item.id}
                    className="clickable"
                    onClick={() => navigate(`/reviews/${item.id}`)}
                  >
                    <td className="cell-clip" title={item.mr_title}>
                      <b className="mono">!{item.mr_iid}</b> {item.mr_title}
                    </td>
                    <td
                      className="mono cell-clip"
                      title={item.repo_project_path}
                    >
                      {item.repo_project_path}
                    </td>
                    <td className="mono muted">{item.profile_name ?? "default"}</td>
                    <td>{item.trigger}</td>
                    <td>
                      <span className="row" style={{ gap: 6 }}>
                        <StatusBadge status={item.status} />
                        {!item.publish && <BenchmarkBadge />}
                      </span>
                    </td>
                    <td className="num">
                      <DurationCell item={item} tick={tick} />
                    </td>
                    <td className="num">{fmtTokens(item.prompt_tokens + item.completion_tokens)}</td>
                    <td className="muted">{new Date(item.created_at).toLocaleString()}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      {canCancel && (
                        <button
                          type="button"
                          className="btn-g"
                          title="Cancel review"
                          disabled={cancelReview.isPending}
                          onClick={(e) => onCancel(e, item.id)}
                        >
                          <Square size={14} />
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <PaginationBar
            page={reviews.data.page}
            perPage={reviews.data.per_page}
            total={reviews.data.total}
            onPageChange={setPage}
            onPerPageChange={(n) => {
              setPerPage(n);
              setPage(1);
            }}
          />
        </div>
      )}
    </div>
  );
}

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Activity,
  Brain,
  GitPullRequestArrow,
  MessagesSquare,
  ThumbsUp,
} from "lucide-react";
import { useDashboardStats } from "../../api/queries";
import type { DashboardActivity, DashboardAgent } from "../../api/types";
import ErrorBox from "../../components/ErrorBox";
import {
  barHeights,
  fmtCompact,
  fmtDayLabel,
  fmtPct,
  groupConsecutiveActivity,
  isErrorActivity,
  type GroupedActivity,
} from "./format";

const WINDOWS = [7, 14, 30] as const;

function StatTile({
  icon: Icon,
  label,
  value,
  detail,
}: {
  icon: React.ComponentType<{ size?: number | string }>;
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="nw-card dash-stat">
      <div className="dash-stat-head">
        <span className="dash-stat-icon">
          <Icon size={15} />
        </span>
        <span className="dash-stat-label">{label}</span>
      </div>
      <div className="dash-stat-value">{value}</div>
      <div className="muted dash-stat-detail">{detail}</div>
    </div>
  );
}

const ACTIVITY_HREF = (item: DashboardActivity): string | null => {
  if (!item.href_id) return null;
  if (item.type.startsWith("review")) return `/reviews/${item.href_id}`;
  if (item.type === "distillation") return `/distillation-runs/${item.href_id}`;
  if (item.type === "verdict") return `/mrs/${item.href_id}`;
  return null;
};

function ActivityRow({ group }: { group: GroupedActivity<DashboardActivity> }) {
  const { item, count } = group;
  const navigate = useNavigate();
  const href = ACTIVITY_HREF(item);
  const isError = isErrorActivity(item);
  return (
    <div
      className={`dash-feed-item${href ? " clickable" : ""}${isError ? " is-error" : ""}`}
      role={href ? "button" : undefined}
      tabIndex={href ? 0 : undefined}
      onClick={() => href && navigate(href)}
      onKeyDown={(e) => {
        if (href && (e.key === "Enter" || e.key === " ")) navigate(href);
      }}
    >
      <span className="badge dash-feed-badge" data-kind={item.type}>
        {item.type.replace("_", " ")}
      </span>
      <div className="dash-feed-body">
        <div className="dash-feed-title">
          {item.title}
          {count > 1 && <span className="dash-feed-count">×{count}</span>}
        </div>
        {item.detail && <div className="muted">{item.detail}</div>}
        <div className="muted">
          {count > 1 ? "Latest: " : ""}
          {new Date(item.at).toLocaleString()}
        </div>
      </div>
    </div>
  );
}

function AgentRow({ a }: { a: DashboardAgent }) {
  return (
    <tr>
      <td>
        <strong>{a.name}</strong>
      </td>
      <td className="muted mono">{a.current_version != null ? `v${a.current_version}` : "—"}</td>
      <td>{a.comments}</td>
      <td>{a.accepted}</td>
      <td>{a.rejected}</td>
      <td>{a.open}</td>
      <td>
        <div className="dash-meter-cell">
          <span className="dash-meter">
            <i style={{ width: `${Math.round((a.hit_rate ?? 0) * 100)}%` }} />
          </span>
          <strong className="mono">{fmtPct(a.hit_rate)}</strong>
        </div>
      </td>
    </tr>
  );
}

function ReviewsPerDayChart({ days, window: windowDays }: { days: { date: string; count: number }[]; window: number }) {
  if (days.length === 0) {
    return <div className="muted dash-empty">No reviews in this window.</div>;
  }
  const heights = barHeights(days);
  const total = days.reduce((sum, d) => sum + d.count, 0);
  const max = Math.max(1, ...days.map((d) => d.count));
  const peakDay = days.find((d) => d.count === max) ?? days[0];
  const avgPerDay = total / days.length;
  return (
    <>
      <div className="dash-bars" role="img" aria-label={`Reviews per day over the last ${windowDays} days, peak ${max}`}>
        {days.map((d, i) => (
          <div key={d.date} className="dash-bar-col">
            <div
              className="dash-bar"
              style={{ height: `${heights[i]}%` }}
              tabIndex={0}
            >
              <span className="dash-bar-tip">{d.count}</span>
            </div>
          </div>
        ))}
      </div>
      <div className="dash-bars-axis">
        <span>{fmtDayLabel(days[0].date)}</span>
        {days.length > 1 && <span>{fmtDayLabel(days[days.length - 1].date)}</span>}
      </div>
      <div className="dash-chart-summary">
        <div className="dash-chart-summary-item">
          <span className="dash-chart-summary-value">{total}</span>
          <span className="dash-chart-summary-label">Total reviews</span>
        </div>
        <div className="dash-chart-summary-item">
          <span className="dash-chart-summary-value">{avgPerDay.toFixed(1)}</span>
          <span className="dash-chart-summary-label">Avg per day</span>
        </div>
        <div className="dash-chart-summary-item">
          <span className="dash-chart-summary-value">
            {max} <span className="muted">on {fmtDayLabel(peakDay.date)}</span>
          </span>
          <span className="dash-chart-summary-label">Busiest day</span>
        </div>
      </div>
    </>
  );
}

export default function DashboardPage() {
  const [days, setDays] = useState<number>(14);
  const stats = useDashboardStats(days);
  const t = stats.data?.tiles;

  return (
    <div className="nw-fade">
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <div className="muted">Review activity and feedback outcomes</div>
        </div>
        <div className="row dash-window-chips" role="group" aria-label="Time window">
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              className={days === w ? "btn-p" : "btn-o"}
              aria-pressed={days === w}
              onClick={() => setDays(w)}
            >
              {w}d
            </button>
          ))}
        </div>
      </div>

      {stats.error && <ErrorBox message={stats.error.message} onRetry={stats.refetch} />}
      {stats.isPending && <div className="spinner">Loading dashboard…</div>}

      {stats.data && t && (
        <>
          <div className="dash-stat-grid">
            <StatTile
              icon={GitPullRequestArrow}
              label="MRs reviewed"
              value={fmtCompact(t.mrs_reviewed)}
              detail={`last ${days} days`}
            />
            <StatTile
              icon={MessagesSquare}
              label="Agent comments"
              value={fmtCompact(t.agent_comments)}
              detail={`${fmtCompact(t.human_replies)} human replies`}
            />
            <StatTile
              icon={ThumbsUp}
              label="Acceptance rate"
              value={fmtPct(t.acceptance_rate)}
              detail={`${t.accepted} accepted · ${t.rejected} rejected`}
            />
            <StatTile
              icon={Brain}
              label="Active learnings"
              value={fmtCompact(t.active_learnings)}
              detail={`${t.pending_distillation} runs pending`}
            />
            <StatTile
              icon={Activity}
              label="Tokens used"
              value={fmtCompact(t.prompt_tokens + t.completion_tokens)}
              detail={`last ${days} days`}
            />
          </div>

          <div className="dash-cols">
            <div className="nw-card section">
              <div className="flex-between">
                <strong>Reviews per day</strong>
                <span className="muted">{days} days</span>
              </div>
              <ReviewsPerDayChart days={stats.data.reviews_per_day} window={days} />
            </div>
            <div className="nw-card section dash-activity">
              <strong>Recent activity</strong>
              <div className="dash-feed">
                {stats.data.activity.length === 0 && (
                  <div className="muted dash-empty">Nothing yet.</div>
                )}
                {groupConsecutiveActivity(stats.data.activity).map((g) => (
                  <ActivityRow key={`${g.item.type}-${g.item.at}`} group={g} />
                ))}
              </div>
            </div>
          </div>

          <h3 className="section dash-subhead">
            Per-agent breakdown
            <span className="muted"> — hit rate = accepted ÷ (accepted + rejected)</span>
          </h3>
          {stats.data.agents.length === 0 ? (
            <div className="muted">No reviewer agents configured yet.</div>
          ) : (
            <div className="table-wrap">
              <table className="nw-table">
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Version</th>
                    <th>Comments</th>
                    <th>Accepted</th>
                    <th>Rejected</th>
                    <th>Open</th>
                    <th>Hit rate</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.data.agents.map((a) => (
                    <AgentRow key={a.agent_id} a={a} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

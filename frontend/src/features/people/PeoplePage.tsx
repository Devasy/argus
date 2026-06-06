import { useState } from "react";
import { NavLink, useParams } from "react-router-dom";
import { useCommentSourceStats, useMe, useUserStats } from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import { fmtPct } from "../dashboard/format";
import DonutChart from "./DonutChart";
import ReviewerGraphView from "./ReviewerGraphView";
import type { UserStats } from "../../api/types";

const WINDOWS = [7, 14, 30] as const;

function hitRate(a: number, r: number): number | null {
  return a + r === 0 ? null : a / (a + r);
}

function UserRow({ u }: { u: UserStats }) {
  const authorRate = hitRate(u.author_stats.accepted, u.author_stats.rejected);
  const reviewerRate = hitRate(u.reviewer_stats.resolved, u.reviewer_stats.rejected);
  return (
    <tr>
      <td>
        <strong>{u.display_name || u.username}</strong>
        <div className="muted mono">@{u.username}</div>
      </td>
      <td>{u.prs_authored}</td>
      <td>{u.author_stats.accepted}</td>
      <td>{u.author_stats.rejected}</td>
      <td>{u.author_stats.open}</td>
      <td className="mono">{fmtPct(authorRate)}</td>
      <td>{u.reviewer_stats.comments}</td>
      <td>{u.reviewer_stats.resolved}</td>
      <td>{u.reviewer_stats.rejected}</td>
      <td>{u.reviewer_stats.ignored}</td>
      <td className="mono">{fmtPct(reviewerRate)}</td>
    </tr>
  );
}

function PeopleOverview() {
  const [days, setDays] = useState<number>(14);
  const stats = useUserStats(true, days);
  const sources = useCommentSourceStats(true, days);

  return (
    <>
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

      {sources.error && <ErrorBox message={sources.error.message} onRetry={sources.refetch} />}
      {sources.data && (
        <div className="donut-row">
          <div className="ui-card">
            <DonutChart
              title="Replies to MM (bot comments)"
              segments={[
                { label: "Resolved", value: sources.data.bot.resolved, color: "var(--state-success-text)" },
                { label: "Rejected", value: sources.data.bot.rejected, color: "var(--state-error-text)" },
                { label: "Ignored", value: sources.data.bot.ignored, color: "var(--state-neutral-text)" },
              ]}
            />
          </div>
          <div className="ui-card">
            <DonutChart
              title="Replies to human reviewers"
              segments={[
                { label: "Resolved", value: sources.data.human.resolved, color: "var(--state-success-text)" },
                { label: "Rejected", value: sources.data.human.rejected, color: "var(--state-error-text)" },
                { label: "Ignored", value: sources.data.human.ignored, color: "var(--state-neutral-text)" },
              ]}
            />
          </div>
        </div>
      )}

      {stats.error && <ErrorBox message={stats.error.message} onRetry={stats.refetch} />}
      {stats.isPending && <div className="spinner">Loading people…</div>}
      {stats.data && (
        <>
          <div className="table-wrap">
            <table className="ui-table">
              <thead>
                <tr>
                  <th rowSpan={2}>Person</th>
                  <th colSpan={5}>As PR author (bot comments received)</th>
                  <th colSpan={5}>As human reviewer (own comments)</th>
                </tr>
                <tr>
                  <th>PRs</th>
                  <th>Accepted</th>
                  <th>Rejected</th>
                  <th>Open</th>
                  <th>Hit rate</th>
                  <th>Comments</th>
                  <th>Resolved</th>
                  <th>Rejected</th>
                  <th>Ignored</th>
                  <th>Hit rate</th>
                </tr>
              </thead>
              <tbody>
                {stats.data.items.map((u) => (
                  <UserRow key={u.actor_id} u={u} />
                ))}
              </tbody>
            </table>
            {stats.data.items.length === 0 && (
              <div className="muted dash-empty">No developer activity yet.</div>
            )}
          </div>
        </>
      )}
    </>
  );
}

const TABS = [
  { slug: "overview", label: "Overview", el: <PeopleOverview /> },
  { slug: "graph", label: "Reviewer graph", el: <ReviewerGraphView /> },
];

export default function PeoplePage() {
  const { section = "overview" } = useParams();
  const active = TABS.find((t) => t.slug === section) ?? TABS[0];
  const me = useMe();

  return (
    <div className="ui-fade">
      <div className="page-head">
        <div>
          <h1>People</h1>
          <div className="muted">
            Per-developer stats as PR author and as human reviewer, and who reviews whom
          </div>
        </div>
        <div className="row dash-window-chips" role="group" aria-label="View">
          {TABS.map((t) => (
            <NavLink
              key={t.slug}
              to={`/people/${t.slug}`}
              className={({ isActive }) => (isActive ? "btn-p" : "btn-o")}
            >
              {t.label}
            </NavLink>
          ))}
        </div>
      </div>
      {me.isPending && <div className="spinner">Checking access…</div>}
      {me.data && me.data.role !== "admin" && (
        <div className="ui-card" style={{ maxWidth: 420 }}>
          <strong>Admins only</strong>
          <p className="muted">
            This view needs an admin-role token. Log in with one via the token page if you have
            one.
          </p>
        </div>
      )}
      {me.data?.role === "admin" && active.el}
    </div>
  );
}

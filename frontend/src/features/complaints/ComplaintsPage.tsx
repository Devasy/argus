import { useMemo, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { useComplaints } from "../../api/queries";
import type { AgentComplaint } from "../../api/types";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";

/** Problems agents reported about OUR tools, prompts and context.
 *
 * Read the rollup before the feed: a single complaint is an agent having a
 * bad day, the same complaint forty times is a bug. Both the zero-edge call
 * graph and the misleading get_hunk error were diagnosed correctly by agents
 * long before a human found them in a Langfuse trace. */

const CATEGORIES = [
  "tool_broken",
  "tool_missing",
  "context_insufficient",
  "context_wrong",
  "prompt_irrelevant",
  "prompt_unclear",
  "instructions_conflict",
  "task_impossible",
] as const;

type CategoryFilter = "all" | (typeof CATEGORIES)[number];
type BlockedFilter = "all" | "blocked" | "worked-around";

const CATEGORY_LABEL: Record<string, string> = {
  tool_broken: "Tool broken",
  tool_missing: "Tool missing",
  context_insufficient: "Context insufficient",
  context_wrong: "Context wrong",
  prompt_irrelevant: "Prompt irrelevant",
  prompt_unclear: "Prompt unclear",
  instructions_conflict: "Instructions conflict",
  task_impossible: "Task impossible",
};

/** Whether the complaint is about our tooling or about what we told the agent.
 * They land with different people, so they are worth telling apart at a
 * glance. */
function isPromptProblem(category: string): boolean {
  return category.startsWith("prompt_") || category === "instructions_conflict";
}

function fmtWhen(iso: string): string {
  return new Date(iso).toLocaleString();
}

export default function ComplaintsPage() {
  const [category, setCategory] = useState<CategoryFilter>("all");
  const [blocked, setBlocked] = useState<BlockedFilter>("all");
  const [target, setTarget] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(50);

  const filters = useMemo(() => {
    const f: {
      category?: string;
      blocked?: boolean;
      target?: string;
      page?: number;
      per_page?: number;
    } = { page, per_page: perPage };
    if (category !== "all") f.category = category;
    if (blocked !== "all") f.blocked = blocked === "blocked";
    if (target !== null) f.target = target;
    return f;
  }, [category, blocked, target, page, perPage]);

  const result = useComplaints(filters);
  const items: AgentComplaint[] = result.data?.items ?? [];
  const rollup = result.data?.by_target ?? [];
  const total = result.data?.total ?? 0;

  const reset = (fn: () => void) => {
    fn();
    setPage(1);
  };

  return (
    <div>
      <div className="page-head">
        <h1>Agent complaints</h1>
        <p className="muted">
          Problems the review agents hit with their own tools, prompts and
          context — not with the code they were reviewing. One report is noise;
          the same one repeated is a bug worth fixing.
        </p>
      </div>

      {result.isError && (
        <ErrorBox
          message={(result.error as Error)?.message ?? "Failed to load complaints"}
          onRetry={() => result.refetch()}
        />
      )}

      {rollup.length > 0 && (
        <div className="card" style={{ marginBottom: 18 }}>
          <h2 style={{ marginTop: 0 }}>What is failing most</h2>
          <table className="table">
            <thead>
              <tr>
                <th>Target</th>
                <th>Category</th>
                <th style={{ textAlign: "right" }}>Reports</th>
                <th style={{ textAlign: "right" }}>Blocked work</th>
                <th>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {rollup.map((r) => (
                <tr
                  key={`${r.target ?? "-"}:${r.category}`}
                  className="clickable"
                  onClick={() => reset(() => setTarget(r.target))}
                >
                  <td>
                    <code>{r.target ?? "(unspecified)"}</code>
                  </td>
                  <td>{CATEGORY_LABEL[r.category] ?? r.category}</td>
                  <td style={{ textAlign: "right" }}>{r.count}</td>
                  <td style={{ textAlign: "right" }}>
                    {r.blocked_count > 0 ? (
                      <span className="red">{r.blocked_count}</span>
                    ) : (
                      "0"
                    )}
                  </td>
                  <td className="muted">{fmtWhen(r.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
        <button
          className={`chip${category === "all" ? " on" : ""}`}
          onClick={() => reset(() => setCategory("all"))}
        >
          All categories
        </button>
        {CATEGORIES.map((c) => (
          <button
            key={c}
            className={`chip${category === c ? " on" : ""}`}
            onClick={() => reset(() => setCategory(c))}
          >
            {CATEGORY_LABEL[c]}
          </button>
        ))}
        <span className="chip-divider" />
        {(["all", "blocked", "worked-around"] as const).map((b) => (
          <button
            key={b}
            className={`chip${blocked === b ? " on" : ""}`}
            onClick={() => reset(() => setBlocked(b))}
          >
            {b === "all"
              ? "Any impact"
              : b === "blocked"
                ? "Blocked the work"
                : "Worked around"}
          </button>
        ))}
        {target !== null && (
          <>
            <span className="chip-divider" />
            <button className="chip on" onClick={() => reset(() => setTarget(null))}>
              target: {target} ✕
            </button>
          </>
        )}
      </div>

      {!result.isLoading && items.length === 0 && (
        <div className="card muted">
          No complaints recorded. Either everything is working, or agents are
          not reporting — check that report_problem is reaching them.
        </div>
      )}

      {items.map((c) => (
        <div key={c.id} className="card" style={{ marginBottom: 10 }}>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            {c.blocked && (
              <span className="red" title="This stopped the agent completing its work">
                <AlertTriangle size={15} />
              </span>
            )}
            <strong>{CATEGORY_LABEL[c.category] ?? c.category}</strong>
            {c.target && <code>{c.target}</code>}
            <span className="chip" style={{ pointerEvents: "none" }}>
              {isPromptProblem(c.category) ? "our instructions" : "our tooling"}
            </span>
            <span className="muted" style={{ marginLeft: "auto" }}>
              {c.stage_name ?? "—"} · {fmtWhen(c.created_at)}
            </span>
          </div>
          <p style={{ marginBottom: 6 }}>{c.detail}</p>
          {c.review_id && (
            <a className="muted" href={`/reviews/${c.review_id}`}>
              view review
            </a>
          )}
        </div>
      ))}

      <PaginationBar
        page={page}
        perPage={perPage}
        total={total}
        onPageChange={setPage}
        onPerPageChange={(n) => {
          setPerPage(n);
          setPage(1);
        }}
      />
    </div>
  );
}

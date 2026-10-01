import { Fragment, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useParams } from "react-router-dom";
import { useDistillationRun, useDistillationRunTrace } from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import StatusBadge from "../../components/StatusBadge";
import ThreadCard from "../../components/ThreadCard";
import TraceTables from "../../components/TraceTables";
import { fmtTokens } from "../../lib/pipelineGraph";

export default function DistillationRunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const run = useDistillationRun(runId!);
  const trace = useDistillationRunTrace(runId!);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  if (run.isLoading) return <div className="muted">Loading…</div>;
  if (run.isError) return <ErrorBox message="Failed to load learning run." />;
  if (!run.data) return null;

  const d = run.data;
  const tokens = fmtTokens(d.prompt_tokens + d.completion_tokens);

  return (
    <div>
      <h2>Learning Run</h2>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <StatusBadge status={d.status} />
        <span className="muted">trigger: {d.trigger}</span>
        <span className="muted">{tokens}</span>
        <span className="muted">notes considered: {d.note_ids.length}</span>
      </div>
      {d.error && <ErrorBox message={d.error} />}
      <h4 className="section">Threads ({d.threads.length})</h4>
      <div className="table-wrap section">
        <table className="nw-table">
          <thead>
            <tr>
              <th>Type</th>
              <th>Label → verdict</th>
              <th>Learnings</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {d.threads.length === 0 && (
              <tr>
                <td colSpan={4} className="muted">
                  No thread decisions recorded.
                </td>
              </tr>
            )}
            {d.threads.map((t) => (
              <Fragment key={t.discussion_id}>
                <tr>
                  <td>
                    {t.thread_type === "bot_thread" ? "reply to bot" : "human comment"}
                    {t.status !== "done" && <span className="muted"> ({t.status})</span>}
                    {t.thread && (
                      <div>
                        <button
                          type="button"
                          className="thread-toggle"
                          aria-expanded={open.has(t.discussion_id)}
                          onClick={() => toggle(t.discussion_id)}
                        >
                          {open.has(t.discussion_id) ? (
                            <ChevronDown size={14} aria-hidden="true" />
                          ) : (
                            <ChevronRight size={14} aria-hidden="true" />
                          )}
                          {open.has(t.discussion_id) ? "hide thread" : "show thread"}
                        </button>
                      </div>
                    )}
                  </td>
                  <td>
                    {t.thread_type === "bot_thread"
                      ? `${t.reconciler_label ?? "—"} → ${t.reply_verdict ?? "—"}`
                      : "—"}
                    {t.verdict_reason && <div className="muted">{t.verdict_reason}</div>}
                  </td>
                  <td className="mono">
                    {t.learning_ids.length === 0
                      ? "—"
                      : t.learning_ids.map((id) => id.slice(0, 8)).join(", ")}
                  </td>
                  <td className="muted">{t.decision_reason ?? "—"}</td>
                </tr>
                {t.thread && open.has(t.discussion_id) && (
                  <tr>
                    <td colSpan={4}>
                      <ThreadCard thread={t.thread} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {trace.data && (
        <TraceTables toolCalls={trace.data.tool_calls} llmRounds={trace.data.llm_rounds} />
      )}
    </div>
  );
}

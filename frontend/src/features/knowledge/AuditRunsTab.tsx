import { useState } from "react";
import { useAuditRunTrace, useAuditRuns } from "../../api/queries";
import type { AuditRun } from "../../api/types";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";
import StatusBadge from "../../components/StatusBadge";
import TraceTables from "../../components/TraceTables";

function fmtDuration(run: AuditRun): string {
  const start = run.started_at ?? run.created_at;
  const end = run.finished_at ?? new Date().toISOString();
  const secs = (new Date(end).getTime() - new Date(start).getTime()) / 1000;
  if (!Number.isFinite(secs) || secs < 0) return "—";
  return secs < 90 ? `${Math.round(secs)}s` : `${(secs / 60).toFixed(1)}m`;
}

/** Expanded trace for one run: the same tables reviews and learning runs use. */
function RunTrace({ runId }: { runId: string }) {
  const trace = useAuditRunTrace(runId);
  if (trace.isPending) return <div className="spinner">Loading trace…</div>;
  if (trace.error) return <ErrorBox message={trace.error.message} />;
  if (!trace.data) return null;
  const { tool_calls, llm_rounds } = trace.data;
  if (tool_calls.length === 0 && llm_rounds.length === 0) {
    return <p className="muted">No trace recorded for this run.</p>;
  }
  return (
    <>
      {tool_calls.length === 0 && llm_rounds.length > 0 && (
        // Worth calling out rather than showing an empty table: an auditor
        // that never read the codebase cannot have grounded its verdicts in
        // it, and this exact shape (LLM rounds, zero tool calls) was a real
        // instrumentation bug.
        <p className="muted" style={{ fontSize: 11.5 }}>
          This run made no tool calls — its verdicts were formed without reading the repository.
        </p>
      )}
      <TraceTables toolCalls={tool_calls} llmRounds={llm_rounds} />
    </>
  );
}

export default function AuditRunsTab() {
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const [openId, setOpenId] = useState<string | null>(null);
  const { data, isPending, error, refetch } = useAuditRuns(undefined, page, perPage);

  const items = data?.items ?? [];

  return (
    <div>
      {isPending && <div className="spinner">Loading audit runs…</div>}
      {error && <ErrorBox message={error.message} onRetry={refetch} />}

      {data && items.length === 0 && (
        <div className="nw-card" style={{ padding: 24, textAlign: "center" }}>
          <p className="muted">
            No audit runs yet. Start one from Settings → Learning auditor.
          </p>
        </div>
      )}

      {items.map((r) => {
        const open = openId === r.id;
        return (
          <div className="nw-card" key={r.id} style={{ marginBottom: 12, padding: 12 }}>
            <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <StatusBadge status={r.status} />
              <b>{r.repo_path ?? r.repo_id}</b>
              <span className="badge">{fmtDuration(r)}</span>
              <span className="badge">{r.verdicts_written} verdict(s)</span>
              {r.audited_ref && (
                <span className="muted" style={{ fontSize: 11.5 }}>
                  ref <code>{r.audited_ref}</code>
                  {r.commit_sha ? ` @ ${r.commit_sha.slice(0, 8)}` : ""}
                </span>
              )}
            </div>

            <div className="row muted" style={{ gap: 8, fontSize: 11.5, marginTop: 4 }}>
              <span>{new Date(r.created_at).toLocaleString()}</span>
              {r.langfuse_trace_id && (
                <>
                  <span>·</span>
                  <span>
                    trace <code>{r.langfuse_trace_id.slice(0, 12)}</code>
                  </span>
                </>
              )}
            </div>

            {r.error && <ErrorBox message={r.error} />}

            <div className="row" style={{ marginTop: 8 }}>
              <button className="btn-o" onClick={() => setOpenId(open ? null : r.id)}>
                {open ? "Hide trace" : "Show trace"}
              </button>
            </div>

            {open && (
              <div style={{ marginTop: 10 }}>
                <RunTrace runId={r.id} />
              </div>
            )}
          </div>
        );
      })}

      {data && (
        <PaginationBar
          page={data.page}
          perPage={data.per_page}
          total={data.total}
          onPageChange={setPage}
          onPerPageChange={(n) => {
            setPerPage(n);
            setPage(1);
          }}
        />
      )}
    </div>
  );
}

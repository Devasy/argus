import { useParams } from "react-router-dom";
import { useDistillationRun, useDistillationRunTrace } from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import StatusBadge from "../../components/StatusBadge";
import TraceTables from "../../components/TraceTables";
import { fmtTokens } from "../../lib/pipelineGraph";

export default function DistillationRunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const run = useDistillationRun(runId!);
  const trace = useDistillationRunTrace(runId!);

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
      {trace.data && (
        <TraceTables toolCalls={trace.data.tool_calls} llmRounds={trace.data.llm_rounds} />
      )}
    </div>
  );
}

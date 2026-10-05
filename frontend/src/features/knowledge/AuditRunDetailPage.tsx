import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuditRun } from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import PipelineGraph from "../../components/PipelineGraph";
import StatusBadge from "../../components/StatusBadge";
import { buildAuditGraphNodes } from "../../lib/auditGraph";

export default function AuditRunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const run = useAuditRun(runId!);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);

  if (run.isPending) return <div className="spinner">Loading audit run…</div>;
  if (run.error) return <ErrorBox message={run.error.message} onRetry={() => run.refetch()} />;
  if (!run.data) return null;

  const d = run.data;
  const specs = buildAuditGraphNodes(d.stages);
  const selectedStage =
    selectedNode != null
      ? d.stages.find((s) => s.stage_name === `${selectedNode}:retry`) ??
        d.stages.find((s) => s.stage_name === selectedNode)
      : undefined;
  const pct = d.planned_count > 0 ? Math.min(100, Math.round((d.grounded_count / d.planned_count) * 100)) : 0;

  return (
    <div className="ui-fade">
      <div className="page-head">
        <div>
          <div className="muted">
            <Link to="/knowledge/audit-runs">Audit runs</Link> / Run
          </div>
          <h1 className="row">
            Audit run <StatusBadge status={d.status} />
          </h1>
        </div>
      </div>

      <div className="run-toolbar">
        <span className="muted">{d.repo_path ?? d.repo_id}</span>
        <span className="muted">trigger: {d.trigger}</span>
        <span className="muted">{d.verdicts_written} verdict(s) written</span>
        {d.langfuse_trace_id && (
          <span className="muted">
            trace <code>{d.langfuse_trace_id.slice(0, 12)}</code>
          </span>
        )}
      </div>

      <div className="section" style={{ maxWidth: 480 }}>
        <div className="row muted" style={{ justifyContent: "space-between", fontSize: 11.5 }}>
          <span>
            {d.grounded_count} of {d.planned_count} learnings checked
          </span>
          <span>{pct}%</span>
        </div>
        <div
          style={{
            height: 8,
            borderRadius: 4,
            background: "var(--border-default)",
            overflow: "hidden",
            marginTop: 4,
          }}
        >
          <div
            style={{
              height: "100%",
              width: `${pct}%`,
              background: "var(--state-success-text)",
              transition: "width 300ms ease",
            }}
          />
        </div>
      </div>

      {d.error && <ErrorBox message={d.error} />}

      <div className="section">
        <PipelineGraph
          stages={[]}
          trace={null}
          specs={specs}
          selectedNode={selectedNode}
          onSelectNode={setSelectedNode}
        />

        <div className="ui-card section" style={{ marginTop: 16 }}>
          {selectedStage == null ? (
            <p className="muted">Select a node above to see its artifact.</p>
          ) : (
            <>
              <h3 className="mono">{selectedStage.stage_name}</h3>
              <div className="row" style={{ gap: 8, alignItems: "center", marginTop: 4, marginBottom: 12 }}>
                <StatusBadge status={selectedStage.status} />
              </div>
              {selectedStage.error && <ErrorBox message={selectedStage.error} />}
              <pre className="mono" style={{ whiteSpace: "pre-wrap", fontSize: 11.5 }}>
                {selectedStage.artifact ? JSON.stringify(selectedStage.artifact, null, 2) : "(no artifact)"}
              </pre>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import ErrorBox from "../../../components/ErrorBox";
import SaveBar from "../../../components/SaveBar";
import Toggle from "../../../components/Toggle";
import {
  useRepositories,
  useSettings,
  useTriggerAudit,
  useUpdateSettings,
} from "../../../api/queries";
import { useDirtyForm } from "../useDirtyForm";

const FIELDS = [
  "audit_enabled",
  "audit_interval_days",
  "audit_learnings_per_run",
  "audit_max_parallel",
  "audit_auto_apply",
  "audit_auto_archive_min_pct",
] as const;

export default function AuditSection() {
  const { data, error, isPending, refetch } = useSettings();
  const save = useUpdateSettings();
  const form = useDirtyForm({});
  const repos = useRepositories(1, 100);
  const trigger = useTriggerAudit();
  const [repoId, setRepoId] = useState("");
  const [ranRunId, setRanRunId] = useState<string | null>(null);
  useEffect(() => {
    if (data) {
      const slice: Record<string, number | boolean> = {};
      for (const f of FIELDS) slice[f] = data.values[f] as number | boolean;
      form.rebase(slice);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  if (isPending) return <div className="spinner" />;
  if (error) return <ErrorBox message={error.message} onRetry={refetch} />;

  const onSave = () => {
    const changed: Record<string, number | boolean> = {};
    for (const k of form.dirtyKeys) changed[k] = form.draft[k] as number | boolean;
    save.mutate([changed], {
      onSuccess: (view) => {
        const slice: Record<string, number | boolean> = {};
        for (const f of FIELDS) slice[f] = view.values[f] as number | boolean;
        form.rebase(slice);
      },
    });
  };

  return (
    <div className="ui-card set-card">
      <h3>Learning auditor</h3>
      <p className="muted">
        Continuous agentic pass that checks due learnings (never checked, checked against code
        that's since changed, or overdue) against the real codebase and proposes archive/merge
        actions for review.
      </p>
      <div className="f-row">
        <div className="fl">
          <b>Auditor enabled</b>
          <span>
            process-start gate — takes effect on next restart{" "}
            <span className="badge">restart required</span>
          </span>
        </div>
        <Toggle
          checked={Boolean(form.draft.audit_enabled)}
          onChange={(v) => form.set("audit_enabled", v)}
          label="Auditor enabled"
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Re-check a learning after (days)</b>
          <span>a learning not checked for this many days becomes due again</span>
        </div>
        <input
          type="number"
          value={String(form.draft.audit_interval_days ?? "")}
          onChange={(e) => form.set("audit_interval_days", Number(e.target.value))}
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Learnings checked per run</b>
          <span>
            caps LLM spend per run; the scheduler keeps queueing runs while more are due
          </span>
        </div>
        <input
          type="number"
          value={String(form.draft.audit_learnings_per_run ?? "")}
          onChange={(e) => form.set("audit_learnings_per_run", Number(e.target.value))}
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Concurrent model calls</b>
          <span>GPU 2 has 2 slots and distillation uses one — usually 1</span>
        </div>
        <input
          type="number"
          value={String(form.draft.audit_max_parallel ?? "")}
          onChange={(e) => form.set("audit_max_parallel", Number(e.target.value))}
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Auto-apply verdicts</b>
          <span>archive automatically when verify agrees and confidence ≥ threshold</span>
        </div>
        <Toggle
          checked={Boolean(form.draft.audit_auto_apply)}
          onChange={(v) => form.set("audit_auto_apply", v)}
          label="Auto-apply verdicts"
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Auto-archive confidence threshold (%)</b>
          <span>only used while auto-apply is on; everything else still waits for a human</span>
        </div>
        <input
          type="number"
          value={String(form.draft.audit_auto_archive_min_pct ?? "")}
          onChange={(e) => form.set("audit_auto_archive_min_pct", Number(e.target.value))}
        />
      </div>
      {save.error ? <ErrorBox message={save.error.message} /> : null}
      <SaveBar
        count={form.dirtyKeys.length}
        onReset={form.reset}
        onSave={onSave}
        saving={save.isPending}
      />

      <div className="f-row" style={{ marginTop: 20, alignItems: "flex-start" }}>
        <div className="fl">
          <b>Run an audit now</b>
          <span>
            queues an audit for one repository immediately instead of waiting for its due
            learnings to accumulate. Runs on its own worker lane; watch its progress on the run
            page.
          </span>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <select value={repoId} onChange={(e) => setRepoId(e.target.value)}>
            <option value="">Select a repository…</option>
            {(repos.data?.items ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {r.project_path}
              </option>
            ))}
          </select>
          <button
            className="btn"
            disabled={!repoId || trigger.isPending}
            onClick={() => {
              setRanRunId(null);
              trigger.mutate([repoId], {
                onSuccess: (res) => setRanRunId(res.audit_run_id),
              });
            }}
          >
            {trigger.isPending ? "Queueing…" : "Run audit"}
          </button>
        </div>
      </div>
      {trigger.error ? <ErrorBox message={trigger.error.message} /> : null}
      {ranRunId ? (
        <p className="muted">
          Queued. <Link to={`/audit-runs/${ranRunId}`}>Watch this run</Link>.
        </p>
      ) : null}
    </div>
  );
}

import { useEffect, useState } from "react";
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
  "audit_max_clusters",
  "audit_auto_apply",
] as const;

export default function AuditSection() {
  const { data, error, isPending, refetch } = useSettings();
  const save = useUpdateSettings();
  const form = useDirtyForm({});
  const repos = useRepositories(1, 100);
  const trigger = useTriggerAudit();
  const [repoId, setRepoId] = useState("");
  const [ran, setRan] = useState<string | null>(null);
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
    <div className="nw-card set-card">
      <h3>Learning auditor</h3>
      <p className="muted">
        Periodic agentic pass that checks stored team learnings against the real codebase and
        proposes archive/merge actions for review — never applies anything automatically.
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
          <b>Audit interval</b>
          <span>days between audits for a given repo</span>
        </div>
        <input
          type="number"
          value={String(form.draft.audit_interval_days ?? "")}
          onChange={(e) => form.set("audit_interval_days", Number(e.target.value))}
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Max clusters per run</b>
          <span>caps LLM spend per repo per audit pass</span>
        </div>
        <input
          type="number"
          value={String(form.draft.audit_max_clusters ?? "")}
          onChange={(e) => form.set("audit_max_clusters", Number(e.target.value))}
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Auto-apply verdicts</b>
          <span>
            when off (recommended), verdicts only ever propose — a human approves every
            archive/merge in the Audit queue tab
          </span>
        </div>
        <Toggle
          checked={Boolean(form.draft.audit_auto_apply)}
          onChange={(v) => form.set("audit_auto_apply", v)}
          label="Auto-apply verdicts"
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
            audits one repository immediately instead of waiting for its interval. Runs
            synchronously and can take several minutes on a large repo; verdicts appear in the
            Audit queue as proposals.
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
              setRan(null);
              trigger.mutate([repoId], {
                onSuccess: (res) =>
                  setRan(
                    res.status === "done"
                      ? `Audited ${res.clusters ?? 0} cluster(s), ${res.verdicts ?? 0} verdict(s).`
                      : `Audit ${res.status}.`,
                  ),
              });
            }}
          >
            {trigger.isPending ? "Auditing…" : "Run audit"}
          </button>
        </div>
      </div>
      {trigger.error ? <ErrorBox message={trigger.error.message} /> : null}
      {ran ? <p className="muted">{ran}</p> : null}
    </div>
  );
}

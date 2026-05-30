import { useEffect } from "react";
import ErrorBox from "../../../components/ErrorBox";
import EnvChip from "../../../components/EnvChip";
import SaveBar from "../../../components/SaveBar";
import Toggle from "../../../components/Toggle";
import { useSettings, useUpdateSettings } from "../../../api/queries";
import { useDirtyForm } from "../useDirtyForm";

const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;
const FIELDS = ["langfuse_enabled", "langfuse_host", "log_level"] as const;

export default function ObservabilitySection() {
  const { data, error, isPending, refetch } = useSettings();
  const save = useUpdateSettings();
  const form = useDirtyForm({});
  useEffect(() => {
    if (data) {
      const slice: Record<string, string | boolean> = {};
      for (const f of FIELDS) slice[f] = data.values[f] as string | boolean;
      form.rebase(slice);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  if (isPending) return <div className="spinner" />;
  if (error) return <ErrorBox message={error.message} onRetry={refetch} />;

  const onSave = () => {
    const changed: Record<string, string | boolean> = {};
    for (const k of form.dirtyKeys) changed[k] = form.draft[k] as string | boolean;
    save.mutate([changed], {
      onSuccess: (view) => {
        const slice: Record<string, string | boolean> = {};
        for (const f of FIELDS) slice[f] = view.values[f] as string | boolean;
        form.rebase(slice);
      },
    });
  };

  return (
    <div className="nw-card set-card">
      <h3>Observability</h3>
      <p className="muted">Tracing and logging configuration for the review pipeline.</p>
      <div className="f-row">
        <div className="fl">
          <b>Langfuse enabled</b>
          <span>send traces to Langfuse</span>
        </div>
        <Toggle
          checked={Boolean(form.draft.langfuse_enabled)}
          onChange={(v) => form.set("langfuse_enabled", v)}
          label="Langfuse enabled"
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Langfuse host</b>
          <span>base URL of the Langfuse instance</span>
        </div>
        <input
          type="text"
          value={String(form.draft.langfuse_host ?? "")}
          onChange={(e) => form.set("langfuse_host", e.target.value)}
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Langfuse public key</b>
          <span>
            Secrets are never editable or readable from the UI. Rotate by updating the env and
            restarting.
          </span>
        </div>
        <div>
          {data?.secrets.langfuse_public_key ? (
            <EnvChip envVar="ARGUS_LANGFUSE_PUBLIC_KEY" />
          ) : (
            <span className="muted">not configured</span>
          )}
        </div>
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Langfuse secret key</b>
          <span>
            Secrets are never editable or readable from the UI. Rotate by updating the env and
            restarting.
          </span>
        </div>
        <div>
          {data?.secrets.langfuse_secret_key ? (
            <EnvChip envVar="ARGUS_LANGFUSE_SECRET_KEY" />
          ) : (
            <span className="muted">not configured</span>
          )}
        </div>
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Log level</b>
          <span>runtime log verbosity</span>
        </div>
        <select
          value={String(form.draft.log_level ?? "")}
          onChange={(e) => form.set("log_level", e.target.value)}
        >
          {LOG_LEVELS.map((l) => (
            <option key={l} value={l}>
              {l}
            </option>
          ))}
        </select>
      </div>
      {save.error ? <ErrorBox message={save.error.message} /> : null}
      <SaveBar
        count={form.dirtyKeys.length}
        onReset={form.reset}
        onSave={onSave}
        saving={save.isPending}
      />
    </div>
  );
}

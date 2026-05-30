import { useEffect } from "react";
import ErrorBox from "../../../components/ErrorBox";
import EnvChip from "../../../components/EnvChip";
import SaveBar from "../../../components/SaveBar";
import { useSettings, useUpdateSettings } from "../../../api/queries";
import { useDirtyForm } from "../useDirtyForm";

const FIELDS = [
  {
    key: "embedding_model",
    label: "Embedding model",
    help: "used for scout-stage semantic search",
  },
  {
    key: "ollama_base_url",
    label: "Ollama base URL",
    help: "local inference endpoint for embeddings",
  },
  {
    key: "distiller_model",
    label: "Distiller model",
    help: "model used to distill/summarize context",
  },
  {
    key: "distiller_api_base",
    label: "Distiller API base",
    help: "base URL for the distiller API",
  },
] as const;

export default function ModelsSection() {
  const { data, error, isPending, refetch } = useSettings();
  const save = useUpdateSettings();
  const form = useDirtyForm({});
  useEffect(() => {
    if (data) {
      const slice: Record<string, string> = {};
      for (const f of FIELDS) slice[f.key] = data.values[f.key] as string;
      form.rebase(slice);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  if (isPending) return <div className="spinner" />;
  if (error) return <ErrorBox message={error.message} onRetry={refetch} />;

  const onSave = () => {
    const changed: Record<string, string> = {};
    for (const k of form.dirtyKeys) changed[k] = form.draft[k] as string;
    save.mutate([changed], {
      onSuccess: (view) => {
        const slice: Record<string, string> = {};
        for (const f of FIELDS) slice[f.key] = view.values[f.key] as string;
        form.rebase(slice);
      },
    });
  };

  return (
    <div className="nw-card set-card">
      <h3>Models &amp; embeddings</h3>
      <p className="muted">Model endpoints used across the review pipeline.</p>
      {FIELDS.map((f) => (
        <div className="f-row" key={f.key}>
          <div className="fl">
            <b>{f.label}</b>
            <span>{f.help}</span>
          </div>
          <input
            type="text"
            value={String(form.draft[f.key] ?? "")}
            onChange={(e) => form.set(f.key, e.target.value)}
          />
        </div>
      ))}
      <div className="f-row">
        <div className="fl">
          <b>Distiller API key</b>
          <span>
            Secrets are never editable or readable from the UI. Rotate by updating the env and
            restarting.
          </span>
        </div>
        <div>
          {data?.secrets.distiller_api_key ? (
            <EnvChip envVar="ARGUS_DISTILLER_API_KEY" />
          ) : (
            <span className="muted">not configured</span>
          )}
        </div>
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

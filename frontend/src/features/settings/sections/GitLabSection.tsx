import { useEffect } from "react";
import ErrorBox from "../../../components/ErrorBox";
import EnvChip from "../../../components/EnvChip";
import SaveBar from "../../../components/SaveBar";
import Toggle from "../../../components/Toggle";
import { useSettings, useUpdateSettings } from "../../../api/queries";
import { useDirtyForm } from "../useDirtyForm";

const FIELDS = ["gitlab_url", "gitlab_ssl_verify"] as const;

export default function GitLabSection() {
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
      <h3>GitLab connection</h3>
      <p className="muted">Connection details for the GitLab instance being polled and reviewed.</p>
      <div className="f-row">
        <div className="fl">
          <b>GitLab URL</b>
          <span>base URL of the GitLab instance</span>
        </div>
        <input
          type="text"
          value={String(form.draft.gitlab_url ?? "")}
          onChange={(e) => form.set("gitlab_url", e.target.value)}
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Verify SSL</b>
          <span>disable only for self-signed internal instances</span>
        </div>
        <Toggle
          checked={Boolean(form.draft.gitlab_ssl_verify)}
          onChange={(v) => form.set("gitlab_ssl_verify", v)}
          label="Verify SSL"
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>GitLab token</b>
          <span>
            Secrets are never editable or readable from the UI. Rotate by updating the env and
            restarting.
          </span>
        </div>
        <div>
          {data?.secrets.gitlab_token ? (
            <EnvChip envVar="ARGUS_GITLAB_TOKEN" />
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

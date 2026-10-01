import { useEffect } from "react";
import ErrorBox from "../../../components/ErrorBox";
import SaveBar from "../../../components/SaveBar";
import Toggle from "../../../components/Toggle";
import { useSettings, useUpdateSettings } from "../../../api/queries";
import { useDirtyForm } from "../useDirtyForm";

const FIELDS = ["poller_enabled", "poll_interval_s"] as const;

export default function PollerSection() {
  const { data, error, isPending, refetch } = useSettings();
  const save = useUpdateSettings();
  const form = useDirtyForm({});
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
      <h3>Poller &amp; worker</h3>
      <p className="muted">
        Controls the background poller that fetches new merge requests from GitLab.
      </p>
      <div className="f-row">
        <div className="fl">
          <b>Poller enabled</b>
          <span>
            process-start gate — takes effect on next restart{" "}
            <span className="badge">restart required</span>
          </span>
        </div>
        <Toggle
          checked={Boolean(form.draft.poller_enabled)}
          onChange={(v) => form.set("poller_enabled", v)}
          label="Poller enabled"
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Poll interval</b>
          <span>seconds · repos can override · applies next cycle</span>
        </div>
        <input
          type="number"
          value={String(form.draft.poll_interval_s ?? "")}
          onChange={(e) => form.set("poll_interval_s", Number(e.target.value))}
        />
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

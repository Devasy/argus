import { useEffect } from "react";
import ErrorBox from "../../../components/ErrorBox";
import SaveBar from "../../../components/SaveBar";
import { useSettings, useUpdateSettings } from "../../../api/queries";
import { useDirtyForm } from "../useDirtyForm";

const FIELDS = [
  {
    key: "scout_max_rounds",
    label: "Scout max rounds",
    help: "agent rounds before the scout stage is cut off",
  },
  { key: "analysis_max_rounds", label: "Analysis max rounds", help: "per analyze fan-out chunk" },
  { key: "verify_max_rounds", label: "Verify max rounds", help: "adversarial verification pass" },
  {
    key: "qa_scenarios_max_rounds",
    label: "QA scenarios max rounds",
    help: "per-chunk QA test-scenario generation pass",
  },
  {
    key: "distiller_max_rounds",
    label: "Distiller max rounds",
    help: "learning distillation from human review comments — it searches existing learnings and reads code per point, so it needs a budget like scout's",
  },
  {
    key: "reasoning_budget_tokens",
    label: "Reasoning budget (tokens)",
    help: "self-hosted (ollama) reasoning models only — forces the model to stop thinking and answer within this many tokens",
  },
  {
    key: "max_inline_comments",
    label: "Max inline comments",
    help: "hard cap the publisher enforces per review",
  },
  { key: "max_chunk_files", label: "Max files per chunk", help: "controls analyzer fan-out width" },
  {
    key: "review_token_ceiling",
    label: "Token ceiling per review",
    help: "run aborts safely past this budget",
  },
  {
    key: "review_llm_timeout_s",
    label: "LLM request timeout (seconds)",
    help: "per-call ceiling before the client gives up on a slow model response",
  },
  {
    key: "model_context_window",
    label: "Model context window (tokens)",
    help: "total context size of the configured model — bounds output so input + output never overflow it",
  },
  {
    key: "model_output_margin",
    label: "Output margin (tokens)",
    help: "safety buffer reserved below the context window on top of the estimated input size",
  },
] as const;

export default function ReviewTuningSection() {
  const { data, error, isPending, refetch } = useSettings();
  const save = useUpdateSettings();
  const form = useDirtyForm({});
  useEffect(() => {
    if (data) {
      const slice: Record<string, number> = {};
      for (const f of FIELDS) slice[f.key] = data.values[f.key] as number;
      form.rebase(slice);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  if (isPending) return <div className="spinner" />;
  if (error) return <ErrorBox message={error.message} onRetry={refetch} />;

  const onSave = () => {
    const changed: Record<string, number> = {};
    for (const k of form.dirtyKeys) changed[k] = form.draft[k] as number;
    // useInvalidating-based mutations take their args as a tuple
    save.mutate([changed], {
      onSuccess: (view) => {
        const slice: Record<string, number> = {};
        for (const f of FIELDS) slice[f.key] = view.values[f.key] as number;
        form.rebase(slice);
      },
    });
  };

  return (
    <div className="nw-card set-card">
      <h3>Review tuning</h3>
      <p className="muted">
        Bounds every review run. Changes apply to the next run — running reviews keep the values
        they started with.
      </p>
      {FIELDS.map((f) => (
        <div className="f-row" key={f.key}>
          <div className="fl">
            <b>{f.label}</b>
            <span>{f.help}</span>
          </div>
          <input
            type="number"
            value={String(form.draft[f.key] ?? "")}
            onChange={(e) => form.set(f.key, Number(e.target.value))}
          />
        </div>
      ))}
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

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  useAgentUsage,
  useAvailableTools,
  useCreateAgent,
  useSetAgentDescription,
  useSetAgentEnabled,
  useUpdateAgent,
} from "../../api/queries";
import type { ReviewerAgent } from "../../api/types";
import ErrorBox from "../../components/ErrorBox";
import Toggle from "../../components/Toggle";
import { addCustomTool, normalizeAllowlist, toggleTool } from "./toolAllowlist";

export default function AgentEditor({
  agent,
  onTestOnMr,
}: {
  agent: ReviewerAgent | null; // null when creating a new agent
  onTestOnMr: (agentName: string) => void;
}) {
  const navigate = useNavigate();
  const current = agent?.current_version ?? null;
  const [name, setName] = useState(agent?.name ?? "");
  const [description, setDescription] = useState(agent?.description ?? "");
  const [model, setModel] = useState(current?.model ?? "");
  const [maxRounds, setMaxRounds] = useState(String(current?.max_rounds ?? 10));
  const [guidelines, setGuidelines] = useState(current?.guidelines ?? "");
  const [allowlist, setAllowlist] = useState<string[]>(
    normalizeAllowlist(current?.tool_allowlist ?? []),
  );
  const [customTool, setCustomTool] = useState("");
  const { data: availableTools } = useAvailableTools();
  const toggleableTools = (availableTools ?? []).filter((t) => !t.always_available);
  const alwaysAvailableTools = (availableTools ?? []).filter((t) => t.always_available);

  useEffect(() => {
    setName(agent?.name ?? "");
    setDescription(agent?.description ?? "");
    setModel(current?.model ?? "");
    setMaxRounds(String(current?.max_rounds ?? 10));
    setGuidelines(current?.guidelines ?? "");
    setAllowlist(normalizeAllowlist(current?.tool_allowlist ?? []));
    setCustomTool("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent?.id]);

  const createAgent = useCreateAgent();
  const updateAgent = useUpdateAgent();
  const setEnabled = useSetAgentEnabled();
  const setDescriptionMutation = useSetAgentDescription();
  const usage = useAgentUsage(agent?.id ?? "");
  const saving = createAgent.isPending || updateAgent.isPending || setDescriptionMutation.isPending;
  const error = createAgent.error ?? updateAgent.error ?? setDescriptionMutation.error;

  const submit = async () => {
    if (agent) {
      const trimmedDescription = description.trim() || null;
      const tasks = [
        updateAgent.mutateAsync([
          agent.id,
          {
            guidelines,
            model: model.trim() || null,
            max_rounds: Number(maxRounds),
            tool_allowlist: allowlist.length > 0 ? allowlist : null,
          },
        ]),
      ];
      if (trimmedDescription !== (agent.description ?? null)) {
        tasks.push(setDescriptionMutation.mutateAsync([agent.id, trimmedDescription]));
      }
      await Promise.all(tasks);
    } else {
      const created = await createAgent.mutateAsync([
        {
          name: name.trim(),
          description: description.trim() || null,
          guidelines,
          model: model.trim() || null,
          max_rounds: Number(maxRounds),
          tool_allowlist: allowlist.length > 0 ? allowlist : null,
        },
      ]);
      navigate(`/agents/${created.id}`);
    }
  };

  return (
    <div className="nw-card" style={{ padding: "18px 20px" }}>
      <div className="row" style={{ marginBottom: 14, flexWrap: "wrap", gap: 10 }}>
        <b style={{ fontSize: 16 }}>{agent ? agent.name : "New reviewer agent"}</b>
        {agent?.current_version && (
          <span className="badge">v{agent.current_version.version} current</span>
        )}
        {agent && usage.data && (
          <span className="muted" style={{ fontSize: 11.5 }}>
            used in {usage.data.review_count} review{usage.data.review_count === 1 ? "" : "s"}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {agent && (
          <>
            <Toggle
              checked={agent.enabled}
              onChange={(v) => setEnabled.mutate([agent.id, v])}
              label="enabled"
            />
            <button className="btn-o" onClick={() => onTestOnMr(agent.name)}>
              Test on an MR
            </button>
          </>
        )}
      </div>

      {error && <ErrorBox message={error.message} />}

      <div className="form-grid">
        {!agent && (
          <div>
            <label htmlFor="a-name">Name (immutable after creation)</label>
            <input id="a-name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </div>
        )}
        <div>
          <label htmlFor="a-desc">Description (shown to Scout for its assignment judgment)</label>
          <input
            id="a-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g. Checks auth/crypto/secrets handling for injection and misuse"
          />
        </div>
        <div>
          <label htmlFor="a-model">
            Model override (optional — falls back to the review's default)
          </label>
          <input id="a-model" value={model} onChange={(e) => setModel(e.target.value)} />
        </div>
        <div>
          <label htmlFor="a-rounds">Max rounds</label>
          <input
            id="a-rounds"
            type="number"
            min={1}
            value={maxRounds}
            onChange={(e) => setMaxRounds(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="a-guidelines">
            Guidelines — the agent&apos;s reviewable contract, layered on top of the shared profile
          </label>
          <textarea
            id="a-guidelines"
            rows={10}
            value={guidelines}
            onChange={(e) => setGuidelines(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="a-allowlist-custom">Tool allowlist (empty = all tools)</label>
          <div className="row" style={{ gap: 8, marginBottom: 8 }}>
            {Array.from(
              new Set([...toggleableTools.map((t) => t.name), ...allowlist]),
            ).map((toolName) => {
              const on = allowlist.includes(toolName);
              const tool = toggleableTools.find((t) => t.name === toolName);
              return (
                <button
                  key={toolName}
                  type="button"
                  className={`tool-chip${on ? " on" : ""}`}
                  title={tool?.description}
                  onClick={() => setAllowlist((prev) => toggleTool(prev, toolName))}
                >
                  {toolName}
                </button>
              );
            })}
          </div>
          {alwaysAvailableTools.length > 0 && (
            <p className="muted" style={{ fontSize: 11.5, marginBottom: 8 }}>
              Always available to every agent regardless of this list:{" "}
              {alwaysAvailableTools.map((t) => t.name).join(", ")}.
            </p>
          )}
          <div className="row" style={{ gap: 8 }}>
            <input
              id="a-allowlist-custom"
              value={customTool}
              onChange={(e) => setCustomTool(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  setAllowlist((prev) => addCustomTool(prev, customTool));
                  setCustomTool("");
                }
              }}
              placeholder="add custom tool name"
            />
            <button
              type="button"
              className="btn-g"
              onClick={() => {
                setAllowlist((prev) => addCustomTool(prev, customTool));
                setCustomTool("");
              }}
            >
              Add
            </button>
          </div>
        </div>
        <div className="form-actions">
          <button
            className="btn-p"
            onClick={submit}
            disabled={saving || !guidelines.trim() || (!agent && !name.trim())}
          >
            {saving
              ? "Saving…"
              : agent
                ? `Save as v${(current?.version ?? 0) + 1}`
                : "Create agent"}
          </button>
        </div>
      </div>
    </div>
  );
}

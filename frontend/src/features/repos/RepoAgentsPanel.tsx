import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { useRepoAgents, useSetRepoAgent } from "../../api/queries";
import Toggle from "../../components/Toggle";

/** Which specialist reviewers Scout may assign on this repository.
 *
 * Agents are global and enabled by default, so every specialist was offered on
 * every repo -- including ones where it has nothing useful to say. Turning one
 * off here is an override for THIS repo only.
 *
 * Presented as one settings row, not one row per agent: there are eight of
 * them with paragraph-length descriptions, and rendering those expanded pushed
 * the merge-request list off the screen. The row states how many are on, and
 * the descriptions stay behind a disclosure for the moment you actually need
 * to tell two similar agents apart. */
export default function RepoAgentsPanel({ repoId }: { repoId: string }) {
  const agents = useRepoAgents(repoId);
  const setAgent = useSetRepoAgent(repoId);
  const [showDetail, setShowDetail] = useState(false);

  const rows = agents.data ?? [];
  const runnable = rows.filter((a) => a.globally_enabled);
  const onHere = runnable.filter((a) => a.enabled_here);

  return (
    <div className="f-row">
      <div className="fl">
        <b>Specialist reviewers</b>
        <span>
          which agents Scout may assign here. Off elsewhere is unaffected.
        </span>
      </div>

      <div>
        <div className="agent-scope-head">
          <span className="muted">
            {agents.isPending
              ? "Loading…"
              : `${onHere.length} of ${runnable.length} enabled`}
          </span>
          {rows.length > 0 && (
            <button
              type="button"
              className="linkish"
              aria-expanded={showDetail}
              onClick={() => setShowDetail((v) => !v)}
            >
              {showDetail ? "Hide" : "What each agent does"}
              <ChevronDown
                size={13}
                style={{
                  transform: showDetail ? "rotate(180deg)" : undefined,
                  transition: "transform 120ms ease",
                }}
              />
            </button>
          )}
        </div>

        {!agents.isPending && rows.length === 0 && (
          <p className="muted" style={{ margin: 0 }}>
            No reviewer agents defined yet.
          </p>
        )}

        <div className="agent-scope-grid">
          {rows.map((a) => (
            <div className="agent-scope-item" key={a.agent_id}>
              <Toggle
                checked={a.enabled_here && a.globally_enabled}
                // A globally-disabled agent cannot run anywhere, so a per-repo
                // toggle would promise something it cannot deliver.
                disabled={!a.globally_enabled || setAgent.isPending}
                onChange={(next) => setAgent.mutate([a.agent_id, next])}
                label={`${a.name} on this repo`}
              />
              <div className="agent-scope-text">
                <span className="agent-scope-name">
                  {a.name}
                  {!a.globally_enabled && (
                    <em className="agent-scope-off"> off globally</em>
                  )}
                </span>
                {showDetail && a.description && (
                  <span className="agent-scope-desc">{a.description}</span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

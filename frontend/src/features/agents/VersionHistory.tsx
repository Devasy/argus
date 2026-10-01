import { useState } from "react";
import { useAgentVersions, useUpdateAgent } from "../../api/queries";
import { diffLines } from "../../lib/textDiff";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";

function formatRate(rate: number | null): string {
  if (rate === null) return "no verdicts yet";
  return `${Math.round(rate * 100)}% accepted`;
}

export default function VersionHistory({ agentId }: { agentId: string }) {
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const versions = useAgentVersions(agentId, page, perPage);
  // The "current" version (highest version number) must stay visible for the
  // diff feature regardless of which page is showing — fetch it separately
  // rather than relying on page 1 of the paginated view being in scope.
  const currentPage = useAgentVersions(agentId, 1, 1);
  const updateAgent = useUpdateAgent();
  const [diffAgainst, setDiffAgainst] = useState<string | null>(null);

  if (versions.isPending || currentPage.isPending) {
    return <div className="spinner">Loading version history…</div>;
  }
  if (versions.error) {
    return <ErrorBox message={versions.error.message} onRetry={versions.refetch} />;
  }
  if (currentPage.error) {
    return <ErrorBox message={currentPage.error.message} onRetry={currentPage.refetch} />;
  }

  const list = versions.data!.items; // newest first, per the API contract
  const current = currentPage.data!.items[0];

  const restore = (versionId: string) => {
    const target = list.find((v) => v.id === versionId);
    if (!target) return;
    updateAgent.mutate([
      agentId,
      {
        guidelines: target.guidelines,
        model: target.model,
        max_rounds: target.max_rounds,
        tool_allowlist: target.tool_allowlist,
      },
    ]);
  };

  return (
    <div className="nw-card" style={{ padding: "18px 20px", marginTop: 14 }}>
      <div className="muted" style={{ fontSize: 11, marginBottom: 10 }}>
        VERSION HISTORY
      </div>
      {updateAgent.error && <ErrorBox message={updateAgent.error.message} />}
      <div className="vrail">
        {list.map((v) => {
          const isCurrent = v.id === current.id;
          return (
          <div key={v.id} className={`v-item${isCurrent ? " cur" : ""}`}>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <b>v{v.version}</b>
              {isCurrent && <span className="badge">current</span>}
              <span className="muted" style={{ fontSize: 11.5 }}>
                {new Date(v.created_at).toLocaleDateString()}
                {v.created_by ? ` · ${v.created_by}` : ""}
              </span>
              <span className="muted" style={{ fontSize: 11.5 }}>
                {v.accepted}/{v.accepted + v.rejected} · {formatRate(v.acceptance_rate)}
              </span>
              <span style={{ flex: 1 }} />
              {!isCurrent && (
                <>
                  <button
                    className="btn-g"
                    onClick={() => setDiffAgainst(diffAgainst === v.id ? null : v.id)}
                  >
                    {diffAgainst === v.id ? "hide diff" : `diff vs current`}
                  </button>
                  <button
                    className="btn-g"
                    onClick={() => restore(v.id)}
                    disabled={updateAgent.isPending}
                  >
                    restore
                  </button>
                </>
              )}
            </div>
            {diffAgainst === v.id && (
              <pre
                className="mono"
                style={{
                  fontSize: 11.5,
                  background: "var(--bg-raised)",
                  padding: "10px 12px",
                  borderRadius: "var(--radius-md)",
                  marginTop: 8,
                  overflowX: "auto",
                }}
              >
                {diffLines(v.guidelines, current.guidelines).map((line, i) => (
                  <div
                    key={i}
                    style={{
                      color:
                        line.kind === "added"
                          ? "var(--state-success-text)"
                          : line.kind === "removed"
                            ? "var(--state-error-text)"
                            : "inherit",
                    }}
                  >
                    {line.kind === "added" ? "+ " : line.kind === "removed" ? "- " : "  "}
                    {line.text}
                  </div>
                ))}
              </pre>
            )}
          </div>
          );
        })}
      </div>
      <div style={{ marginTop: 12 }}>
        <PaginationBar
          page={versions.data!.page}
          perPage={versions.data!.per_page}
          total={versions.data!.total}
          onPageChange={setPage}
          onPerPageChange={(n) => {
            setPerPage(n);
            setPage(1);
          }}
        />
      </div>
    </div>
  );
}

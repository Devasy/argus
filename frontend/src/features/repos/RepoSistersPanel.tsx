import { useEffect, useState } from "react";
import { useRepoSisters, useRepositories, useSetRepoSisters } from "../../api/queries";
import type { RepoSisterLinkIn } from "../../api/types";
import Toggle from "../../components/Toggle";

const MAX_SISTERS = 5;

/** Related repositories reviews of THIS repo may read for context (reference only, never
 * commented on). Empty by default. Saved as a whole list with its own button, so it stays
 * independent of the surrounding repository form. */
export default function RepoSistersPanel({ repoId }: { repoId: string }) {
  const sisters = useRepoSisters(repoId);
  const repos = useRepositories(1, 200);
  const save = useSetRepoSisters(repoId);
  const [rows, setRows] = useState<RepoSisterLinkIn[]>([]);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (sisters.data && !dirty) {
      setRows(
        sisters.data.map(({ sister_repo_id, branch, match_source_branch, enabled }) => ({
          sister_repo_id, branch, match_source_branch, enabled,
        })),
      );
    }
  }, [sisters.data, dirty]);

  const others = (repos.data?.items ?? []).filter((r) => r.id !== repoId);
  const defaultBranch = (id: string) =>
    sisters.data?.find((s) => s.sister_repo_id === id)?.sister_default_branch ?? null;

  const update = (i: number, patch: Partial<RepoSisterLinkIn>) => {
    setRows((prev) => prev.map((r, j) => (j === i ? { ...r, ...patch } : r)));
    setDirty(true);
  };
  const remove = (i: number) => {
    setRows((prev) => prev.filter((_, j) => j !== i));
    setDirty(true);
  };
  const add = () => {
    const taken = new Set(rows.map((r) => r.sister_repo_id));
    const next = others.find((r) => !taken.has(r.id));
    if (!next) return;
    setRows((prev) => [
      ...prev,
      { sister_repo_id: next.id, branch: null, match_source_branch: true, enabled: true },
    ]);
    setDirty(true);
  };
  const onSave = () =>
    save.mutate(
      [rows.map((r) => ({ ...r, branch: r.branch?.trim() ? r.branch.trim() : null }))],
      { onSuccess: () => setDirty(false) },
    );

  return (
    <div className="f-row">
      <div className="fl">
        <b>Related repositories</b>
        <span>
          reviews of this repo can read these for context (DTOs, contracts, shared types). They
          never comment on them. Uses the MR&apos;s source branch name, then its target branch
          name, when the related repo has it; otherwise the branch below (blank = its default).
        </span>
      </div>

      <div>
        {sisters.isPending && <span className="muted">Loading…</span>}
        {!sisters.isPending && rows.length === 0 && (
          <p className="muted" style={{ margin: 0 }}>
            None. Reviews only see this repository.
          </p>
        )}

        {rows.map((r, i) => (
          <div
            key={`${r.sister_repo_id}-${i}`}
            style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6, flexWrap: "wrap" }}
          >
            <select
              value={r.sister_repo_id}
              aria-label="Related repository"
              onChange={(e) => update(i, { sister_repo_id: e.target.value })}
            >
              {others.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.project_path}
                </option>
              ))}
            </select>
            <input
              value={r.branch ?? ""}
              aria-label="Branch"
              placeholder={`branch (default: ${defaultBranch(r.sister_repo_id) ?? "repo default"})`}
              onChange={(e) => update(i, { branch: e.target.value })}
            />
            <Toggle
              checked={r.match_source_branch}
              onChange={(next) => update(i, { match_source_branch: next })}
              label="Prefer the MR's branch names"
            />
            <span className="muted">prefer MR branches</span>
            <button type="button" className="linkish" onClick={() => remove(i)}>
              Remove
            </button>
          </div>
        ))}

        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 4 }}>
          <button
            type="button"
            className="btn-o"
            onClick={add}
            disabled={rows.length >= MAX_SISTERS || others.length <= rows.length}
          >
            Add related repo
          </button>
          {dirty && (
            <button type="button" className="btn-p" onClick={onSave} disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save related repos"}
            </button>
          )}
          {save.isError && (
            <span className="muted" role="alert">
              {(save.error as Error).message}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

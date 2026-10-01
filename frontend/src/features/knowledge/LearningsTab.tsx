import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Search } from "lucide-react";
import { useLearnings, useLearningsSearch, useRepositories } from "../../api/queries";
import type { Learning } from "../../api/types";
import ErrorBox from "../../components/ErrorBox";
import FilterBuilder from "../../components/FilterBuilder";
import PaginationBar from "../../components/PaginationBar";
import { buildQueryParams, newConditionId, type Condition } from "../../components/filterConditions";
import { learningsFilterFields } from "./learningsFilterFields";

const DEFAULT_CONDITIONS: Condition[] = [
  { id: newConditionId(), field: "status", operator: "is", value: "active" },
];

function fmtPct(rate: number | null): string {
  return rate === null ? "no verdicts yet" : `${Math.round(rate * 100)}% hit rate`;
}

function isTrueButUnused(l: Learning): boolean {
  return l.groundedness !== null && l.groundedness > 0.6 && l.reputation < 0.4;
}

function strengthClass(reputation: number): string {
  if (reputation > 0.6) return "green";
  if (reputation < 0.4) return "red";
  return "";
}

function groundedClass(groundedness: number): string {
  if (groundedness > 0.6) return "green";
  if (groundedness < 0.4) return "red";
  return "";
}

export default function LearningsTab() {
  // Draft state is what the user is editing; applied state is what the query
  // hooks actually use. Nothing fetches until Search is submitted.
  const [draftConditions, setDraftConditions] = useState<Condition[]>(DEFAULT_CONDITIONS);
  const [appliedConditions, setAppliedConditions] = useState<Condition[]>(DEFAULT_CONDITIONS);
  const [draftQuery, setDraftQuery] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const repos = useRepositories(1, 200);

  const fields = useMemo(
    () =>
      learningsFilterFields(
        (repos.data?.items ?? []).map((r) => ({ value: r.id, label: r.project_path })),
      ),
    [repos.data],
  );

  const conditionParams = useMemo(
    () => buildQueryParams(fields, appliedConditions),
    [fields, appliedConditions],
  );

  const listResult = useLearnings({ ...conditionParams, page, per_page: perPage });
  const searchResult = useLearningsSearch(query, conditionParams);
  const searching = query.trim().length > 0;
  const active = searching ? searchResult : listResult;

  function applyFilters() {
    setAppliedConditions(draftConditions);
    setQuery(draftQuery);
    setPage(1);
  }

  const repoNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const r of repos.data?.items ?? []) map.set(r.id, r.project_path);
    return map;
  }, [repos.data]);

  const items = active.data?.items ?? [];

  return (
    <div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          applyFilters();
        }}
      >
        <div className="search-row">
          <Search size={15} />
          <input
            placeholder="Semantic search across learnings — e.g. how do we handle retries?"
            value={draftQuery}
            onChange={(e) => setDraftQuery(e.target.value)}
          />
        </div>

        <div className="nw-card" style={{ marginBottom: 14, marginTop: 12 }}>
          <FilterBuilder fields={fields} conditions={draftConditions} onChange={setDraftConditions} />
          <div className="row" style={{ justifyContent: "flex-end", marginTop: 12 }}>
            <button type="submit" className="btn-p">
              Search
            </button>
          </div>
        </div>
      </form>

      {active.isPending && <div className="spinner">Loading learnings…</div>}
      {active.error && <ErrorBox message={active.error.message} onRetry={active.refetch} />}

      {active.data && items.length === 0 && (
        <div className="nw-card" style={{ padding: 24, textAlign: "center" }}>
          <p className="muted">No learnings match these filters.</p>
        </div>
      )}

      {items.map((l) => (
        <div className="nw-card learning-card" key={l.id}>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <b>{l.topic}</b>
            <span
              className={`badge ${
                l.kind === "do_not_suggest" ? "red" : l.kind === "missed_pattern" ? "yellow" : "green"
              }`}
            >
              {l.kind === "do_not_suggest"
                ? "do-not-suggest"
                : l.kind === "missed_pattern"
                  ? "missed-pattern"
                  : "guidance"}
            </span>
            <span className="badge">
              {l.repo_id ? (repoNameById.get(l.repo_id) ?? "repo-specific") : "global"}
            </span>
            {l.status === "archived" && <span className="badge">archived</span>}
            <span className={`badge ${strengthClass(l.reputation)}`}>
              Strength {Math.round(l.reputation * 100)}%
            </span>
            {l.groundedness !== null ? (
              <span
                className={`badge ${groundedClass(l.groundedness)}`}
                title={
                  l.last_audited_at
                    ? `Last audited ${new Date(l.last_audited_at).toLocaleString()}`
                    : "Not yet audited"
                }
              >
                Grounded {Math.round(l.groundedness * 100)}%
              </span>
            ) : (
              <span className="badge" title="Not yet audited">
                Grounded —
              </span>
            )}
            {isTrueButUnused(l) && (
              <span
                className="badge yellow"
                title="Auditor confirms this is correct, but it isn't earning hits from real usage — rewrite it, don't delete it."
              >
                True but unused
              </span>
            )}
            {l.file_pattern && <span className="fileref mono">{l.file_pattern}</span>}
          </div>
          <div className="learning-hint">
            <ReactMarkdown>{l.hint_text}</ReactMarkdown>
          </div>
          <div className="row muted" style={{ fontSize: 11.5, gap: 14 }}>
            <span>{l.hit_count} hits</span>
            <span>{l.miss_count} misses</span>
            <span>{fmtPct(l.hit_rate)}</span>
            <span>
              Verdicts: {l.hit_count}&#10003; / {l.harmful_count}&#10007; / {l.ignored_count}&#8211;
            </span>
            <span>{new Date(l.created_at).toLocaleDateString()}</span>
          </div>
          {l.mr_iid != null && (
            <div className="row muted" style={{ fontSize: 11.5, gap: 6 }}>
              <span>Learnt from:</span>
              {l.mr_web_url ? (
                <a href={l.mr_web_url} target="_blank" rel="noreferrer">
                  !{l.mr_iid} {l.mr_title}
                </a>
              ) : (
                <span>
                  !{l.mr_iid} {l.mr_title}
                </span>
              )}
              {l.learned_from_username && <span>• @{l.learned_from_username}</span>}
            </div>
          )}
        </div>
      ))}

      {!searching && listResult.data && (
        <PaginationBar
          page={listResult.data.page}
          perPage={listResult.data.per_page}
          total={listResult.data.total}
          onPageChange={setPage}
          onPerPageChange={(n) => {
            setPerPage(n);
            setPage(1);
          }}
        />
      )}
    </div>
  );
}

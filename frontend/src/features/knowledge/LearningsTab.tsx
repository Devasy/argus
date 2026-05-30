import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Search } from "lucide-react";
import { useLearnings, useLearningsSearch, useRepositories } from "../../api/queries";
import type { Learning } from "../../api/types";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";

type KindFilter = "all" | "guidance" | "do_not_suggest" | "missed_pattern";
type ScopeFilter = "all" | "global" | "repo";
type StatusFilter = "all" | "active" | "archived";
type PerfFilter = "all" | "performing" | "underperforming" | "no-data";

function matchesPerf(l: Learning, f: PerfFilter): boolean {
  if (f === "all") return true;
  if (f === "no-data") return l.hit_rate === null;
  if (l.hit_rate === null) return false;
  return f === "performing" ? l.hit_rate >= 0.5 : l.hit_rate < 0.5;
}

function fmtPct(rate: number | null): string {
  return rate === null ? "no verdicts yet" : `${Math.round(rate * 100)}% hit rate`;
}

function needsAttention(l: Learning): boolean {
  return (l.groundedness !== null && l.groundedness < 0.4) || l.harmful_count > 0;
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
  const [kind, setKind] = useState<KindFilter>("all");
  const [scope, setScope] = useState<ScopeFilter>("all");
  const [status, setStatus] = useState<StatusFilter>("active");
  const [perf, setPerf] = useState<PerfFilter>("all");
  const [attentionOnly, setAttentionOnly] = useState(false);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const repos = useRepositories(1, 200);

  const filters = useMemo(() => {
    const f: { kind?: string; status?: string; page?: number; per_page?: number } = {
      page,
      per_page: perPage,
    };
    if (kind !== "all") f.kind = kind;
    if (status !== "all") f.status = status;
    return f;
  }, [kind, status, page, perPage]);

  const listResult = useLearnings(filters);
  const searchResult = useLearningsSearch(query, {});
  const searching = query.trim().length > 0;
  const active = searching ? searchResult : listResult;

  const repoNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const r of repos.data?.items ?? []) map.set(r.id, r.project_path);
    return map;
  }, [repos.data]);

  const displayed = (active.data?.items ?? []).filter((l) => {
    if (scope === "global" && l.repo_id !== null) return false;
    if (scope === "repo" && l.repo_id === null) return false;
    if (attentionOnly && !needsAttention(l)) return false;
    return matchesPerf(l, perf);
  });

  return (
    <div>
      <div className="search-row">
        <Search size={15} />
        <input
          placeholder="Semantic search across learnings — e.g. how do we handle retries?"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {!searching && (
        <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
          {(["all", "guidance", "do_not_suggest", "missed_pattern"] as const).map((k) => (
            <button
              key={k}
              className={`chip${kind === k ? " on" : ""}`}
              onClick={() => {
                setKind(k);
                setPage(1);
              }}
            >
              {k === "all"
                ? "All kinds"
                : k === "guidance"
                  ? "Guidance"
                  : k === "do_not_suggest"
                    ? "Do-not-suggest"
                    : "Missed pattern"}
            </button>
          ))}
          <span className="chip-divider" />
          {(["all", "global", "repo"] as const).map((s) => (
            <button
              key={s}
              className={`chip${scope === s ? " on" : ""}`}
              onClick={() => setScope(s)}
            >
              {s === "all" ? "All scopes" : s === "global" ? "Global" : "Repo-specific"}
            </button>
          ))}
          <span className="chip-divider" />
          {(["active", "archived", "all"] as const).map((s) => (
            <button
              key={s}
              className={`chip${status === s ? " on" : ""}`}
              onClick={() => {
                setStatus(s);
                setPage(1);
              }}
            >
              {s === "all" ? "All statuses" : s === "active" ? "Active" : "Archived"}
            </button>
          ))}
          <span className="chip-divider" />
          {(["all", "performing", "underperforming", "no-data"] as const).map((p) => (
            <button key={p} className={`chip${perf === p ? " on" : ""}`} onClick={() => setPerf(p)}>
              {p === "all"
                ? "Any performance"
                : p === "performing"
                  ? "Performing well"
                  : p === "underperforming"
                    ? "Underperforming"
                    : "No verdicts yet"}
            </button>
          ))}
          <span className="chip-divider" />
          <button
            className={`chip${attentionOnly ? " on" : ""}`}
            onClick={() => setAttentionOnly((v) => !v)}
          >
            Needs attention
          </button>
        </div>
      )}

      {active.isPending && <div className="spinner">Loading learnings…</div>}
      {active.error && <ErrorBox message={active.error.message} onRetry={active.refetch} />}

      {active.data && displayed.length === 0 && (
        <div className="nw-card" style={{ padding: 24, textAlign: "center" }}>
          <p className="muted">No learnings match these filters.</p>
        </div>
      )}

      {displayed.map((l) => (
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

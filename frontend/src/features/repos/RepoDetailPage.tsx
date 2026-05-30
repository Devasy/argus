import { useEffect, useState } from "react";
import { ExternalLink, Pencil } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  useMergeRequests,
  useProfiles,
  useRepository,
  useUpdateRepository,
} from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";
import StatusBadge from "../../components/StatusBadge";
import Toggle from "../../components/Toggle";
import RepoAgentsPanel from "./RepoAgentsPanel";

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

function CommentCounts({
  accepted,
  rejected,
  leftover,
}: {
  accepted: number;
  rejected: number;
  leftover: number;
}) {
  if (accepted + rejected + leftover === 0) {
    return <span className="muted">—</span>;
  }
  return (
    <div className="row" style={{ gap: 8 }}>
      {accepted > 0 && (
        <span style={{ color: "var(--state-success-text)" }} title="Accepted">
          {accepted}✓
        </span>
      )}
      {rejected > 0 && (
        <span style={{ color: "var(--state-error-text)" }} title="Rejected">
          {rejected}✗
        </span>
      )}
      {leftover > 0 && (
        <span style={{ color: "var(--text-tertiary)" }} title="Leftover / unresolved">
          {leftover}…
        </span>
      )}
    </div>
  );
}

const DEFAULT_PROFILE_VALUE = "__default__";

type StateFilter = "all" | "opened" | "merged" | "closed";

function EditRepoSettings({
  repoId,
  pollIntervalS,
  defaultProfileId,
  autoReviewEnabled,
  onClose,
}: {
  repoId: string;
  pollIntervalS: number;
  defaultProfileId: string | null;
  autoReviewEnabled: boolean;
  onClose: () => void;
}) {
  const profiles = useProfiles();
  const updateRepository = useUpdateRepository();
  const [pollInterval, setPollInterval] = useState(String(pollIntervalS));
  const [profileId, setProfileId] = useState(defaultProfileId ?? DEFAULT_PROFILE_VALUE);
  const [autoReview, setAutoReview] = useState(autoReviewEnabled);

  const submit = async () => {
    try {
      await updateRepository.mutateAsync([
        repoId,
        {
          poll_interval_s: Number(pollInterval),
          default_profile_id: profileId === DEFAULT_PROFILE_VALUE ? null : profileId,
          auto_review_enabled: autoReview,
        },
      ]);
      onClose();
    } catch {
      /* error surfaced via updateRepository.error */
    }
  };

  return (
    <div className="nw-card section">
      {updateRepository.error && <ErrorBox message={updateRepository.error.message} />}
      <div className="f-row">
        <div className="fl">
          <b>Poll interval</b>
          <span>seconds between GitLab polls for this repository</span>
        </div>
        <input
          type="number"
          min={1}
          value={pollInterval}
          onChange={(e) => setPollInterval(e.target.value)}
        />
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Default profile</b>
          <span>reviewer profile used when a trigger doesn't specify one</span>
        </div>
        <select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
          <option value={DEFAULT_PROFILE_VALUE}>default (built-in)</option>
          {(profiles.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </div>
      <div className="f-row">
        <div className="fl">
          <b>Auto-review on poll</b>
          <span>
            automatically queue an incremental review when the poller sees a new commit on an
            open, non-draft MR
          </span>
        </div>
        <Toggle checked={autoReview} onChange={setAutoReview} label="Auto-review on poll" />
      </div>
      <RepoAgentsPanel repoId={repoId} />
      <div className="form-actions">
        <button className="btn-o" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn-p"
          onClick={submit}
          disabled={updateRepository.isPending || !pollInterval.trim() || Number(pollInterval) <= 0}
        >
          {updateRepository.isPending ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

export default function RepoDetail() {
  const { repoId } = useParams<{ repoId: string }>();
  const navigate = useNavigate();
  const repo = useRepository(repoId!);
  const [editing, setEditing] = useState(false);
  const [stateFilter, setStateFilter] = useState<StateFilter>("all");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const stateParam = stateFilter === "all" ? undefined : stateFilter;
  const mrs = useMergeRequests(repoId!, stateParam, page, perPage);

  useEffect(() => {
    setEditing(false);
  }, [repoId]);

  const items = mrs.data?.items ?? [];
  const counts = mrs.data?.state_counts ?? { all: 0, opened: 0, merged: 0, closed: 0 };

  return (
    <div className="nw-fade">
      <div className="page-head">
        <div>
          <div className="muted">
            <Link to="/repos">Repositories</Link> /
          </div>
          <h1 className="mono">{repo.data?.project_path ?? "Repository"}</h1>
          {repo.data && !repo.data.enabled && (
            <div style={{ marginTop: 6 }}>
              <StatusBadge status="canceled" label="Disabled" />
            </div>
          )}
        </div>
        {repo.data && !editing && (
          <button className="btn-o" onClick={() => setEditing(true)}>
            <Pencil size={15} /> Edit settings
          </button>
        )}
      </div>

      {repo.error && <ErrorBox message={repo.error.message} onRetry={repo.refetch} />}

      {repo.data && editing && (
        <EditRepoSettings
          repoId={repo.data.id}
          pollIntervalS={repo.data.poll_interval_s}
          defaultProfileId={repo.data.default_profile_id}
          autoReviewEnabled={repo.data.auto_review_enabled}
          onClose={() => setEditing(false)}
        />
      )}

      {mrs.error && <ErrorBox message={mrs.error.message} onRetry={mrs.refetch} />}
      {mrs.isPending && <div className="spinner">Loading merge requests…</div>}

      {mrs.data && (
        <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
          {(["all", "opened", "merged", "closed"] as const).map((s) => (
            <button
              key={s}
              className={`chip${stateFilter === s ? " on" : ""}`}
              onClick={() => {
                setStateFilter(s);
                setPage(1);
              }}
            >
              {s === "all" ? "All" : s === "opened" ? "Open" : s === "merged" ? "Merged" : "Closed"}{" "}
              ({counts[s]})
            </button>
          ))}
        </div>
      )}

      {mrs.data && (
        <div className="table-wrap">
          <table className="nw-table">
            <thead>
              <tr>
                <th>!IID</th>
                <th className="col-fill">Title</th>
                <th>Author</th>
                <th>State</th>
                <th title="Bot review comments by outcome: accepted / rejected / leftover (open, answered, dismissed, or unresolved)">
                  Comments
                </th>
                <th>Updated</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 && (
                <tr>
                  <td colSpan={7} className="muted">
                    {stateFilter === "all"
                      ? "No merge requests synced yet — the poller picks them up on its next cycle."
                      : "No merge requests match this filter."}
                  </td>
                </tr>
              )}
              {items.map((mr) => (
                <tr
                  key={mr.id}
                  className="clickable"
                  onClick={() => navigate(`/mrs/${mr.id}`, { state: { mrTitle: mr.title } })}
                >
                  <td className="mono">!{mr.mr_iid}</td>
                  <td className="col-fill">{mr.title}</td>
                  <td>{mr.author_username ?? "—"}</td>
                  <td>
                    <StatusBadge status={mr.state} />
                  </td>
                  <td>
                    <CommentCounts
                      accepted={mr.accepted_count}
                      rejected={mr.rejected_count}
                      leftover={mr.leftover_count}
                    />
                  </td>
                  <td className="muted">{formatDate(mr.mr_updated_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <a href={mr.web_url} target="_blank" rel="noreferrer" title="Open in GitLab">
                      <ExternalLink size={14} />
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <PaginationBar
            page={mrs.data.page}
            perPage={mrs.data.per_page}
            total={mrs.data.total}
            onPageChange={setPage}
            onPerPageChange={(n) => {
              setPerPage(n);
              setPage(1);
            }}
          />
        </div>
      )}
    </div>
  );
}

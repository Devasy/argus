import { useState } from "react";
import { ClipboardList, ExternalLink, Play } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../../api/client";
import {
  useMergeRequest,
  useProfiles,
  useProxies,
  useReconcileAndDistill,
} from "../../api/queries";
import type { Note } from "../../api/types";
import BenchmarkBadge from "../../components/BenchmarkBadge";
import ErrorBox from "../../components/ErrorBox";
import Modal from "../../components/Modal";
import StatusBadge from "../../components/StatusBadge";
import { fmtTokens } from "../../lib/pipelineGraph";

function NoteCard({ note }: { note: Note }) {
  return (
    <div className="nw-card section">
      <div className="flex-between">
        <div className="row">
          <strong>{note.author_username ?? "unknown"}</strong>
          <span className="badge" data-kind={note.kind}>
            {note.author_type === "bot" ? "bot" : "human"} · {note.kind}
          </span>
          {note.disposition !== "open" && <StatusBadge status="done" label={note.disposition} />}
        </div>
        {note.file_path && (
          <span className="muted mono">
            {note.file_path}
            {note.line != null ? `:${note.line}` : ""}
          </span>
        )}
      </div>
      <div className="md-body">
        <ReactMarkdown>{note.body}</ReactMarkdown>
      </div>
    </div>
  );
}

function TriggerDialog({ mrId, onClose }: { mrId: string; onClose: () => void }) {
  const proxies = useProxies();
  const profiles = useProfiles();
  const [reviewer, setReviewer] = useState("");
  const [profileId, setProfileId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.triggerReview(mrId, {
        reviewer: reviewer || null,
        profile_id: profileId || null,
      });
      navigate(`/reviews/${result.review_id}`);
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
      setSubmitting(false);
    }
  };

  return (
    <Modal title="Trigger review" onClose={onClose}>
      <div className="form-grid">
        {error && <ErrorBox message={error} />}
        <div>
          <label htmlFor="reviewer">Reviewer proxy (optional — default endpoint if empty)</label>
          <select id="reviewer" value={reviewer} onChange={(e) => setReviewer(e.target.value)}>
            <option value="">Default LLM endpoint</option>
            {(proxies.data ?? [])
              .filter((p) => p.enabled)
              .map((p) => (
                <option key={p.id} value={p.reviewer}>
                  {p.reviewer}
                </option>
              ))}
          </select>
        </div>
        <div>
          <label htmlFor="profile">Reviewer profile (optional — repo default if empty)</label>
          <select id="profile" value={profileId} onChange={(e) => setProfileId(e.target.value)}>
            <option value="">Repo default</option>
            {(profiles.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.current_version ? ` (v${p.current_version.version})` : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="form-actions">
          <button className="btn-o" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-p" onClick={submit} disabled={submitting}>
            {submitting ? "Starting…" : "Start review"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

export default function MRDetail() {
  const { mrId } = useParams<{ mrId: string }>();
  const navigate = useNavigate();
  const detail = useMergeRequest(mrId!);
  const [triggering, setTriggering] = useState(false);
  const reconcileAndDistill = useReconcileAndDistill();
  const [learningsMessage, setLearningsMessage] = useState<string | null>(null);
  const [triggeringQa, setTriggeringQa] = useState(false);
  const [qaError, setQaError] = useState<string | null>(null);

  const doTriggerQaScenarios = async () => {
    setQaError(null);
    setTriggeringQa(true);
    try {
      const result = await api.triggerReview(mrId!, { mode: "qa_scenarios" });
      navigate(`/reviews/${result.review_id}`);
    } catch (err) {
      setQaError(String(err instanceof Error ? err.message : err));
      setTriggeringQa(false);
    }
  };

  const doTriggerLearnings = async () => {
    setLearningsMessage(null);
    try {
      const result = await reconcileAndDistill.mutateAsync(mrId!);
      setLearningsMessage(
        result.queued_run
          ? `Learning run queued (${result.note_count} note${result.note_count === 1 ? "" : "s"})`
          : "No new candidates found",
      );
    } catch {
      /* error surfaced via reconcileAndDistill.error */
    }
  };

  return (
    <div className="nw-fade">
      {detail.error && <ErrorBox message={detail.error.message} onRetry={detail.refetch} />}
      {detail.isPending && <div className="spinner">Loading merge request…</div>}

      {detail.data && (
        <>
          <div className="page-head">
            <div>
              <div className="muted">
                <Link to="/repos">Repositories</Link> / MR
              </div>
              <h1>
                <span className="mono">!{detail.data.mr.mr_iid}</span> {detail.data.mr.title}
              </h1>
              <div className="row" style={{ marginTop: 6 }}>
                <StatusBadge status={detail.data.mr.state} />
                <span className="muted">by {detail.data.mr.author_username ?? "unknown"}</span>
                <a href={detail.data.mr.web_url} target="_blank" rel="noreferrer">
                  GitLab <ExternalLink size={12} />
                </a>
              </div>
            </div>
            <div className="row" style={{ gap: 8 }}>
              <button
                className="btn-o"
                onClick={doTriggerLearnings}
                disabled={reconcileAndDistill.isPending}
              >
                {reconcileAndDistill.isPending ? "Checking…" : "Trigger learnings"}
              </button>
              <button className="btn-o" onClick={doTriggerQaScenarios} disabled={triggeringQa}>
                <ClipboardList size={15} />
                {triggeringQa ? "Starting…" : "Trigger QA scenarios"}
              </button>
              <button className="btn-p" onClick={() => setTriggering(true)}>
                <Play size={15} /> Trigger review
              </button>
            </div>
          </div>

          {learningsMessage && <p className="muted">{learningsMessage}</p>}
          {reconcileAndDistill.error && <ErrorBox message={reconcileAndDistill.error.message} />}
          {qaError && <ErrorBox message={qaError} />}

          <h3 className="section">Review history ({detail.data.reviews.length})</h3>
          {detail.data.reviews.length === 0 && (
            <div className="muted">No reviews yet for this MR.</div>
          )}
          {detail.data.reviews.length > 0 && (
            <div className="table-wrap table-scroll">
              <table className="nw-table">
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Review</th>
                    <th>Scope</th>
                    <th>Trigger</th>
                    <th>Profile</th>
                    <th>Tokens</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.data.reviews.map((rev) => (
                    <tr
                      key={rev.id}
                      className="clickable"
                      onClick={() => navigate(`/reviews/${rev.id}`)}
                    >
                      <td>
                        <span className="row" style={{ gap: 6 }}>
                          <StatusBadge status={rev.status} />
                          {!rev.publish && <BenchmarkBadge />}
                        </span>
                      </td>
                      <td>
                        {rev.sequence_number != null ? (
                          <div className="rev-label">
                            <span className="rev-num">Review {rev.sequence_number}</span>
                            {rev.head_commit_sha && (
                              <span className="rev-sha mono">
                                {rev.head_commit_sha.slice(0, 7)}
                              </span>
                            )}
                          </div>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td>
                        {rev.sequence_number == null ? (
                          <span className="muted">—</span>
                        ) : rev.incremental_file_count != null ? (
                          <span className="scope-badge">
                            incremental · {rev.incremental_file_count} files
                          </span>
                        ) : (
                          <span className="scope-badge full">full</span>
                        )}
                      </td>
                      <td className="muted">{rev.trigger}</td>
                      <td className="muted">{rev.profile_version ?? "—"}</td>
                      <td className="muted">{fmtTokens(rev.tokens) ?? "0 tok"}</td>
                      <td className="muted">
                        {rev.started_at ? new Date(rev.started_at).toLocaleString() : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3 className="section">Learning runs ({detail.data.distillation_runs.length})</h3>
          {detail.data.distillation_runs.length === 0 && (
            <div className="muted">No learning runs yet for this MR.</div>
          )}
          {detail.data.distillation_runs.length > 0 && (
            <div className="table-wrap table-scroll">
              <table className="nw-table">
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Trigger</th>
                    <th>Tokens</th>
                    <th>Started</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.data.distillation_runs.map((run) => (
                    <tr
                      key={run.id}
                      className="clickable"
                      onClick={() => navigate(`/distillation-runs/${run.id}`)}
                    >
                      <td>
                        <StatusBadge status={run.status} />
                      </td>
                      <td className="muted">{run.trigger}</td>
                      <td className="muted">{fmtTokens(run.tokens) ?? "0 tok"}</td>
                      <td className="muted">
                        {run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3 className="section">Discussion notes ({detail.data.notes.length})</h3>
          {detail.data.notes.length === 0 && (
            <div className="muted">No notes synced for this MR yet.</div>
          )}
          {detail.data.notes.map((note) => (
            <NoteCard key={note.id} note={note} />
          ))}
        </>
      )}

      {triggering && mrId && <TriggerDialog mrId={mrId} onClose={() => setTriggering(false)} />}
    </div>
  );
}

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { useMergeRequests, useRepositories } from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import Modal from "../../components/Modal";

export default function TestOnMrDialog({
  agentName,
  onClose,
}: {
  agentName: string;
  onClose: () => void;
}) {
  const repos = useRepositories(1, 200);
  const [repoId, setRepoId] = useState("");
  const mrs = useMergeRequests(repoId, undefined, 1, 200);
  const [mrId, setMrId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.triggerReview(mrId, { force_agents: [agentName] });
      navigate(`/reviews/${result.review_id}`);
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
      setSubmitting(false);
    }
  };

  return (
    <Modal title={`Test "${agentName}" on an MR`} onClose={onClose}>
      <div className="form-grid">
        {error && <ErrorBox message={error} />}
        {repos.error && <ErrorBox message={repos.error.message} onRetry={repos.refetch} />}
        <div>
          <label htmlFor="tm-repo">Repository</label>
          <select
            id="tm-repo"
            value={repoId}
            onChange={(e) => {
              setRepoId(e.target.value);
              setMrId("");
            }}
          >
            <option value="">Select a repository</option>
            {(repos.data?.items ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {r.project_path}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="tm-mr">Merge request</label>
          <select
            id="tm-mr"
            value={mrId}
            onChange={(e) => setMrId(e.target.value)}
            disabled={!repoId}
          >
            <option value="">{repoId ? "Select an MR" : "Pick a repository first"}</option>
            {(mrs.data?.items ?? []).map((mr) => (
              <option key={mr.id} value={mr.id}>
                !{mr.mr_iid} {mr.title}
              </option>
            ))}
          </select>
        </div>
        <p className="muted" style={{ fontSize: 11.5 }}>
          This forces &quot;{agentName}&quot; to run regardless of Scout&apos;s own judgment — the
          resulting review&apos;s trace records whether Scout would have assigned it independently.
        </p>
        <div className="form-actions">
          <button className="btn-o" onClick={onClose}>
            Cancel
          </button>
          <button className="btn-p" onClick={submit} disabled={submitting || !mrId}>
            {submitting ? "Starting…" : "Run test review"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

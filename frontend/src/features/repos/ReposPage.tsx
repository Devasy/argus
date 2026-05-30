import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";
import {
  useCreateRepository,
  useProfiles,
  useRepositories,
  useUpdateRepository,
} from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import Modal from "../../components/Modal";
import PaginationBar from "../../components/PaginationBar";
import StatusBadge from "../../components/StatusBadge";
import Toggle from "../../components/Toggle";

export default function Repos() {
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const repos = useRepositories(page, perPage);
  const profiles = useProfiles();
  const [adding, setAdding] = useState(false);
  const [projectPath, setProjectPath] = useState("");
  const navigate = useNavigate();
  const createRepository = useCreateRepository();
  const updateRepository = useUpdateRepository();

  const profileName = (id: string | null) => {
    if (id === null) return "default (built-in)";
    if (profiles.isPending) return "…";
    return profiles.data?.find((p) => p.id === id)?.name ?? "unknown profile";
  };

  const submit = async () => {
    try {
      await createRepository.mutateAsync([projectPath.trim()]);
      setAdding(false);
      setProjectPath("");
    } catch {
      /* error surfaced via createRepository.error */
    }
  };

  return (
    <div className="nw-fade">
      <div className="page-head">
        <h1>Repositories</h1>
        <button className="btn-p" onClick={() => setAdding(true)}>
          <Plus size={15} /> Add repository
        </button>
      </div>

      {repos.error && <ErrorBox message={repos.error.message} onRetry={repos.refetch} />}
      {updateRepository.error && <ErrorBox message={updateRepository.error.message} />}
      {repos.isPending && <div className="spinner">Loading repositories…</div>}

      {repos.data && (
        <div className="section">
          {repos.data.items.length === 0 && (
            <div className="muted">No repositories yet — add one to start polling MRs.</div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {repos.data.items.map((repo) => (
              <div
                key={repo.id}
                className="nw-card clickable"
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 14,
                  padding: "18px 22px",
                  opacity: repo.enabled ? 1 : 0.65,
                }}
                onClick={() => navigate(`/repos/${repo.id}`)}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="mono" style={{ fontWeight: 600 }}>{repo.project_path}</div>
                  <div className="muted" style={{ marginTop: 5 }}>
                    {repo.mr_count} MRs tracked · every {repo.poll_interval_s}s · default agent{" "}
                    <b className="mono">{profileName(repo.default_profile_id)}</b>
                  </div>
                </div>
                <div onClick={(e) => e.stopPropagation()} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  {repo.enabled ? (
                    <StatusBadge status="done" label="Enabled" />
                  ) : (
                    <StatusBadge status="canceled" label="Disabled" />
                  )}
                  <Toggle
                    checked={repo.enabled}
                    disabled={updateRepository.isPending}
                    label={`Enable ${repo.project_path}`}
                    onChange={(v) => updateRepository.mutate([repo.id, { enabled: v }])}
                  />
                </div>
              </div>
            ))}
          </div>
          <PaginationBar
            page={repos.data.page}
            perPage={repos.data.per_page}
            total={repos.data.total}
            onPageChange={setPage}
            onPerPageChange={(n) => {
              setPerPage(n);
              setPage(1);
            }}
          />
        </div>
      )}

      {adding && (
        <Modal title="Add repository" onClose={() => setAdding(false)}>
          <div className="form-grid">
            {createRepository.error && <ErrorBox message={createRepository.error.message} />}
            <div>
              <label htmlFor="project-path">GitLab project path</label>
              <input
                id="project-path"
                value={projectPath}
                onChange={(e) => setProjectPath(e.target.value)}
                placeholder="group/project"
                autoFocus
              />
              <div className="muted" style={{ marginTop: 4 }}>
                Branch, project ID and settings are read from GitLab automatically.
              </div>
            </div>
            <div className="form-actions">
              <button className="btn-o" onClick={() => setAdding(false)}>
                Cancel
              </button>
              <button
                className="btn-p"
                onClick={submit}
                disabled={createRepository.isPending || !projectPath.trim()}
              >
                {createRepository.isPending ? "Adding…" : "Add"}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

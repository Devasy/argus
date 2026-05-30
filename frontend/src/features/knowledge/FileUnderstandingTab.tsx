import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useFileKnowledge, useRepositories } from "../../api/queries";
import ErrorBox from "../../components/ErrorBox";
import PaginationBar from "../../components/PaginationBar";

export default function FileUnderstandingTab() {
  const repos = useRepositories(1, 200);
  const [repoId, setRepoId] = useState("");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(20);
  const files = useFileKnowledge(repoId, page, perPage);
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <div>
      <div style={{ marginBottom: 14, maxWidth: 360 }}>
        <label htmlFor="fu-repo">Repository</label>
        <select
          id="fu-repo"
          value={repoId}
          onChange={(e) => {
            setRepoId(e.target.value);
            setPage(1);
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

      {!repoId && (
        <p className="muted">
          Pick a repository to see what argus has learned about its files.
        </p>
      )}
      {repoId && files.isPending && <div className="spinner">Loading…</div>}
      {repoId && files.error && <ErrorBox message={files.error.message} onRetry={files.refetch} />}
      {repoId && files.data && files.data.items.length === 0 && (
        <p className="muted">No file understanding stored yet for this repository.</p>
      )}

      {(files.data?.items ?? []).map((f) => {
        const isOpen = expanded === f.id;
        return (
          <div className="nw-card file-accordion" key={f.id}>
            <button
              className="file-accordion-head"
              onClick={() => setExpanded(isOpen ? null : f.id)}
            >
              {isOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
              <span className="mono">{f.file_path}</span>
              <span className="muted" style={{ marginLeft: "auto", fontSize: 11.5 }}>
                updated {new Date(f.updated_at).toLocaleDateString()}
                {f.blob_sha ? ` · ${f.blob_sha.slice(0, 8)}` : ""}
              </span>
            </button>
            {isOpen && (
              <div className="file-accordion-body">
                {f.summary && <p>{f.summary}</p>}
                {!f.summary && <p className="muted">No summary recorded.</p>}
                {f.notes && f.notes.length > 0 && (
                  <>
                    <div className="micro">NOTES</div>
                    {f.notes.map((n, i) => (
                      <div key={i} className="file-note">
                        <span>{n.note}</span>
                        <span className="muted" style={{ fontSize: 11 }}>
                          {new Date(n.at).toLocaleDateString()}
                        </span>
                      </div>
                    ))}
                  </>
                )}
              </div>
            )}
          </div>
        );
      })}

      {repoId && files.data && (
        <PaginationBar
          page={files.data.page}
          perPage={files.data.per_page}
          total={files.data.total}
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

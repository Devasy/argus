import { useState } from "react";
import { Pencil, Plus } from "lucide-react";
import { useCreateProxy, useProxies, useUpdateProxy } from "../../../api/queries";
import type { ReviewerProxy } from "../../../api/types";
import ErrorBox from "../../../components/ErrorBox";
import Modal from "../../../components/Modal";
import Toggle from "../../../components/Toggle";

function EditProxyModal({ proxy, onClose }: { proxy: ReviewerProxy; onClose: () => void }) {
  const [proxyUrl, setProxyUrl] = useState(proxy.proxy_url);
  const updateProxy = useUpdateProxy();

  const submit = async () => {
    try {
      await updateProxy.mutateAsync([proxy.id, { proxy_url: proxyUrl.trim() }]);
      onClose();
    } catch {
      /* error surfaced via updateProxy.error */
    }
  };

  return (
    <Modal title={`Edit proxy — ${proxy.reviewer}`} onClose={onClose}>
      <div className="form-grid">
        {updateProxy.error && <ErrorBox message={updateProxy.error.message} />}
        <div>
          <label htmlFor="e-url">Proxy URL</label>
          <input
            id="e-url"
            value={proxyUrl}
            onChange={(e) => setProxyUrl(e.target.value)}
            placeholder="http://10.0.0.1:8765"
            autoFocus
          />
        </div>
        <div className="form-actions">
          <button className="btn-o" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-p"
            onClick={submit}
            disabled={updateProxy.isPending || !proxyUrl.trim()}
          >
            {updateProxy.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

export default function ProxiesSection() {
  const proxies = useProxies();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<ReviewerProxy | null>(null);
  const [reviewer, setReviewer] = useState("");
  const [proxyUrl, setProxyUrl] = useState("");
  const createProxy = useCreateProxy();
  const toggleProxy = useUpdateProxy();

  const submit = async () => {
    try {
      await createProxy.mutateAsync([reviewer.trim(), proxyUrl.trim()]);
      setAdding(false);
      setReviewer("");
      setProxyUrl("");
    } catch {
      /* error surfaced via createProxy.error */
    }
  };

  return (
    <>
      <div className="page-head">
        <h3>Reviewer proxies</h3>
        <button className="btn-p" onClick={() => setAdding(true)}>
          <Plus size={15} /> Register proxy
        </button>
      </div>
      <p className="muted section">
        Maps a reviewer name to a Claude-CLI-proxy URL. Reviews triggered with that reviewer route
        through the registered proxy.
      </p>
      {proxies.error && <ErrorBox message={proxies.error.message} onRetry={proxies.refetch} />}
      {proxies.isPending && <div className="spinner">Loading proxies…</div>}
      {toggleProxy.error && <ErrorBox message={toggleProxy.error.message} />}
      {proxies.data && (
        <div className="table-wrap">
          <table className="nw-table">
            <thead>
              <tr>
                <th>Reviewer</th>
                <th>Proxy URL</th>
                <th>Enabled</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {proxies.data.length === 0 && (
                <tr>
                  <td colSpan={4} className="muted">
                    No proxies registered.
                  </td>
                </tr>
              )}
              {proxies.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    <strong>{p.reviewer}</strong>
                  </td>
                  <td className="mono muted">{p.proxy_url}</td>
                  <td>
                    <Toggle
                      checked={p.enabled}
                      disabled={toggleProxy.isPending}
                      label={`Enable ${p.reviewer}`}
                      onChange={(v) => toggleProxy.mutate([p.id, { enabled: v }])}
                    />
                  </td>
                  <td>
                    <button className="btn-g" onClick={() => setEditing(p)}>
                      <Pencil size={13} /> Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {adding && (
        <Modal title="Register reviewer proxy" onClose={() => setAdding(false)}>
          <div className="form-grid">
            {createProxy.error && <ErrorBox message={createProxy.error.message} />}
            <div>
              <label htmlFor="x-reviewer">Reviewer name</label>
              <input
                id="x-reviewer"
                value={reviewer}
                onChange={(e) => setReviewer(e.target.value)}
                placeholder="alice"
                autoFocus
              />
            </div>
            <div>
              <label htmlFor="x-url">Proxy URL</label>
              <input
                id="x-url"
                value={proxyUrl}
                onChange={(e) => setProxyUrl(e.target.value)}
                placeholder="http://10.0.0.1:8765"
              />
            </div>
            <div className="form-actions">
              <button className="btn-o" onClick={() => setAdding(false)}>
                Cancel
              </button>
              <button
                className="btn-p"
                onClick={submit}
                disabled={createProxy.isPending || !reviewer.trim() || !proxyUrl.trim()}
              >
                {createProxy.isPending ? "Registering…" : "Register"}
              </button>
            </div>
          </div>
        </Modal>
      )}
      {editing && <EditProxyModal proxy={editing} onClose={() => setEditing(null)} />}
    </>
  );
}

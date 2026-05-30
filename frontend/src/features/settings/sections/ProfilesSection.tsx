import { useState } from "react";
import { Pencil, Plus } from "lucide-react";
import { useCreateProfile, useProfiles, useUpdateProfile } from "../../../api/queries";
import type { ProfileVersionIn, ReviewerProfile } from "../../../api/types";
import ErrorBox from "../../../components/ErrorBox";
import Modal from "../../../components/Modal";
import StatusBadge from "../../../components/StatusBadge";

function parseAllowlist(text: string): string[] | null {
  const items = text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return items.length > 0 ? items : null;
}

function ProfileForm({
  existing,
  onDone,
  onClose,
}: {
  existing: ReviewerProfile | null;
  onDone: () => void;
  onClose: () => void;
}) {
  const current = existing?.current_version ?? null;
  const [name, setName] = useState(existing?.name ?? "");
  const [systemPrompt, setSystemPrompt] = useState(current?.system_prompt ?? "");
  const [guidelines, setGuidelines] = useState(current?.guidelines ?? "");
  const [allowlist, setAllowlist] = useState((current?.tool_allowlist ?? []).join(", "));
  const [model, setModel] = useState(current?.model ?? "");
  const createProfile = useCreateProfile();
  const updateProfile = useUpdateProfile();
  const saving = createProfile.isPending || updateProfile.isPending;
  const error = createProfile.error ?? updateProfile.error;

  const submit = async () => {
    const body: ProfileVersionIn = {
      name: name.trim(),
      system_prompt: systemPrompt,
      guidelines: guidelines.trim() || null,
      tool_allowlist: parseAllowlist(allowlist),
      model: model.trim() || null,
    };
    try {
      if (existing) {
        await updateProfile.mutateAsync([existing.id, body]);
      } else {
        await createProfile.mutateAsync([body]);
      }
      onDone();
    } catch {
      /* error surfaced via createProfile.error / updateProfile.error */
    }
  };

  return (
    <Modal
      title={existing ? `Edit profile (creates v${(current?.version ?? 0) + 1})` : "New profile"}
      onClose={onClose}
    >
      <div className="form-grid">
        {error && <ErrorBox message={error.message} />}
        <div>
          <label htmlFor="p-name">Name</label>
          <input id="p-name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        </div>
        <div>
          <label htmlFor="p-prompt">System prompt</label>
          <textarea
            id="p-prompt"
            rows={6}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="p-guidelines">Guidelines (optional)</label>
          <textarea
            id="p-guidelines"
            rows={4}
            value={guidelines}
            onChange={(e) => setGuidelines(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="p-allowlist">Tool allowlist (comma-separated, empty = all tools)</label>
          <input
            id="p-allowlist"
            value={allowlist}
            onChange={(e) => setAllowlist(e.target.value)}
            placeholder="get_hunk, get_file_lines"
          />
        </div>
        <div>
          <label htmlFor="p-model">Model (optional — currently not applied by the runner)</label>
          <input id="p-model" value={model} onChange={(e) => setModel(e.target.value)} />
        </div>
        <div className="form-actions">
          <button className="btn-o" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-p"
            onClick={submit}
            disabled={saving || !name.trim() || !systemPrompt.trim()}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

export default function ProfilesSection() {
  const profiles = useProfiles();
  const [editing, setEditing] = useState<ReviewerProfile | null>(null);
  const [creating, setCreating] = useState(false);

  const closeForms = () => {
    setEditing(null);
    setCreating(false);
  };

  return (
    <>
      <div className="page-head">
        <h3>Reviewer profiles</h3>
        <button className="btn-p" onClick={() => setCreating(true)}>
          <Plus size={15} /> New profile
        </button>
      </div>
      {profiles.error && <ErrorBox message={profiles.error.message} onRetry={profiles.refetch} />}
      {profiles.isPending && <div className="spinner">Loading profiles…</div>}
      {profiles.data && (
        <div className="table-wrap">
          <table className="nw-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Version</th>
                <th>Model</th>
                <th>Tools</th>
                <th>Type</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {profiles.data.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted">
                    No profiles yet.
                  </td>
                </tr>
              )}
              {profiles.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    <strong>{p.name}</strong>
                  </td>
                  <td className="mono">
                    {p.current_version ? `v${p.current_version.version}` : "—"}
                  </td>
                  <td className="muted">{p.current_version?.model ?? "—"}</td>
                  <td className="muted">
                    {p.current_version?.tool_allowlist?.join(", ") ?? "all"}
                  </td>
                  <td>
                    {p.is_builtin ? <StatusBadge status="done" label="Built-in" /> : "custom"}
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
      {(creating || editing) && (
        <ProfileForm existing={editing} onClose={closeForms} onDone={closeForms} />
      )}
    </>
  );
}

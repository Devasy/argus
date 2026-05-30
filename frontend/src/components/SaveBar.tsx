export default function SaveBar({
  count,
  onReset,
  onSave,
  saving,
}: {
  count: number;
  onReset: () => void;
  onSave: () => void;
  saving: boolean;
}) {
  if (count === 0) return null;
  return (
    <div className="savebar">
      <span>
        {count} unsaved change{count === 1 ? "" : "s"}
      </span>
      <span style={{ flex: 1 }} />
      <button className="btn-o savebar-reset" onClick={onReset} disabled={saving}>
        Reset
      </button>
      <button className="btn-p" onClick={onSave} disabled={saving}>
        {saving ? "Saving…" : "Save changes"}
      </button>
    </div>
  );
}

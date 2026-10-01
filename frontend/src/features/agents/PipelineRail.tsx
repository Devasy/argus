const STAGES = [
  { key: "scout", label: "Scout", role: "context" },
  { key: "analyze", label: "Analyzer", role: "fan-out" },
  { key: "reviewer-agents", label: "Reviewer agents", role: "fan-out · dynamic" },
  { key: "verify", label: "Verifier", role: "gate" },
  { key: "publish", label: "Publisher", role: "output" },
] as const;

export default function PipelineRail() {
  return (
    <div className="nw-card" style={{ padding: "14px 16px", marginBottom: 14 }}>
      <div className="muted" style={{ marginBottom: 8, fontSize: 11 }}>
        PIPELINE TOPOLOGY
      </div>
      {STAGES.map((s) => (
        <div key={s.key} className="row" style={{ padding: "6px 0", fontSize: 13 }}>
          <span style={{ fontWeight: s.key === "reviewer-agents" ? 600 : 400 }}>{s.label}</span>
          <span className="stage-tag" style={{ marginLeft: "auto" }}>
            {s.role}
          </span>
        </div>
      ))}
      <p className="muted" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
        Reviewer agents below are dynamically assigned by Scout, per MR — this list is a map of
        where they run, not a per-stage editor.
      </p>
    </div>
  );
}

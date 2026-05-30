import { FlaskConical } from "lucide-react";

// A dry-run review (e.g. a golden-set benchmark pass): nothing was posted to
// GitLab, and its outcomes are never charged to real learnings.
export default function BenchmarkBadge() {
  return (
    <span
      className="badge"
      title="Dry run: nothing posted to GitLab, no learning outcomes charged"
      style={{ background: "var(--bg-raised)", color: "var(--text-tertiary)" }}
    >
      <FlaskConical size={12} />
      <span>Benchmark</span>
    </span>
  );
}

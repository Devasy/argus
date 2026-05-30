import {
  AlertOctagon,
  CheckCircle,
  Circle,
  Clock,
  GitMerge,
  RefreshCw,
  XCircle,
} from "lucide-react";
import type { ComponentType } from "react";

interface Config {
  bg: string;
  color: string;
  label: string;
  icon: ComponentType<{ size?: number | string; className?: string }>;
  spin?: boolean;
}

const CONFIG: Record<string, Config> = {
  done: {
    bg: "var(--state-success-bg)",
    color: "var(--state-success-text)",
    label: "Done",
    icon: CheckCircle,
  },
  merged: {
    bg: "var(--state-success-bg)",
    color: "var(--state-success-text)",
    label: "Merged",
    icon: GitMerge,
  },
  running: {
    bg: "var(--state-warning-bg)",
    color: "var(--state-warning)",
    label: "Running",
    icon: RefreshCw,
    spin: true,
  },
  queued: {
    bg: "var(--state-warning-bg)",
    color: "var(--state-warning)",
    label: "Queued",
    icon: Clock,
  },
  failed: {
    bg: "var(--state-error-bg)",
    color: "var(--state-error-text)",
    label: "Failed",
    icon: AlertOctagon,
  },
  canceled: {
    bg: "var(--bg-raised)",
    color: "var(--text-tertiary)",
    label: "Canceled",
    icon: XCircle,
  },
  opened: {
    bg: "var(--brand-purple-tint)",
    color: "var(--state-info-text)",
    label: "Open",
    icon: Circle,
  },
  closed: { bg: "var(--bg-raised)", color: "var(--text-tertiary)", label: "Closed", icon: XCircle },
};

const FALLBACK: Config = {
  bg: "var(--bg-raised)",
  color: "var(--text-tertiary)",
  label: "",
  icon: Circle,
};

export default function StatusBadge({ status, label }: { status: string; label?: string }) {
  const cfg = CONFIG[status] ?? { ...FALLBACK, label: status };
  const Icon = cfg.icon;
  return (
    <span className="badge" style={{ background: cfg.bg, color: cfg.color }}>
      <Icon size={12} className={cfg.spin ? "nw-spin" : undefined} />
      <span>{label ?? cfg.label}</span>
    </span>
  );
}

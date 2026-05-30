import { NavLink, useParams } from "react-router-dom";
import ReviewTuningSection from "./sections/ReviewTuningSection";
import GitLabSection from "./sections/GitLabSection";
import PollerSection from "./sections/PollerSection";
import AuditSection from "./sections/AuditSection";
import ModelsSection from "./sections/ModelsSection";
import ObservabilitySection from "./sections/ObservabilitySection";
import ProxiesSection from "./sections/ProxiesSection";
import ApiAccessSection from "./sections/ApiAccessSection";
import ProfilesSection from "./sections/ProfilesSection";

const SECTIONS = [
  { slug: "profiles", label: "Review profiles", el: <ProfilesSection /> },
  { slug: "proxies", label: "Reviewer proxies", el: <ProxiesSection /> },
  { slug: "review-tuning", label: "Review tuning", el: <ReviewTuningSection /> },
  { slug: "gitlab", label: "GitLab connection", el: <GitLabSection /> },
  { slug: "poller", label: "Poller & worker", el: <PollerSection /> },
  { slug: "auditor", label: "Learning auditor", el: <AuditSection /> },
  { slug: "models", label: "Models & embeddings", el: <ModelsSection /> },
  { slug: "observability", label: "Observability", el: <ObservabilitySection /> },
  { slug: "api-access", label: "API access", el: <ApiAccessSection /> },
];

export default function SettingsPage() {
  const { section = "review-tuning" } = useParams();
  const active = SECTIONS.find((s) => s.slug === section) ?? SECTIONS[2];
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="muted">
            Runtime configuration — stored server-side, applied without redeploys. Secrets stay in
            the environment.
          </p>
        </div>
      </div>
      <div className="set-grid">
        <nav className="set-nav">
          {SECTIONS.map((s) => (
            <NavLink
              key={s.slug}
              to={`/settings/${s.slug}`}
              className={({ isActive }) => (isActive ? "active" : "")}
            >
              {s.label}
            </NavLink>
          ))}
        </nav>
        <div>{active.el}</div>
      </div>
    </div>
  );
}

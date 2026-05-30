import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import {
  FolderGit2,
  Settings as SettingsIcon,
  Sun,
  Moon,
  GitPullRequestArrow,
  Sparkles,
  Brain,
  LayoutDashboard,
  ListChecks,
  AlertTriangle,
} from "lucide-react";
import { getToken, setUnauthorizedHandler } from "./api/client";
import TokenGate from "./pages/TokenGate";
import Complaints from "./features/complaints/ComplaintsPage";
import Dashboard from "./features/dashboard/DashboardPage";
import Repos from "./features/repos/ReposPage";
import Reviews from "./features/reviews/ReviewsPage";
import RepoDetail from "./features/repos/RepoDetailPage";
import MRDetail from "./features/mrs/MRDetailPage";
import ReviewDetail from "./features/run/ReviewDetailPage";
import DistillationRunDetail from "./features/run/DistillationRunDetailPage";
import Settings from "./features/settings/SettingsPage";
import Agents from "./features/agents/AgentsPage";
import Knowledge from "./features/knowledge/KnowledgePage";

function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem("argus_theme") ?? "light");
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("argus_theme", theme);
  }, [theme]);
  return { theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) };
}

export default function App() {
  const { theme, toggle } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    setUnauthorizedHandler(() => {
      navigate("/token", { state: { from: location.pathname }, replace: true });
    });
  }, [navigate, location.pathname]);

  useEffect(() => {
    if (!getToken() && location.pathname !== "/token") {
      navigate("/token", { state: { from: location.pathname }, replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (location.pathname === "/token") {
    return (
      <Routes>
        <Route path="/token" element={<TokenGate />} />
      </Routes>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <GitPullRequestArrow size={20} /> argus
        </div>
        <nav>
          <NavLink to="/" end>
            <LayoutDashboard size={16} /> Dashboard
          </NavLink>
          <NavLink to="/reviews">
            <ListChecks size={16} /> Reviews
          </NavLink>
          <NavLink to="/repos">
            <FolderGit2 size={16} /> Repositories
          </NavLink>
          <NavLink to="/agents">
            <Sparkles size={16} /> Agents
          </NavLink>
          <NavLink to="/knowledge">
            <Brain size={16} /> Knowledge
          </NavLink>
          <NavLink to="/complaints">
            <AlertTriangle size={16} /> Complaints
          </NavLink>
          <NavLink to="/settings/review-tuning">
            <SettingsIcon size={16} /> Settings
          </NavLink>
        </nav>
        <div className="foot">
          <button className="btn-g" onClick={toggle}>
            {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
            {theme === "dark" ? "Light" : "Dark"}
          </button>
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/reviews" element={<Reviews />} />
          <Route path="/repos" element={<Repos />} />
          <Route path="/repos/:repoId" element={<RepoDetail />} />
          <Route path="/mrs/:mrId" element={<MRDetail />} />
          <Route path="/reviews/:reviewId" element={<ReviewDetail />} />
          <Route path="/distillation-runs/:runId" element={<DistillationRunDetail />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/agents/:agentId" element={<Agents />} />
          <Route path="/knowledge/*" element={<Knowledge />} />
          <Route path="/complaints" element={<Complaints />} />
          <Route path="/settings/:section?" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}

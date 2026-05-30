import { NavLink, Route, Routes } from "react-router-dom";
import LearningsTab from "./LearningsTab";
import FileUnderstandingTab from "./FileUnderstandingTab";
import AuditQueue from "./AuditQueue";
import AuditRunsTab from "./AuditRunsTab";

export default function KnowledgePage() {
  return (
    <div>
      <div className="page-head">
        <h1>Knowledge</h1>
      </div>
      <div className="tabs">
        <NavLink to="/knowledge" end className={({ isActive }) => (isActive ? "active" : "")}>
          Learnings
        </NavLink>
        <NavLink to="/knowledge/files" className={({ isActive }) => (isActive ? "active" : "")}>
          File understanding
        </NavLink>
        <NavLink to="/knowledge/audit-queue" className={({ isActive }) => (isActive ? "active" : "")}>
          Audit queue
        </NavLink>
        <NavLink to="/knowledge/audit-runs" className={({ isActive }) => (isActive ? "active" : "")}>
          Audit runs
        </NavLink>
      </div>
      <Routes>
        <Route index element={<LearningsTab />} />
        <Route path="files" element={<FileUnderstandingTab />} />
        <Route path="audit-queue" element={<AuditQueue />} />
        <Route path="audit-runs" element={<AuditRunsTab />} />
      </Routes>
    </div>
  );
}

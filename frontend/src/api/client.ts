import type {
  MergeRequestDetail,
  PaginatedMergeRequests,
  ProfileVersionIn,
  Repository,
  Review,
  ReviewTrigger,
  ReviewerProfile,
  ReviewerProxy,
  SettingsView,
  Trace,
  ReviewerAgent,
  ReviewerAgentCreate,
  AvailableTool,
  ReviewerAgentVersionHistory,
  Learning,
  FileKnowledgeEntry,
  Paginated,
  DistillationRun,
  DashboardStats,
  Note,
  NoteVerdictIn,
  AuditVerdict,
  AuditRun,
  ReviewListItem,
  ReviewQueueSummary,
  PaginatedComplaints,
  RepoAgentSetting,
} from "./types";

const BASE = "/api";
const TOKEN_KEY = "argus_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function http<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(BASE + path, { ...init, headers });
  if (res.status === 401) {
    clearToken();
    onUnauthorized();
    throw new ApiError(401, "Invalid or missing API token");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

/** Unauthenticated reachability probe (the one route without auth). */
export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(BASE + "/health");
    return res.ok;
  } catch {
    return false;
  }
}

export const api = {
  repositories: (params: { page?: number; per_page?: number } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<Repository>>(`/repositories?${qs}`);
  },
  createRepository: (project_path: string) =>
    http<Repository>("/repositories", { method: "POST", body: JSON.stringify({ project_path }) }),
  mergeRequests: (
    repoId: string,
    params: { state?: string; page?: number; per_page?: number } = {},
  ) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<PaginatedMergeRequests>(`/repositories/${repoId}/merge-requests?${qs}`);
  },
  mergeRequest: (mrId: string) => http<MergeRequestDetail>(`/merge-requests/${mrId}`),
  triggerReview: (mrId: string, body: ReviewTrigger) =>
    http<{ review_id: string }>(`/merge-requests/${mrId}/reviews`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  reconcileAndDistill: (mrId: string) =>
    http<{ queued_run: boolean; note_count: number }>(
      `/merge-requests/${mrId}/reconcile-and-distill`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  review: (reviewId: string) => http<Review>(`/reviews/${reviewId}`),
  trace: (reviewId: string) => http<Trace>(`/reviews/${reviewId}/trace`),
  reviews: (params: {
    status?: string;
    repo_id?: string;
    profile_id?: string;
    page?: number;
    per_page?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<ReviewListItem>>(`/reviews?${qs}`);
  },
  reviewQueueSummary: () => http<ReviewQueueSummary>("/reviews/queue-summary"),
  cancelReview: (reviewId: string) =>
    http<{ status: string }>(`/reviews/${reviewId}/cancel`, { method: "POST" }),
  distillationRun: (runId: string) => http<DistillationRun>(`/distillation-runs/${runId}`),
  distillationRunTrace: (runId: string) => http<Trace>(`/distillation-runs/${runId}/trace`),
  profiles: () => http<ReviewerProfile[]>("/profiles"),
  availableTools: () => http<AvailableTool[]>("/agents/available-tools"),
  createProfile: (body: ProfileVersionIn) =>
    http<ReviewerProfile>("/profiles", { method: "POST", body: JSON.stringify(body) }),
  updateProfile: (profileId: string, body: ProfileVersionIn) =>
    http<ReviewerProfile>(`/profiles/${profileId}`, { method: "PUT", body: JSON.stringify(body) }),
  reviewerProxies: () => http<ReviewerProxy[]>("/reviewer-proxies"),
  createReviewerProxy: (reviewer: string, proxy_url: string) =>
    http<ReviewerProxy>("/reviewer-proxies", {
      method: "POST",
      body: JSON.stringify({ reviewer, proxy_url }),
    }),
  updateReviewerProxy: (id: string, patch: { proxy_url?: string; enabled?: boolean }) =>
    http<ReviewerProxy>(`/reviewer-proxies/${id}`, { method: "PUT", body: JSON.stringify(patch) }),
  repository: (id: string) => http<Repository>(`/repositories/${id}`),
  updateRepository: (
    id: string,
    patch: Partial<
      Pick<Repository, "enabled" | "poll_interval_s" | "default_profile_id" | "auto_review_enabled">
    >,
  ) => http<Repository>(`/repositories/${id}`, { method: "PUT", body: JSON.stringify(patch) }),
  settings: () => http<SettingsView>("/settings"),
  updateSettings: (values: Record<string, string | number | boolean>) =>
    http<SettingsView>("/settings", { method: "PUT", body: JSON.stringify({ values }) }),
  agents: (params: { page?: number; per_page?: number } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<ReviewerAgent>>(`/agents?${qs}`);
  },
  createAgent: (body: ReviewerAgentCreate) =>
    http<ReviewerAgent>("/agents", { method: "POST", body: JSON.stringify(body) }),
  updateAgent: (
    id: string,
    patch: {
      guidelines?: string;
      model?: string | null;
      max_rounds?: number;
      tool_allowlist?: string[] | null;
    },
  ) => http<ReviewerAgent>(`/agents/${id}`, { method: "PUT", body: JSON.stringify(patch) }),
  setAgentEnabled: (id: string, enabled: boolean) =>
    http<ReviewerAgent>(`/agents/${id}/enabled`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  setAgentDescription: (id: string, description: string | null) =>
    http<ReviewerAgent>(`/agents/${id}/description`, {
      method: "PUT",
      body: JSON.stringify({ description }),
    }),
  agentVersions: (id: string, params: { page?: number; per_page?: number } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<ReviewerAgentVersionHistory>>(`/agents/${id}/versions?${qs}`);
  },
  agentUsage: (id: string) => http<{ review_count: number }>(`/agents/${id}/usage`),
  complaints: (params: {
    review_id?: string;
    category?: string;
    target?: string;
    blocked?: boolean;
    page?: number;
    per_page?: number;
  }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<PaginatedComplaints>(`/complaints?${qs}`);
  },
  repoAgents: (repoId: string) =>
    http<RepoAgentSetting[]>(`/repositories/${repoId}/agents`),
  setRepoAgent: (repoId: string, agentId: string, enabled: boolean) =>
    http<RepoAgentSetting>(`/repositories/${repoId}/agents/${agentId}`, {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  learnings: (params: {
    repo_id?: string;
    kind?: string;
    status?: string;
    page?: number;
    per_page?: number;
  }) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<Learning>>(`/learnings?${qs}`);
  },
  searchLearnings: (
    q: string,
    params: { repo_id?: string; page?: number; per_page?: number } = {},
  ) => {
    const qs = new URLSearchParams({ q });
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<Learning>>(`/learnings/search?${qs}`);
  },
  fileKnowledge: (repoId: string, params: { page?: number; per_page?: number } = {}) => {
    const qs = new URLSearchParams({ repo_id: repoId });
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<FileKnowledgeEntry>>(`/file-knowledge?${qs}`);
  },
  dashboardStats: (days: number) =>
    http<DashboardStats>(`/stats/dashboard?days=${days}`),
  submitNoteVerdict: (noteId: string, body: NoteVerdictIn) =>
    http<Note>(`/notes/${noteId}/verdict`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  auditVerdicts: (
    params: {
      state?: string;
      repo_id?: string;
      include_no_action?: boolean;
      page?: number;
      per_page?: number;
    } = {},
  ) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<AuditVerdict>>(`/audit-verdicts?${qs}`);
  },
  approveVerdict: (id: string) =>
    http<{ ok: boolean; applied: boolean }>(`/audit-verdicts/${id}/approve`, { method: "POST" }),
  rejectVerdict: (id: string) =>
    http<{ ok: boolean }>(`/audit-verdicts/${id}/reject`, { method: "POST" }),
  auditRuns: (params: { repo_id?: string; page?: number; per_page?: number } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined) qs.set(k, String(v));
    return http<Paginated<AuditRun>>(`/audit-runs?${qs}`);
  },
  auditRunTrace: (runId: string) => http<Trace>(`/audit-runs/${runId}/trace`),
  triggerAudit: (repoId: string, maxClusters?: number) => {
    const qs = new URLSearchParams();
    if (maxClusters !== undefined) qs.set("max_clusters", String(maxClusters));
    return http<{ status: string; clusters?: number; verdicts?: number }>(
      `/repositories/${repoId}/audit?${qs}`,
      { method: "POST" },
    );
  },
};

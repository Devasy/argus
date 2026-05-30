import { useMutation, useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { api } from "./client";
import type { ProfileVersionIn, Repository, SettingsView, ReviewerAgentCreate, NoteVerdictIn } from "./types";

export const queryKeys = {
  repositories: (page: number, per_page: number) => ["repositories", page, per_page] as const,
  repository: (id: string) => ["repositories", id] as const,
  mrs: (repoId: string, state: string | undefined, page: number, per_page: number) =>
    ["repositories", repoId, "mrs", state, page, per_page] as const,
  mr: (id: string) => ["mrs", id] as const,
  profiles: ["profiles"] as const,
  availableTools: ["agents", "available-tools"] as const,
  proxies: ["proxies"] as const,
  settings: ["settings"] as const,
  agents: (page: number, per_page: number) => ["agents", page, per_page] as const,
  agentVersions: (id: string, page: number, per_page: number) =>
    ["agents", id, "versions", page, per_page] as const,
  agentUsage: (id: string) => ["agents", id, "usage"] as const,
  learnings: (filters: Record<string, string | number>) => ["learnings", filters] as const,
  complaints: (filters: Record<string, string | number | boolean>) =>
    ["complaints", filters] as const,
  learningsSearch: (q: string, filters: Record<string, string | number>) =>
    ["learnings", "search", q, filters] as const,
  fileKnowledge: (repoId: string, page: number, per_page: number) =>
    ["file-knowledge", repoId, page, per_page] as const,
  auditVerdicts: (state: string, page: number, per_page: number, includeNoAction?: boolean) =>
    ["audit-verdicts", state, page, per_page, includeNoAction ?? false] as const,
  review: (id: string) => ["reviews", id] as const,
  reviewTrace: (id: string) => ["reviews", id, "trace"] as const,
  reviews: (filters: Record<string, string | number>) => ["reviews", filters] as const,
  reviewQueueSummary: ["reviews", "queue-summary"] as const,
  distillationRun: (id: string) => ["distillation-runs", id] as const,
  distillationRunTrace: (id: string) => ["distillation-runs", id, "trace"] as const,
  auditRuns: (repoId: string | undefined, page: number, per_page: number) =>
    ["audit-runs", repoId ?? "all", page, per_page] as const,
  auditRunTrace: (id: string) => ["audit-runs", id, "trace"] as const,
  dashboard: (days: number) => ["dashboard", days] as const,
};

export const useRepositories = (page = 1, per_page = 20) =>
  useQuery({
    queryKey: queryKeys.repositories(page, per_page),
    queryFn: () => api.repositories({ page, per_page }),
  });
export const useRepository = (id: string) =>
  useQuery({ queryKey: queryKeys.repository(id), queryFn: () => api.repository(id) });
export const useMergeRequests = (
  repoId: string,
  state?: string,
  page = 1,
  per_page = 20,
) =>
  useQuery({
    queryKey: queryKeys.mrs(repoId, state, page, per_page),
    queryFn: () => api.mergeRequests(repoId, { state, page, per_page }),
  });
export const useMergeRequest = (id: string) =>
  useQuery({ queryKey: queryKeys.mr(id), queryFn: () => api.mergeRequest(id) });
export const useProfiles = () => useQuery({ queryKey: queryKeys.profiles, queryFn: api.profiles });
// The tool list rarely changes (it tracks the pipeline's own @tool
// definitions, not per-review data), so a long staleTime avoids refetching it
// on every AgentEditor mount without needing manual cache invalidation.
export const useAvailableTools = () =>
  useQuery({
    queryKey: queryKeys.availableTools,
    queryFn: api.availableTools,
    staleTime: 5 * 60 * 1000,
  });
export const useProxies = () =>
  useQuery({ queryKey: queryKeys.proxies, queryFn: api.reviewerProxies });
export const useSettings = () => useQuery({ queryKey: queryKeys.settings, queryFn: api.settings });

function useInvalidating<TArgs extends unknown[], TOut>(
  fn: (...args: TArgs) => Promise<TOut>,
  keys: readonly (readonly string[])[],
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: TArgs) => fn(...args),
    onSuccess: () => keys.forEach((k) => qc.invalidateQueries({ queryKey: k })),
  });
}

export const useCreateRepository = () =>
  useInvalidating((path: string) => api.createRepository(path), [["repositories"]]);
export const useRepoAgents = (repoId: string) =>
  useQuery({
    queryKey: ["repo-agents", repoId] as const,
    queryFn: () => api.repoAgents(repoId),
  });

export const useSetRepoAgent = (repoId: string) =>
  useInvalidating(
    (agentId: string, enabled: boolean) => api.setRepoAgent(repoId, agentId, enabled),
    [["repo-agents"]],
  );

export const useUpdateRepository = () =>
  useInvalidating(
    (
      id: string,
      patch: Partial<
        Pick<Repository, "enabled" | "poll_interval_s" | "default_profile_id" | "auto_review_enabled">
      >,
    ) => api.updateRepository(id, patch),
    [["repositories"]],
  );
export const useUpdateProxy = () =>
  useInvalidating(
    (id: string, patch: { proxy_url?: string; enabled?: boolean }) =>
      api.updateReviewerProxy(id, patch),
    [queryKeys.proxies],
  );
export const useUpdateSettings = () =>
  useInvalidating(
    (values: SettingsView["values"]) => api.updateSettings(values),
    [queryKeys.settings],
  );

export const useCreateProfile = () =>
  useInvalidating((body: ProfileVersionIn) => api.createProfile(body), [queryKeys.profiles]);
export const useUpdateProfile = () =>
  useInvalidating(
    (profileId: string, body: ProfileVersionIn) => api.updateProfile(profileId, body),
    [queryKeys.profiles],
  );
export const useCreateProxy = () =>
  useInvalidating(
    (reviewer: string, proxy_url: string) => api.createReviewerProxy(reviewer, proxy_url),
    [queryKeys.proxies],
  );

export const useAgents = (page = 1, per_page = 20) =>
  useQuery({
    queryKey: queryKeys.agents(page, per_page),
    queryFn: () => api.agents({ page, per_page }),
  });
export const useAgentVersions = (id: string, page = 1, per_page = 20) =>
  useQuery({
    queryKey: queryKeys.agentVersions(id, page, per_page),
    queryFn: () => api.agentVersions(id, { page, per_page }),
  });
export const useAgentUsage = (id: string) =>
  useQuery({
    queryKey: queryKeys.agentUsage(id),
    queryFn: () => api.agentUsage(id),
    enabled: !!id,
  });

export const useCreateAgent = () =>
  useInvalidating((body: ReviewerAgentCreate) => api.createAgent(body), [["agents"]]);
export const useUpdateAgent = () =>
  useInvalidating(
    (
      id: string,
      patch: {
        guidelines?: string;
        model?: string | null;
        max_rounds?: number;
        tool_allowlist?: string[] | null;
      },
    ) => api.updateAgent(id, patch),
    [["agents"]],
  );
export const useSetAgentEnabled = () =>
  useInvalidating(
    (id: string, enabled: boolean) => api.setAgentEnabled(id, enabled),
    [["agents"]],
  );
export const useSetAgentDescription = () =>
  useInvalidating(
    (id: string, description: string | null) => api.setAgentDescription(id, description),
    [["agents"]],
  );

export const useComplaints = (
  filters: {
    review_id?: string;
    category?: string;
    target?: string;
    blocked?: boolean;
    page?: number;
    per_page?: number;
  } = {},
) => useQuery({ queryKey: queryKeys.complaints(filters), queryFn: () => api.complaints(filters) });

export const useLearnings = (
  filters: {
    repo_id?: string;
    kind?: string;
    status?: string;
    page?: number;
    per_page?: number;
  } = {},
) => useQuery({ queryKey: queryKeys.learnings(filters), queryFn: () => api.learnings(filters) });

export const useLearningsSearch = (
  q: string,
  filters: { repo_id?: string; page?: number; per_page?: number } = {},
) =>
  useQuery({
    queryKey: queryKeys.learningsSearch(q, filters),
    queryFn: () => api.searchLearnings(q, filters),
    enabled: q.trim().length > 0,
  });

export const useFileKnowledge = (repoId: string, page = 1, per_page = 20) =>
  useQuery({
    queryKey: queryKeys.fileKnowledge(repoId, page, per_page),
    queryFn: () => api.fileKnowledge(repoId, { page, per_page }),
    enabled: !!repoId,
  });

export const useReview = (id: string) =>
  useQuery({ queryKey: queryKeys.review(id), queryFn: () => api.review(id) });
export const useTrace = (id: string) =>
  useQuery({ queryKey: queryKeys.reviewTrace(id), queryFn: () => api.trace(id) });

export const useReviews = (
  filters: {
    status?: string;
    repo_id?: string;
    profile_id?: string;
    page?: number;
    per_page?: number;
  } = {},
  options: {
    refetchInterval?: number | false | ((query: Query<Awaited<ReturnType<typeof api.reviews>>>) => number | false);
  } = {},
) =>
  useQuery({
    queryKey: queryKeys.reviews(filters),
    queryFn: () => api.reviews(filters),
    refetchInterval: options.refetchInterval,
  });

export const useReviewQueueSummary = (
  options: {
    refetchInterval?:
      | number
      | false
      | ((query: Query<Awaited<ReturnType<typeof api.reviewQueueSummary>>>) => number | false);
  } = {},
) =>
  useQuery({
    queryKey: queryKeys.reviewQueueSummary,
    queryFn: api.reviewQueueSummary,
    refetchInterval: options.refetchInterval,
  });

export const useCancelReview = () =>
  useInvalidating((id: string) => api.cancelReview(id), [
    ["reviews"],
  ]);

export const useDistillationRun = (id: string) =>
  useQuery({ queryKey: queryKeys.distillationRun(id), queryFn: () => api.distillationRun(id) });
export const useDistillationRunTrace = (id: string) =>
  useQuery({
    queryKey: queryKeys.distillationRunTrace(id),
    queryFn: () => api.distillationRunTrace(id),
  });

export const useReconcileAndDistill = () =>
  useMutation({ mutationFn: (mrId: string) => api.reconcileAndDistill(mrId) });

export const useDashboardStats = (days: number) =>
  useQuery({
    queryKey: queryKeys.dashboard(days),
    queryFn: () => api.dashboardStats(days),
    refetchInterval: 30_000,
  });

export const useAuditVerdicts = (
  state = "proposed",
  page = 1,
  per_page = 20,
  includeNoAction = false,
) =>
  useQuery({
    queryKey: queryKeys.auditVerdicts(state, page, per_page, includeNoAction),
    queryFn: () =>
      api.auditVerdicts({ state, page, per_page, include_no_action: includeNoAction }),
  });

export const useApproveVerdict = () =>
  useInvalidating((id: string) => api.approveVerdict(id), [
    ["audit-verdicts"],
    ["learnings"],
  ]);

export const useRejectVerdict = () =>
  useInvalidating((id: string) => api.rejectVerdict(id), [["audit-verdicts"]]);

export const useAuditRuns = (repoId?: string, page = 1, per_page = 20) =>
  useQuery({
    queryKey: queryKeys.auditRuns(repoId, page, per_page),
    queryFn: () => api.auditRuns({ repo_id: repoId, page, per_page }),
    // A running audit updates as clusters complete.
    refetchInterval: (q) =>
      q.state.data?.items.some((r) => r.status === "running") ? 5000 : false,
  });

export const useAuditRunTrace = (id: string) =>
  useQuery({
    queryKey: queryKeys.auditRunTrace(id),
    queryFn: () => api.auditRunTrace(id),
  });

/** Run the auditor for one repo now, rather than waiting for its interval. */
export const useTriggerAudit = () =>
  useInvalidating(
    (repoId: string, maxClusters?: number) => api.triggerAudit(repoId, maxClusters),
    [["audit-verdicts"], ["learnings"], ["audit-runs"]],
  );

export const useSubmitNoteVerdict = (mrId: string) => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ noteId, body }: { noteId: string; body: NoteVerdictIn }) =>
      api.submitNoteVerdict(noteId, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: queryKeys.mr(mrId) }),
  });
};

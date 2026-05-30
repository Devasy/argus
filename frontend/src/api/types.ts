export type MRState = "opened" | "merged" | "closed";

export interface Repository {
  id: string;
  project_path: string;
  gitlab_project_id: number;
  enabled: boolean;
  poll_interval_s: number;
  default_profile_id: string | null;
  auto_review_enabled: boolean;
  mr_count: number;
}

export interface MergeRequest {
  id: string;
  mr_iid: number;
  title: string;
  state: MRState; // GitLab states, not enforced
  author_username: string | null;
  web_url: string;
  mr_updated_at: string | null;
  accepted_count: number;
  rejected_count: number;
  leftover_count: number;
}

export interface MergeRequestStateCounts {
  all: number;
  opened: number;
  merged: number;
  closed: number;
}

export interface PaginatedMergeRequests {
  items: MergeRequest[];
  total: number;
  page: number;
  per_page: number;
  state_counts: MergeRequestStateCounts;
}

export interface Note {
  id: string;
  author_username: string | null;
  author_type: string; // "human" | "bot"
  kind: string; // "inline" | "summary" | ...
  body: string;
  file_path: string | null;
  line: number | null;
  disposition: string; // "open" | "accepted" | "rejected_with_rationale" | ...
  verdict_rationale: string | null;
  verdict_confidence: string | null; // "high" | "medium" | "low" | null
  verdict_source: string | null; // "verdict_agent" | "human" | null
}

export interface ReviewSummary {
  id: string;
  status: string;
  trigger: string;
  profile_version: string | null;
  tokens: number;
  started_at: string | null;
  sequence_number: number | null;
  head_commit_sha: string | null;
  incremental_file_count: number | null;
  // false = a dry run (e.g. a golden-set benchmark pass): nothing was posted
  // to GitLab and its outcomes are never charged to real learnings.
  publish: boolean;
}

export interface MergeRequestDetail {
  mr: MergeRequest;
  notes: Note[];
  reviews: ReviewSummary[];
  distillation_runs: DistillationRunSummary[];
}

export interface ReviewerProxy {
  id: string;
  reviewer: string;
  proxy_url: string;
  enabled: boolean;
}

export interface ProfileVersion {
  id: string;
  version: number;
  system_prompt: string;
  guidelines: string | null;
  tool_allowlist: string[] | null;
  model: string | null;
}

export interface ReviewerProfile {
  id: string;
  name: string;
  description: string | null;
  is_builtin: boolean;
  current_version: ProfileVersion | null;
}

export interface ProfileVersionIn {
  name: string;
  system_prompt: string;
  guidelines?: string | null;
  tool_allowlist?: string[] | null;
  model?: string | null;
}

export interface ReviewTrigger {
  reviewer?: string | null;
  llm_endpoint_id?: string | null;
  profile_id?: string | null;
  force_agents?: string[] | null;
  mode?: "full" | "qa_scenarios";
}

export type ReviewStatus = "queued" | "running" | "done" | "failed" | "canceled";

export interface ReviewStage {
  name: string;
  // Distinct from ReviewStatus: stages also report "pending" before they start
  // (see lib/pipelineGraph.ts stageStatusToNodeStatus).
  status: ReviewStatus | "pending";
  started_at: string | null;
  finished_at: string | null;
  // Candidates this stage emitted, and for verify how many it let through.
  produced: number | null;
  allowed: number | null;
  // analyze writes one row but tags findings per chunk; keyed by chunk id.
  produced_by_chunk: Record<string, number>;
}

export interface ReviewCandidate {
  finding_id: string;
  node: string; // graph node that produced it, e.g. "analyze:c1" or "design"
  title: string | null;
  file_path: string | null;
  line: number | null;
  severity: string | null;
  type: string | null;
  valid: boolean | null; // null = never judged by any gate
  gate: "grounding" | "verify" | null; // which gate decided
  reason: string | null;
}

export interface Review {
  id: string;
  mr_id: string;
  status: ReviewStatus;
  summary: string | null;
  error: string | null;
  stages: ReviewStage[];
  candidates: ReviewCandidate[];
  started_at: string | null;
  finished_at: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  publish: boolean;
}

export interface ReviewListItem {
  id: string;
  mr_id: string;
  mr_iid: number;
  mr_title: string;
  repo_id: string;
  repo_project_path: string;
  profile_name: string | null;
  trigger: string;
  status: ReviewStatus;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  prompt_tokens: number;
  completion_tokens: number;
  queue_position: number | null;
  eta_seconds: number | null;
  publish: boolean;
}

export interface ReviewQueueSummary {
  queued: number;
  running: number;
  avg_duration_s: number | null;
}

export interface TraceToolCall {
  stage: string;
  seq: number;
  tool: string;
  status: string;
  duration_ms: number | null;
}

export interface TraceLLMRound {
  stage: string;
  seq: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  latency_ms: number | null;
  error: string | null;
}

export interface Trace {
  tool_calls: TraceToolCall[];
  llm_rounds: TraceLLMRound[];
}

export type DistillationRunStatus = "queued" | "running" | "done" | "failed";

export interface DistillationRunSummary {
  id: string;
  status: DistillationRunStatus;
  trigger: string;
  tokens: number;
  started_at: string | null;
}

export interface DistillationRun {
  id: string;
  mr_id: string;
  status: DistillationRunStatus;
  trigger: string;
  note_ids: string[];
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface SettingsView {
  values: Record<string, string | number | boolean>;
  overridden: string[];
  secrets: Record<string, boolean>;
}

export interface ReviewerAgentVersion {
  id: string;
  version: number;
  guidelines: string;
  model: string | null;
  max_rounds: number;
  tool_allowlist: string[] | null;
}

export interface ReviewerAgent {
  id: string;
  name: string;
  description: string | null;
  enabled: boolean;
  current_version: ReviewerAgentVersion | null;
}

// Served by GET /agents/available-tools, generated from the pipeline's own
// registered tools rather than a hand-maintained list -- see
// features/agents/toolAllowlist.ts for why that distinction matters.
export interface AvailableTool {
  name: string;
  description: string;
  always_available: boolean;
}

export interface ReviewerAgentVersionHistory extends ReviewerAgentVersion {
  created_by: string | null;
  created_at: string;
  accepted: number;
  rejected: number;
  acceptance_rate: number | null;
}

export interface ReviewerAgentCreate {
  name: string;
  description?: string | null;
  guidelines: string;
  model?: string | null;
  max_rounds?: number;
  tool_allowlist?: string[] | null;
}

export interface Learning {
  id: string;
  repo_id: string | null;
  topic: string;
  hint_text: string;
  file_pattern: string | null;
  kind: "guidance" | "do_not_suggest" | "missed_pattern";
  status: "active" | "archived";
  hit_count: number;
  miss_count: number;
  inconclusive_count: number;
  harmful_count: number;
  ignored_count: number;
  reputation: number;
  injection_count: number;
  groundedness: number | null;
  last_audited_at: string | null;
  hit_rate: number | null;
  created_at: string;
  mr_iid: number | null;
  mr_title: string | null;
  mr_web_url: string | null;
  learned_from_username: string | null;
}

export interface AuditCitation {
  file: string;
  line: number;
  quote: string;
}

export type AuditVerdictType =
  | "corroborated"
  | "stale"
  | "contradicted"
  | "unfalsifiable"
  | "duplicate_of"
  | "conflicts_with"
  | "ungrounded";

export type AuditProposedAction = "none" | "archive" | "merge" | "flag_for_rewrite" | "escalate_to_human";

export type AuditVerdictState = "proposed" | "approved" | "rejected" | "applied";

export interface AuditRun {
  id: string;
  repo_id: string;
  repo_path: string | null;
  status: "running" | "done" | "failed";
  audited_ref: string | null;
  commit_sha: string | null;
  clusters_examined: number;
  verdicts_written: number;
  /** Set once the run starts, so a stalled run is still traceable. */
  langfuse_trace_id: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface AuditVerdict {
  id: string;
  learning_id: string;
  learning_topic: string | null;
  verdict: AuditVerdictType;
  confidence: number;
  rationale: string | null;
  citations: AuditCitation[];
  related_learning_id: string | null;
  proposed_action: AuditProposedAction;
  /** Replacement wording proposed for rewrite actions (flag_for_rewrite, merge). */
  suggested_hint_text: string | null;
  /** What the learning says today, so the proposal can be read against it. */
  current_hint_text: string | null;
  state: AuditVerdictState;
  created_at: string;
  /** The repo the auditor actually searched. */
  audited_repo_id: string | null;
  audited_repo_path: string | null;
  /** The repo the learning is about; null means global (every repo). */
  learning_repo_id: string | null;
  learning_repo_path: string | null;
}
export interface FileKnowledgeEntry {
  id: string;
  repo_id: string;
  file_path: string;
  blob_sha: string | null;
  summary: string | null;
  notes: { note: string; review_id: string | null; at: string }[] | null;
  updated_at: string;
}
export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  per_page: number;
}

export interface DashboardTiles {
  mrs_reviewed: number;
  agent_comments: number;
  human_replies: number;
  accepted: number;
  rejected: number;
  acceptance_rate: number | null;
  active_learnings: number;
  pending_distillation: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface DashboardDay {
  date: string;
  count: number;
}

export interface DashboardAgent {
  agent_id: string;
  name: string;
  current_version: number | null;
  comments: number;
  accepted: number;
  rejected: number;
  open: number;
  hit_rate: number | null;
}

export interface DashboardActivity {
  type: string;
  title: string;
  detail: string | null;
  at: string;
  href_id: string | null;
}

export interface DashboardStats {
  window_days: number;
  tiles: DashboardTiles;
  reviews_per_day: DashboardDay[];
  agents: DashboardAgent[];
  activity: DashboardActivity[];
}

export interface NoteVerdictIn {
  disposition: "accepted" | "rejected_with_rationale" | "dismissed_ambiguous";
  comment?: string | null;
}

export interface AgentComplaint {
  id: string;
  review_id: string | null;
  stage_name: string | null;
  category: string;
  target: string | null;
  detail: string;
  blocked: boolean;
  created_at: string;
}

export interface ComplaintTargetCount {
  target: string | null;
  category: string;
  count: number;
  blocked_count: number;
  last_seen: string;
}

export interface PaginatedComplaints {
  items: AgentComplaint[];
  total: number;
  page: number;
  per_page: number;
  by_target: ComplaintTargetCount[];
}

export interface RepoAgentSetting {
  agent_id: string;
  name: string;
  description: string | null;
  globally_enabled: boolean;
  /** Effective for this repo: a globally-enabled agent can still be off here. */
  enabled_here: boolean;
}

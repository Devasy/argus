import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    project_path: str
    gitlab_project_id: int
    enabled: bool
    poll_interval_s: int
    default_profile_id: uuid.UUID | None = None
    auto_review_enabled: bool = False
    mr_count: int = 0


class PaginatedRepositories(BaseModel):
    items: list[RepositoryOut]
    total: int
    page: int
    per_page: int


class MergeRequestOut(BaseModel):
    id: uuid.UUID
    mr_iid: int
    title: str
    state: str
    author_username: str | None = None
    web_url: str
    mr_updated_at: datetime | None = None
    accepted_count: int = 0
    rejected_count: int = 0
    leftover_count: int = 0


class MergeRequestStateCounts(BaseModel):
    all: int
    opened: int
    merged: int
    closed: int


class PaginatedMergeRequests(BaseModel):
    items: list[MergeRequestOut]
    total: int
    page: int
    per_page: int
    state_counts: MergeRequestStateCounts


class NoteOut(BaseModel):
    id: uuid.UUID
    author_username: str | None = None
    author_type: str
    kind: str
    body: str
    file_path: str | None = None
    line: int | None = None
    disposition: str


class ReviewSummaryOut(BaseModel):
    id: uuid.UUID
    status: str
    trigger: str
    profile_version: str | None = None
    tokens: int
    started_at: datetime | None = None
    sequence_number: int | None = None
    head_commit_sha: str | None = None
    incremental_file_count: int | None = None
    publish: bool = True


class DistillationRunSummaryOut(BaseModel):
    id: uuid.UUID
    status: str
    trigger: str
    tokens: int
    started_at: datetime | None = None


class DistillationRunOut(BaseModel):
    id: uuid.UUID
    mr_id: uuid.UUID
    status: str
    trigger: str
    note_ids: list[str]
    error: str | None = None
    langfuse_trace_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    prompt_tokens: int
    completion_tokens: int


class MergeRequestDetail(BaseModel):
    mr: MergeRequestOut
    notes: list[NoteOut]
    reviews: list[ReviewSummaryOut]
    distillation_runs: list[DistillationRunSummaryOut]


class RepositoryCreate(BaseModel):
    project_path: str


class ReviewerProxyCreate(BaseModel):
    reviewer: str
    proxy_url: str


class ReviewerProxyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    reviewer: str
    proxy_url: str
    enabled: bool


class ProfileVersionIn(BaseModel):
    name: str
    system_prompt: str
    guidelines: str | None = None
    tool_allowlist: list[str] | None = None
    model: str | None = None


class ProfileVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    version: int
    system_prompt: str
    guidelines: str | None = None
    tool_allowlist: list[str] | None = None
    model: str | None = None


class ReviewerProfileOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    is_builtin: bool
    current_version: ProfileVersionOut | None = None


class RepositoryUpdate(BaseModel):
    enabled: bool | None = None
    poll_interval_s: int | None = None
    default_profile_id: uuid.UUID | None = None
    auto_review_enabled: bool | None = None


class RepoAgentSettingOut(BaseModel):
    agent_id: uuid.UUID
    name: str
    description: str | None = None
    globally_enabled: bool
    # Effective for THIS repo: an agent enabled globally can still be off here.
    enabled_here: bool


class RepoAgentSettingUpdate(BaseModel):
    enabled: bool


class ReviewerProxyUpdate(BaseModel):
    proxy_url: str | None = None
    enabled: bool | None = None


class SettingsUpdate(BaseModel):
    values: dict[str, bool | int | str]


class ReviewerAgentVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    version: int
    guidelines: str
    model: str | None = None
    max_rounds: int
    tool_allowlist: list[str] | None = None


class ReviewerAgentOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    enabled: bool
    current_version: ReviewerAgentVersionOut | None = None


class PaginatedReviewerAgents(BaseModel):
    items: list[ReviewerAgentOut]
    total: int
    page: int
    per_page: int


class AvailableToolOut(BaseModel):
    name: str
    description: str
    # True for tools every agent receives regardless of tool_allowlist (see
    # argus.review.pipeline.ALWAYS_AVAILABLE_TOOLS) -- the frontend should
    # show these as informational rather than as a togglable chip, since
    # unchecking one would not actually revoke access.
    always_available: bool


class ReviewerAgentCreate(BaseModel):
    name: str
    description: str | None = None
    guidelines: str
    model: str | None = None
    max_rounds: int = 10
    tool_allowlist: list[str] | None = None


class ReviewerAgentVersionIn(BaseModel):
    guidelines: str | None = None
    model: str | None = None
    max_rounds: int | None = None
    tool_allowlist: list[str] | None = None


class ReviewerAgentEnabledUpdate(BaseModel):
    enabled: bool


class ReviewerAgentDescriptionUpdate(BaseModel):
    description: str | None = None


class ReviewerAgentVersionHistoryOut(BaseModel):
    id: uuid.UUID
    version: int
    guidelines: str
    model: str | None = None
    max_rounds: int
    tool_allowlist: list[str] | None = None
    created_by: str | None = None
    created_at: datetime
    accepted: int
    rejected: int
    acceptance_rate: float | None = None


class PaginatedReviewerAgentVersions(BaseModel):
    items: list[ReviewerAgentVersionHistoryOut]
    total: int
    page: int
    per_page: int


class LearningOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    repo_id: uuid.UUID | None = None
    topic: str
    hint_text: str
    file_pattern: str | None = None
    kind: str
    status: str
    hit_count: int
    miss_count: int
    inconclusive_count: int
    harmful_count: int = 0
    ignored_count: int = 0
    reputation: float = 0.5
    injection_count: int = 0
    groundedness: float | None = None
    last_audited_at: datetime | None = None
    created_at: datetime
    mr_iid: int | None = None
    mr_title: str | None = None
    mr_web_url: str | None = None
    learned_from_username: str | None = None

    @computed_field
    @property
    def hit_rate(self) -> float | None:
        total = self.hit_count + self.miss_count
        return (self.hit_count / total) if total else None


class PaginatedLearnings(BaseModel):
    items: list[LearningOut]
    total: int
    page: int
    per_page: int


class AgentComplaintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    review_id: uuid.UUID | None = None
    stage_name: str | None = None
    category: str
    target: str | None = None
    detail: str
    blocked: bool
    created_at: datetime


class ComplaintTargetCount(BaseModel):
    """One row of the "what is failing most" rollup. A single complaint is
    noise; this is the view that turns forty of them into a bug report."""
    target: str | None = None
    category: str
    count: int
    blocked_count: int
    last_seen: datetime


class PaginatedComplaints(BaseModel):
    items: list[AgentComplaintOut]
    total: int
    page: int
    per_page: int
    # Rollup across the WHOLE filtered set, not just this page -- the point of
    # the page is spotting the tool that fails everywhere.
    by_target: list[ComplaintTargetCount] = []


class FileKnowledgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    repo_id: uuid.UUID
    file_path: str
    blob_sha: str | None = None
    summary: str | None = None
    notes: list | None = None
    updated_at: datetime


class PaginatedFileKnowledge(BaseModel):
    items: list[FileKnowledgeOut]
    total: int
    page: int
    per_page: int


class AuditVerdictOut(BaseModel):
    id: uuid.UUID
    learning_id: uuid.UUID
    learning_topic: str | None = None
    verdict: str
    confidence: float
    rationale: str | None = None
    citations: list[dict] = []
    related_learning_id: uuid.UUID | None = None
    proposed_action: str
    # For rewrite actions: what the learning says now, and what the auditor
    # proposes it should say. Both are needed to review the change -- a
    # replacement is only judgeable next to what it replaces.
    suggested_hint_text: str | None = None
    current_hint_text: str | None = None
    state: str
    created_at: datetime
    # Which repository the auditor actually searched. Without it a verdict
    # cannot be judged: "no matches found in the codebase" means nothing until
    # you know WHICH codebase, and an audit of the Python backend once
    # proposed archiving a React learning on exactly that reasoning.
    audited_repo_id: uuid.UUID | None = None
    audited_repo_path: str | None = None
    # The learning's own scope. None means global -- it belongs to every repo,
    # so a single repo's evidence can never settle it.
    learning_repo_id: uuid.UUID | None = None
    learning_repo_path: str | None = None


class PaginatedAuditVerdicts(BaseModel):
    items: list[AuditVerdictOut]
    total: int
    page: int
    per_page: int


class DashboardTilesOut(BaseModel):
    mrs_reviewed: int
    agent_comments: int
    human_replies: int
    accepted: int
    rejected: int
    acceptance_rate: float | None = None
    active_learnings: int
    pending_distillation: int
    prompt_tokens: int
    completion_tokens: int


class DashboardDayOut(BaseModel):
    date: str          # "YYYY-MM-DD"
    count: int


class DashboardAgentOut(BaseModel):
    agent_id: uuid.UUID
    name: str
    current_version: int | None = None
    comments: int
    accepted: int
    rejected: int
    open: int
    hit_rate: float | None = None


class DashboardActivityOut(BaseModel):
    type: str          # review_running|review_done|review_failed|verdict|learning|distillation
    title: str
    detail: str | None = None
    at: datetime
    href_id: uuid.UUID | None = None   # review id / mr id / run id for linking


class DashboardStatsOut(BaseModel):
    window_days: int
    tiles: DashboardTilesOut
    reviews_per_day: list[DashboardDayOut]
    agents: list[DashboardAgentOut]
    activity: list[DashboardActivityOut]


class ReviewListItemOut(BaseModel):
    id: uuid.UUID
    mr_id: uuid.UUID
    mr_iid: int
    mr_title: str
    repo_id: uuid.UUID
    repo_project_path: str
    profile_name: str | None = None
    trigger: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    prompt_tokens: int
    completion_tokens: int
    queue_position: int | None = None
    eta_seconds: float | None = None
    publish: bool = True


class PaginatedReviews(BaseModel):
    items: list[ReviewListItemOut]
    total: int
    page: int
    per_page: int


class ReviewQueueSummaryOut(BaseModel):
    queued: int
    running: int
    avg_duration_s: float | None = None

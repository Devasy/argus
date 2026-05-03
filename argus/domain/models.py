import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Float, ForeignKey,
                        Integer, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = 768
TIMESTAMPTZ = TIMESTAMP(timezone=True)


def _uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True,
                         server_default=text("gen_random_uuid()"))


def _now():
    return mapped_column(TIMESTAMPTZ, server_default=text("now()"))


class Base(DeclarativeBase):
    pass


Base.metadata.schema = "argus"


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("provider", "project_path"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(Text, default="gitlab")
    project_path: Mapped[str] = mapped_column(Text)
    gitlab_project_id: Mapped[int] = mapped_column(BigInteger)
    default_branch: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_s: Mapped[int] = mapped_column(Integer, default=120)
    default_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reviewer_profiles.id"))
    poll_cursor: Mapped[dict | None] = mapped_column(JSONB)
    auto_review_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()


class Actor(Base):
    __tablename__ = "actors"
    __table_args__ = (UniqueConstraint("provider", "provider_user_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(Text, default="gitlab")
    provider_user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)


class MergeRequest(Base):
    __tablename__ = "merge_requests"
    __table_args__ = (
        UniqueConstraint("repo_id", "mr_iid"),
        CheckConstraint("state IN ('opened','merged','closed','locked')",
                        name="mr_state_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"))
    mr_iid: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("actors.id"))
    merged_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("actors.id"))
    source_branch: Mapped[str] = mapped_column(Text)
    target_branch: Mapped[str] = mapped_column(Text)
    head_sha: Mapped[str] = mapped_column(Text)
    draft: Mapped[bool] = mapped_column(Boolean, default=False)
    has_conflicts: Mapped[bool] = mapped_column(Boolean, default=False)
    blocking_discussions_resolved: Mapped[bool] = mapped_column(Boolean, default=True)
    detailed_merge_status: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[list | None] = mapped_column(JSONB)
    web_url: Mapped[str] = mapped_column(Text)
    merged_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    mr_created_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    mr_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class MRVersion(Base):
    __tablename__ = "mr_versions"
    __table_args__ = (UniqueConstraint("mr_id", "provider_version_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    mr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merge_requests.id"))
    provider_version_id: Mapped[int] = mapped_column(BigInteger)
    head_commit_sha: Mapped[str] = mapped_column(Text)
    base_commit_sha: Mapped[str | None] = mapped_column(Text)
    start_commit_sha: Mapped[str] = mapped_column(Text)
    version_created_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class MRParticipant(Base):
    __tablename__ = "mr_participants"
    __table_args__ = (
        UniqueConstraint("mr_id", "actor_id", "role"),
        CheckConstraint("role IN ('author','assignee','reviewer','approver','participant')",
                        name="participant_role_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    mr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merge_requests.id"))
    actor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("actors.id"))
    role: Mapped[str] = mapped_column(Text)
    review_state: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class Discussion(Base):
    __tablename__ = "discussions"
    __table_args__ = (UniqueConstraint("mr_id", "provider_discussion_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    mr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merge_requests.id"))
    provider_discussion_id: Mapped[str] = mapped_column(Text)
    individual_note: Mapped[bool] = mapped_column(Boolean, default=False)
    resolvable: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("actors.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("mr_id", "provider_note_id"),
        CheckConstraint("author_type IN ('bot','human','system')",
                        name="note_author_type_check"),
        CheckConstraint("kind IN ('inline','summary','system')",
                        name="note_kind_check"),
        CheckConstraint(
            "disposition IN ('open','accepted','accepted_manually','answered',"
            "'rejected_with_rationale','dismissed_ambiguous','resolved_no_answer',"
            "'replied_unclassified')",
            name="note_disposition_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    mr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merge_requests.id"))
    review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reviews.id"))
    discussion_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("discussions.id"))
    parent_note_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("notes.id"))
    provider_note_id: Mapped[int] = mapped_column(BigInteger)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("actors.id"))
    author_type: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    system_event_type: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    line: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[dict | None] = mapped_column(JSONB)
    suggestions: Mapped[list | None] = mapped_column(JSONB)
    disposition: Mapped[str] = mapped_column(Text, default="open")
    note_created_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    raw: Mapped[dict | None] = mapped_column(JSONB)


class ReviewerProfile(Base):
    __tablename__ = "reviewer_profiles"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("profile_versions.id", use_alter=True))


class ProfileVersion(Base):
    __tablename__ = "profile_versions"
    __table_args__ = (UniqueConstraint("profile_id", "version"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviewer_profiles.id"))
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text)
    guidelines: Mapped[str | None] = mapped_column(Text)
    tool_allowlist: Mapped[list | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(Text)
    temperature: Mapped[float | None] = mapped_column(Float)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ReviewerAgent(Base):
    """A named, versioned reviewer agent Scout can dynamically assign to an MR.
    Distinct from ReviewerProfile: a profile's guidelines apply to every
    pipeline stage; an agent's guidelines apply only when Scout assigns it."""
    __tablename__ = "reviewer_agents"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reviewer_agent_versions.id", use_alter=True))


class ReviewerAgentRepoSetting(Base):
    """Per-repo override of whether a reviewer agent may run.

    Agents are global and enabled by default, so every specialist was offered
    to Scout on every repository -- including ones where it has nothing useful
    to say (a frontend agent on a plugins repo), which costs a round of LLM
    time and adds noise to the review.

    Opt-OUT by design: the absence of a row means the agent runs, so adding
    this changes nothing for repos nobody has configured. Only the exceptions
    you have actually observed need recording.
    """

    __tablename__ = "reviewer_agent_repo_settings"
    __table_args__ = (UniqueConstraint("agent_id", "repo_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviewer_agents.id"))
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ReviewerAgentVersion(Base):
    __tablename__ = "reviewer_agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviewer_agents.id"))
    version: Mapped[int] = mapped_column(Integer)
    guidelines: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    max_rounds: Mapped[int] = mapped_column(Integer, default=10)
    tool_allowlist: Mapped[list | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class ReviewReviewerAgentVersion(Base):
    """Records which reviewer-agent version ran on which review — the join
    that lets acceptance-rate be computed per agent version."""
    __tablename__ = "review_reviewer_agent_versions"
    __table_args__ = (UniqueConstraint("review_id", "agent_version_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviews.id"))
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reviewer_agent_versions.id"))


class LLMEndpoint(Base):
    __tablename__ = "llm_endpoints"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('anthropic','openai','gemini','ollama','claude_cli_proxy')",
            name="endpoint_provider_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, unique=True)
    provider: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    api_key_ref: Mapped[str | None] = mapped_column(Text)  # env var NAME, never the key
    model: Mapped[str] = mapped_column(Text)
    tool_calling_native: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class ReviewerProxy(Base):
    __tablename__ = "reviewer_proxies"
    id: Mapped[uuid.UUID] = _uuid_pk()
    reviewer: Mapped[str] = mapped_column(Text, unique=True)   # gitlab username
    proxy_url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now()


class RuntimeSetting(Base):
    """UI-editable runtime config overlaying env defaults. Never holds secrets."""
    __tablename__ = "runtime_settings"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)   # {"v": <scalar>}
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','done','failed','canceled')",
                        name="review_status_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    mr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merge_requests.id"))
    mr_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mr_versions.id"))
    profile_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("profile_versions.id"))
    llm_endpoint_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("llm_endpoints.id"))
    trigger: Mapped[str] = mapped_column(Text, default="manual")
    status: Mapped[str] = mapped_column(Text, default="queued")
    # "full" (default): normal scout/analyze/verify/publish review.
    # "qa_scenarios": dedicated per-chunk QA test-scenario run -- skips
    # analyze_chunk/run_reviewer_agent/verify entirely (see pipeline.py's
    # build_graph branching), publishes only a checklist comment.
    mode: Mapped[str] = mapped_column(Text, default="full")
    # False = run everything, record every finding, post nothing. Orthogonal
    # to `mode` above, which says what kind of review this is.
    publish: Mapped[bool] = mapped_column(Boolean, default=True,
                                          server_default=text("true"))
    llm_config: Mapped[dict | None] = mapped_column(JSONB)
    # What the endpoint reported serving. NULL means unknown, NOT llm_config's
    # model: reviews before this column, and hosted providers, cannot be
    # attributed, so exclude NULLs from per-model comparisons rather than
    # bucketing them under the requested name.
    served_model: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    force_agents: Mapped[list | None] = mapped_column(JSONB)
    incremental_files: Mapped[list | None] = mapped_column(JSONB)
    langfuse_trace_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    created_at: Mapped[datetime] = _now()


class ReviewStage(Base):
    __tablename__ = "review_stages"
    __table_args__ = (
        UniqueConstraint("review_id", "stage_name"),
        CheckConstraint("status IN ('running','done','failed')",
                        name="stage_status_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviews.id"))
    stage_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running")
    artifact: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint("severity IN ('critical','high','medium','low')",
                        name="finding_severity_check"),
        CheckConstraint("type IN ('issue','suggestion','question')",
                        name="finding_type_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviews.id"))
    stage: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    file_path: Mapped[str] = mapped_column(Text)
    line: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    suggestion_old: Mapped[str | None] = mapped_column(Text)
    suggestion_new: Mapped[str | None] = mapped_column(Text)
    verdict_valid: Mapped[bool | None] = mapped_column(Boolean)
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    published_note_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("notes.id"))
    provider_suggestion_id: Mapped[int | None] = mapped_column(BigInteger)
    contributing_learning_ids: Mapped[list | None] = mapped_column(JSONB)
    # True when the finding is about code outside this MR's diff, so it was
    # reported in the summary rather than as an inline comment. Persisted so
    # these are visible to analytics and incremental review rather than being
    # indistinguishable from findings that simply lost the inline budget.
    outside_diff: Mapped[bool] = mapped_column(Boolean, default=False,
                                               server_default="false")


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "(review_id IS NOT NULL)::int + (distillation_run_id IS NOT NULL)::int"
            " + (audit_run_id IS NOT NULL)::int = 1",
            name="tool_calls_exactly_one_parent_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reviews.id"))
    distillation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("distillation_runs.id"))
    audit_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audit_runs.id"))
    stage_name: Mapped[str | None] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(Text)
    args: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="ok")
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _now()


class AgentComplaint(Base):
    """A problem an agent hit with its own tools, prompt, or context.

    Agents are the best-positioned observers of our tooling and they had no
    way to speak. Every bug found while investigating the 2026-08 review
    failures was already known to an agent and buried in a `thinking` block:
    "graph_impact didn't give me much useful information" (the call graph had
    zero edges), "the hunk_ids don't seem to be working" (a misleading error
    message). Both were correct diagnoses of real bugs that took a human
    reading Langfuse traces to surface.

    Read in aggregate, not individually: one complaint about a tool is noise,
    forty is a bug. Agents will sometimes be wrong -- "the graph is broken"
    when a repo genuinely has no callers -- so this is a triage queue, never
    an alarm.
    """

    __tablename__ = "agent_complaints"
    __table_args__ = (
        CheckConstraint(
            "category IN ('tool_broken','tool_missing','context_insufficient',"
            "'context_wrong','prompt_irrelevant','prompt_unclear',"
            "'instructions_conflict','task_impossible')",
            name="agent_complaint_category_check"),
        # Mirrors tool_calls: a complaint belongs to exactly one kind of run.
        CheckConstraint(
            "(review_id IS NOT NULL)::int + (distillation_run_id IS NOT NULL)::int"
            " + (audit_run_id IS NOT NULL)::int = 1",
            name="agent_complaints_exactly_one_parent_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reviews.id"))
    distillation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("distillation_runs.id"))
    audit_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audit_runs.id"))
    stage_name: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    # The tool, prompt, or input at fault: "graph_impact", "SCOUT_STATIC".
    # Free text because the agent may name something we have not thought of,
    # and constraining it would silently drop the most interesting reports.
    target: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    # Whether this stopped the work or the agent found a way around it. Kept
    # instead of a severity field: severity invites rote grading, while this
    # is a fact the agent actually knows.
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()


class LLMRound(Base):
    __tablename__ = "llm_rounds"
    __table_args__ = (
        CheckConstraint(
            "(review_id IS NOT NULL)::int + (distillation_run_id IS NOT NULL)::int"
            " + (audit_run_id IS NOT NULL)::int = 1",
            name="llm_rounds_exactly_one_parent_check"),
        CheckConstraint(
            "status IN ('running','done','failed')",
            name="llm_rounds_status_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reviews.id"))
    distillation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("distillation_runs.id"))
    audit_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audit_runs.id"))
    stage_name: Mapped[str | None] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="done")
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class AuditRun(Base):
    __tablename__ = "audit_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','done','failed')",
                        name="audit_run_status_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"))
    status: Mapped[str] = mapped_column(Text, default="running")
    # What was actually examined. Without these a verdict of "this pattern
    # does not exist in the codebase" cannot be checked: an audit that ran
    # against `master` declared a learning about data/rabbitmq/custom.conf
    # nonexistent when the file lives on develop-7.0.0. commit_sha existed
    # but was never written.
    audited_ref: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    clusters_examined: Mapped[int] = mapped_column(Integer, default=0)
    verdicts_written: Mapped[int] = mapped_column(Integer, default=0)
    # Written when the run starts, exactly as reviews do it: the id is a pure
    # function of the run id, and a run that dies mid-way is the one whose
    # trace someone will go looking for.
    langfuse_trace_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    created_at: Mapped[datetime] = _now()


class AuditVerdict(Base):
    __tablename__ = "audit_verdicts"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('corroborated','stale','contradicted','unfalsifiable',"
            "'duplicate_of','conflicts_with','ungrounded')",
            name="audit_verdict_check"),
        CheckConstraint(
            "proposed_action IN ('none','archive','merge','flag_for_rewrite',"
            "'escalate_to_human')", name="audit_action_check"),
        CheckConstraint("state IN ('proposed','approved','rejected','applied')",
                        name="audit_state_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    audit_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("audit_runs.id"))
    learning_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("learnings.id"))
    verdict: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list | None] = mapped_column(JSONB)
    related_learning_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learnings.id"))
    proposed_action: Mapped[str] = mapped_column(Text, default="none")
    # Concrete replacement text for the learning's hint, required whenever the
    # action rewrites rather than removes (flag_for_rewrite, merge). Without
    # it, approving such a verdict did nothing at all: apply_verdict only ever
    # handled archive and merge, so a human could approve a "needs rewriting"
    # verdict and watch the learning stay exactly as it was. Naming the
    # replacement is what makes approval an action instead of an opinion.
    suggested_hint_text: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="proposed")
    applied_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    created_at: Mapped[datetime] = _now()


class Learning(Base):
    __tablename__ = "learnings"
    __table_args__ = (
        CheckConstraint("status IN ('active','archived')", name="learning_status_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("repositories.id"))
    topic: Mapped[str] = mapped_column(Text)
    hint_text: Mapped[str] = mapped_column(Text)
    file_pattern: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="guidance")  # guidance | do_not_suggest
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM))
    status: Mapped[str] = mapped_column(Text, default="active")
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    miss_count: Mapped[int] = mapped_column(Integer, default=0)
    inconclusive_count: Mapped[int] = mapped_column(Integer, default=0)
    harmful_count: Mapped[int] = mapped_column(Integer, default=0)
    ignored_count: Mapped[int] = mapped_column(Integer, default=0)
    groundedness: Mapped[float | None] = mapped_column(Float)
    last_audited_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    audited_at_sha: Mapped[str | None] = mapped_column(Text)
    source_review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reviews.id"))
    mr_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("merge_requests.id"))
    source_note_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("notes.id"))
    learned_from_actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("actors.id"))
    created_at: Mapped[datetime] = _now()
    file_paths: Mapped[list | None] = mapped_column(JSONB)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)


class DistillationRun(Base):
    __tablename__ = "distillation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','done','failed')",
                        name="distillation_run_status_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    mr_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merge_requests.id"))
    trigger: Mapped[str] = mapped_column(Text, default="reconcile")
    status: Mapped[str] = mapped_column(Text, default="queued")
    note_ids: Mapped[list] = mapped_column(JSONB)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    langfuse_trace_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    created_at: Mapped[datetime] = _now()


class InjectionEvent(Base):
    __tablename__ = "injection_events"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('pending','hit','miss','inconclusive','harmful','ignored')",
            name="injection_outcome_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    learning_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("learnings.id"))
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviews.id"))
    outcome: Mapped[str] = mapped_column(Text, default="pending")
    finding_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("findings.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)


class FileKnowledge(Base):
    __tablename__ = "file_knowledge"
    __table_args__ = (UniqueConstraint("repo_id", "file_path"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"))
    file_path: Mapped[str] = mapped_column(Text)
    blob_sha: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    key_symbols: Mapped[list | None] = mapped_column(JSONB)
    notes: Mapped[list | None] = mapped_column(JSONB)  # appendable observations
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIM))
    updated_by_review_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reviews.id"))
    updated_at: Mapped[datetime] = _now()


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("kind IN ('reply','applied','reaction','resolved','ui_answer')",
                        name="feedback_kind_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id"))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("actors.id"))
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class RawEvent(Base):
    __tablename__ = "raw_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    repo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("repositories.id"))
    mr_iid: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = _now()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','done','failed')",
                        name="job_status_check"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    dedup_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    run_after: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()

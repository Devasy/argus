import operator
from typing import Annotated, Literal

from pydantic import BaseModel


class Hunk(BaseModel):
    hunk_id: str
    file_id: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    diff_text: str


class FileChange(BaseModel):
    file_id: str
    path: str
    old_path: str | None = None
    language: str | None = None
    change_kind: Literal["added", "modified", "deleted", "renamed"]
    hunk_ids: list[str] = []
    summary: str | None = None
    risk_flags: list[str] = []
    # False when GitLab returned the file without a usable diff body -- it
    # collapses/omits `diff` on large MRs (the `collapsed`/`too_large` flags),
    # and a binary file never has one. Such a file still appears in the change
    # list but has NO hunks, so get_hunk can return nothing for it. Surfacing
    # this explicitly is what stops an agent burning its whole round budget
    # re-requesting hunks that can never exist; read the file with
    # get_file_lines instead.
    diff_available: bool = True


class Chunk(BaseModel):
    chunk_id: str
    file_ids: list[str]
    lenses: list[str]


class ReviewPlan(BaseModel):
    intent: str
    mr_summary: str
    files: list[FileChange]
    chunks: list[Chunk] = []
    assigned_agents: list[str] = []
    notes_for_agents: str = ""


class CandidateFinding(BaseModel):
    finding_id: str
    stage: str
    chunk_id: str | None = None
    # "question" is not a defect claim -- it is an open question for the MR
    # author, used when a change looks unrelated to the MR's stated intent or
    # its motivation is not apparent from the diff. It is exempt from the
    # "must describe a concrete failure" rule every other finding follows,
    # because the whole point is that the agent does NOT know whether
    # something is wrong. Reviewers ask this constantly; the bot could not.
    type: Literal["issue", "suggestion", "question"]
    severity: Literal["critical", "high", "medium", "low"]
    confidence: float
    file_path: str
    line: int
    title: str
    body: str
    evidence_quote: str
    suggestion_old: str | None = None
    suggestion_new: str | None = None
    # Short ids (first 8 chars) of team learnings that drove this finding, as
    # cited by the reviewer agent. Empty when the finding was found unaided.
    contributing_learning_ids: list[str] = []
    # True when this finding is about code the MR did not touch -- typically a
    # caller that the change breaks. GitLab can only anchor an inline comment
    # to a line inside the diff, so these cannot be posted inline; they are
    # collected into a dedicated summary section instead of being silently
    # dropped or retried as un-anchored noise. Set by the publisher, not by
    # the agent (the agent has no view of the diff's line ranges).
    outside_diff: bool = False


class Verdict(BaseModel):
    finding_id: str
    valid: bool
    reason: str
    # Set when the underlying issue is real but the candidate finding itself
    # was malformed (bad formatting, wrong evidence quote, unclear body) --
    # publish uses this corrected version instead of the original when present.
    corrected: CandidateFinding | None = None
    # Short ids (first 8 chars) of do_not_suggest learnings the verifier
    # actually applied to reach this verdict. A suppression's only observable
    # effect is a finding being dropped, so this is the sole way it can be
    # credited rather than looking permanently unused.
    applied_learning_ids: list[str] = []


class CompiledReview(BaseModel):
    published_finding_ids: list[str] = []
    summary_markdown: str = ""


class TestScenario(BaseModel):
    __test__ = False  # not a pytest test class, just named TestScenario

    scenario_id: str = ""
    chunk_id: str | None = None
    title: str
    area: Literal["happy_path", "edge_case", "regression", "error_handling"]
    steps: list[str]
    expected_result: str
    relevant_files: list[str] = []


class ReviewState(BaseModel):
    review_id: str
    plan: ReviewPlan | None = None
    findings: Annotated[list[CandidateFinding], operator.add] = []
    verdicts: list[Verdict] = []
    compiled: CompiledReview | None = None
    # Grounding outcome from `verify`, propagated explicitly rather than via
    # in-place mutation of CandidateFinding objects (which is fragile across
    # checkpoint/resume serialize-deserialize round-trips). `verify` runs once
    # downstream of the `gather` fan-in point, so plain "last write wins"
    # fields (not operator.add-reduced) are safe here.
    ungrounded_ids: list[str] = []
    line_corrections: dict[str, int] = {}
    # Fanned out per chunk by qa_chunk (mode="qa_scenarios"), same reduction
    # strategy as `findings` above -- multiple concurrent chunk branches each
    # contribute their own scenarios.
    test_scenarios: Annotated[list[TestScenario], operator.add] = []

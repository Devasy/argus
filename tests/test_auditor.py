from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from argus.knowledge.audit_scheduler import select_repos_to_audit
from argus.knowledge.auditor import (ClusterAudit, LearningVerdict,
                                         format_cluster_prompt)


class _L:
    def __init__(self, id, topic, hint, paths=None, pattern=None):
        self.id = id
        self.topic = topic
        self.hint_text = hint
        self.file_paths = paths
        self.file_pattern = pattern
        self.hit_count = 0
        self.harmful_count = 0
        self.ignored_count = 0
        self.miss_count = 0
        self.inconclusive_count = 0


def test_prompt_lists_every_learning_with_its_id():
    cluster = [_L("aaaaaaaa-0000", "logging", "use lazy formatting"),
               _L("bbbbbbbb-0000", "logging2", "prefer lazy log fmt")]
    prompt = format_cluster_prompt(cluster)
    assert "aaaaaaaa-0000" in prompt
    assert "bbbbbbbb-0000" in prompt
    assert "use lazy formatting" in prompt


def test_prompt_includes_outcome_evidence():
    l = _L("cccccccc-0000", "t", "h")
    l.hit_count = 4
    l.harmful_count = 2
    prompt = format_cluster_prompt([l])
    assert "4" in prompt and "2" in prompt


def test_cluster_audit_schema_round_trip():
    audit = ClusterAudit(verdicts=[
        LearningVerdict(learning_id="aaaaaaaa-0000", verdict="corroborated",
                        confidence=0.8, rationale="matches",
                        citations=[{"file": "a.py", "line": 1, "quote": "x"}],
                        proposed_action="none")])
    assert audit.verdicts[0].verdict == "corroborated"
    assert audit.verdicts[0].citations[0].file == "a.py"


def test_verdict_rejects_unknown_action():
    with pytest.raises(Exception):
        LearningVerdict(learning_id="x", verdict="stale", confidence=0.5,
                        rationale="r", citations=[], proposed_action="delete")


class _Repo:
    def __init__(self, id, last_audit_at=None):
        self.id = id
        self.last_audit_at = last_audit_at


def test_never_audited_repo_is_selected():
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    repos = [_Repo("a", None)]
    assert len(select_repos_to_audit(repos, now, 7)) == 1


def test_recently_audited_repo_is_skipped():
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    repos = [_Repo("a", now - timedelta(days=2))]
    assert select_repos_to_audit(repos, now, 7) == []


def test_overdue_repo_is_selected():
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    repos = [_Repo("a", now - timedelta(days=30))]
    assert len(select_repos_to_audit(repos, now, 7)) == 1


# --- proposed_action must follow from the verdict ---------------------------
# The first production audit (run 9455ec42, Diskbot) returned five verdicts,
# every one with proposed_action="none" -- including an `ungrounded` one. The
# prompt said "be conservative, prefer none" and never said when an action WAS
# warranted, so the model rationally proposed nothing every time. Since
# apply_verdict only acts on "archive" and "merge", that made the entire
# approve/apply pipeline unreachable: audits produced opinions nobody could
# act on.

import pytest

from argus.knowledge.auditor import _SYSTEM, default_action_for


@pytest.mark.parametrize("verdict,expected", [
    ("stale", "archive"),
    ("contradicted", "archive"),
    ("unfalsifiable", "flag_for_rewrite"),
    ("duplicate_of", "merge"),
    ("conflicts_with", "escalate_to_human"),
])
def test_bare_none_is_upgraded_to_the_implied_action(verdict, expected):
    assert default_action_for(verdict, "none") == expected


@pytest.mark.parametrize("verdict", ["corroborated", "ungrounded"])
def test_verdicts_that_genuinely_imply_no_action_stay_none(verdict):
    """corroborated is working as intended; ungrounded means we found no code
    referent, which is not evidence the learning is wrong."""
    assert default_action_for(verdict, "none") == "none"


def test_an_explicitly_proposed_action_is_never_overridden():
    """The default only fills a gap -- it must not overrule a model that
    actually thought about it."""
    assert default_action_for("stale", "flag_for_rewrite") == "flag_for_rewrite"
    assert default_action_for("corroborated", "archive") == "archive"


def test_prompt_states_the_mapping_rather_than_only_urging_caution():
    """The prompt is the first line of defence; the code default is a backstop
    for a model that ignores it."""
    flat = " ".join(_SYSTEM.split())
    assert "stale -> archive" in flat
    assert "duplicate_of -> merge" in flat
    # and it must warn against the exact failure observed
    assert "Do NOT answer 'none' merely because you are cautious" in flat


# --- global learnings must not be audited against one repo ------------------
# An audit of the Python backend proposed archiving a GLOBAL Ant Design
# learning because "no matches found for setFieldValue anywhere in the
# codebase... the codebase is a Python backend". True of that repo, and
# irrelevant: the learning belongs to a 305-file React UI the auditor never
# looked at. Approving it would have destroyed correct team knowledge.

async def test_audit_clustering_excludes_global_learnings(db):
    """A repo audit sees only its own learnings. `include_global=False` is the
    difference between "this repo disproves it" and "I looked in the wrong
    place"."""
    import uuid as _uuid

    from argus.domain.models import Learning, Repository
    from argus.knowledge.clustering import cluster_learnings

    repo = Repository(provider="gitlab",
                      project_path=f"g/scope-{_uuid.uuid4()}",
                      gitlab_project_id=60000000 + _uuid.uuid4().int % 9000000)
    db.add(repo)
    await db.flush()

    db.add(Learning(repo_id=repo.id, topic="scoped", hint_text="belongs here",
                    kind="guidance", status="active"))
    db.add(Learning(repo_id=None, topic="global", hint_text="applies everywhere",
                   kind="guidance", status="active"))
    await db.flush()

    audited = await cluster_learnings(db, repo_id=repo.id, include_global=False)
    topics = {l.topic for c in audited for l in c}
    assert "scoped" in topics
    assert "global" not in topics, "a global learning was audited against one repo"

    # Review injection must still see it -- global learnings apply everywhere,
    # and narrowing that would silently weaken every review.
    for_review = await cluster_learnings(db, repo_id=repo.id, include_global=True)
    assert "global" in {l.topic for c in for_review for l in c}


def test_auditor_asks_for_repo_scoped_learnings_only():
    """Pin the call site: the parameter defaults to True for the review path,
    so the auditor must opt out explicitly."""
    import inspect

    from argus.knowledge import auditor

    src = inspect.getsource(auditor.run_audit_for_repo)
    assert "include_global=False" in src


# --- observability regressions (2026-08-09) --------------------------------
# Audit runs recorded 23 LLM rounds and ZERO tool calls. That is
# indistinguishable from an auditor judging learnings without ever opening the
# codebase -- the exact thing an audit is supposed to do. The cause was
# audit_cluster never forwarding `callbacks` to run_stage_agent: LangChain
# fires on_tool_start/on_tool_end on the GRAPH invocation, so a callback
# attached only to the model sees LLM rounds and no tools.

def test_audit_cluster_forwards_callbacks_to_the_agent():
    import inspect

    from argus.knowledge.auditor import audit_cluster

    assert "callbacks" in inspect.signature(audit_cluster).parameters
    src = inspect.getsource(audit_cluster)
    assert "callbacks=callbacks" in src, (
        "callbacks must reach run_stage_agent, or no ToolCall row is ever "
        "written for an audit")


def test_run_audit_passes_its_callbacks_into_each_cluster():
    import inspect

    from argus.knowledge.auditor import run_audit_for_repo

    src = inspect.getsource(run_audit_for_repo)
    assert "audit_cluster(" in src
    call = src[src.index("audit_cluster("):]
    assert "callbacks=callbacks" in call[:200]


def test_audit_run_is_langfuse_traced_like_a_review():
    """Reviews persist their trace id when the run starts, so a run that dies
    mid-way is still traceable. Audits now do the same, via the same
    LangfuseRun helper reviews and distillation use (langfuse_enabled/
    create_trace_id live there now, not duplicated per pipeline)."""
    import inspect

    from argus.knowledge.auditor import run_audit_for_repo

    src = inspect.getsource(run_audit_for_repo)
    assert 'LangfuseRun.start(\n            settings, "audit"' in src
    assert "row.langfuse_trace_id = trace_id" in src
    # the id must be stored before any cluster work begins
    assert src.index("langfuse_trace_id = trace_id") < src.index("for cluster in clusters")
    # and the span must be closed, or the trace never flushes
    assert "audit_span_stack.close()" in src


def test_audit_run_model_carries_a_trace_id_column():
    from argus.domain.models import AuditRun

    assert "langfuse_trace_id" in AuditRun.__table__.columns

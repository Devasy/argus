# Argus Code Review Fixes Tracker & Running Tally

> **Persistent CodeRabbit Review & Remediation Ledger (Part 1: Stages 1–5)**  
> *Tracks all automated review comments, suggested fixes, severity, and resolution status across PR stages.*  
> **Stage 6+ Part 2 Tracker**: See [REVIEW_FIXES_TRACKER_PART2.md](./REVIEW_FIXES_TRACKER_PART2.md) for Stages 6–9 organized by priority.  
> **Last Updated**: 2026-09-27 19:25 IST  
> **Rate Limit Reminder**: CodeRabbit AI allows **1 review per hour**.

---

## 1. Summary Dashboard

| Stage | PR # | Scope | Total Comments | Resolved | Pending | Status |
|---|---|---|---|---|---|---|
| **Stage 1** | [PR #2](https://github.com/Devasy/argus/pull/2) | Core Foundation, Config & Models | 10 | 10 | 0 | **RESOLVED & SQUASHED** |
| **Stage 2** | [PR #3](https://github.com/Devasy/argus/pull/3) | GitLab Client, Normalizer & Diff Engine | 8 | 0 | 8 | **SQUASHED TO MAIN (Fixes Deferred)** |
| **Stage 3** | [PR #4](https://github.com/Devasy/argus/pull/4) | Ingest Poller, Reconciler & Job Queue | 3 | 0 | 3 | **SQUASHED TO MAIN (Fixes Deferred)** |
| **Stage 4** | [PR #5](https://github.com/Devasy/argus/pull/5) | LiteLLM Gateway, Reasoning & Langfuse | 8 | 0 | 8 | **SQUASHED TO MAIN (Fixes Deferred)** |
| **Stage 5** | [PR #6](https://github.com/Devasy/argus/pull/6) | pgvector Learnings, BM25 & Distiller | 24 (15 CR + 9 Greptile) | 4 | 20 | **SQUASHED TO MAIN (20 Fixes Pending)** |
| **Stage 6** | [PR #7](https://github.com/Devasy/argus/pull/7) | LangGraph Review Pipeline, AST Tools & Publisher | **33** (24 CR + 9 Greptile) | 0 | 33 | **[TRACKED IN PART 2](./REVIEW_FIXES_TRACKER_PART2.md)** |

---

## 2. Stage 2 (PR #3) — Running Tally of Pending Fixes

| # | File & Line | Severity / Category | Finding & Impact | Status | Planned Fix |
|---|---|---|---|---|---|
| **S2-01** | [`argus/gitlab/client.py:16`](./argus/gitlab/client.py#L12-L19) | 🟡 Minor / Security (CWE-319) | Accepts `http://` in `base_url`, potentially sending `PRIVATE-TOKEN` over unencrypted HTTP. | **PENDING** | Validate `urllib.parse.urlparse(base_url).scheme == "https"`; raise `ValueError("GitLab base_url must use HTTPS")`. |
| **S2-02** | [`argus/gitlab/client.py:44`](./argus/gitlab/client.py#L32-L45) | 🟠 Major / Data Integrity | In `_get_paginated`, if `MAX_PAGES` is reached while `X-Next-Page` still exists, returns partial results silently. | **PENDING** | When `page > MAX_PAGES` and `X-Next-Page` is present, raise `RuntimeError(f"Pagination limit reached ({MAX_PAGES} pages) with more results remaining for {path}")`. |
| **S2-03** | [`argus/gitlab/client.py:77`](./argus/gitlab/client.py#L76-L78) | 🟠 Major / Data Integrity | `list_versions` makes a single unpaginated `_get` call. Large MRs with >20 versions miss older revisions. | **PENDING** | Change `return await self._get(...)` to `return await self._get_paginated(...)`. |
| **S2-04** | [`argus/gitlab/normalizer.py:47`](./argus/gitlab/normalizer.py#L36-L48) | 🟠 Major / Data Integrity | `upsert_actor` returns existing row without updating fields. Username/name/avatar changes in GitLab remain stale. | **PENDING** | If actor exists, update `actor.username`, `actor.display_name`, and `actor.avatar_url` from current payload before returning. |
| **S2-05** | [`argus/gitlab/normalizer.py:114`](./argus/gitlab/normalizer.py#L101-L115) | 🟠 Major / Data Integrity | If approver, reviewer, or assignee is removed in GitLab, stale `MRParticipant` rows persist in the database. | **PENDING** | Reconcile participants: query existing `MRParticipant` rows for the MR, compare with current payload set, and `session.delete()` any removed rows. |
| **S2-06** | [`argus/gitlab/normalizer.py:114`](./argus/gitlab/normalizer.py#L111-L115) | 🟠 Major / Data Integrity | `approved_at` reads root `(approvals or {}).get("updated_at")` instead of per-approver `ap.get("approved_at")`. | **PENDING** | Change to `approved_at=_ts(ap.get("approved_at"))`. |
| **S2-07** | [`argus/review/diffsvc.py:70`](./argus/review/diffsvc.py#L61-L74) | 🟠 Major / Correctness | `_git_diff` only passes new path. For renamed files, git diff shows entire file as added (`@@ -0,0 +1,N @@`). | **PENDING** | Accept `old_path` in `_git_diff`, add `-M` flag and pass both paths `[old_path, path]` so git detects rename hunks. Pass `fc.old_path` at call site. |
| **S2-08** | [`migrations/versions/ed742e4d572a...:37`](./migrations/versions/ed742e4d572a_make_mr_versions_base_commit_sha.py#L31-L39) | 🟠 Major / Migration Safety | Migration `downgrade()` alters `base_commit_sha` to `nullable=False`. Fails if any row has `NULL`. | **PENDING** | In `downgrade()`, execute `UPDATE mr_versions SET base_commit_sha = '' WHERE base_commit_sha IS NULL` before `op.alter_column`. |

---

## 3. Detailed Fix Specifications for Stage 2 (PR #3)

### S2-01: HTTPS Scheme Enforcement in GitLabClient
```python
# argus/gitlab/client.py
class GitLabClient:
    def __init__(self, base_url: str, token: str, ssl_verify: bool | str = True):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https":
            raise ValueError(f"GitLab base_url must use HTTPS, got: {parsed.scheme}://...")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v4",
            headers={"PRIVATE-TOKEN": token},
            verify=ssl_verify,
            timeout=30.0,
        )
```

### S2-02: Paginated Fetch Overflow Guard
```python
# argus/gitlab/client.py
    async def _get_paginated(self, path: str, **params) -> list[dict]:
        params = {k: v for k, v in params.items() if v is not None}
        params["per_page"] = PER_PAGE
        out: list[dict] = []
        page = 1
        while page <= MAX_PAGES:
            r = await self._http.get(path, params={**params, "page": page})
            r.raise_for_status()
            out.extend(r.json())
            if not r.headers.get("X-Next-Page"):
                return out
            page += 1
        raise RuntimeError(f"Pagination limit exceeded ({MAX_PAGES} pages) for {path}")
```

### S2-03: MR Versions Pagination
```python
# argus/gitlab/client.py
    async def list_versions(self, project, iid) -> list[dict]:
        return await self._get_paginated(f"/projects/{self._proj(project)}/merge_requests/{iid}/versions")
```

### S2-04: Actor Detail Freshness
```python
# argus/gitlab/normalizer.py
async def upsert_actor(session: AsyncSession, user: dict) -> Actor:
    actor = (await session.execute(select(Actor).where(
        Actor.provider == "gitlab",
        Actor.provider_user_id == user["id"]))).scalar_one_or_none()
    if actor is None:
        actor = Actor(provider="gitlab", provider_user_id=user["id"],
                      username=user.get("username", ""),
                      display_name=user.get("name"),
                      avatar_url=user.get("avatar_url"))
        session.add(actor)
        await session.flush()
    else:
        actor.username = user.get("username", "")
        actor.display_name = user.get("name")
        actor.avatar_url = user.get("avatar_url")
    return actor
```

### S2-05 & S2-06: Participant Reconciliation & Approver Timestamp
```python
# argus/gitlab/normalizer.py
    current_participants = set()
    if author:
        await _upsert_participant(session, mr.id, author.id, "author")
        current_participants.add((author.id, "author"))
    for u in mr_payload.get("assignees") or []:
        a = await upsert_actor(session, u)
        await _upsert_participant(session, mr.id, a.id, "assignee")
        current_participants.add((a.id, "assignee"))
    for r in reviewers or []:
        a = await upsert_actor(session, r["user"])
        await _upsert_participant(session, mr.id, a.id, "reviewer",
                                  review_state=r.get("state"))
        current_participants.add((a.id, "reviewer"))
    for ap in (approvals or {}).get("approved_by") or []:
        a = await upsert_actor(session, ap["user"])
        await _upsert_participant(session, mr.id, a.id, "approver",
                                  approved_at=_ts(ap.get("approved_at")))
        current_participants.add((a.id, "approver"))

    # Reconcile: delete removed participants
    existing = (await session.execute(
        select(MRParticipant).where(
            MRParticipant.mr_id == mr.id,
            MRParticipant.role.in_(["assignee", "reviewer", "approver"])
        )
    )).scalars().all()
    for ep in existing:
        if (ep.actor_id, ep.role) not in current_participants:
            await session.delete(ep)
```

### S2-07: Diff Rename Detection with `-M`
```python
# argus/review/diffsvc.py
def _git_diff(repo: Path, base_sha: str, head_sha: str, path: str, old_path: str | None = None) -> str:
    paths = [old_path, path] if old_path and old_path != path else [path]
    cmd = ["git", "diff", "--no-color", "-M", f"{base_sha}...{head_sha}", "--", *paths]
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:200] or "git diff failed")
    return proc.stdout

# Call site in backfill_collapsed_diffs:
text = _git_diff(repo, base_sha, head_sha, fc.path, getattr(fc, "old_path", None))
```

### S2-08: Safe Migration Downgrade
```python
# migrations/versions/ed742e4d572a_make_mr_versions_base_commit_sha.py
def downgrade() -> None:
    op.execute(
        sa.text("UPDATE mr_versions SET base_commit_sha = '' WHERE base_commit_sha IS NULL")
    )
    op.alter_column('mr_versions', 'base_commit_sha',
               existing_type=sa.TEXT(),
               nullable=False,
               schema='argus')
```

---

## 4. Stage 3 (PR #4) — Running Tally of Pending Fixes

| # | File & Line | Severity / Category | Finding & Impact | Status | Planned Fix |
|---|---|---|---|---|---|
| **S3-01** | [`argus/ingest/poller.py:149`](./argus/ingest/poller.py#L145-L155) | 🟠 Major / Stability & Availability | An error during processing one MR rolls back earlier successfully processed MRs in the batch and stalls cursor advancement. | **PENDING** | Wrap each MR's sync and enqueue in a SAVEPOINT or independent commit, and advance `repo.poll_cursor.updated_after` incrementally past succeeded MRs only. |
| **S3-02** | [`argus/ingest/reconciler.py:138`](./argus/ingest/reconciler.py#L130-L145) | 🟡 Minor / Performance & Scalability | `already_distilled` query selects all non-null `Learning.source_note_id` across the entire database, causing slow polling as DB grows. | **PENDING** | Join `Note` on `Note.id == Learning.source_note_id` and filter by `Note.mr_id == mr.id`. |
| **S3-03** | [`argus/jobs/queue.py:59`](./argus/jobs/queue.py#L50-L65) | 🟠 Major / Data Integrity | Concurrent enqueue can cause race conditions or orphan `Review` rows if dedup check fails after review is created. | **PENDING** | Use nested transaction / SAVEPOINT (`session.begin_nested()`) on insert and clean up orphan `Review` if enqueue returns `None`. |

---

## 5. Detailed Fix Specifications for Stage 3 (PR #4)

### S3-01: Per-MR Error Isolation & Cursor Tracking in Poller
```python
# argus/ingest/poller.py
    for mr_summary in mrs:
        try:
            async with session.begin_nested():
                await _sync_single_mr(...)
                repo.poll_cursor.updated_after = mr_summary["updated_at"]
                await session.flush()
        except Exception as e:
            logger.error("Failed to sync MR !%s: %s", mr_summary.get("iid"), e)
            continue
```

### S3-02: Scope Already Distilled Notes to Current MR
```python
# argus/ingest/reconciler.py
    already_distilled = set((await session.execute(
        select(Learning.source_note_id)
        .join(Note, Note.id == Learning.source_note_id)
        .where(Note.mr_id == mr.id)
    )).scalars().all())
```

### S3-03: Deduplication SAVEPOINT & Orphan Review Cleanup
```python
# argus/jobs/queue.py
async def enqueue(session: AsyncSession, kind: str, payload: dict, dedup_key: str | None) -> Job | None:
    job = Job(kind=kind, payload=payload, dedup_key=dedup_key)
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError:
        return None
    return job
```

---

## 6. Stage 4 (PR #5) — Running Tally of Pending Fixes

| # | File & Line | Severity / Category | Finding & Impact | Status | Planned Fix |
|---|---|---|---|---|---|
| **S4-01** | [`argus/llm/factory.py:9`](./argus/llm/factory.py#L8-L15) | 🟡 Minor / Correctness | `create_llm` appends to caller's `callbacks` list in-place, causing unintended side-effects across calls. | **PENDING** | Make a shallow copy: `callbacks = list(callbacks or [])`. |
| **S4-02** | [`argus/llm/health.py:64`](./argus/llm/health.py#L55-L68) | 🟡 Minor / Correctness | Health check treats 4xx client errors (e.g. 401 Unauthorized or 404 Model Not Found) as healthy. | **PENDING** | Check `200 <= status < 400` to mark healthy, otherwise report unhealthy with status code. |
| **S4-03** | [`argus/llm/langfuse_run.py:14`](./argus/llm/langfuse_run.py#L10-L20) | 🟠 Major / Compatibility | Langfuse callback wrapper relies on modern SDK attributes without checking version compatibility. | **PENDING** | Enforce minimum required Langfuse SDK version or catch `AttributeError` with a graceful fallback. |
| **S4-04** | [`argus/llm/trace.py:65`](./argus/llm/trace.py#L55-L70) | 🟠 Major / Data Integrity | Missing or non-standard token usage objects in LLM responses cause missing trace metrics. | **PENDING** | Standardize token extraction across LiteLLM providers: check `usage.prompt_tokens`, `usage.completion_tokens`, and fall back to estimated counts. |
| **S4-05** | [`argus/llm/trace.py:102`](./argus/llm/trace.py#L95-L110) | 🟠 Major / Security (Data Leak) | Raw prompt parameters and user code snippets sent directly to Langfuse spans without redaction. | **PENDING** | Apply sensitive data filtering / token scrubbing before logging generation inputs to Langfuse spans. |
| **S4-06** | [`argus/llm/trace.py:125`](./argus/llm/trace.py#L120-L130) | 🟡 Minor / Observability | Failed tool executions omit elapsed duration metric from the trace span. | **PENDING** | Compute and log `elapsed_ms` in `finally` block or exception handler for failed tool spans. |
| **S4-07** | [`argus/llm/trace.py:132`](./argus/llm/trace.py#L128-L135) | 🟠 Major / Observability | Tool name is cleared or omitted on error, making failed tool executions untraceable by tool type. | **PENDING** | Preserve `tool_name` in span metadata even when an exception is raised. |
| **S4-08** | [`tests/test_langfuse_trace_link.py:30`](./tests/test_langfuse_trace_link.py#L25-L35) | 🟡 Minor / Quality | Test assertions mock SDK calls but don't assert commit trace URL format. | **PENDING** | Add assertions verifying the URL format `https://cloud.langfuse.com/project/.../traces/...`. |

---

## 7. Historical Stage 1 (PR #2) — Resolved Ledger

| # | File & Line | Severity | Topic | Status |
|---|---|---|---|---|
| **S1-01** | `argus/logging_setup.py:59` | 🟠 Major | Avoid global `sys.excepthook` override during testing | **RESOLVED** |
| **S1-02** | `argus/config.py:33` | 🟠 Major | Default database credentials in production configuration | **RESOLVED** |
| **S1-03** | `argus/config.py:64` | 🟡 Minor | Unbounded token string validation | **RESOLVED** |
| **S1-04** | `migrations/env.py:32` | 🟠 Major | Search path isolation in alembic migrations | **RESOLVED** |
| **S1-05** | `migrations/versions/baseline.py:89` | 🟠 Major | Index on review status and timestamps | **RESOLVED** |
| **S1-06** | `docker-compose.yml:14` | 🟡 Minor | Healthcheck for postgres container | **RESOLVED** |
| **S1-07** | `README.md:45` | 🟡 Minor | Modern uv installation instructions | **RESOLVED** |
| **S1-08** | `pyproject.toml:22` | 🟡 Minor | Pin dependency versions | **RESOLVED** |
| **S1-09** | `tests/conftest.py:28` | 🟡 Minor | Isolated async engine session scope | **RESOLVED** |
| **S1-10** | `docker/Dockerfile:12` | 🟡 Minor | Multi-stage slim base image build | **RESOLVED** |

---

## 8. Stage 5 (PR #6) — CodeRabbit & Greptile Review Findings & Remediation Plan

### A. Summary of Findings (24 Total: 4 Patched in `f75ef5d`, 20 Deferred)

| # | File & Line | Reviewer & Severity | Finding & Impact | Status | Planned Fix |
|---|---|---|---|---|---|
| **G5-01** | [`agentic_distiller.py:36`](./argus/knowledge/agentic_distiller.py#L29-L37) | Greptile (🔴 P1) | Distiller imports `argus.review.*` which does not exist in Stage 5, causing `ModuleNotFoundError`. | **RESOLVED (`f75ef5d`)** | Guarded with `try...except ImportError` fallback returning empty result safely. |
| **G5-02** | [`audit_scheduler.py:26`](./argus/knowledge/audit_scheduler.py#L25-L35) | Greptile (🔴 P1) | Production code never invokes `run_audit_forever`; continuous audit is never started. | **PENDING** | Hook background audit task into application startup lifespan (scheduled for Stage 7 API server). |
| **G5-03** | [`learnings.py:152`](./argus/knowledge/learnings.py#L148-L158) | Greptile (🔴 P1) | BM25 path requires non-empty `lexical_query`. Only tests call `relevant_learnings`; no production caller yet. | **PENDING** | Production review pipeline in Stage 6 (`argus/review/stages.py`) consumes `relevant_learnings`. |
| **G5-04** | [`audit_scheduler.py:64`](./argus/knowledge/audit_scheduler.py#L60-L70) | Greptile (🔴 P1) | Workspace failure returns error dict, but scheduler ignored it and recorded `status = "done"`. | **RESOLVED (`f75ef5d`)** | Checked `result.get('status') == 'failed'` to record `status = 'failed'`. |
| **G5-05** | [`audit_apply.py:132`](./argus/knowledge/audit_apply.py#L125-L135) | Greptile (🔴 P1) | Merge verdict didn't verify `survivor.repo_id == learning.repo_id`, risking cross-repo merge corruption. | **RESOLVED (`f75ef5d`)** | Enforced `and survivor.repo_id == learning.repo_id`. |
| **G5-06** | [`auditor.py:413`](./argus/knowledge/auditor.py#L408-L418) | Greptile (🔴 P1) | `uuid.UUID(v.related_learning_id)` outside try/except block could raise unhandled `ValueError`. | **RESOLVED (`f75ef5d`)** | Added `_safe_uuid` helper catching `(ValueError, TypeError, AttributeError)`. |
| **G5-07** | [`graphify.py:123`](./argus/knowledge/graphify.py#L120-L130) | Greptile (🟡 P2) | Reuses cached `graph.json` without verifying commit SHA if extraction fails. | **PENDING** | Unlink/delete existing `graph.json` before extract (as CodeRabbit also suggested in CR5-09). |
| **G5-08** | [`auditor.py:420`](./argus/knowledge/auditor.py#L415-L425) | Greptile (🔴 P1) | When `_safe_uuid` returns `None` on invalid survivor, verdict remains `merge` but cannot be applied. | **PENDING** | Downgrade merge action to `escalate_to_human` when `related_learning_id` is missing or invalid. |
| **G5-09** | [`audit_scheduler.py:64`](./argus/knowledge/audit_scheduler.py#L60-L70) | Greptile (🟡 P2) | When auditor fails with `reason="workspace"`, checking only `error` logs generic failure. | **PENDING** | Read `result.get("error") or result.get("reason")` when recording audit run failure. |
| **CR5-01** | [`acceptance.py:13-32`](./argus/knowledge/acceptance.py#L13-L32) | CodeRabbit (🟠 Major) | Attribution query joins `Review` through `MergeRequest.mr_id` instead of directly through `Note.review_id`. | **PENDING** | Join through `Note.review_id` so each note is attributed strictly to the agent version used for its review. |
| **CR5-02** | [`agentic_distiller.py:164-226`](./argus/knowledge/agentic_distiller.py#L164-L226) | CodeRabbit (🟠 Major) | `wm.release` is not in an outer `try/finally` around tool/callback setup; setup error leaves workspace leaked. | **PENDING** | Wrap all tool, callback, and model setup within `try/finally` around `wm.acquire` to guarantee `wm.release`. |
| **CR5-03** | [`audit_apply.py:130-132`](./argus/knowledge/audit_apply.py#L130-L132) | CodeRabbit (🟠 Major) | Merge guard allows merging into a survivor whose status is archived or pending rewrite. | **PENDING** | Enforce `survivor.status == "active"` in addition to existing existence, distinct-row, and repo checks. |
| **CR5-04** | [`audit_apply.py:45-52`](./argus/knowledge/audit_apply.py#L45-L52) | CodeRabbit (🟡 Minor) | `citation_is_resolvable` allows generated graph artifacts (`graph.json`) to serve as evidence citations. | **PENDING** | Reject generated artifacts in `citation_is_resolvable`; ensure read tools do not expose them to the model. |
| **CR5-05** | [`audit_scheduler.py:63-64`](./argus/knowledge/audit_scheduler.py#L63-L64) | CodeRabbit (🟡 Minor) | Failed result handling in audit scheduler ignores `reason` field returned by `run_audit_for_repo`. | **PENDING** | Read `result.get("error") or result.get("reason")` with generic fallback. |
| **CR5-06** | [`audit_scheduler.py:43-56`](./argus/knowledge/audit_scheduler.py#L43-L56) | CodeRabbit (🟠 Major) | Race condition in audit scheduler: concurrent processes can insert duplicate `AuditRun` with status "running". | **PENDING** | Acquire per-repository advisory lock and check for existing running `AuditRun` before inserting. |
| **CR5-07** | [`auditor.py:442-448`](./argus/knowledge/auditor.py#L442-L448) | CodeRabbit (🟠 Major) | `trace_id` and `langfuse_run` accessed in `finally` block can raise `UnboundLocalError` if failure occurs early. | **PENDING** | Initialize `trace_id = None` and `langfuse_run = None` before `try` block. |
| **CR5-08** | [`auditor.py:420`](./argus/knowledge/auditor.py#L415-L425) | CodeRabbit (🟠 Major) | Non-null `related_learning_id` outside cluster learning IDs violates foreign key or causes orphaned merge. | **PENDING** | Validate `related_learning_id` against cluster IDs; downgrade orphan merges to `escalate_to_human`. |
| **CR5-09** | [`graphify.py:114-123`](./argus/knowledge/graphify.py#L114-L123) | CodeRabbit (🟡 Minor) | Failed `graphify extract` can return an existing `graph.json` from a previous workspace run. | **PENDING** | Remove existing `graph.json` before running `graphify extract`. |
| **CR5-10** | [`graphify.py:363-370`](./argus/knowledge/graphify.py#L363-L370) | CodeRabbit (🟡 Minor) | "NO EDGES" warning inferred from resolved seeds rather than graph's actual edge list. | **PENDING** | Update warning condition to check `len(graph.get("edges", [])) == 0`. |
| **CR5-11** | [`learnings.py:47-60`](./argus/knowledge/learnings.py#L47-L60) | CodeRabbit (🟠 Major) | In `learning_id` update branch, missing repo check and unconditionally overwrites provenance fields. | **PENDING** | Check `learning.repo_id == repo_id`; update `mr_id`, `source_note_id`, `actor_id` only when not None. |
| **CR5-12** | [`284f7a53dde7_learning_outcome_attribution.py:57`](./migrations/versions/284f7a53dde7_learning_outcome_attribution.py#L50-L57) | CodeRabbit (🟡 Minor) | Migration `downgrade()` recreates `injection_outcome_check` failing if `harmful`/`ignored` rows exist. | **PENDING** | Add `UPDATE injection_events SET outcome = 'miss' WHERE outcome IN ('harmful','ignored')` in `downgrade()`. |
| **CR5-13** | [`fe7f260027bf_learning_audit.py:131`](./migrations/versions/fe7f260027bf_learning_audit.py#L125-L135) | CodeRabbit (🟡 Minor) | Migration `downgrade()` check constraint rejects audit rows where `review_id` and `distillation_run_id` are NULL. | **PENDING** | Add `DELETE FROM {SCHEMA}.tool_calls WHERE audit_run_id IS NOT NULL` before creating check constraint in `downgrade()`. |
| **CR5-14** | [`test_agentic_distiller.py:9`](./tests/test_agentic_distiller.py#L5-L15) | CodeRabbit (🟡 Minor) | Test imports `argus.review.stages` unconditionally, failing test collection in Stage 5. | **PENDING** | Add `pytest.importorskip("argus.review.stages")` before importing `stages`. |
| **CR5-15** | [`test_audit_rewrite_actions.py:171`](./tests/test_audit_rewrite_actions.py#L156-L171) | CodeRabbit (🔵 Trivial) | Test copies `persisted_action` logic locally instead of exercising production helper in `auditor.py`. | **PENDING** | Extract `resolve_persisted_action` helper in `auditor.py` and import/test it directly. |

---

## 9. Stage 6+ Review Ledger Moved to Part 2

> [!NOTE]
> Due to the substantial volume of in-depth architectural and security findings on Stage 6 (33 total findings: 24 CodeRabbit + 9 Greptile), Stage 6 and subsequent stages (7–9) are maintained in **Part 2** with **Priority-Based Segregation**:
> 
> 👉 **[Open REVIEW_FIXES_TRACKER_PART2.md](./REVIEW_FIXES_TRACKER_PART2.md)**
> 
> - **🔴 Tier 1: Critical Security & Concurrency Blockers** (7 findings: Token leak, Symlink traversal, Path traversal, Worktree race, etc.)
> - **🟠 Tier 2: Major Architecture, Data Integrity & State Management** (13 findings: Checkpoint resume, Tool allowlist bypass, Cascade deletes, etc.)
> - **🟡 Tier 3: Minor Quality, Linters & Test Resilience** (13 findings: Ruff command hygiene, Context budget async, Truncation prefix, etc.)

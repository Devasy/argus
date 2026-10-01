# Argus Code Review Fixes Tracker — Part 2 (Stages 6–10)

> **Persistent CodeRabbit & Greptile Review & Remediation Ledger (Part 2)**  
> *Dedicated tracker for Stage 6 (Review Pipeline), Stage 7 (API Server), Stage 8 (Frontend UI), Stage 9 (Benchmarks), and Stage 10 (Enterprise Extensions).*  
> **Preceding Stages**: See [REVIEW_FIXES_TRACKER.md (Part 1)](./REVIEW_FIXES_TRACKER.md) for Stages 1–5.  
> **Last Updated**: 2026-09-27 23:40 IST  
> **Organization**: **Priority-Based Segregation** (Tier 1: Critical/Security, Tier 2: Major/Architecture, Tier 3: Minor/Quality).


---

## 1. Summary Dashboard (Part 2)

| Stage | PR # | Scope | Reviewers | Total Comments | P1 Critical | P2 Major | P3 Minor | Status |
|---|---|---|---|---|---|---|---|---|
| **Stage 6** | [PR #7](https://github.com/Devasy/argus/pull/7) | LangGraph Review Pipeline, AST Tools & Publisher | CodeRabbit + Greptile | **33** (24 CR + 9 Greptile) | 7 | 13 | 13 | **SQUASHED TO MAIN (33 Fixes Tracked)** |
| **Stage 7** | [PR #8](https://github.com/Devasy/argus/pull/8) | FastAPI Server, Auth & WebSockets | CodeRabbit + Greptile | **29** (19 CR + 10 Greptile) | 9 | 9 | 11 | **SQUASHED TO MAIN (29 Fixes Tracked)** |
| **Stage 8** | [PR #9](https://github.com/Devasy/argus/pull/9) | React SPA, Slate Theme & Settings | CodeRabbit + Greptile | **37** (28 CR + 9 Greptile) | 6 | 13 | 18 | **SQUASHED TO MAIN (37 Fixes Tracked)** |
| **Stage 9** | [PR #10](https://github.com/Devasy/argus/pull/10) | Benchmarks & Evaluation Harness | CodeRabbit + Greptile | **16** (8 CR + 8 Greptile) | 7 | 2 | 7 | **SQUASHED TO MAIN (16 Fixes Tracked)** |
| **Stage 10** | [PR #11](https://github.com/Devasy/argus/pull/11) | Enterprise Extensions: Sister Repos & Workers | CodeRabbit + Greptile | **25** (9 CR + 16 Greptile) | 10 | 11 | 4 | **SQUASHED TO MAIN (25 Fixes Tracked)** |


---

## 2. Stage 6 (PR #7) — Priority-Based Review Breakdown

### 🔴 Tier 1: Critical Security & Concurrency Blockers (7 Findings)

High-risk vulnerabilities involving credential disclosure, host file path traversal, race conditions, and worktree corruption:

| ID | File & Line | Reviewer | Core Finding & Attack Vector | Planned Remediation |
|---|---|---|---|---|
| **P1-01** | [`workspace.py:17`](./argus/review/workspace.py#L12-L22) | CodeRabbit & Greptile | **GitLab Token Leak**: If git clone/fetch fails, `RuntimeError` includes raw command with `https://oauth2:TOKEN@...`, leaking tokens into review error logs and database rows. | Redact credentials from `args` and stderr/stdout with `re.sub(r'://[^@]+@', '://***@', text)` before constructing `RuntimeError`. |
| **P1-02** | [`navigation.py:146-153`](./argus/review/navigation.py#L146-L158) | CodeRabbit & Greptile | **Symlink Traversal**: Fallback python search accepts directory/file symlinks pointing outside the worktree, reading host filesystem contents into LLM context. | Use `os.walk(followlinks=False)`, prune `SKIP_DIRS`, and check `p.resolve().is_relative_to(self.root.resolve())` before yielding candidate files. |
| **P1-03** | [`grounding.py:44-51`](./argus/review/grounding.py#L44-L55) | CodeRabbit & Greptile | **Path Traversal in Grounding**: Finding `file_path` with `..` or absolute root reads files outside worktree during evidence quote verification. | Resolve workspace root and candidate path; reject paths where `(workspace / file_path).resolve()` is not relative to `workspace.resolve()`. |
| **P1-04** | [`workspace.py:57-69`](./argus/review/workspace.py#L55-L70) | CodeRabbit & Greptile | **Concurrent Worktree Deletion**: Worktree directories named only `wt-{sha[:12]}`. Concurrent reviews for same commit share directory; first to finish deletes it while second is still executing. | Allocate unique worktree per review: `wt-{sha[:12]}-{review_id}`. Serialize `git fetch` operations using a per-bare-repo asyncio lock. |
| **P1-05** | [`runner.py:180-183`](./argus/review/runner.py#L180-L188) | CodeRabbit | **Mid-Review Push Race**: Runner parses diffs from GitLab API while worktree checks out `mr_payload["sha"]`. If developer pushes new commit mid-run, diff and worktree code diverge. | Fetch diffs for the exact version identified by `mr_payload["sha"]`; verify head SHA hasn't changed before launching pipeline. |
| **P1-06** | [`runner.py:347`](./argus/review/runner.py#L340-L352) | Greptile | **Cancellation Overwrite**: If user cancels review while pipeline runs, runner blindly overwrites `review.status = "done"` or `"failed"` over `"canceled"`. | Update status conditionally: `WHERE id = :id AND status != 'canceled'`. |
| **P1-07** | [`pipeline.py:495-505`](./argus/review/pipeline.py#L490-L505) | CodeRabbit & Greptile | **Specialist Tool Restriction Bypass**: Specialist's `tool_allowlist` is loaded from DB, but chunk-scoped specialists receive all profile tools; restricted tools remain callable. | Filter available tools by intersecting with `agent.tool_allowlist` in both `_run_analyze_chunk` and `run_reviewer_agent`. |

---

### 🟠 Tier 2: Major Architecture, Data Integrity & State Management (13 Findings)

Bugs affecting LangGraph DAG execution, checkpoint resuming, database constraints, and finding deduplication:

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P2-01** | [`pipeline.py:439-456`](./argus/review/pipeline.py#L439-L460) | CodeRabbit & Greptile | **Empty Plan Routing Failure**: MRs with no diff hunks produce 0 chunks; `fan_out` returns 0 Send commands, prematurely terminating graph before `publish`. | If `len(chunks) == 0`, route directly to `gather`/`publish` with an empty finding set so the review concludes and persists state. |
| **P2-02** | [`pipeline.py:843-851`](./argus/review/pipeline.py#L843-L851) | CodeRabbit | **Stale Checkpoint Resume**: Resuming from LangGraph checkpoint ignores commit SHA; if MR was updated, review resumes with stale AST state. | Validate checkpoint head SHA against current `mr_payload["sha"]`; reapply fresh state if SHA differs. |
| **P2-03** | [`publisher.py:88`](./argus/review/publisher.py#L85-L102) | Greptile | **Verifier Line Overwrite**: Grounding line correction applied after verifier correction overwrites verifier's chosen line with an earlier quote occurrence. | Prioritize verifier line corrections as authoritative over grounding quote line searches. |
| **P2-04** | [`pipeline.py:254-256`](./argus/review/pipeline.py#L254-L260) | CodeRabbit | **Missing Agent Attribution**: Chunk-scoped specialist path forgets to link `ReviewReviewerAgentVersion`, causing its findings to be omitted from acceptance calculations. | Insert `(review_id, agent.version_id)` link idempotently during specialist result handling loop. |
| **P2-05** | [`publisher.py:336-360`](./argus/review/publisher.py#L336-L360) | CodeRabbit | **Duplicate Finding Persistence**: Resuming `publish_review` re-adds overflow and outside-diff `Finding` rows without checking for existing rows. | Query existing `Finding` rows for the review and skip findings that already exist before calling `s.add()`. |
| **P2-06** | [`navigation.py:214-222`](./argus/review/navigation.py#L214-L225) | CodeRabbit | **Unbounded Search Timeout**: `search` thread await lacks timeout; pathological regexes or massive repos hang review thread indefinitely. | Wrap `asyncio.wait_for(timeout=SEARCH_TIMEOUT_S)` and check monotonic deadline between files in `_search_python`. |
| **P2-07** | [`851c26a89b75_review_agent_versions_join.py:25`](./migrations/versions/851c26a89b75_review_agent_versions_join.py#L20-L30) | CodeRabbit | **Missing Cascade Delete**: Deleting a `Review` row fails with foreign key violation on `review_reviewer_agent_versions`. | Add `ondelete="CASCADE"` to `review_id` foreign key constraint in migration and model. |
| **P2-08** | [`publisher.py:398-408`](./argus/review/publisher.py#L398-L410) | CodeRabbit | **Lost Suggestion Hunks**: Overflow and outside-diff findings omit `suggestion_old` and `suggestion_new` during `Finding` creation. | Copy `suggestion_old` and `suggestion_new` from findings into persisted database rows. |
| **P2-09** | [`publisher.py:390`](./argus/review/publisher.py#L385-L395) | Greptile | **Duplicate Summary Discussions**: Resumed or replayed reviews post duplicate root summary comments on GitLab MR. | Check existing MR discussions for existing Argus summary bot note before posting a new discussion thread. |
| **P2-10** | [`stages.py:948-951`](./argus/review/stages.py#L945-L955) | CodeRabbit | **Message Doubling on Retry**: Forced convergence retry prepends `user_msg` onto `result["messages"]`, causing token blowout and confusing the LLM. | Pass `result["messages"]` directly followed only by `FORCE_CONVERGENCE_DIRECTIVE` without prepending `user_msg`. |
| **P2-11** | [`pipeline.py:231-235`](./argus/review/pipeline.py#L230-L240) | CodeRabbit | **Dynamic Tool Docstring Bug**: Specialist tool relies on runtime docstring interpolation which is stripped by Python optimization flags (`-OO`). | Explicitly set `description` argument on `@tool(name=..., description=...)` decorator using `agent.description`. |
| **P2-12** | [`listing.py:35-38`](./argus/review/listing.py#L35-L40) | CodeRabbit | **Queue Position Type Crash**: If job payload is malformed or non-dict, `job.payload.get("review_id")` raises `AttributeError` or `ValueError`. | Validate `isinstance(job.payload, dict)` and wrap UUID conversion in a defensive try/except block. |
| **P2-13** | [`listing.py:42-67`](./argus/review/listing.py#L42-L67) | CodeRabbit | **Negative Offset Query Error**: If client passes `page=0` or `per_page=0`, SQL query crashes with negative `OFFSET` / invalid `LIMIT`. | Clamp `page = max(1, page)` and `per_page = max(1, min(100, per_page))`. |

---

### 🟡 Tier 3: Minor Quality, Linters & Test Resilience (13 Findings)

Code quality improvements, argument parsing hygiene, and test suite isolation:

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P3-01** | [`linters.py:18`](./argus/review/linters.py#L15-L25) | CodeRabbit | **Ruff Option Injection**: Files starting with a dash (e.g. `-test.py`) are parsed by Ruff CLI as command options rather than file paths. | Insert argument terminator `--` before file paths: `["ruff", "check", "--", *paths]`. |
| **P3-02** | [`tools.py:51-59`](./argus/review/tools.py#L50-L65) | CodeRabbit | **Oversized First Block Truncation**: When first diff hunk exceeds limit, `_truncate_result` returns only truncation notice with 0 bytes of code. | Contribute a prefix of the first block within byte limit before appending truncation warning. |
| **P3-03** | [`stages.py:642-659`](./argus/review/stages.py#L640-L660) | CodeRabbit | **Premature Forced Convergence**: When `max_rounds=3`, `_RoundBudget.force_at` triggers on round 1, giving LLM no opportunity for normal tool use. | Adjust `force_at = max(1, max_rounds - 1)` so the initial round is always unforced. |
| **P3-04** | [`test_agent_complaints.py:100-101`](./tests/test_agent_complaints.py#L95-L105) | CodeRabbit | **Cross-Test Pollution**: Test checks `AgentComplaint` count without scoping to `seeded_review_id`, failing if other tests leave rows. | Add `where(AgentComplaint.review_id == seeded_review_id)` to assertions. |
| **P3-05** | [`test_context_budget.py:150-151`](./tests/test_context_budget.py#L145-L155) | CodeRabbit | **Event Loop Nesting Warning**: Test uses `asyncio.get_event_loop().run_until_complete()` inside pytest-asyncio environment. | Convert test function to `async def` and `await get_file_lines.ainvoke()` directly. |
| **P3-06** | [`test_repo_agent_scoping.py:184-193`](./tests/test_repo_agent_scoping.py#L180-L195) | CodeRabbit | **Brittle Source Inspection**: Test uses `inspect.getsource()` to verify agent exclusion rather than executing the resolution query. | Refactor to behavioral test executing the actual agent resolution database query. |
| **P3-07** | [`test_verify_context_isolation.py:45`](./tests/test_verify_context_isolation.py#L40-L55) | CodeRabbit | **Flaky Assertion**: Checks context isolation via exact token length rather than content substring absence. | Assert that unauthorized sister repository symbols are absent from tool context. |
| **P3-08** | [`test_runner.py:88`](./tests/test_runner.py#L80-L95) | CodeRabbit | **Missing Mock Cleanup**: Mock GitLab client does not reset `list_discussions` between subtests. | Reset mock call history in fixture teardown. |
| **P3-09** | [`test_stages.py:65`](./tests/test_stages.py#L60-L70) | CodeRabbit | **Convergence Assertion Update**: Test expects legacy message structure; needs update for clean retry messages. | Align test assertions with deduplicated message format. |
| **P3-10** | [`candidates.py:42`](./argus/review/candidates.py#L40-L50) | CodeRabbit | **Candidate Filter Optimization**: Multiple linear list comprehensions over file candidates. | Consolidate candidate filtering into a single generator pass. |
| **P3-11** | [`prevalence.py:55`](./argus/review/prevalence.py#L50-L60) | CodeRabbit | **Score Normalization Guard**: Division by zero if all files have 0 prevalence hits. | Add `if total_occurrences == 0: return 0.0` guard. |
| **P3-12** | [`router.py:32`](./argus/review/router.py#L30-L40) | CodeRabbit | **Fallback Route Clarity**: Unhandled review mode falls back to default with silent warning. | Add explicit debug log indicating fallback route selection. |
| **P3-13** | [`tool_registry.py:45`](./argus/review/tool_registry.py#L40-L55) | CodeRabbit | **Registry Redundant Lookup**: Double dict lookup in `get_tool` method. | Use `self._tools.get(name)` with single lookup. |

---

## 3. Stage 7 (PR #8) — Priority-Based Review Breakdown

### 🔴 Tier 1: Critical Security & Concurrency Blockers (9 Findings)

High-risk vulnerabilities involving authentication bypass, unhandled concurrency races, data corruption during backfills, and audit denial-of-service:

| ID | File & Line | Reviewer | Core Finding & Attack Vector | Planned Remediation |
|---|---|---|---|---|
| **P1-01** | [`auth.py:11`](./argus/api/auth.py#L10-L20) | CodeRabbit & Greptile | **Anonymous Admin Access**: If `ARGUS_API_TOKEN` is unset or empty string, `secrets.compare_digest("", "")` succeeds, granting unauthenticated admin access to all API routes. | Require non-empty token: `if not EXPECTED_TOKEN or not h: raise HTTPException(status_code=401)`. Fail closed in production. |
| **P1-02** | [`app.py:793`](./argus/api/app.py#L790-L800) | Greptile | **Settings Desync with Worker Loop**: Mutating runtime settings via `/api/settings` updates PostgreSQL but active background poller loop caches previous state without reloading. | Trigger an event or set an inter-process reload flag / notify channel so worker loops re-read `effective_settings()` immediately. |
| **P1-03** | [`app.py:929`](./argus/api/app.py#L925-L935) | Greptile | **Cancellation Overwrite Race**: When a user cancels a running review, worker completion blindly sets `review.status = "done"` or `"failed"`, resurrecting or overwriting `"canceled"`. | Update status conditionally using SQL: `UPDATE reviews SET status = :status WHERE id = :id AND status != 'canceled'`. |
| **P1-04** | [`app.py:1163`](./argus/api/app.py#L1160-L1170) | Greptile | **Unbounded / Negative Audit Limit**: Manual audit endpoint accepts unvalidated `limit` values (e.g., negative or unbounded integers), crashing SQL queries or triggering memory exhaustion. | Validate query param: `limit: int = Query(default=10, ge=1, le=100)`. |
| **P1-05** | [`app.py:1183`](./argus/api/app.py#L1180-L1195) | CodeRabbit & Greptile | **Audit Lock Bypass & Thread Starvation**: Non-atomic check-and-set for manual audit allows concurrent requests to launch duplicate long-running audit jobs, exhausting DB connections. | Use an atomic Redis lock or DB transaction `SELECT ... FOR UPDATE` with `NOWAIT`, and offload audit execution to background queue. |
| **P1-06** | [`backfill_outcomes.py:35`](./argus/backfill_outcomes.py#L30-L40) | CodeRabbit & Greptile | **Backfill Erases Genuine Outcomes**: Outcome backfill unconditionally resets learning counters and verdicts to zero across all items, wiping out legitimate historical feedback data. | Restrict backfill update to unclassified or pre-attribution records (`WHERE outcome IS NULL`), preserving finalized outcomes. |
| **P1-07** | [`app.py:145`](./argus/api/app.py#L140-L150) | Greptile | **Bot Noise Distillation Poisoning**: Changes authored by automated bots (e.g. Renovate, Dependabot) trigger distillation jobs, creating noisy or empty learning entries. | Filter out known bot authors (`[bot]`, `dependabot`, `renovate`) before enqueuing distillation tasks. |
| **P1-08** | [`app.py:1286`](./argus/api/app.py#L1280-L1295) | Greptile | **Concurrent Approval Repeat Merge**: Rapid concurrent calls to approve endpoint can issue multiple simultaneous merge requests to GitLab API, corrupting MR state. | Wrap approval action in an atomic lock or check GitLab MR state before triggering merge. |
| **P1-09** | [`test_api_settings.py:65`](./tests/test_api_settings.py#L60-L70) | Greptile | **Settings Test Reads Missing File**: Test expects a hardcoded file path from previous environment, failing when run in isolated container or CI. | Use `tmp_path` fixture or mock file system reads in settings persistence test. |

---

### 🟠 Tier 2: Major Architecture, Data Integrity & State Management (9 Findings)

Issues impacting database integrity, backfill correctness, and API endpoint robustness:

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P2-01** | [`app.py:335`](./argus/api/app.py#L330-L340) | Greptile & CodeRabbit | **Negative Page Size in Reviews Query**: Negative `per_page` or `page` values cause SQL `OFFSET`/`LIMIT` errors or 500 responses. | Enforce `Query(ge=1, le=100)` on pagination parameters. |
| **P2-02** | [`app.py:1190`](./argus/api/app.py#L1185-L1200) | CodeRabbit | **Sync Audit Execution in Async Route**: Running synchronous audit function inside FastAPI route handler blocks the event loop for minutes. | Offload audit execution to RQ worker or execute in threadpool via `run_in_threadpool`. |
| **P2-03** | [`auth.py:11`](./argus/api/auth.py#L10-L15) | CodeRabbit | **Hardcoded Dev Token Vulnerability**: Fallback dev token check allows default secret in non-dev environments if `ENV` isn't strictly checked. | Disallow dev token bypass when `ENVIRONMENT == "production"`. |
| **P2-04** | [`stats.py:93`](./argus/api/stats.py#L90-L100) | CodeRabbit | **Contaminated Agent Stats**: Per-agent review counts aggregate benchmark MRs and legacy review notes, skewing production metrics. | Add `WHERE is_benchmark = false` and filter by active review run IDs. |
| **P2-05** | [`backfill_outcomes.py:30`](./argus/backfill_outcomes.py#L25-L35) | CodeRabbit | **Indiscriminate Outcome Reset**: Resets already-verified outcome labels when backfill script is rerun incrementally. | Filter query to `Learning.verdict.is_(None)` so existing human-verified verdicts are protected. |
| **P2-06** | [`backfill_tokens.py:40`](./argus/backfill_tokens.py#L35-L45) | CodeRabbit | **Token Total Overwrite**: Backfill script recalculates token usage from traces and overwrites manually adjusted or finalized token totals. | Use conditional update `SET token_count = COALESCE(token_count, :calculated)` or only update where missing. |
| **P2-07** | [`settings_store.py:20`](./argus/settings_store.py#L18-L25) | CodeRabbit | **Ineffective GitLab Overrides**: Settings store loads GitLab config from environment without checking runtime overrides table. | Merge runtime settings overrides over env defaults when constructing GitLab client config. |
| **P2-08** | [`settings_store.py:22`](./argus/settings_store.py#L20-L25) | CodeRabbit | **Missing `worker_enabled` Setting**: `worker_enabled` flag cannot be toggled via runtime settings API despite being exposed in UI. | Add `worker_enabled` column and field to `RuntimeSettings` model and schema. |
| **P2-09** | [`auth.py:14`](./argus/api/auth.py#L12-L18) | CodeRabbit | **Bearer Scheme Enforcement**: Authorization header accepting raw tokens without `Bearer ` prefix causes confusion with standard OAuth proxies. | Require `Bearer ` prefix strictly and strip leading whitespace before comparison. |

---

### 🟡 Tier 3: Minor Quality, Hygiene & Test Coverage (11 Findings)

Code cleanup, query efficiency, and edge case handling:

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P3-01** | [`app.py:265`](./argus/api/app.py#L260-L270) | CodeRabbit | **Timing Attack Resilience**: String comparison in WebSocket handshake could be susceptible to timing analysis. | Use `hmac.compare_digest` for token validation in WebSocket upgrade. |
| **P3-02** | [`app.py:370`](./argus/api/app.py#L365-L375) | CodeRabbit | **Null Value Handling in Update**: Patching model with explicit `null` for non-nullable fields causes DB constraint violation. | Filter out `None` values or validate against schema field nullability before `setattr`. |
| **P3-03** | [`app.py:514`](./argus/api/app.py#L510-L520) | CodeRabbit | **Sorting Crash on Unfinished Reviews**: Sorting reviews by `finished_at` crashes if review is in progress (`None`). | Use `nulls_last()` or default to epoch in sort key. |
| **P3-04** | [`app.py:687`](./argus/api/app.py#L680-L690) | CodeRabbit | **Agent Name Conflict 500**: Creating agent with duplicate name causes unhandled `IntegrityError` (500). | Catch `IntegrityError` and return `409 Conflict` with clear error message. |
| **P3-05** | [`stats.py:101`](./argus/api/stats.py#L98-L108) | CodeRabbit | **N+1 Query in Agent Stats**: Loops through agents executing separate SQL queries for metrics. | Group by `agent_id` in a single aggregation query. |
| **P3-06** | [`backfill_outcomes.py:44`](./argus/backfill_outcomes.py#L40-L50) | CodeRabbit | **Memory Inefficiency in Event Count**: Loads all historical event rows to compute `len()`. | Use `SELECT count(*)` query to fetch scalar count directly. |
| **P3-07** | [`settings_store.py:23`](./argus/settings_store.py#L22-L28) | CodeRabbit | **Dynamic Log Level Application**: `log_level` setting updated in DB is not applied to active Python logger at startup. | Call `logging.getLogger().setLevel(effective.log_level)` after loading settings. |
| **P3-08** | [`test_auth.py:75`](./tests/test_auth.py#L70-L80) | CodeRabbit | **Order-Dependent Test Assertions**: Tests assert specific list index of returned proxies, causing flaky tests if DB order changes. | Assert by key/set membership rather than positional index. |
| **P3-09** | [`test_stats.py:63`](./tests/test_stats.py#L60-L70) | CodeRabbit | **Multi-Day Test Data Gap**: Stats window tests only seed data for single 24-hour period. | Seed multi-day timeline fixtures to verify windowing logic across day boundaries. |
| **P3-10** | [`app.py:329`](./argus/api/app.py#L325-L330) | CodeRabbit | **Zero Page Pagination**: Requesting `page=0` generates offset `-per_page` in SQL. | Enforce minimum `page=1` in route dependency. |
| **P3-11** | [`auth.py:14`](./argus/api/auth.py#L12-L16) | CodeRabbit | **Non-ASCII Token Handling**: UTF-8 bytes in authorization header can crash ASCII decoders. | Strip or validate ASCII encoding before `compare_digest`. |

---

## 4. Stage 8 (PR #9) — Priority-Based Review Breakdown

### 🔴 Tier 1: Critical Security & Concurrency Blockers (6 Findings)

High-risk vulnerabilities involving authentication token exposure, proxy timeouts, and editor state desynchronization:

| ID | File & Line | Reviewer | Core Finding & Attack Vector | Planned Remediation |
|---|---|---|---|---|
| **P1-01** | [`ws.ts:32`](./frontend/src/api/ws.ts#L30-L38) | Greptile & CodeRabbit | **Auth Token URL Leak**: Bearer token passed via WebSocket URL query string (`?token=...`), causing secrets to be logged in plain text in Nginx access logs, browser history, and proxy headers. | Use WebSocket subprotocol negotiation (`Sec-WebSocket-Protocol: token, <JWT>`) or exchange token for a short-lived, single-use ticket endpoint (`POST /ws-ticket`). |
| **P1-02** | [`nginx.conf:19`](./frontend/nginx.conf#L15-L25) | Greptile | **Proxy Timeout on Long Audits & Reviews**: Nginx defaults `proxy_read_timeout 60s`, terminating active WebSocket telemetry streams and long-running inline audit calls mid-execution. | Configure `proxy_read_timeout 3600s;` and `proxy_send_timeout 3600s;` for `/api/` and WebSocket `/ws/` locations. |
| **P1-03** | [`ReviewDetailPage.tsx:46`](./frontend/src/features/run/ReviewDetailPage.tsx#L40-L55) | Greptile & CodeRabbit | **Stale Review View Bleed**: Navigating between different review IDs retains previous review DAG state, timeline nodes, and trace findings until new payload finishes fetching. | Reset component state or key TanStack Query cache strictly by `reviewId` with `placeholderData: undefined`. |
| **P1-04** | [`ReviewDetailPage.tsx:86`](./frontend/src/features/run/ReviewDetailPage.tsx#L80-L95) | Greptile | **Terminal State Polling Stall**: When a review reaches terminal state (`done`/`failed`), polling stops abruptly; in-flight WebSocket messages dropped cause UI to never reflect final status. | Execute a final deterministic `refetch()` query upon receiving terminal WebSocket event. |
| **P1-05** | [`AgentEditor.tsx:67`](./frontend/src/features/agents/AgentEditor.tsx#L60-L75) | Greptile & CodeRabbit | **Tool Restriction Clear Failure**: Clearing all tools in the editor sends an empty array `[]` which backend treats as `None` (unrestricted), unintentionally granting access to all tools. | Distinguish between unrestricted (`null`) and restricted to zero tools (`[]`) in API serialization. |
| **P1-06** | [`AgentEditor.tsx:47`](./frontend/src/features/agents/AgentEditor.tsx#L40-L55) | Greptile | **Version Restore State Desync**: Restoring an earlier agent version updates backend, but active editor form state is not re-seeded from the restored version record. | Re-initialize dirty form state and query cache upon successful version restore mutation. |

---

### 🟠 Tier 2: Major Architecture, Data Integrity & UI Robustness (13 Findings)

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P2-01** | [`LearningsTab.tsx:91`](./frontend/src/features/knowledge/LearningsTab.tsx#L85-L98) | Greptile | **Hardcoded Search Limit**: Learnings search query hardcodes `limit=20` with no pagination controls, truncating matches in large repositories. | Implement server-side pagination or infinite scroll for search results. |
| **P2-02** | [`AuditSection.tsx:108`](./frontend/src/features/settings/sections/AuditSection.tsx#L100-L115) | Greptile | **No-op Auto-Apply Toggle**: The "Auto-apply suggested learnings" toggle modifies local UI state but does not persist to `runtime_settings` store. | Wire toggle to `PUT /settings` mutation payload for `audit_auto_apply`. |
| **P2-03** | [`useDirtyForm.ts:19`](./frontend/src/features/settings/useDirtyForm.ts#L15-L25) | Greptile | **Form Edits Overwritten by Background Refetch**: Background polling query refetch silently clobbers uncommitted user input in form fields. | Guard form reset in `useEffect`: only re-seed initial values if form is clean (`!isDirty`). |
| **P2-04** | [`ws.ts:32`](./frontend/src/api/ws.ts#L30-L40) | CodeRabbit | **Unhandled WebSocket Errors**: Connection rejections (401/403) fail silently without notifying callers or triggering backoff retry. | Expose error callback and exponential backoff retry mechanism on WebSocket client. |
| **P2-05** | [`ComplaintsPage.tsx:118`](./frontend/src/features/complaints/ComplaintsPage.tsx#L115-L125) | CodeRabbit | **Keyboard Accessibility on Rollup Targets**: Interactive elements lack keyboard tab indexing and `onKeyDown` handlers. | Convert to native button or add `tabIndex={0}` and enter/space key listeners. |
| **P2-06** | [`format.ts:56`](./frontend/src/features/dashboard/format.ts#L50-L65) | CodeRabbit | **Activity Destination Collisions**: Dashboard activity formatter aggregates disparate actions with differing target URLs into identical keys. | Key activities by `(action, destination_url)` tuple to preserve unique navigation targets. |
| **P2-07** | [`AuditRunsTab.tsx:19`](./frontend/src/features/knowledge/AuditRunsTab.tsx#L15-L30) | CodeRabbit | **Active Trace Refresh Gap**: Expanding an audit run trace does not subscribe to active updates while the audit run is executing. | Enable `refetchInterval` conditionally while `run.status === 'running'`. |
| **P2-08** | [`FileUnderstandingTab.tsx:8`](./frontend/src/features/knowledge/FileUnderstandingTab.tsx#L5-L15) | CodeRabbit | **Repository Dropdown Pagination Truncation**: Repositories beyond the first 20 are omitted from the selector due to unpaginated query. | Fetch all repositories with `per_page=100` or introduce searchable combobox. |
| **P2-09** | [`FileUnderstandingTab.tsx:28`](./frontend/src/features/knowledge/FileUnderstandingTab.tsx#L25-L35) | CodeRabbit | **Silent Repository Load Failure**: Network or auth error when loading repository list leaves UI in perpetual spinner without error banner. | Add `isError` check and render `<ErrorBox>` primitive. |
| **P2-10** | [`LearningsTab.tsx:82`](./frontend/src/features/knowledge/LearningsTab.tsx#L78-L90) | CodeRabbit | **Filter Order Inversion**: Client-side filters applied after slice pagination, leading to incomplete or empty pages despite matching records. | Filter records prior to applying pagination offset and limit. |
| **P2-11** | [`LearningsTab.tsx:95`](./frontend/src/features/knowledge/LearningsTab.tsx#L90-L105) | CodeRabbit | **Hidden Filter State During Search**: Entering a search query hides active filter chips, confusing users about why expected records do not appear. | Keep active filter badges visible and provide a one-click "Clear filters" action. |
| **P2-12** | [`ReviewDetailPage.tsx:46`](./frontend/src/features/run/ReviewDetailPage.tsx#L45-L60) | CodeRabbit | **Selected Node Orphan on MR Navigation**: Changing selected review does not clear `selectedNodeId`, causing mismatched stage details. | Reset `selectedNodeId` to `null` whenever `reviewId` route parameter changes. |
| **P2-13** | [`pipelineGraph.ts:119`](./frontend/src/lib/pipelineGraph.ts#L115-L125) | CodeRabbit | **Elapsed Time Rounding Overflow**: Seconds rounded to 60 without rolling into minutes (e.g. displaying `1m 60s`). | Use `Math.floor(seconds / 60)` and modulo for remaining seconds. |

---

### 🟡 Tier 3: Minor Quality, Linters & Accessibility (18 Findings)

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P3-01** | [`frontend-backlog.md:36`](./docs/frontend-backlog.md#L35-L40) | CodeRabbit | **Stale Backlog References**: Review cancel endpoint marked as unbuilt despite being implemented in Stage 7 API. | Update backlog document to reflect completed backend endpoint. |
| **P3-02** | [`eslint.config.js:1`](./frontend/eslint.config.js#L1-L5) | CodeRabbit | **Missing ESLint Dependency**: `@eslint/js` imported in config but missing from `package.json` devDependencies. | Add `"@eslint/js": "^9.0.0"` to `devDependencies`. |
| **P3-03** | [`client.ts:81`](./frontend/src/api/client.ts#L75-L85) | CodeRabbit | **204 Empty Body JSON Crash**: Parsing JSON on HTTP 204 No Content responses throws `SyntaxError`. | Return `null` if `response.status === 204` or `content-length === '0'`. |
| **P3-04** | [`Modal.tsx:21`](./frontend/src/components/Modal.tsx#L15-L25) | CodeRabbit | **Modal Accessibility**: Dialog lacks `role="dialog"`, `aria-modal="true"`, and Escape key listener. | Add accessibility attributes and global `keydown` Escape handler. |
| **P3-05** | [`PipelineGraph.tsx:127`](./frontend/src/components/PipelineGraph.tsx#L120-L135) | CodeRabbit | **Unhandled ELK Layout Failure**: If ELK graph computation rejects, graph component freezes in loading skeleton. | Catch layout promise rejection and fallback to simple layered coordinate layout. |
| **P3-06** | [`AgentEditor.tsx:66`](./frontend/src/features/agents/AgentEditor.tsx#L60-L70) | CodeRabbit | **Negative `maxRounds` Input**: Form allows submitting 0 or negative values for agent rounds budget. | Add validation rule enforcing `maxRounds >= 1`. |
| **P3-07** | [`AgentsPage.tsx:20`](./frontend/src/features/agents/AgentsPage.tsx#L15-L25) | CodeRabbit | **Agent Pagination Cap**: Registry query caps at 200 items; deep-linked agents beyond cap show 404. | Fetch specific agent by ID via `GET /agents/{id}` when deep linking. |
| **P3-08** | [`TestOnMrDialog.tsx:17`](./frontend/src/features/agents/TestOnMrDialog.tsx#L12-L22) | CodeRabbit | **Unconditional Hook Execution**: Query for MRs fired before user selects a repository. | Pass `enabled: Boolean(selectedRepoId)` to TanStack Query call. |
| **P3-09** | [`VersionHistory.tsx:34`](./frontend/src/features/agents/VersionHistory.tsx#L30-L40) | CodeRabbit | **Empty Version History Guard**: Accessing `versions[0]` throws TypeError if agent has no versions. | Add optional chaining `versions?.[0]` and render empty notice. |
| **P3-10** | [`ComplaintsPage.tsx:180`](./frontend/src/features/complaints/ComplaintsPage.tsx#L175-L185) | CodeRabbit | **Premature Empty State**: "No complaints found" banner renders momentarily while data is still loading. | Check `!isLoading && complaints.length === 0`. |
| **P3-11** | [`DashboardPage.tsx:120`](./frontend/src/features/dashboard/DashboardPage.tsx#L115-L125) | CodeRabbit | **Peak Calculation with Zero Data**: All-zero activity displays NaN or broken chart scale. | Set fallback scale minimum: `Math.max(peak, 5)`. |
| **P3-12** | [`format.ts:10`](./frontend/src/features/dashboard/format.ts#L5-L15) | CodeRabbit | **Compact Number Formatting**: Ad-hoc `1.2k` formatting is not locale-aware. | Utilize `new Intl.NumberFormat(undefined, { notation: 'compact' })`. |
| **P3-13** | [`RepoAgentsPanel.tsx:56`](./frontend/src/features/repos/RepoAgentsPanel.tsx#L50-L60) | CodeRabbit | **A11y Decorative Icons**: SVG chevron icons announce redundant noise to screen readers. | Add `aria-hidden="true"` to non-interactive decorative icons. |
| **P3-14** | [`ReposPage.tsx:74`](./frontend/src/features/repos/ReposPage.tsx#L70-L80) | CodeRabbit | **Clickable Row Keyboard Navigation**: Table row click handlers unreachable via keyboard navigation. | Add `<Link>` wrapper or enter key listener. |
| **P3-15** | [`ReviewDetailPage.tsx:153`](./frontend/src/features/run/ReviewDetailPage.tsx#L145-L160) | CodeRabbit | **Cancel Error Handling**: Review cancellation mutation rejection displays unhandled console error. | Catch error in `onError` callback and display toast notification. |
| **P3-16** | [`AuditSection.tsx:81`](./frontend/src/features/settings/sections/AuditSection.tsx#L75-L85) | CodeRabbit | **Empty Numeric Input Zero Conversion**: Clearing an input field converts value to `0` and persists zero. | Allow null/undefined for empty fields before triggering patch. |
| **P3-17** | [`SettingsPage.tsx:26`](./frontend/src/features/settings/SettingsPage.tsx#L20-L30) | CodeRabbit | **Unknown Section Slug Redirection**: Entering invalid settings URL slug renders empty view without updating route. | Redirect invalid slugs to `/settings/api` via `useNavigate(..., { replace: true })`. |
| **P3-18** | [`textDiff.ts:6`](./frontend/src/lib/textDiff.ts#L1-L10) | CodeRabbit | **Empty Diff Line Count**: Passing empty string `""` to diff parser returns 1 line instead of 0. | Add early exit: `if (!text) return []`. |

---

## 5. Stage 9 (PR #10) — Priority-Based Review Breakdown

### 🔴 Tier 1: Critical Benchmark Validity & Data Isolation Blockers (7 Findings)

High-risk flaws compromising benchmark determinism, corpus temporal integrity, and evaluation reproducibility:

| ID | File & Line | Reviewer | Core Finding & Attack Vector | Planned Remediation |
|---|---|---|---|---|
| **P1-01** | [`run.py:67`](./argus/benchmark/run.py#L60-L75) | Greptile | **Duplicate Trigger Stranding**: Triggering a benchmark run when an active review is already in flight creates orphan reviews that stall pipeline workers. | Check for existing active review on target MR and reuse or wait before enqueuing a duplicate benchmark job. |
| **P1-02** | [`run.py:83`](./argus/benchmark/run.py#L75-L90) | Greptile | **Benchmark State Contamination**: Live developer reviews on benchmark repositories overwrite `is_benchmark = True` run records, corrupting historical baseline stats. | Scope benchmark review runs to dedicated ephemeral sandbox projects or add immutable `is_benchmark` flag guards. |
| **P1-03** | [`retrieval_snapshot.py:143`](./argus/benchmark/retrieval_snapshot.py#L135-L150) | Greptile & CodeRabbit | **Cross-Repo MR Collision**: Snapshot builder queries MR by integer `mr_iid` without scoping to repository project ID, mixing MRs across different repositories with identical IIDs. | Key and query MRs strictly by composite tuple `(project_id, mr_iid)`. |
| **P1-04** | [`retrieval_snapshot.py:102`](./argus/benchmark/retrieval_snapshot.py#L95-L110) | Greptile | **Multi-Revision Diff Bleed**: Diffs fetched without target version SHA mix old and new commits from amended MRs, skewing ground-truth line numbers. | Pass explicit `mr_version_sha` to diff fetcher ensuring exact revision pinning. |
| **P1-05** | [`retrieval_snapshot.py:60`](./argus/benchmark/retrieval_snapshot.py#L55-L65) | Greptile | **Knowledge Corpus Time-Travel**: Benchmark retrieval snapshot loads post-dated learnings created after the benchmark MR was opened, artificially inflating retrieval recall scores. | Enforce strict temporal cutoff filter: `WHERE created_at <= :mr_created_at`. |
| **P1-06** | [`retrieval_snapshot.py:59`](./argus/benchmark/retrieval_snapshot.py#L50-L65) | Greptile | **Lexical Corpus Candidate Loss**: Benchmark snapshot captures only pgvector dense embeddings, omitting BM25 keyword index state and invalidating hybrid retrieval benchmarks. | Snapshot both pgvector dense embeddings and BM25 token frequencies at snapshot time. |
| **P1-07** | [`run.py:109`](./argus/benchmark/run.py#L100-L115) | Greptile | **Premature Finding Scoring**: Benchmark score calculation begins before asynchronous finding persistence or reconciliation finishes, registering false zero-finding scores. | Await review terminal status verification and poll until finding count stabilizes before computing F1. |

---

### 🟠 Tier 2: Major Architecture & Probe Stability (2 Findings)

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P2-01** | [`retrieval_probe.py:153`](./argus/benchmark/retrieval_probe.py#L145-L160) | Greptile | **Embedding Endpoint Cache Collision**: Probe cache keys queries solely by text, causing results to be reused across different embedding models (e.g. OpenAI vs local Ollama). | Include `model_name` and `dimension` in probe cache key: `(model_name, query_hash)`. |
| **P2-02** | [`run.py:68`](./argus/benchmark/run.py#L65-L75) | CodeRabbit | **Orphan Review on Enqueue Failure**: `Review` DB row created before Redis job enqueue; if Redis is down, orphan pending row remains indefinitely. | Wrap in transaction or commit `Review` row only after successful job queue insertion. |

---

### 🟡 Tier 3: Minor Quality, Linters & Score Hygiene (7 Findings)

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P3-01** | [`retrieval_eval.py:4`](./argus/benchmark/retrieval_eval.py#L1-L10) | CodeRabbit | **Unimplemented CLI Option**: CLI documentation advertises `--newest` flag which is not implemented in argparse. | Implement `--newest` sorting in argument parser or remove from usage string. |
| **P3-02** | [`retrieval_probe.py:173`](./argus/benchmark/retrieval_probe.py#L165-L180) | CodeRabbit | **HTTP Connection Starvation**: Creates ephemeral `httpx.AsyncClient` instances per query instead of reusing a shared connection pool. | Manage `httpx.AsyncClient` via async context manager / reusable session. |
| **P3-03** | [`retrieval_snapshot.py:46`](./argus/benchmark/retrieval_snapshot.py#L40-L50) | CodeRabbit | **Ambiguous Review ID Prefix**: Prefix matching on 8-character review IDs can match multiple runs in large databases. | Reject non-unique review prefixes and require minimum 12 characters. |
| **P3-04** | [`retrieval_snapshot.py:143`](./argus/benchmark/retrieval_snapshot.py#L140-L150) | CodeRabbit | **Unclosed GitLab Client**: Benchmark snapshot generator does not close underlying HTTP transport on `GitLabClient`. | Call `await client.close()` or wrap in `async with`. |
| **P3-05** | [`retrieval_snapshot.py:143`](./argus/benchmark/retrieval_snapshot.py#L145-L150) | CodeRabbit | **Redundant MR Query**: Queries MR metadata multiple times within same snapshot pass. | Memoize MR metadata in local dict cache during snapshot pass. |
| **P3-06** | [`run.py:83`](./argus/benchmark/run.py#L80-L90) | CodeRabbit | **Unbounded Poll Timeout**: Benchmark runner polls indefinitely if worker crashes silently. | Implement strict `timeout_seconds` with clear timeout error report. |
| **P3-07** | [`scoring.py:47`](./argus/benchmark/scoring.py#L40-L55) | CodeRabbit | **Duplicate Ground-Truth Matching**: Single generated finding matching multiple ground-truth items is counted multiple times, artificially boosting recall > 1.0. | Track matched ground-truth IDs in a set and enforce 1-to-1 matching. |

---

## 6. Stage 10 (PR #11) — Priority-Based Review Breakdown

### 🔴 Tier 1: Critical Security, Startup & State Blockers (10 Findings)

High-risk bugs causing immediate server/worker startup failure, missing database migrations, runtime type errors in the review pipeline, and broken frontend builds:

| ID | File & Line | Reviewer | Core Finding & Attack Vector | Planned Remediation |
|---|---|---|---|---|
| **P1-01** | [`app.py:15`](./argus/api/app.py#L15) | Greptile | **Missing Authentication Symbols**: `argus.api.auth` defines neither `Principal` nor `require_role`. Importing `app.py` raises `ImportError`, completely blocking API startup. | Define `Principal` dataclass and `require_role` dependency in `argus.api.auth`. |
| **P1-02** | [`app.py:243`](./argus/api/app.py#L243) | Greptile | **Missing Audit Recovery Function**: Startup lifespan imports `reclaim_stale_audit_runs` from `argus.knowledge.audit_scheduler`, which does not exist, crashing startup. | Implement `reclaim_stale_audit_runs` in `argus.knowledge.audit_scheduler` or remove unneeded import. |
| **P1-03** | [`app.py:245`](./argus/api/app.py#L245) | Greptile | **Startup Reclaims Live Jobs**: Startup uses zero-second cutoff when reclaiming in-flight reviews, causing secondary app instances to requeue active running reviews. | Use a realistic stale timeout (e.g. `now - timedelta(minutes=15)`) instead of `now` when detecting abandoned runs. |
| **P1-04** | [`models.py:40`](./argus/domain/models.py#L40) | Greptile | **Missing Alembic Migration**: Repository columns (`sister_repos`), job priority column, and sister links table added to models without a matching Alembic migration. | Generate and include Alembic migration script for `repo_sisters`, `job.priority`, and new repo settings columns. |
| **P1-05** | [`app.py:1252`](./argus/api/app.py#L1252) | Greptile | **Learning Route Unsupported Filter**: `list_learnings` and `search_learnings` receive unsupported `scope` keyword argument, throwing `TypeError` on all `/learnings` requests. | Update `list_learnings` and `search_learnings` in `argus/knowledge/store.py` to accept `scope` filter. |
| **P1-06** | [`pipeline.py:551`](./argus/review/pipeline.py#L551) | Greptile | **Chunk Planner Argument Rejection**: `plan_chunks` called with `hunks` and `token_budget`, but only accepts `files` and `max_chunk_files`, throwing `TypeError` during review scout. | Update `plan_chunks` signature and implementation in `pipeline.py` to accept and respect `hunks` and `token_budget`. |
| **P1-07** | [`pipeline.py:70`](./argus/review/pipeline.py#L70) | Greptile | **Missing Chunk Hunk Fields**: `Chunk` dataclass defines neither `hunk_ids` nor `part`, causing `AttributeError` when scoped helpers access them. | Add `hunk_ids: list[str] = field(default_factory=list)` and `part: int = 1` to `Chunk`. |
| **P1-08** | [`pipeline.py:1032`](./argus/review/pipeline.py#L1032) | Greptile | **Parallel Verdict Reducer Collision**: Parallel verifier branches write `verdicts`, `ungrounded_ids`, and `line_corrections` without LangGraph state reducers, causing fan-in write collisions. | Add `@reducer` / `Annotated[list, operator.add]` or dictionary merging reducers to verifier state channels. |
| **P1-09** | [`workers.py:29`](./argus/jobs/workers.py#L29) | Greptile | **Missing Worker Endpoint Resolver**: `resolve_llm_config_by_name` imported from `argus.llm.config` does not exist, raising `ImportError` on worker launch. | Implement `resolve_llm_config_by_name` in `argus.llm.config` or map worker specs to existing config resolvers. |
| **P1-10** | [`PeoplePage.tsx:8`](./frontend/src/features/people/PeoplePage.tsx#L8) | Greptile | **Broken Frontend TypeScript Build**: `PeoplePage.tsx` imports unsupplied hooks, types, and child views, causing `tsc` compilation failure. | Provide mock/stub implementations or complete the missing hooks and components in frontend API/features. |

---

### 🟠 Tier 2: Major Architecture, Data Integrity & Edge Cases (11 Findings)

Flaws affecting follow-up review execution, sister repository streaming, tool result clipping, and UI form state:

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P2-01** | [`sisters.py:62`](./argus/review/sisters.py#L62) | Greptile | **Sister Context Uncalled in Review**: Review runner never invokes sister repo acquisition or injects sister inspection tools into `PipelineDeps`. | Wire sister repo acquisition into `runner.py` and supply tools to `PipelineDeps`. |
| **P2-02** | [`followup.py:146`](./argus/review/followup.py#L146) | Greptile | **Follow-up Checks Uninvoked**: Re-review runner never executes `follow_up_prior_comments`, leaving prior bot threads unverified. | Wire `follow_up_prior_comments` into `runner.py` when review detects existing bot comments. |
| **P2-03** | [`RepoSistersPanel.tsx:52`](./frontend/src/features/repos/RepoSistersPanel.tsx#L52) | Greptile | **Nested Sister Save Payload**: Save handler wraps `rows.map(...)` in an extra outer array, sending list-of-lists to API and failing schema validation. | Flatten payload to `rows.map((r) => ({ ...r, branch: r.branch?.trim() ? r.branch.trim() : null }))`. |
| **P2-04** | [`models.py:692`](./argus/domain/models.py#L692) | Greptile | **Unused Job Priority**: `Job.priority` column is never assigned in `enqueue` and `claim_next` still orders strictly by `created_at`. | Update `enqueue` to accept priority and order `claim_next` by `priority.desc(), created_at.asc()`. |
| **P2-05** | [`workers.py:73`](./argus/jobs/workers.py#L73) | Greptile | **Uncovered Job Kinds Starvation**: When worker specs omit a job kind, jobs of that kind remain enqueued forever. | Fallback omitted job kinds to default background worker pool. |
| **P2-06** | [`pipeline.py:1027`](./argus/review/pipeline.py#L1027) | CodeRabbit | **String Review ID in UUID Column**: `AgentComplaint.review_id` passed as `str` instead of `uuid.UUID`, raising asyncpg DB commit error. | Wrap `uuid.UUID(state.review_id)` when constructing `AgentComplaint`. |
| **P2-07** | [`sisters.py:176`](./argus/review/sisters.py#L176) | CodeRabbit | **Unbounded Sister File Memory Load**: `sister_read_file` loads entire file into memory before line slicing, causing OOM on large dumps/lockfiles. | Stream lines or check `target.stat().st_size > 5_000_000` before reading. |
| **P2-08** | [`tools.py:62`](./argus/review/tools.py#L62) | CodeRabbit | **Silent Hunk Truncation**: Clipped oversized hunks omit `TRUNCATION_MARKER`, leading reviewer memory to mistakenly believe hunk was fully read. | Append `TRUNCATION_MARKER` and instructions on how to inspect remaining hunk lines. |
| **P2-09** | [`FilterBuilder.tsx:59`](./frontend/src/components/FilterBuilder.tsx#L59) | CodeRabbit | **Unmatched Filter Condition Trapping**: Missing field returns `null`, hiding condition from user but leaving it in `onChange` payload. | Render unmatched-field row with a remove button so user can eliminate orphaned conditions. |
| **P2-10** | [`FilterBuilder.tsx:60`](./frontend/src/components/FilterBuilder.tsx#L60) | CodeRabbit | **Desynchronized Operator/Value Display**: React falls back to first select option while stored condition retains invalid value. | Normalize stored condition when options change or display explicit invalid state. |
| **P2-11** | [`FilterBuilder.tsx:64`](./frontend/src/components/FilterBuilder.tsx#L64) | CodeRabbit | **Missing Filter A11y Labels**: Operator select, search input, and value fields lack accessible names for screen readers. | Add `aria-label` or `<label>` associating controls with target field names. |

---

### 🟡 Tier 3: Minor Quality, Linters & Error States (4 Findings)

| ID | File & Line | Reviewer | Core Finding & Impact | Planned Remediation |
|---|---|---|---|---|
| **P3-01** | [`pipeline.py:52`](./argus/review/pipeline.py#L52) | Greptile | **Allowlist Test Assertion Desync**: `test_exemption_set_is_narrow` fails because sister tools were added to exemption set. | Update test assertions to match intended tool-allowlist policy or isolate sister tool permissions. |
| **P3-02** | [`followup.py:230`](./argus/review/followup.py#L230) | CodeRabbit | **Conflated Reply & Resolve Failure**: If `resolve_discussion` fails after `create_note` succeeds, action remains marked "replied" and is never retried. | Separate try/except blocks for note creation and discussion resolution. |
| **P3-03** | [`PeoplePage.tsx:168`](./frontend/src/features/people/PeoplePage.tsx#L168) | CodeRabbit | **Blank Screen on `/me` Failure**: When `/me` endpoint returns 401 or network error, page renders empty blank view. | Render `ErrorBox` with error message and retry button when `me.error` is present. |
| **P3-04** | [`test_sisters.py:208`](./tests/test_sisters.py#L208) | CodeRabbit | **Brittle Source-Text Inspection**: Test checks exact string in `publisher.py` instead of verifying behavioral filtering of foreign-file findings. | Refactor to behavioral test calling `publish_review` with stub deps. |

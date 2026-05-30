# Frontend feature backlog (deferred from the MVP port)

Features of the old pr-agent UI intentionally NOT in the argus MVP frontend,
each mapped to the backend work it needs. Source analysis:
`frontend-compatibility.md` at the platform root. Spec:
`docs/superpowers/specs/2026-07-04-frontend-mvp-design.md`.

## Milestones (UI enterprise upgrade)

Roadmap from `docs/superpowers/specs/2026-07-10-ui-enterprise-upgrade-design.md`.
Each milestone is independently shippable; status updated as phases land.

| Milestone | Delivers | Status |
| --- | --- | --- |
| **M1 — Foundation** | TanStack Query migration, feature folders, shared primitives (Toggle/EnvChip/SaveBar), typed unions, URL-addressable settings sections | ✅ done (plan `2026-07-10-ui-phase1-2-foundation-and-config.md`, tasks 7–9) |
| **M2 — Config everywhere** | `runtime_settings` store (DB > env > default, secrets env-only), `GET/PUT /settings`, config center UI, repo enable/interval/default-profile edit, proxy edit/disable | ✅ done (all tasks 1–11 complete) |
| **M3 — Agents studio** | `GET /agents` registry rail, versioned guidelines editor, version history + per-version acceptance rate, diff/restore, test-on-MR | ⬜ planned |
| **M4 — Pipeline DAG redo** | reactflow + elkjs auto-layout, design-system node cards, fan-out group containers, run toolbar (elapsed/tokens/tool-calls), re-run | ✅ done (plan `2026-07-10-ui-enterprise-upgrade-design.md`, tasks 1–6); cancel and live log tail deferred, see rows below |
| **M5 — MR detail + verdicts** | Enriched MR detail (labels/reviewers/counts), AnswerBox verdict route (`POST /notes/{id}/verdict`), manual learnings trigger | ⬜ planned |
| **M6 — Reviews queue + Dashboard** | Cross-repo run queue (`GET /reviews` + filters), analytics overview/reviewers/activity endpoints + dashboard | ⬜ planned |
| **M7 — Knowledge** | Learnings browser (list/search/edit/toggle via pgvector store), file-understanding tab | ✅ done (list/search/browse; edit/toggle deferred, see rows below) |

| Feature (old UI) | Missing backend work | Notes |
| --- | --- | --- |
| Analytics dashboard (overview + per-reviewer stats, `Home.tsx`) | Aggregation endpoints (e.g. `GET /analytics/overview`, `GET /analytics/reviewers`) over reviews/notes/feedback | M6 (plan TBD); largest gap; needs product decision on which metrics matter post-cutover |
| Learnings browser (list, search, filters) | `GET /learnings`, `GET /learnings/search?q=` over the pgvector learnings store | ✅ Done (M7) — `/knowledge` route, `LearningsTab.tsx` |
| File understanding tab | Whole domain concept absent (per-file purpose/symbols table + routes) | ✅ Done (M7) — `/knowledge/files` route, `FileUnderstandingTab.tsx`, using only populated `FileKnowledge` fields |
| Archive/reactivate a learning from the UI | `PUT /learnings/{id}` | Deferred this phase (M7 follow-up), per user decision |
| Edit a learning's `hint_text`/`topic` | `PUT /learnings/{id}` | Deferred this phase (M7 follow-up), per user decision |
| Populate `FileKnowledge.purpose`/`key_symbols` | Needs backend design + a population mechanism | Deferred (M7 follow-up); columns currently dead, excluded from `GET /file-knowledge` response |
| Comment answer / verdict workflow (AnswerBox) | Endpoint to attach a human verdict/answer to a bot note; disposition fields already exist | M5 (plan TBD); reconciler currently ingests verdicts from GitLab discussion replies instead |
| Repo settings edit (enable toggle, poll interval, default profile) | ✅ `PUT /repositories/{id}` | Done (M2) |
| Single-repo fetch for deep links | ✅ `GET /repositories/{id}` | Done (M2) |
| LLM endpoint CRUD | `GET/POST /llm-endpoints` routes (table + `resolve_llm_config` already exist) | M3 (plan TBD); registered via direct DB insert; UI awaiting design |
| Rerun a past review by review id | ✅ `POST /merge-requests/{mr_id}/reviews` (existing trigger endpoint, reused) | Done (M4) — `ReviewDetailPage`'s Re-run button, shown only when the review is terminal |
| Cancel a running review | `POST /reviews/{id}/cancel` (not yet built) | Deferred (M4 follow-up); needs a pipeline node-boundary interruption mechanism so an in-flight agent/tool step can be aborted cleanly — the `'canceled'` status already exists in the DB status constraint but is currently unreachable from any code path |
| Per-stage live log tail | Needs a log-line stream/storage mechanism per stage | Deferred (M4 follow-up); superseded for now by the node panel's live-updating trace rows (tool calls/LLM rounds refresh via WS/poll while a review runs), which cover most of the same debugging need |
| Default-profile selection UI | Depends on `PUT /repositories/{id}` (set `default_profile_id`) | M2 dependency done; was a client-side stub even in the old UI |
| Reviewer proxy edit/disable from UI | ✅ `PUT /reviewer-proxies/{id}` | Done (M2) |
| Automated frontend tests | ✅ vitest (`useDirtyForm.test.ts`, `pipelineGraph.test.ts`) | Done (M2) |

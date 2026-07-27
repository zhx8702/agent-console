# 群聊关系图 LLM 批处理状态

- Last update: 2026-05-16T16:04:00+08:00
- Phase: Phase 1 + Phase 2 complete, deployed, smoke passed
- Action: Controlled per-event batch runner and window/chunk relationship extraction implemented
- Background sessions: oceanic-canyon; lucky-slug
- Verification sessions: fast-cloud; glow-river; glow-dune
- Artifact: tasks/group-relationship-llm-batch/PLAN.md; plugins/memory/router.py; plugins/memory/store.py; frontend/src/lib/api.ts; frontend/src/pages/RelationshipGraphPage.tsx
- Blocker: 无

## Completed Commits

- `0d2df3e docs(tasks): design group relationship batch extraction`
- `758988b feat(memory): add controlled relationship extraction batches`
- `a2c2f0b docs(tasks): add supervisor progress incident review`
- `66355e7 docs(tasks): detail window relationship extraction phase`
- `7b0a1b8 feat(memory): add window relationship extraction`

## Phase 1 Completed

Implemented controlled per-event extraction runner:

- `/plugins/memory/group-graph/extract-daily`
- New controls: `batch_limit`, `max_jobs`, `continuous`, `time_budget_seconds`
- Legacy `limit` compatibility retained
- Stop reasons: `single_batch_complete`, `max_jobs_reached`, `time_budget_reached`, `no_ready_jobs`, `llm_unavailable`, `empty_day`
- Frontend AI batch controls: 20/50/100, continuous toggle, max jobs

## Phase 2 Completed

Implemented window/chunk relationship extraction:

- `/plugins/memory/group-graph/extract-window`, admin-only
- Date/session scoped window selection from `plugin_memory_event`
- Controls: `window_size`, `max_windows`, `cursor_event_id`, `dry_run`
- Safe internal LLM prompt with bounded sender-prefixed transcript
- Candidate validation:
  - allowed graph predicates only
  - evidence ids must be from current window
  - confidence clamped
  - no raw text in response
- Persistence:
  - `source_type="llm_group_window"`
  - stable normalized key based on date/predicate/subject/object/evidence ids
  - `original_text=""`
  - value_json contains relation metadata and evidence ids, not transcript
- Graph sync mapping for `llm_group_window` items into graph facts
- Frontend controls: window size 30/50/80, max windows 1/3/5, dry run, safe output summary

## Verification

Main verification:

- `git diff --check`: pass
- `python3 -m py_compile plugins/memory/store.py plugins/memory/router.py`: pass
- `pytest tests/unit/test_memory_router.py tests/unit/test_memory_graph.py tests/unit/test_memory_store_compat.py -q`: 100 passed
- `cd frontend && npm run build`: pass, existing Vite large chunk warning only

Deployment:

- `docker compose --profile app up -d --build api frontend`: pass
- API container up
- frontend container up
- migration completed

Smoke:

- `/healthz`: pass
- `/readyz`: pass, errors=[]
- `/plugins/memory/group-graph/extract-window` without admin token: 403 pass
- frontend bundle: `assets/index-DRf2XOCY.js`
- bundle contains window extraction controls and safe-output note

## Remaining Follow-up Ideas

Not blockers for this task:

- Add background async all-day worker for large historical catch-up.
- Add quality evaluation dashboard for window extraction precision/recall.
- Add conflict/merge scoring across windows and days.
- Split the large frontend chunk later to remove Vite warning.

## Supervisor Checklist Reminder

1. Send update when Codex starts, with session id.
2. Send update when Codex completes.
3. Send update before main verification starts.
4. Send update after tests/build complete.
5. Send update when commit completes.
6. Send update when deploy starts.
7. Send update when deploy completes.
8. Send update when smoke starts/completes.
9. Then final summary.

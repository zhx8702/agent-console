# Group Relationship Graph Continuation Plan

## Goal
Continue development based on:
- `docs/group-relationship-memory.md`
- `docs/memory-acceptance-model.md`
- implemented API: `GET /plugins/memory/group-graph`

Deliver a useful read-only Relationship Graph MVP that consumes the existing memory plugin API, respects acceptance/privacy rules, and is covered by tests.

## Scope
In scope:
- Verify and harden `GET /plugins/memory/group-graph` response against the docs.
- Add/adjust frontend API client types for group graph.
- Add standalone Relationship Graph page/route/navigation.
- Provide filters: tenant/channel/source/session, acceptance status, node/edge type, min confidence, limit.
- Render read-only graph/list details without raw chat text.
- Add/adjust backend and frontend tests where practical.

Out of scope for this batch unless already trivial:
- New extraction pipeline or daily scheduler.
- Write/review actions (`accept`, `reject`, supersede, evidence detail endpoint) unless needed by MVP.
- Raw evidence display.

## Acceptance Criteria
- `GET /plugins/memory/group-graph` returns a documented `schema.version = group-graph.v1`, schema node/edge types, scope, filters, nodes, edges, counts, and no raw content/original_text.
- Default graph retrieval includes only accepted/active normal-safe backing items; explicit `acceptance_status` can include review states for review UI.
- Frontend has standalone Relationship Graph / 群聊关系图 route reachable from navigation.
- UI calls `/plugins/memory/group-graph`, not provider/wxbot raw APIs.
- UI labels candidate/review/non-accepted state clearly.
- Tests pass for touched areas, or failures are documented with cause.

## Phases

### Phase 1 — Recon and gap analysis
- Read current docs and API implementation.
- Identify missing pieces vs MVP acceptance criteria.
- Output: implementation checklist.
- Status: Completed 2026-05-15.
- Gap analysis:
  - Backend `/plugins/memory/group-graph` existed and already returned scope/filters/nodes/edges/counts with default accepted-active gating.
  - Missing/weak contract pieces were documented schema metadata (`group-graph.v1`, node types, edge types), `relation_type` alias compatibility, and router-level defensive raw-content scrubbing.
  - Frontend had an older Memory Graph area under `/memory` calling `/plugins/memory/graph/*`; it did not provide a standalone Group Relationship Graph route using `/plugins/memory/group-graph`.

### Phase 2 — Backend API hardening
- Add schema block and aliases if missing.
- Normalize filter naming (`relation_type`/`edge_type` compatibility if needed).
- Ensure privacy fields are absent.
- Add/adjust unit tests.
- Status: Completed 2026-05-15.
- Implemented:
  - Added canonical `GROUP_GRAPH_SCHEMA_VERSION`, `GROUP_GRAPH_NODE_TYPES`, and `GROUP_GRAPH_EDGE_TYPES`.
  - Store response now includes `schema.version = group-graph.v1`, `schema.node_types`, and `schema.edge_types`.
  - Router accepts `relation_type` as an alias for `edge_type`, returns normalized filter metadata, and recursively removes raw text fields such as `content` and `original_text` from group-graph payloads.
  - Backend tests now assert schema contract, alias forwarding, and privacy scrubbing.

### Phase 3 — Frontend MVP
- Add client method/types.
- Add standalone page and route/nav item.
- Implement filters and read-only graph/list rendering.
- Ensure no raw message display.
- Status: Completed 2026-05-15.
- Implemented:
  - Added group graph API client types and `getGroupGraph`.
  - Added `/relationship-graph` standalone page and navigation entry labeled `群聊关系图`.
  - Page calls `/plugins/memory/group-graph` and supports filters for `tenant_id`, `channel`, `source_key`, `session_id`, `acceptance_status`, `node_type`, `edge/relation type`, `min_confidence`, and `limit`.
  - Page renders read-only SVG graph, relation list, node list, and safe metadata detail panel. Acceptance states are labeled; raw chat text is not requested or displayed.

### Phase 4 — Verification and polish
- Run backend tests and frontend build/tests.
- Fix regressions.
- Update this plan with final status and paths.
- Status: Completed 2026-05-15.
- Verification:
  - `pytest tests/unit/test_memory_router.py tests/unit/test_memory_graph.py` passed: 38 passed.
  - `npm run build` in `frontend/` passed. Vite emitted the existing large chunk warning for the main bundle.

## Current Status
- 2026-05-15 14:50: Plan created by supervisor.
- Existing backend endpoint found in `plugins/memory/router.py` and `plugins/memory/store.py`.
- Existing router unit smoke test already covers basic query forwarding/privacy.
- 2026-05-15: Phase 2 and Phase 3 implemented and verified.
- Remaining risks:
  - The MVP uses a deterministic circular SVG layout, not a force-directed graph engine; this is adequate for inspection but may need clustering/pagination for large graphs.
  - The page relies on the memory plugin group graph API only; extraction pipeline, daily scheduler, review actions, and raw evidence detail endpoints remain out of scope.
  - Frontend build reports a chunk-size warning (`index-*.js` over 500 kB), but the build succeeds.

## Artifact Paths
- Plan: `tasks/group-relationship-graph/PLAN.md`
- Backend: `plugins/memory/router.py`, `plugins/memory/store.py`, `tests/unit/test_memory_router.py`
- Frontend: `frontend/src/lib/api.ts`, `frontend/src/App.tsx`, `frontend/src/pages/RelationshipGraphPage.tsx`, `frontend/src/styles.css`

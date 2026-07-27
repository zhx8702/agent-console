# User Memory Module Audit Report

Date: 2026-05-15

## Executive Summary

The memory module is usable for scoped user/session memory, manual memory controls, deterministic extraction, acceptance gating, retrieval, vector fallback, and an operator-facing frontend. Runtime prompt safety is materially better than a simple "store everything" design: prompt retrieval filters active, normal-sensitivity, accepted-or-legacy-accepted items before injection (`plugins/memory/store.py:1446`, `plugins/memory/store.py:1631`, `plugins/memory/hooks.py:392`).

The module is not complete relative to the acceptance and group relationship specs. The main gaps are review permission boundaries on read endpoints, raw event/profile exposure in several admin/operator APIs, partial acceptance lifecycle consistency, no deterministic daily relationship extraction pipeline, limited relationship graph UX, and no durable graph-specific acceptance/review actions. The standalone Relationship Graph exists and is routed, but it is a read-only projection over existing memory graph rows, not a mature group relationship memory system (`frontend/src/App.tsx:34`, `frontend/src/App.tsx:162`, `plugins/memory/store.py:7814`).

## Current Capability Map

### Profiles

Status: usable.

- Identity profiles and session profiles are exposed through `/profiles`, `/session-profiles`, and `/runtime-profile` (`plugins/memory/router.py:371`, `plugins/memory/router.py:410`, `plugins/memory/router.py:459`).
- Runtime save updates identity counters, session summaries, recent turns, open items, decisions, and short-term memory (`plugins/memory/store.py:6391`).
- Legacy identity/session profile fields are still maintained as compatibility caches and imported into item rows when needed (`plugins/memory/store.py:6416`).

Gaps:

- Profile/event list endpoints have no explicit admin/user auth boundary in router-level code.
- Runtime session payload includes broad profile structures for prompt assembly (`plugins/memory/hooks.py:486`); downstream prompt filtering helps, but this area should stay tightly audited.

### Items And Manual Controls

Status: mature for CRUD/search/forget; partial for end-user permissions.

- Items support tenant/channel/source/user/session scope, source type, memory type, status, sensitivity, confidence, pinned, priority, normalized key, source evidence ids, and original text (`plugins/memory/store.py:2427`).
- Manual `/remember`, `/search`, `/forget`, `/update` enforce current-user targeting unless admin token is supplied (`plugins/memory/router.py:808`, `plugins/memory/router.py:837`, `plugins/memory/router.py:876`, `plugins/memory/router.py:899`).
- Manual memories are pinned and confidence 1.0 by default (`plugins/memory/store.py:2462`).

Gaps:

- General `/items`, `/events`, `/graph/*`, and profile endpoints are broad read surfaces without the same current-user/admin gate (`plugins/memory/router.py:476`, `plugins/memory/router.py:547`, `plugins/memory/router.py:652`).
- `/items` returns `content` and `original_text` via `list_memory_items` (`plugins/memory/store.py:3341`), which is acceptable only for protected admin/operator contexts.

### Extraction

Status: usable deterministic extraction; partial LLM extraction.

- Deterministic extraction handles explicit memory/profile markers, preferences, constraints, basic sensitivity, one-off lookup/service suppression, replacement/invalidation, and normalized keys (`plugins/memory/store.py:1040`).
- Optional structured LLM extraction validates action shape and adds acceptance hints (`plugins/memory/structured_extractor.py:145`).
- Memory save is wired after postprocess and via effect handling/flow paths (`plugins/memory/hooks.py:577`, `plugins/memory/hooks.py:593`).
- Async extraction jobs are enqueued from runtime save and can be drained by the inbound worker when enabled (`plugins/memory/store.py:6572`, `app/workers/inbound_worker.py:75`).

Gaps:

- LLM extraction is disabled by default in settings and depends on worker drain flags.
- Extraction is current-turn/user-centric; group relationship extraction is not the rule/stat daily pipeline described in `docs/group-relationship-memory.md`.

### Acceptance

Status: usable but partial.

- Acceptance metadata is stored in `value_json.acceptance` and finalized into API fields: status, score, reason, signals, history, supersede ids, extraction confidence (`plugins/memory/store.py:2564`).
- Deterministic acceptance scoring implements explicitness/evidence/durability/actionability/consistency/source reliability with joke/uncertainty/contradiction/sensitivity penalties (`plugins/memory/store.py:321`, `plugins/memory/store.py:5450`).
- Admin review actions support accept, reject, needs_review, mark_joke, expire, and supersede (`plugins/memory/router.py:930`, `plugins/memory/store.py:5038`).
- Legacy acceptance audit/backfill exists and is admin-gated for writes (`plugins/memory/router.py:679`, `plugins/memory/router.py:710`, `plugins/memory/router.py:736`).

Gaps:

- Acceptance read/audit endpoints are not admin-gated.
- Manual `/remember` can create an active item without acceptance metadata; legacy absence is treated as accepted in prompt gates, which is intentional for compatibility but weakens audit precision.
- Review actions update item status and graph/vector sync through `update_memory_item`, but there is no separate immutable audit table.

### Retrieval And Prompt Gating

Status: mature for item retrieval; usable for optional graph retrieval.

- Prompt eligibility requires active status, normal sensitivity, not deleted, and absent-or-accepted acceptance metadata (`plugins/memory/store.py:1446`).
- SQL/vector/hybrid retrieval re-ranks only after prompt eligibility and scope filtering (`plugins/memory/store.py:1631`, `plugins/memory/store.py:3780`, `plugins/memory/store.py:3838`).
- Hooks attach relevant items and optional graph facts/episodes before capability execution (`plugins/memory/hooks.py:392`).
- Prompt rendering filters item status, source, sensitivity, confidence, and duplicates again (`app/common/prompting.py:115`).

Gaps:

- Graph prompt rendering trusts retrieved graph rows to already be safe and does not independently check acceptance metadata on facts/episodes (`app/common/prompting.py:183`).
- Prompt line construction for graph facts can include `object_value`; retrieval gates reduce risk, but graph object values should be treated as sensitive by default unless backed by safe accepted item.

### Vector And Search

Status: usable optional capability.

- Item and graph vector indexing are optional and disabled unless configured (`plugins/memory/vector_index.py:98`).
- Item vector indexability requires active, normal, not deleted, identity/session scope, and accepted-or-legacy accepted (`plugins/memory/vector_index.py:98`).
- Rebuild and smoke endpoints are admin-gated (`plugins/memory/router.py:1009`).
- SQL fallback is present when vector search fails (`plugins/memory/store.py:3805`).

Gaps:

- No evidence from this audit that vector index consistency is continuously verified after every acceptance transition beyond best-effort sync.
- Operational metrics for vector hit rate, fallback rate, stale index count, and rebuild errors are thin.

### Graph

Status: usable projection; partial relationship graph.

- Memory items sync into graph entities/facts/episodes and status changes propagate to graph projections (`tests/unit/test_memory_graph.py:200`).
- Graph fact listing is scoped and joins entities on matching tenant/channel/source/user (`tests/unit/test_memory_graph.py:216`, `tests/unit/test_memory_graph.py:279`).
- `/group-graph` returns a sanitized read-only graph with schema, filters, nodes, edges, evidence ids/counts, and default accepted-only behavior (`plugins/memory/router.py:495`, `plugins/memory/store.py:7814`).
- Group graph tests assert raw memory content and original text are not selected or serialized (`tests/unit/test_memory_graph.py:309`).

Gaps:

- The graph is derived from existing `plugin_memory_entity`, `plugin_memory_fact`, and `plugin_memory_episode`, not from daily group records with rule/stat extraction.
- No separate evidence endpoint exists for `/plugins/memory/group-graph/evidence/{edge_id}`.
- Group graph accepts explicit `acceptance_status` filters without apparent role/permission checks.
- It is user-row centric internally; relationship graph queries pass `user_id=None`, which broadens session/tenant graph projection and needs explicit authorization review (`plugins/memory/store.py:7863`).

### Episodes

Status: usable for session summaries/retrieval; partial as acceptance-managed episodic memory.

- Session state tracks recent turns, open items, decisions, and compact summaries (`plugins/memory/store.py:6448`).
- Episodic memory maps to graph episodes and can be retrieved in graph/hybrid paths.

Gaps:

- Episodes are not clearly review-only by default in all surfaces.
- Relationship graph does not expose source episode ids as first-class evidence consistently; it mostly rolls episode event ids/memory ids into edge references.

### Audit And Backfill

Status: usable for legacy audit/backfill and job maintenance.

- Acceptance stats, legacy audit, and controlled backfill exist (`plugins/memory/router.py:679`, `plugins/memory/router.py:710`, `plugins/memory/router.py:736`).
- LLM extraction job list/stats/maintenance avoid raw result payloads in router responses (`plugins/memory/router.py:930` onward; tested in `tests/unit/test_memory_router.py:437`).
- WeChat SDK backfill imports events, applies deterministic extraction, can enqueue LLM jobs, and updates profile summaries (`plugins/memory/store.py:8548`).

Gaps:

- Backfill is provider-specific to WeChat, not source-plugin-neutral.
- Backfill stores raw event text in `plugin_memory_event`; normal `/events` exposes event text and should be admin/debug only.

### Frontend UX

Status: usable operator workbench; partial review UX; partial relationship graph UX.

- `MemoryPage` has typed support for items, graph preview, extraction job maintenance, acceptance stats/audit/backfill, acceptance details, history, duplicate hints, and review actions (`frontend/src/pages/MemoryPage.tsx:439`, `frontend/src/pages/MemoryPage.tsx:732`).
- `RelationshipGraphPage` is standalone and routed at `/relationship-graph` (`frontend/src/App.tsx:34`, `frontend/src/App.tsx:162`).
- Relationship graph supports filters, read-only SVG layout, edge/node lists, safe metadata, and evidence id display (`frontend/src/pages/RelationshipGraphPage.tsx:102`).

Gaps:

- Relationship graph lacks time range filters despite backend support, review actions, evidence summary panel, better layout, search, timeline, and permission-aware modes.
- `MemoryPage` still includes legacy raw graph/entity/fact/episode tables where object values, titles, and summaries are displayed; this needs admin-only framing and safer copy/export behavior.

## Completeness By Area

| Area | Rating | Notes |
| --- | --- | --- |
| Profiles | Usable | Runtime/profile APIs exist; auth/read scoping needs hardening. |
| Items | Mature | CRUD, normalized keys, dedupe hints, sensitivity, pinned/manual semantics. |
| Extraction | Usable/Partial | Deterministic current-turn extraction is solid; LLM optional; no daily group pipeline. |
| Acceptance | Usable/Partial | Score/review/backfill exist; audit/read permissions and immutable audit are missing. |
| Retrieval/prompt gating | Mature | Strong item gates; graph prompt gating should be independently rechecked. |
| Vector/search | Usable | Optional index, fallback, admin rebuild/smoke; ops metrics limited. |
| Graph | Partial | Item graph projection and sanitized group graph exist; relationship graph spec mostly unimplemented. |
| Episodes | Partial | Useful session summary/episodic projection; weak lifecycle/review separation. |
| Audit/backfill | Usable/Partial | Good maintenance tools; provider-neutrality and read permissions lag. |
| Frontend UX | Partial | Operator memory page is broad; Relationship Graph MVP is basic read-only. |

## Key Risks And Gaps

### Safety And Privacy

- Broad read endpoints can expose private memory or event text without router-level auth: `/events`, `/items`, `/graph/entities`, `/graph/facts`, `/graph/episodes`, `/graph/preview`, profiles (`plugins/memory/router.py:371`, `plugins/memory/router.py:476`, `plugins/memory/router.py:547`, `plugins/memory/router.py:652`).
- `MemoryPage` displays graph fact object values and episode titles/summaries from legacy graph APIs; these may be derived from private messages.
- Relationship graph raw-field scrubbing is key-name based (`plugins/memory/router.py:1` area); nested private content under non-blocklisted keys could leak.
- `group-graph` review/candidate filters are available without explicit admin authorization.

### Correctness

- `get_group_relationship_graph` compares `from`/`to` timestamps as strings (`plugins/memory/store.py:7958`), which is fragile across timezone/format differences.
- Graph acceptance for rows without backing items falls back from row status to accepted (`plugins/memory/store.py:1774`); acceptable for legacy active rows but risky for relationship graph semantics.
- Existing graph extraction is LLM-assisted memory graph, not observable behavior-first group relationship extraction.
- Supersede/expire/reject are item-level; graph projection consistency depends on sync and lacks dedicated graph edge lifecycle.

### UX

- Relationship Graph has no evidence summary endpoint/panel, no search, no timeline, no fit/pan/zoom/drag, no mode separation beyond filters, and no review actions.
- MemoryPage mixes memory item review, raw graph diagnostics, extraction jobs, and backfill into one dense surface.
- Acceptance review is present in item UI but not graph-edge-centric.

### Ops And Performance

- `group-graph` fetches entities/facts/episodes with capped limits and assembles in memory; large tenants/groups need pagination/cursors and indexes.
- LLM job drain is opt-in and worker-bound; no first-class operational dashboard for extraction lag, dead jobs by scope, prompt-injected count, or review throughput.
- Vector index sync is best-effort and warnings-only; drift detection is limited to rebuild/smoke tools.

### Tests

- Strong unit coverage exists for router endpoints, graph scoping, graph sanitization, acceptance review calls, extraction job safety, and graph prompt retrieval.
- Missing tests: profile/events/items auth boundaries, `group-graph` permission behavior for candidates/review states, evidence endpoint, date range parsing, graph prompt independent acceptance gates, relationship graph frontend interactions, and daily group extraction.

## Roadmap

### P0: Close Prompt/Privacy Holes

1. Add explicit authorization policy to broad memory read endpoints.
   - Files/APIs: `plugins/memory/router.py` for `/profiles`, `/session-profiles`, `/runtime-profile`, `/events`, `/items`, `/graph/*`, `/items/acceptance-*`, `/group-graph`.
   - Suggested verification: API tests that normal users can access only their own current-user scoped memory; admin token required for cross-user, raw event, graph diagnostic, acceptance audit, and candidate/review graph filters.

2. Split safe public payloads from admin/debug payloads.
   - Files/APIs: `plugins/memory/router.py`, `plugins/memory/store.py`.
   - Suggested verification: tests asserting no `content`, `original_text`, `user_text`, `assistant_text`, graph `object_value`, episode `summary`, provider profile fields, or raw metadata in non-admin endpoints.

3. Recheck graph prompt eligibility at render/retrieval boundary.
   - Files/APIs: `plugins/memory/store.py`, `app/common/prompting.py`.
   - Suggested verification: unit tests where graph facts/episodes with pending/rejected/sensitive/deleted backing items never appear in `user_memory` or prompt sections.

4. Harden `group-graph` acceptance and time filtering.
   - Files/APIs: `plugins/memory/store.py:get_group_relationship_graph`.
   - Suggested verification: date parsing tests for timezone-aware `from`/`to`; tests that default graph returns only accepted active normal-backed edges; review states require admin.

### P1: Make Acceptance And Review Operationally Complete

1. Add immutable audit records or a dedicated acceptance history table.
   - Files/APIs: schema migration, `review_memory_item_acceptance`, legacy backfill.
   - Suggested verification: accept/reject/expire/supersede actions append immutable entries and survive value_json overwrite.

2. Normalize acceptance columns for queryability.
   - Files/APIs: migration for `acceptance_status`, `acceptance_score`, `extraction_confidence`, `accepted_at`, `reviewed_at`, `reviewed_by`.
   - Suggested verification: stats/audit queries use indexed columns and match value_json migration output.

3. Build graph edge review workflow.
   - Files/APIs: `GET /plugins/memory/group-graph`, new review endpoints for graph-backed item/fact actions, frontend Relationship Graph review mode.
   - Suggested verification: accepting/rejecting an edge updates backing item, fact/episode status, vector index, and default graph visibility.

4. Add `/plugins/memory/group-graph/evidence/{edge_id}`.
   - Files/APIs: new router/store methods.
   - Suggested verification: returns evidence ids/counts/sanitized summaries by default; raw mode admin-only; no source text without explicit permission.

### P2: Implement True Group Relationship Memory

1. Add provider-neutral daily extraction pipeline.
   - Files/APIs: memory plugin service/job, source event interfaces, graph upsert helpers.
   - Suggested verification: seeded day-1/day-2 group records increment evidence counts, preserve first_seen, update last_seen, and increase confidence only with multi-day/direct evidence.

2. Add rule/stat observable extraction before LLM semantic extraction.
   - Files/APIs: new graph extraction module distinct from `graph_extractor.py`.
   - Suggested verification: mention/reply/resource/task edges are deterministic; LLM-only semantic edges default to candidate/needs_review.

3. Improve Relationship Graph UX.
   - Files: `frontend/src/pages/RelationshipGraphPage.tsx`, `frontend/src/lib/api.ts`, styles.
   - Suggested verification: Playwright tests for filters, search, selection inspector, evidence panel, review labels, responsive layout, and no raw text.

4. Add ops metrics.
   - Files/APIs: memory store/router metrics endpoints or existing telemetry.
   - Suggested verification: counters for candidate count, auto-accept rate, review rate, reject reasons, prompt-injected count, extraction lag, dead jobs, vector fallback, and post-accept corrections.

## Recommended Verification Suite

- Backend unit: `tests/unit/test_memory_router.py`, `tests/unit/test_memory_graph.py`, plus new auth/privacy/date-range/acceptance-transition tests.
- Backend integration: seed memory items/events/facts/episodes and hit `/plugins/memory/group-graph`, retrieval, prompt assembly, review actions, and vector rebuild/smoke.
- Frontend: add React/Playwright coverage for `MemoryPage` acceptance review and `RelationshipGraphPage` filters/selection/evidence with sanitized fixtures.
- Privacy regression: fixture strings placed in raw fields should never appear in safe endpoint responses or screenshots.

## Bottom Line

The current module is a solid memory-item system with meaningful prompt gates and an early graph projection. It is not yet a complete group relationship memory module. The next engineering focus should be authorization and safe payload boundaries first, then acceptance review consistency, then the provider-neutral daily relationship extraction pipeline and richer graph UX.

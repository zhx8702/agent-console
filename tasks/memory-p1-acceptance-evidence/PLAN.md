# Memory P1 Acceptance Audit + Graph Evidence Plan

## Goal
Continue after P0 privacy/auth hardening by making memory acceptance/review more auditable and relationship graph edges easier to inspect safely.

## Scope
This batch implements a focused P1 slice from `tasks/user-memory-module-audit/REPORT.md`:

1. Immutable/durable acceptance audit records for review actions.
2. Safe `/plugins/memory/group-graph/evidence/{edge_id}` endpoint.
3. Tests proving privacy boundaries and audit behavior.

## In Scope
- Backend schema/store/router changes.
- Unit tests for review actions and evidence endpoint.
- Documentation/status updates in this task folder.

## Out of Scope
- Provider-neutral daily group relationship extraction pipeline.
- Full frontend graph review UX.
- Large acceptance column normalization migration unless needed as a small preparatory change.
- Gateway/OpenClaw supervision changes.

## Acceptance Criteria
- Accept/reject/needs_review/mark_joke/expire/supersede review actions append durable audit/history entries that survive item `value_json` overwrite paths.
- Audit records include item id, tenant/channel/source/user/session where available, previous status, new status, reviewer/admin actor if available, reason, and timestamp.
- Evidence endpoint returns safe edge evidence by default: edge metadata, evidence ids/counts, backing memory item ids, event ids, non-raw summaries only.
- Evidence endpoint raw mode or sensitive raw fields require admin and must not leak raw message text in safe mode.
- Tests cover safe evidence response, admin/raw behavior if implemented, and acceptance audit persistence.

## Files Likely Involved
- `plugins/memory/router.py`
- `plugins/memory/store.py`
- migrations under `migrations/` or plugin schema helpers
- `tests/unit/test_memory_router.py`
- `tests/unit/test_memory_graph.py`
- this task plan/status

## Verification Plan
- `python3 -m pytest tests/unit/test_memory_router.py tests/unit/test_memory_graph.py`
- Add narrower tests if new schema helpers are introduced.

## Risks
- Existing acceptance history is embedded in `value_json`; adding a separate table must remain backward compatible.
- Evidence endpoint must not expose raw event text or memory content to non-admin callers.
- Graph edges may be synthetic IDs; endpoint needs robust parsing/lookup.

## Progress Log
- 2026-05-15 16:36: Plan created. Next: hand implementation batch to Codex and monitor.

# Memory Module P0 Privacy/Auth Hardening Plan

## Goal
Implement the highest-priority P0 hardening items from `tasks/user-memory-module-audit/REPORT.md` so memory read surfaces and graph/prompt paths have safer authorization and payload boundaries.

## Scope
In scope for this implementation batch:
- Add/strengthen authorization policy for broad memory read endpoints where feasible without breaking existing admin/operator workflows.
- Separate safe payloads from admin/debug payloads for memory events/items/graph surfaces.
- Harden `group-graph` review-state access and time filtering.
- Add regression tests for privacy/auth/date-range behavior.
- Keep behavior backward-compatible for admin-token/operator use.

Primary files:
- `plugins/memory/router.py`
- `plugins/memory/store.py`
- `app/common/prompting.py` if prompt graph gating needs independent checks
- `tests/unit/test_memory_router.py`
- `tests/unit/test_memory_graph.py`

Out of scope for this batch:
- New migrations for indexed acceptance columns.
- Dedicated immutable audit table.
- Full daily group relationship extraction pipeline.
- Frontend UX overhaul beyond any tiny compatibility adjustments needed.

## Acceptance Criteria
- Normal/non-admin calls cannot access cross-user raw memory read surfaces unless scoped to current user where the endpoint supports that concept.
- Admin/debug surfaces remain available with admin token.
- Safe endpoint responses do not leak raw fields such as `content`, `original_text`, `user_text`, `assistant_text`, `raw_text`, `message_text`, graph `object_value`, or episode summaries unless explicitly admin/debug.
- `group-graph` default remains accepted/safe; requesting candidate/review/rejected/superseded/expired states requires admin authorization.
- `group-graph` `from`/`to` filtering uses robust datetime comparison instead of string ordering.
- Focused backend tests pass.

## Progress Log
- 2026-05-15 15:53: Plan created. Next: hand implementation batch to Codex and monitor.
- 2026-05-15 16:35: Implemented router read auth/safe payload split, `group-graph` review-state admin gate, datetime range filtering, and graph prompt eligibility hardening. Added focused router/graph regression tests.

## Verification Plan
- `python3 -m pytest tests/unit/test_memory_router.py tests/unit/test_memory_graph.py`
- Add narrower tests if Codex introduces new auth helpers.

## Final Artifacts
- Changed files:
  - `plugins/memory/router.py`
  - `plugins/memory/store.py`
  - `app/common/prompting.py`
  - `tests/unit/test_memory_router.py`
  - `tests/unit/test_memory_graph.py`
  - `tasks/memory-p0-hardening/PLAN.md`
- Prompt boundary note: graph retrieval now requires active, normal, not-deleted backing memory with missing-or-accepted acceptance metadata; prompt rendering also skips graph facts/episodes marked non-active, non-normal, or non-accepted as a defensive guard. No broader prompt refactor was made.
- Remaining risk: admin/debug endpoints intentionally still return raw diagnostic payloads for operator flows; no migration was added for indexed acceptance fields in this batch.
- Verification output:
  - `python3 -m pytest tests/unit/test_memory_router.py tests/unit/test_memory_graph.py`: passed, 41 tests.
  - Additional check: `python3 -m pytest tests/unit/test_prompting.py`: passed, 6 tests.

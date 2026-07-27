# User Memory Module Audit Plan

## Goal
Assess whether the current agent-console user memory module is complete enough, identify gaps, and propose prioritized optimizations.

## Scope
In scope:
- Memory plugin backend: profiles, items, extraction, acceptance model, graph entities/facts/episodes, runtime retrieval/profile, vector/search, backfill, review/audit APIs.
- Frontend memory UX: MemoryPage and related visualization/review surfaces.
- Documentation alignment: `docs/memory-acceptance-model.md`, `docs/group-relationship-memory.md`, README/docs if relevant.
- Tests and operational readiness.

Out of scope for this audit:
- Implementing fixes unless they are tiny documentation notes.
- Raw private message inspection.
- Provider-specific wxbot behavior except as upstream evidence source.

## Acceptance Criteria
- Produce a concise but useful audit report with:
  - Current implemented capabilities.
  - Completeness assessment by area.
  - Main risks/gaps.
  - Recommended optimization roadmap: P0/P1/P2.
  - Concrete file/API references.
  - Suggested verification/tests.
- Do not expose private raw memory/message contents.

## Phases

### Phase 1 — Recon
- Read docs and identify intended architecture.
- Inspect memory plugin/router/store/models/tests/frontend.
- Output capability map.

### Phase 2 — Gap analysis
- Compare current implementation to intended model.
- Identify missing workflows, safety gaps, UX gaps, testing gaps, performance/ops gaps.

### Phase 3 — Report
- Write `tasks/user-memory-module-audit/REPORT.md`.
- Update this PLAN status.

## Current Status
- 2026-05-15 15:36: Plan created. Codex audit to be launched.
- 2026-05-15 15:40: Read-only audit completed. Report written to `tasks/user-memory-module-audit/REPORT.md`.

## Artifact Paths
- Plan: `tasks/user-memory-module-audit/PLAN.md`
- Report: `tasks/user-memory-module-audit/REPORT.md`

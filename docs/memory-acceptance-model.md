# Memory Acceptance Model

## Background And Problems

The memory pipeline has three separate decisions that must not be collapsed:

- LLM extraction confidence is not factual probability. It only says how confident the extractor is that a candidate was expressed in the source text and matches the requested schema.
- Extracted does not mean accepted. An extracted candidate can be a joke, a one-off request, a weak inference, a contradiction, or sensitive data that must not become prompt memory.
- Graph display does not mean prompt use. Graph entities, facts, and episodes can be useful for review and debugging while still being excluded from default retrieval and prompt injection.

The acceptance model formalizes this separation. Extraction creates memory candidates. Acceptance decides whether a candidate may affect runtime behavior.

## Concepts

- Memory candidate: a proposed memory from deterministic extraction, LLM extraction, graph extraction, manual entry, or backfill. It has evidence and extraction metadata but may not be usable.
- Accepted memory: a candidate approved for normal retrieval and prompt use while active.
- Needs review: a candidate that may be useful but must not affect prompts until a human or stronger evidence accepts it.
- Rejected: a candidate judged non-memory, joke, too uncertain, too weak, unsafe, or otherwise inappropriate.
- Superseded: a previously accepted memory replaced by a newer accepted memory.
- Expired: a previously accepted memory no longer current because of TTL, time-bound semantics, or manual expiration.
- `extraction_confidence`: confidence that the extractor correctly identified and structured the candidate from the source.
- `acceptance_score`: confidence in accepting the candidate as durable, actionable, safe memory for runtime use. It is not the same as `extraction_confidence`.

## State Machine

Canonical acceptance states:

```text
candidate -> accepted
candidate -> needs_review
candidate -> rejected
needs_review -> accepted
needs_review -> rejected
accepted -> superseded
accepted -> expired
accepted -> rejected
superseded -> accepted    # only by explicit restore/manual correction
expired -> accepted       # only by fresh evidence or manual restore
rejected -> needs_review  # only by manual reopen
```

Mapping to existing `plugin_memory_item.status`:

- `accepted` maps to `active`.
- `candidate` maps to `pending`.
- `needs_review` maps to `pending`.
- `rejected` maps to `pending` in P4a metadata-only storage, and can map to `archived` or a dedicated status in a later migration.
- `superseded` maps to `invalidated`.
- `expired` maps to `archived`.
- Existing `deleted` remains a deletion state outside acceptance.

In P4a, acceptance fields live in `value_json.acceptance` for compatibility:

```json
{
  "acceptance": {
    "status": "accepted",
    "score": 0.86,
    "reason": "acceptance_score_auto_accept",
    "extraction_confidence": 0.92,
    "recommendation": "accepted",
    "signals": {
      "explicitness": 1.0,
      "evidence_strength": 0.92,
      "durability": 0.9,
      "actionability": 0.85,
      "consistency": 1.0,
      "recency": 0.8,
      "source_reliability": 0.95,
      "joke_score": 0.0,
      "uncertainty_score": 0.0,
      "contradiction_score": 0.0,
      "sensitivity_risk": 0.0
    }
  }
}
```

## Scoring Model

Positive signals, each normalized `0..1`:

- `explicitness`: user directly asked to remember it or clearly stated it as a durable fact/preference.
- `evidence_strength`: source evidence quality and directness; multiple independent evidence ids should raise it.
- `durability`: expected persistence beyond the current turn/session.
- `actionability`: usefulness for future responses, routing, personalization, or constraints.
- `consistency`: agreement with existing accepted memory.
- `recency`: freshness, especially for preferences that can change.
- `source_reliability`: manual and explicit-user sources score higher than weak automatic inference or backfill.

Penalties, each normalized `0..1`:

- `joke_score`: humor, sarcasm, "just kidding", or playful non-factual phrasing.
- `uncertainty_score`: maybe/probably/not-sure phrasing or assistant/user uncertainty.
- `contradiction_score`: conflict with accepted or protected memory.
- `sensitivity_risk`: PII, secrets, credentials, health/legal/financial facts, addresses, or other sensitive content.

Default score:

```text
positive =
  explicitness * 0.18 +
  evidence_strength * 0.18 +
  durability * 0.16 +
  actionability * 0.14 +
  consistency * 0.14 +
  recency * 0.08 +
  source_reliability * 0.12

penalty =
  joke_score * 0.45 +
  uncertainty_score * 0.30 +
  contradiction_score * 0.35 +
  sensitivity_risk * 0.60

acceptance_score = clamp(positive - penalty, 0, 1)
```

Initial thresholds:

- Auto accept: `acceptance_score >= 0.78`, sensitivity normal, no strong joke/uncertainty/contradiction penalty.
- Needs review: `0.35 <= acceptance_score < 0.78`, or explicit LLM review recommendation, or sensitive/contradictory content.
- Reject: `acceptance_score < 0.35`, strong joke/non-factual signal, or explicit LLM reject recommendation when not otherwise protected by manual review policy.

A single weak evidence item must not auto accept unless explicitness, durability, actionability, and source reliability are also high.

## Suggested LLM Schema

Structured extraction should keep the current action shape and add optional acceptance fields:

```json
{
  "actions": [
    {
      "op": "add",
      "memory_type": "preference",
      "content": "User prefers concise Chinese replies.",
      "normalized_key": "constraint:response_defaults:language_style",
      "confidence": 0.92,
      "extraction_confidence": 0.92,
      "sensitivity": "normal",
      "reason": "explicit preference",
      "tone": "literal",
      "intent_strength": 0.95,
      "durability": 0.9,
      "actionability": 0.9,
      "acceptance_recommendation": "accepted",
      "acceptance_reason": "Explicit durable response preference.",
      "scores": {
        "explicitness": 1.0,
        "evidence_strength": 0.92,
        "durability": 0.9,
        "actionability": 0.9,
        "consistency": 1.0,
        "recency": 1.0,
        "source_reliability": 0.95,
        "joke_score": 0.0,
        "uncertainty_score": 0.0,
        "contradiction_score": 0.0,
        "sensitivity_risk": 0.0
      }
    }
  ]
}
```

The LLM may recommend, but deterministic policy decides final acceptance.

## System Rules

Must review or reject:

- Jokes, sarcasm, roleplay, obviously fictional statements, or "just kidding" content.
- Uncertain claims: maybe, probably, I think, not sure, hearsay, or ambiguous references.
- One-off operational requests, temporary task details, shipment/order/refund requests, or lookup requests without durable intent.
- Contradictions with existing accepted, manual, or pinned memory.
- Sensitive content unless explicitly allowed by a future policy and never secrets/credentials.
- Low evidence or source text that requires inference beyond what was said.

Auto accepted cases:

- Manual memory created by an authorized user.
- Explicit "remember this" user statements with normal sensitivity, high durability/actionability, no contradiction, and score above threshold.
- Clear stable preferences or constraints such as default language, response style, accessibility needs, or durable brand/category preference.

Sensitive info handling:

- Secrets, credentials, access tokens, passwords, private keys, and payment data must be rejected or quarantined, never prompt-injected.
- PII and health/legal/financial facts default to `needs_review` and remain excluded from prompts.
- Raw evidence may include sensitive source text, so APIs and UI must avoid exposing original private message bodies outside admin/debug contexts.

## Prompt Usage Gating

Default prompt and retrieval paths must include only accepted and active memory:

- `plugin_memory_item.status = 'active'`
- `sensitivity = 'normal'`
- `deleted_at IS NULL`
- acceptance metadata absent or `value_json.acceptance.status = 'accepted'`

Candidates, `needs_review`, rejected, superseded, expired, pending, invalidated, deleted, sensitive, and private items are excluded by default. Debug or admin review endpoints may list them, but must clearly label them and avoid leaking private source message contents.

Current P4a compatibility: existing active-status SQL and graph retrieval gates remain authoritative, and runtime helpers also treat explicit acceptance metadata other than `accepted` as non-injectable. New candidates and review items map to `pending`, so they stay excluded from default prompt use.

## Evidence Requirements

Every accepted memory should be traceable to evidence ids:

- `source_event_id`
- `source_trace_id`
- graph source event ids or memory item ids
- manual actor/audit id when available

The stored memory may keep a short evidence reference list in `value_json.evidence`. Private source message text must not be surfaced through normal APIs or UI. Single weak evidence should produce `needs_review`, not auto accept.

## UI/API Requirements

P4a:

- List memory items with `acceptance_status`, `acceptance_score`, `acceptance_reason`, `acceptance_signals`, and `extraction_confidence` when present.
- Display these fields in MemoryPage without changing review workflows.
- Keep existing status filters; `pending` includes candidates and review items.

P4b:

- Add Review mode filters: candidate, needs_review, rejected, superseded, expired, accepted.
- Add admin actions: Accept, Reject, Mark joke, Expire, Supersede.
- Actions must update acceptance metadata, existing `status`, graph projection status, vector index, and legacy runtime cache consistently.

P4c:

- Add bulk review queues, evidence summaries, contradiction views, and audit history.
- Review actions append compact entries to `value_json.acceptance.history` with action, result status, reason, actor, time, previous acceptance/item status, current item status, and supersede ids when relevant.
- Supersede requests may send `superseded_by_item_id` to mark the current item superseded, or `supersedes_item_id` to accept the current item while invalidating an old same-scope item.
- List/API payloads expose `acceptance_history`, `superseded_by_item_id`, `supersedes_item_id`, `duplicate_hint`, and `possible_conflicts` from `value_json`/derived same-key hints. Conflict summaries include ids and statuses only, not private content or original text.
- Add policy controls for sensitivity categories and source reliability.
- Add metrics: candidate count, auto-accept rate, review rate, reject reasons, prompt-injected count, and post-accept corrections.

## Migration And Compatibility Strategy

`memory_items`:

- P4a stores acceptance metadata in `value_json.acceptance`; no schema migration required.
- Existing rows with no acceptance metadata are treated as legacy accepted only when `status='active'`, `sensitivity='normal'`, and `deleted_at IS NULL`.
- Later migration may add indexed columns: `acceptance_status`, `acceptance_score`, `extraction_confidence`, `accepted_at`, `reviewed_at`, `reviewed_by`.

`graph_facts` and `episodes`:

- Graph display may include pending or review artifacts in admin views, but default graph retrieval must only return active accepted backing items.
- When a memory item is superseded, expired, invalidated, or rejected, linked facts/episodes must stop being active for prompt use.

`episodes`:

- Episodic memory should default lower durability and actionability.
- Session summaries and episodes are review/display aids unless explicitly accepted as durable memory.

Backfill:

- Backfilled candidates get lower source reliability than explicit current-turn statements.
- Backfill should favor `candidate` or `needs_review` unless repeated evidence supports acceptance.
- Legacy rows with absent `value_json.acceptance` remain compatible with current prompt eligibility when they are active, normal-sensitivity, and not deleted. This compatibility is intentional and should not be changed during audit work.
- Operators should first use the legacy acceptance audit/stats APIs to measure missing metadata by scope/status/type and inspect ID previews only; these APIs must not expose memory content, `original_text`, or private message bodies.
- Conservative API backfill may add missing acceptance metadata as `needs_review` or `candidate` with an `acceptance.history` entry that records `admin_backfill` and a `legacy_acceptance_backfill` reason. Non-API jobs may record `backfill`. It must not overwrite existing `accepted`, `rejected`, `needs_review`, `candidate`, `superseded`, or `expired` metadata.
- There must be no automatic bulk migration that marks legacy absent acceptance as `accepted`; accepted migration requires explicit review or stronger evidence.

## Phased Implementation Plan

P4a minimal foundation:

- Add `docs/memory-acceptance-model.md`.
- Add metadata-only acceptance fields under `value_json.acceptance`.
- Add `extraction_confidence` to LLM extraction actions.
- Compute acceptance status/score/reason/signals for structured memory actions.
- Map accepted to existing `active`; candidate/review/rejected to existing `pending`.
- Preserve current default retrieval/prompt gates.
- Display acceptance status/score/reason in MemoryPage when present.
- Add unit tests for joke/uncertain not auto accepted, clear preference auto accepted, and pending/review excluded by current retrieval gates.

P4b review workflow:

- Add explicit API actions: Accept, Reject, Mark joke, Expire, Supersede.
- Add review filters and counts.
- Add audit metadata without exposing raw private messages.
- Sync status changes to graph/vector/cache.

P4c hardening:

- Add dedicated indexed acceptance columns through a compatible migration.
- Add evidence table or normalized evidence references.
- Add contradiction detection across accepted memories.
- Add retention/expiration jobs.
- Add metrics and operator dashboards.

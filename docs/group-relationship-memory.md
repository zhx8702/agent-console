# Group Relationship Memory / Relationship Graph

## Background And Goal

Group Relationship Memory builds a relationship graph for a specified group-capable session from daily chat records. The goal is to gradually enrich observable connections among people, groups, topics, projects, tools, events, tasks, and artifacts, then present those connections in a Relationship Graph / 群聊关系图 for exploration and review.

This feature is not a social judgment engine. It is a memory and visualization layer for operational understanding:

- Who interacted with whom through observable message behavior.
- Which people repeatedly participate in the same topics, projects, tasks, events, and artifacts.
- Which questions, answers, requests, fixes, tests, and resource-sharing patterns appear over time.
- Which candidate semantic relationships may be useful after review and acceptance.

Daily chat records should be processed incrementally. Each day adds evidence, updates counts, refreshes `last_seen`, and may raise confidence when signals are consistent across days. Weak one-day signals should remain candidates or `needs_review` until stronger evidence or human review supports acceptance.

## Architecture Placement

This is a memory plugin aggregation and visualization capability. It is not wxbot-specific and must not be implemented as a wxbot-only feature. Source plugins such as wxbot, Discord, Slack, or future chat/import providers should be treated as upstream evidence sources, while the memory plugin owns graph extraction, aggregation, acceptance, evidence projection, and read-only graph APIs.

Suggested capability names:

- `memory.group_relationship_graph`
- `memory.visualization.group_graph`

Capability classification:

- Category: visualization + memory aggregation.
- Scope type: `session` and group-capable session.
- Scope identifiers: `tenant_id`, `channel`, `source_key`, and `session_id`.

The feature may be presented in product copy as a group relationship graph because the first useful workflow is group chat exploration. The underlying capability, APIs, storage keys, and frontend data contract should remain provider-neutral and session-scoped rather than hardcoded to wxbot, chatrooms, or any single source plugin.

## Ownership Boundaries

Provider/source plugins own ingestion and source-specific access only:

- Raw messages and source event ids.
- Roster/member snapshots and source metadata.
- Provider-specific delivery, cursoring, import, and sync behavior.

The memory plugin owns:

- Extraction of relationship candidates from allowed message metadata and derived features.
- Aggregation, merge/upsert, confidence scoring, and acceptance state.
- Evidence references, evidence counts, sanitized summaries, and review metadata.
- Read-only Relationship Graph APIs used by the frontend.
- Privacy filtering for graph payloads and evidence payloads.

The frontend Relationship Graph page owns visualization and review-facing interaction. It must call the memory plugin graph APIs, not wxbot roster APIs or provider raw-message APIs. Provider-specific identifiers may appear only as scoped metadata when authorized and should not be required for rendering the graph.

## Principles

### Observable Behavior First

The first layer of the graph should come from directly observable behavior:

- Mentions, including direct `@` references and explicit named references when available.
- Replies, threads, quotes, or platform-provided parent message links.
- Shared participation in the same topic, project, task, event, artifact, or time-bounded discussion.
- Question and answer pairs.
- Task collaboration, such as a request, acknowledgement, implementation, fix, test, or follow-up.
- Resource exchange, such as a link, document, tool, issue id, pull request id, or artifact shared in context.

These interaction edges should be extracted by rules and statistics first. They must not depend on an LLM to decide that an interaction happened when platform metadata or deterministic parsing can identify it.

### LLMs Propose, Policy Decides

LLMs may propose candidate semantic edges, labels, summaries, and normalizations. They are not the factual judge.

Examples of LLM-assisted candidates:

- Normalizing repeated phrases into a `topic` node.
- Suggesting that a set of messages are about the same `project`.
- Suggesting that a person is interested in a topic based on repeated explicit participation.
- Summarizing evidence for review without exposing raw chat text by default.

The deterministic acceptance policy, review workflow, and evidence model decide whether a candidate becomes accepted memory. This follows the separation in [Memory Acceptance Model](memory-acceptance-model.md): extraction confidence is not acceptance, extracted/displayed does not mean accepted, and graph display does not mean prompt use.

### No Unsafe Auto-Inference

The system must not automatically infer sensitive, subjective, or high-risk relationships unless there is explicit evidence and review. The default behavior is `needs_review` or reject.

Do not auto-infer:

- Friendship, romantic relationships, hostility, trust, social closeness, loyalty, status, or subjective sentiment.
- Organizational hierarchy, authority, employment status, or reporting lines unless explicitly represented in trusted source data or clearly stated and reviewed.
- Political views, religion, health, legal, financial status, protected attributes, or other sensitive categories.
- Private life details, identity traits, or intent beyond observable work/chat behavior.

Even when explicit evidence exists, these categories require a policy check and review before being accepted or exposed beyond an authorized review surface.

### Display Is Not Prompt Use

Graph display and prompt retrieval are separate decisions.

The Relationship Graph may show candidates and `needs_review` edges for debugging and review when clearly labeled. Runtime prompt use must only include accepted, active, normal-sensitivity memory according to the acceptance model:

- `acceptance.status = accepted`
- active backing item/fact where applicable
- normal sensitivity
- not deleted, expired, superseded, rejected, or pending review

No edge should affect prompts merely because it appears in the Relationship Graph.

### Evidence First

Every node and edge must be traceable to evidence. Each relation should include evidence ids, episode ids, message/event counts, and extraction metadata.

The normal API/UI surface must not expose raw chat message content. It should expose compact evidence references and summaries:

- Evidence ids or source event ids.
- Episode ids or daily window ids.
- Source message count.
- First and last seen timestamps.
- Short sanitized summaries when available.
- Extraction method and review/acceptance state.

## Data Model

The graph should support typed nodes and typed edges with metadata sufficient for review, filtering, confidence scoring, and prompt gating.

### Node Types

Initial node types:

- `person`: a group participant, represented by stable internal member id and display nickname when allowed.
- `group`: the source group chat, channel, room, or tenant-scoped conversation.
- `topic`: a normalized subject of discussion.
- `project`: a named project, product area, repo, customer engagement, or ongoing initiative.
- `tool`: a tool, service, plugin, API, platform, dependency, or operational system.
- `event`: a time-bounded incident, meeting, release, outage, migration, decision, or milestone.
- `task`: a requested or assigned unit of work.
- `artifact`: a document, issue, PR, commit, file, URL, image, report, dataset, or shared resource.

Future node types may include `decision`, `question`, `location`, or `organization`, but sensitive and identity-heavy types should require explicit product and policy review.

Recommended node fields:

```json
{
  "id": "node_person_123",
  "type": "person",
  "label": "Display nickname",
  "canonical_key": "tenant_id:source_key:channel:session_id:member-id",
  "tenant_id": "tenant_id",
  "source_key": "chat_provider",
  "channel": "group_or_channel_id",
  "session_id": "session_or_import_scope",
  "confidence": 0.92,
  "acceptance_status": "accepted",
  "evidence_count": 17,
  "first_seen": "2026-05-01T00:00:00Z",
  "last_seen": "2026-05-15T00:00:00Z",
  "review_state": "auto_accepted",
  "metadata": {}
}
```

`label` must be a display-safe value. The API should avoid exposing bulk profile fields, avatars, private identifiers, phone numbers, emails, or account metadata unless explicitly required and authorized.

### Edge Types

Initial interaction edges:

- `mentioned`: person or artifact was mentioned by a person or in a group.
- `replied_to`: one person replied to another person or message-linked participant.
- `asked`: a person asked another person, group, or topic-scoped question.
- `answered`: a person answered a question or responded to a requester.
- `co_participated`: people participated in the same bounded topic, event, task, or thread.
- `requested`: a person requested a task, answer, review, resource, or action.
- `provided_resource`: a person provided a link, document, artifact, command, file, reference, or other resource.

Initial semantic/work edges:

- `collaborated_with`: people repeatedly worked on the same task, event, project, artifact, or issue.
- `works_on`: a person is observed working on a project, task, tool, or artifact.
- `interested_in`: a person repeatedly participates in or explicitly asks about a topic.
- `maintains`: a person is explicitly or repeatedly associated with upkeep of a tool, project, or artifact.
- `reported_issue`: a person reported an issue, defect, incident, or problem.
- `fixed_issue`: a person fixed or claimed to fix an issue.
- `tested`: a person tested a fix, feature, workflow, artifact, or system.

Sensitive or subjective edge types such as `friend_of`, `hostile_to`, `manager_of`, `politically_aligned_with`, or `health_related_to` should not be part of the default extraction set.

### Edge Metadata

Every edge should include:

```json
{
  "id": "edge_abc123",
  "from": "node_person_123",
  "to": "node_project_456",
  "type": "works_on",
  "confidence": 0.81,
  "acceptance_status": "needs_review",
  "evidence_count": 5,
  "first_seen": "2026-05-08T00:00:00Z",
  "last_seen": "2026-05-15T00:00:00Z",
  "source_event_ids": ["evt_1", "evt_2"],
  "source_episode_ids": ["episode_2026_05_15_group_1"],
  "source_message_count": 9,
  "extraction_method": "stat",
  "review_state": "unreviewed",
  "acceptance": {
    "status": "needs_review",
    "score": 0.67,
    "reason": "multi_day_signal_below_auto_accept_threshold",
    "extraction_confidence": 0.74,
    "recommendation": "needs_review"
  },
  "history": []
}
```

Required edge metadata:

- `confidence`: graph confidence for display and ranking.
- `acceptance_status`: canonical state from the acceptance model, such as `candidate`, `needs_review`, `accepted`, `rejected`, `superseded`, or `expired`.
- `evidence_count`: number of distinct evidence units supporting the relation.
- `first_seen`: first timestamp/window in which the relation was observed.
- `last_seen`: latest timestamp/window in which the relation was observed.
- `source_event_ids`: source event ids used as evidence references.
- `source_message_count`: count of source messages contributing to the edge.
- `extraction_method`: one of `rule`, `stat`, `llm`, or a combined value such as `rule+stat` or `stat+llm`.
- `review_state`: review workflow state, such as `unreviewed`, `auto_accepted`, `human_accepted`, `human_rejected`, `expired`, or `superseded`.

## Extraction And Aggregation

### Daily Pipeline

The daily incremental pipeline:

1. Select daily window for `tenant_id`, `source_key`, `channel`, and optional `session_id`.
2. Load message metadata and allowed derived text features for that window.
3. Run deterministic extraction for observable interaction edges.
4. Run statistical aggregation for co-participation, repeated topic sharing, message counts, and recency.
5. Run LLM extraction only for candidate semantic edges, summaries, topic normalization, and review hints.
6. Merge/upsert nodes and edges by canonical keys.
7. Update evidence counts, source ids, confidence, `first_seen`, and `last_seen`.
8. Apply acceptance scoring and gating.
9. Queue low-confidence, sensitive, contradictory, or LLM-only edges for review.
10. Publish read-only graph data for the Relationship Graph.

### Rule And Statistical Extraction

Rule/stat extraction should handle:

- `mentioned`: platform mention metadata and safe parsed references.
- `replied_to`: reply/thread/quote metadata.
- `asked` and `answered`: question markers, reply links, direct mentions, and adjacency patterns.
- `co_participated`: participants in the same thread, topic cluster, event window, or task window.
- `requested`: request/action verbs plus recipient or group context.
- `provided_resource`: URLs, artifact ids, issue ids, PR ids, commands, file references, or attachments.

Rule/stat results can become accepted when evidence is direct, non-sensitive, repeated or explicit, and above the acceptance threshold. For example, a platform reply edge with many occurrences is a direct observable relation. A single adjacent message that might be an answer is weaker and should remain `candidate` or `needs_review`.

### LLM Candidate Extraction

LLMs may be used for:

- Topic and project normalization.
- Candidate `works_on`, `interested_in`, `collaborated_with`, `maintains`, `reported_issue`, `fixed_issue`, and `tested` edges.
- Evidence summaries for review.
- Contradiction or uncertainty hints.
- Sensitivity risk hints.

LLM output must include extraction confidence and evidence references. It must not be accepted solely because the LLM is confident. The final acceptance state is assigned by deterministic policy and review.

### Merge And Upsert

Nodes and edges should have stable canonical keys:

- Person: `tenant_id` + `source_key` + `channel` + `session_id` + stable member id.
- Group: `tenant_id` + `source_key` + `channel` + `session_id`.
- Topic: `tenant_id` + `source_key` + `channel` + `session_id` + normalized topic key.
- Project/tool/artifact: `tenant_id` + normalized key and scoped source references.
- Edge: `tenant_id` + `source_key` + `channel` + `session_id` + from canonical key + edge type + to canonical key.

Merging should:

- Preserve `first_seen`.
- Set `last_seen` to the latest evidence timestamp.
- Increment `evidence_count` and `source_message_count`.
- Append bounded evidence ids, with retention limits for very high-volume edges.
- Recompute confidence and acceptance score.
- Preserve human review decisions unless new evidence requires a policy transition such as reopening a rejected edge to `needs_review`.

### Multi-Day Accumulation

Repeated evidence across days should raise confidence more than repeated evidence within one burst. Confidence should grow when signals are:

- Direct.
- Repeated across multiple daily windows.
- Consistent with existing accepted graph edges.
- Supported by multiple evidence types, such as reply metadata plus task references.
- Recent enough to remain useful.

Single-day weak signals default to `candidate` or `needs_review`, especially when LLM-derived or semantically ambiguous.

## Confidence And Acceptance

This feature should reuse [Memory Acceptance Model](memory-acceptance-model.md) rather than creating a parallel lifecycle.

### Confidence Composition

Graph edge `confidence` should be derived from:

- `evidence_count`: more independent evidence increases confidence with diminishing returns.
- `recency`: recent observations increase active relevance; stale edges gradually fade.
- `consistency`: agreement with existing accepted nodes/edges and absence of contradiction.
- `source_reliability`: platform metadata and manual review score higher than weak inference or backfill.
- `llm_confidence`: only used as one signal for LLM-proposed semantic candidates.
- `evidence_diversity`: multiple evidence kinds or multiple days score higher than one burst.
- `sensitivity_risk`: sensitive or subjective categories lower confidence for automatic acceptance and force review.

Suggested display confidence:

```text
base =
  evidence_strength * 0.30 +
  evidence_diversity * 0.15 +
  consistency * 0.15 +
  recency * 0.15 +
  source_reliability * 0.15 +
  extraction_confidence * 0.10

confidence = clamp(base - sensitivity_penalty - contradiction_penalty - uncertainty_penalty, 0, 1)
```

This display confidence is related to but not identical to `acceptance_score`. `acceptance_score` decides whether the relation can become accepted memory and follow prompt-use rules.

### Acceptance Policy

Acceptance states should match the canonical model:

- `candidate`: extracted but not yet accepted.
- `needs_review`: potentially useful but requires review before prompt use.
- `accepted`: approved for normal graph use and, only when otherwise eligible, prompt retrieval.
- `rejected`: not memory, unsafe, too uncertain, too sensitive, or otherwise inappropriate.
- `superseded`: replaced by newer accepted relation.
- `expired`: no longer current or beyond TTL/time-bound validity.

Initial thresholds should follow the acceptance model:

- Auto accept when `acceptance_score >= 0.78`, sensitivity is normal, evidence is direct or repeated, and there is no strong joke, uncertainty, contradiction, or subjective/sensitive inference.
- `needs_review` when `0.35 <= acceptance_score < 0.78`, when the LLM recommends review, when the edge is semantic/ambiguous, or when policy requires review.
- Reject when `acceptance_score < 0.35`, when evidence is non-factual, joke-like, unsafe, contradictory, or disallowed.

A single weak evidence item must not auto-accept. LLM-only semantic edges should default to `candidate` or `needs_review` unless backed by explicit, repeated, non-sensitive evidence and allowed by policy.

## Privacy And Safety

### No Raw Chat Text By Default

Normal graph APIs and Relationship Graph views must not return raw chat message content. They should return:

- Evidence ids.
- Episode ids.
- Source message counts.
- Time windows.
- Sanitized summaries.
- Aggregated counts.
- Acceptance and review metadata.

Raw source text may exist in protected storage for authorized audit/debug workflows, but it should require explicit permission and must not be used in the default Relationship Graph API.

### Member Identity Display

Person nodes should use a minimal display strategy:

- Internal stable id for joins and deduplication.
- Display nickname or group-safe alias for UI labels.
- Optional disambiguator only when needed, such as short member id suffix.
- No bulk avatar exposure by default.
- No phone, email, external profile, private account id, or hidden metadata in standard payloads.

When a member leaves a group, visibility rules should be defined by product policy. The graph may retain historical aggregate edges while avoiding unnecessary profile exposure.

### Sensitive Relations

Sensitive categories should be rejected, hidden, or review-gated by default:

- Health, legal, financial, political, religious, protected class, private identity, family, romance, hostility, and subjective psychological claims.
- Secrets, credentials, tokens, payment information, addresses, or private contact data.
- Hierarchy and employment claims unless sourced from trusted directory data or explicit reviewed evidence.

The Relationship Graph should visually distinguish review-only sensitive candidates and avoid exposing them to normal users unless the tenant policy allows it.

## API Draft

The API is owned by the memory plugin. Endpoint names may keep `group-graph` for user-facing clarity, but request/response fields must use provider-neutral scope identifiers: `tenant_id`, `channel`, `source_key`, and `session_id`.

### `GET /plugins/memory/group-graph`

Returns a read-only relationship graph for standalone Relationship Graph and review surfaces.

Query filters:

- `tenant_id`: tenant id or implicit tenant context.
- `channel`: group/channel id.
- `source_key`: chat provider or source system.
- `session_id`: optional session or import scope.
- `from`: inclusive start timestamp/date.
- `to`: exclusive end timestamp/date.
- `relation_type`: one or more edge types.
- `node_type`: one or more node types.
- `acceptance_status`: one or more acceptance states.
- `min_confidence`: minimum confidence threshold.
- `limit`: maximum edge count, with server-side caps.

Example request:

```http
GET /plugins/memory/group-graph?tenant_id=t1&source_key=chat_provider&channel=c1&session_id=s1&from=2026-05-01&to=2026-05-15&acceptance_status=accepted,needs_review&min_confidence=0.45&limit=500
```

Example response:

```json
{
  "schema": {
    "version": "group-graph.v1",
    "node_types": ["person", "group", "topic", "project", "tool", "event", "task", "artifact"],
    "edge_types": ["mentioned", "replied_to", "asked", "answered", "co_participated", "collaborated_with", "works_on", "interested_in", "maintains", "reported_issue", "fixed_issue", "tested", "requested", "provided_resource"]
  },
  "filters": {
    "tenant_id": "t1",
    "source_key": "chat_provider",
    "channel": "c1",
    "session_id": "s1",
    "from": "2026-05-01",
    "to": "2026-05-15",
    "acceptance_status": ["accepted", "needs_review"],
    "min_confidence": 0.45,
    "limit": 500
  },
  "nodes": [
    {
      "id": "node_person_123",
      "type": "person",
      "label": "Display nickname",
      "confidence": 1.0,
      "acceptance_status": "accepted",
      "evidence_count": 24,
      "first_seen": "2026-05-01T00:00:00Z",
      "last_seen": "2026-05-15T00:00:00Z"
    }
  ],
  "edges": [
    {
      "id": "edge_abc123",
      "from": "node_person_123",
      "to": "node_topic_456",
      "type": "interested_in",
      "confidence": 0.72,
      "acceptance_status": "needs_review",
      "evidence_count": 6,
      "source_message_count": 11,
      "first_seen": "2026-05-06T00:00:00Z",
      "last_seen": "2026-05-15T00:00:00Z",
      "extraction_method": "stat+llm",
      "review_state": "unreviewed"
    }
  ],
  "page": {
    "limit": 500,
    "next_cursor": null
  }
}
```

Default response behavior:

- Exclude raw chat text.
- Do not return raw message content, raw roster payloads, or provider private profile fields.
- Return only nodes, edges, evidence ids, source event ids, counts, time windows, sanitized labels, sanitized summaries, and review/acceptance metadata.
- Include accepted edges by default for normal users.
- Include candidates and `needs_review` only in review/debug modes with explicit filters and permissions.
- Enforce server-side limits to prevent large privacy-heavy exports.
- Do not proxy wxbot, Discord, Slack, or other source-plugin raw/roster APIs through this endpoint.

### `GET /plugins/memory/group-graph/evidence/{edge_id}`

Returns evidence references and safe summaries for a specific edge. It must not return raw message text by default.

Query options:

- `include_raw=false` by default and only allowed for privileged audit flows.
- `limit`: maximum evidence references.

Example response:

```json
{
  "edge_id": "edge_abc123",
  "evidence": [
    {
      "source_event_id": "evt_1",
      "source_episode_id": "episode_2026_05_15_group_1",
      "window": {
        "from": "2026-05-15T00:00:00Z",
        "to": "2026-05-16T00:00:00Z"
      },
      "summary": "Sanitized evidence summary without raw message text.",
      "message_count": 3,
      "extraction_method": "rule"
    }
  ],
  "totals": {
    "evidence_count": 6,
    "source_message_count": 11
  }
}
```

## Relationship Graph UI

The frontend should expose this as a standalone Relationship Graph / 群聊关系图 page, not as a tab embedded inside `MemoryPage`. Suggested routes:

- `/memory/relationships`
- `/relationships`

The page name can be group-oriented for users, but the frontend data model should stay generic and call only memory plugin graph APIs. It must not call wxbot roster endpoints, wxbot raw-message endpoints, or other provider-specific APIs to render the graph.

The Relationship Graph page should provide three modes.

### Overview Mode

Overview mode is the default read-only graph:

- Shows accepted, active, normal-sensitivity relations by default.
- Uses a limited time range such as last 7 or 30 days.
- Highlights strongest relationship clusters and high-evidence edges.
- Avoids review-only or sensitive candidates unless the user switches mode with permission.

### Explore Mode

Explore mode helps users inspect relationships:

- Left panel: filters for time range, node type, relation type, acceptance status, confidence, `source_key`, and project/topic.
- Center: graph canvas with pan, zoom, fit-to-view, node drag, and search.
- Right panel: selected node/edge inspector.
- Time slider/range filter to compare daily, weekly, or custom windows.
- Search by person nickname, topic, project, task, tool, or artifact label.

### Review Mode

Review mode is for candidates and `needs_review` edges:

- Shows candidate, `needs_review`, rejected, expired, and superseded states when authorized.
- Provides accept/reject actions in later phases.
- Shows evidence ids, counts, confidence breakdown, source methods, and history.
- Labels LLM-proposed edges clearly.
- Keeps raw chat text hidden by default.

### Layout And Visual Encoding

The MVP can use SVG/HTML with a simple force-ish layout. A heavy graph library is not required initially.

Recommended visual encoding:

- Node color by type:
  - person
  - group
  - topic
  - project
  - tool
  - event
  - task
  - artifact
- Edge thickness by `evidence_count`.
- Edge opacity by confidence and recency.
- Dashed edges for `candidate` and `needs_review`.
- Muted edges for low confidence or stale relations.
- Badges or icons for extraction method: `rule`, `stat`, `llm`, or combined.
- Warning/review marker for sensitive or policy-gated candidates, without revealing sensitive content in the label.

Interaction requirements:

- Click node: show type, display label, counts, connected edge summary, first/last seen, acceptance distribution, and history.
- Click edge: show relation type, confidence, acceptance status, evidence count, source message count, first/last seen, extraction method, review state, and evidence references.
- Click evidence link: open evidence summary panel, not raw text by default.
- Time range change: update visible nodes/edges and stale markers.
- Filter change: preserve selection when possible; otherwise clear inspector.

### Plugin Capability And Navigation Draft

This page should be exposed through the plugin-style system as a memory visualization capability rather than as a wxbot page. The existing architecture uses `config/plugin-marketplace.yaml` capability declarations, `PluginRegistry` discovery, startup-mounted plugin routes, and static frontend navigation in `frontend/src/App.tsx`. The first implementation can follow that shape without requiring a full dynamic frontend route registry.

Suggested manifest/capability shape:

```yaml
name: memory
capabilities:
  routes:
    - /plugins/memory
  visualizations:
    - id: memory.group_relationship_graph
      name: Relationship Graph
      path: /memory/relationships
      scope_type: session/group-capable session
      categories: [memory_aggregation, visualization]
      required_api:
        - GET /plugins/memory/group-graph
        - GET /plugins/memory/group-graph/evidence/{edge_id}
```

Navigation requirements:

- Register the page under the memory plugin's capability metadata when plugin UI registration exists.
- Until dynamic frontend route registration exists, add a standalone frontend route such as `/memory/relationships` or `/relationships` and a navigation item labeled `Relationship Graph` / `群聊关系图`.
- Keep the existing `MemoryPage` focused on user/session memory profiles and facts. The graph page may link back to memory records, but it should not be rendered inside `MemoryPage`.
- Hide or disable the navigation item if the memory plugin is not installed/enabled, following the existing plugin state/manifest approach where practical.
- Use the memory plugin API as the sole backend contract for the visualization.

## Phased Plan

### P5a: Specification

Create this specification and align terminology with the acceptance model. No complex implementation.

### P5b: Read-Only Aggregation API

Build a read-only `group-graph` API from existing graph, facts, episodes, and memory items where available.

Scope:

- Derive nodes and edges from existing accepted graph/fact/episode data.
- Include acceptance status and confidence metadata where present.
- Exclude raw chat text.
- Exclude all raw text/content payloads, including raw message text, raw roster records, source profile fields, and raw provider metadata.
- Return only graph nodes, graph edges, evidence ids, source event ids, counts, time windows, sanitized labels, sanitized summaries, confidence, acceptance state, and review metadata.
- Add evidence id/count fields where available.
- Support core filters: `tenant_id`, `channel`, `source_key`, `session_id`, time range, relation type, node type, acceptance status, min confidence, and limit.
- Keep provider/source-plugin APIs behind the memory aggregation boundary; the frontend must not call source roster/raw-message APIs for this page.

### P5c: Relationship Graph MVP Read-Only

Add Relationship Graph UI with:

- Overview and Explore modes.
- Left filters, center SVG/HTML graph, right inspector.
- Visual encodings for type, confidence, evidence count, recency, and acceptance status.
- Evidence summary view without raw text.

### P5d: Daily Extraction Job

Add the daily incremental extraction pipeline:

- Daily window selection.
- Rule/stat extraction for observable edges.
- LLM candidate extraction for semantic edges only.
- Merge/upsert logic.
- Confidence and acceptance scoring.
- Review queue generation.

### P5e: Edge Acceptance Review

Add review workflows:

- Review filters and queues.
- Accept/reject/expire/supersede actions.
- Acceptance history.
- Prompt-use gating updates.
- Audit entries.

### P5f: Advanced Layout And Timeline

Improve the Relationship Graph:

- Better layout stability.
- Timeline comparison.
- Cluster views.
- Edge bundling or aggregation for dense groups.
- Historical playback.
- More detailed confidence explanations.

## Testing Plan

### Unit Tests

- Canonical key generation for nodes and edges.
- Edge merge/upsert preserves `first_seen`, updates `last_seen`, increments counts, and bounds evidence references.
- Confidence composition handles evidence count, recency, consistency, source reliability, LLM confidence, sensitivity, uncertainty, and contradiction.
- Acceptance mapping follows the canonical state model.
- Single weak evidence defaults to `candidate` or `needs_review`.
- LLM-only semantic edge does not auto-accept without policy-allowed evidence.

### API Tests

- `GET /plugins/memory/group-graph` returns schema, nodes, edges, filters, and pagination metadata.
- Filters work for `tenant_id`, `channel`, `source_key`, `session_id`, time range, relation type, node type, acceptance status, confidence, and limit.
- Default API excludes candidates and `needs_review` for normal users unless explicitly authorized.
- Evidence endpoint returns ids/counts/summaries, not raw text by default.
- Server-side limits prevent oversized exports.

### UI Tests

- Overview renders accepted graph with node colors, edge thickness, stale/low-confidence styling, and no raw source text.
- Explore filters update the canvas and inspector.
- Clicking a node shows counts, connected edges, acceptance distribution, and first/last seen.
- Clicking an edge shows relation metadata, confidence, evidence counts, acceptance state, extraction method, and history.
- Review mode labels candidates and `needs_review` edges distinctly.
- Time slider/range filter updates the graph without overlapping or broken labels.

### End-To-End Tests

- Import or seed daily records, run aggregation, fetch graph, and render the Relationship Graph.
- Add a second day of consistent evidence and verify counts, confidence, and `last_seen` increase.
- Verify weak single-day semantic signal remains `needs_review`.
- Verify accepted normal-sensitivity relation can appear in the default Relationship Graph.
- Verify review-only relation does not enter prompt retrieval.

### Privacy Tests

- Group graph endpoint does not include raw message text by default.
- Evidence endpoint does not include raw message text by default.
- Member nodes do not expose avatars, phone numbers, emails, private account ids, or bulk profile fields by default.
- Sensitive relation candidates are not auto-accepted.
- Prompt retrieval excludes `candidate`, `needs_review`, rejected, expired, superseded, sensitive, private, deleted, and pending relations.
- API responses remain safe when evidence summaries are absent.

## Open Questions And Decisions Needed

- What is the stable source identifier for group members across imports, providers, and nickname changes?
- Which existing tables should back P5b: graph facts, episodes, memory items, source events, or a new projection table?
- Should `group-graph` be a live aggregation query, a materialized projection, or both?
- What are the tenant and role permissions for viewing candidates, `needs_review`, and rejected edges?
- What review actions are allowed for group admins versus system admins?
- How long should evidence id lists be retained on high-volume edges before rolling up to counts and windows?
- What TTL or decay should apply to stale relationships by edge type?
- Which semantic edge types are valuable enough for the MVP, and which should wait for review tooling?
- How should deleted messages or privacy requests affect existing evidence ids, counts, and accepted edges?
- What is the default Relationship Graph time range for large groups?
- Should person-person `co_participated` edges be collapsed through topic/task/event nodes to reduce dense graph noise?
- What metrics should define success: review throughput, accepted-edge precision, prompt-safety violations, user engagement, or operator debugging value?

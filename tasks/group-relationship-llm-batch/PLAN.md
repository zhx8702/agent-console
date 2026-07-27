# 群聊关系图 LLM 批处理方案

日期：2026-05-16

## 背景

当前 Relationship Graph 的「运行所选日期 AI 抽取」已经不是全量无边界执行，而是按 `plugin_memory_extraction_job` 小批处理：

```text
历史消息导入 plugin_memory_event
→ 为可抽取 event 创建 plugin_memory_extraction_job
→ run_daily_group_relationship_extraction 按日期/scope claim jobs
→ 对每个 job 调 structured/graph LLM extractor
→ 写 memory item / entity / fact / episode
→ group graph 读取图谱投影
```

现状限制：

- 处理单位约等于 `1 job ≈ 1 条导入事件/消息`。
- 当前前端固定传 `limit=5`。
- 后端 request schema `limit <= 20`，store 内部也 clamp 到 20。
- API 是同步处理：一次请求会真正逐个跑 LLM job；如果一次处理太多，HTTP 等待时间、限流和失败恢复都会变差。
- 已有 daily relationship extractor 是 stats/rule-only MVP；真正图谱关系仍主要来自每条 job 的 LLM graph extractor。

## 目标

让群聊关系图 AI 抽取变成「可控、可恢复、可观察」的批处理，而不是简单把 `limit=5` 改成大数字。

## 设计原则

1. **短期不直接把一整天消息塞进单次 LLM。**
   - 1580 条一天直接拼成一个 prompt 不可靠，token、质量、失败恢复都差。
   - 当前先继续复用 per-event job，保证幂等和失败可重试。

2. **前端给运营控制档位。**
   - 单批处理数量：`20 / 50 / 100`。
   - 默认：`50`。
   - 连续处理当前日期：可选。
   - 连续处理上限：默认 `200`，可配置但后端强制上限。

3. **后端强制安全边界。**
   - `batch_limit` 最大 100。
   - `max_jobs` 最大 500。
   - `time_budget_seconds` 默认 60，最大 180。
   - 到达 job 上限、时间预算、无更多 job、LLM 不可用或错误时停止并返回 stop_reason。

4. **同步 API 只做受控 run，不承诺一次跑完整天。**
   - 对于 1580 条，一次连续 run 处理 200 条是上限，不是默认全量。
   - 如果平均每条 LLM 2s，200 条会非常久，因此 time budget 会提前截断。
   - UI 应明确显示“本轮处理 X 条，仍剩 Y 条”。

5. **后续再做真正的 window/chunk 关系抽取。**
   - Phase 2 可以将 30-80 条消息作为一个 window，LLM 输出 relation deltas + evidence event ids。
   - 当前实现先把 job runner 做好，避免在不稳定基础上改抽取质量模型。

## 推荐参数

### 交互式人工点击

- 默认 batch：50
- 单次模式：最多处理 50
- 连续模式：最多处理 200
- 时间预算：60 秒

### 初次验证某天

- batch：20
- max_jobs：20 或 50
- 适合验证 scope、成本、错误率。

### 稳定后追数据

- batch：50 或 100
- max_jobs：200
- 多次运行，直到 `remaining_ready=0`。

### 不建议

- 不建议一次 500/1000 条同步跑。
- 如果需要全量追平，应后续实现后台异步 task/worker，不走单个 HTTP 请求。

## API 变更

### Request: `/plugins/memory/group-graph/extract-daily`

新增字段，兼容旧 `limit`：

```json
{
  "tenant_id": "default",
  "channel": "wechat",
  "source_key": "wxbot",
  "session_id": "xxx@chatroom",
  "date": "2026-05-11",
  "user_id": "__group__",
  "batch_limit": 50,
  "max_jobs": 50,
  "continuous": false,
  "time_budget_seconds": 60
}
```

兼容规则：

- 如果只传旧 `limit`，等价于 `batch_limit=limit, max_jobs=limit, continuous=false`。
- `continuous=true` 时，默认 `max_jobs=200`。
- `continuous=false` 时，默认 `max_jobs=batch_limit`。

### Response summary

返回安全 metadata，不包含原始聊天：

```json
{
  "ok": true,
  "jobs": {
    "claimed": 50,
    "succeeded": 49,
    "failed": 1,
    "dead": 0,
    "batches": 1
  },
  "controls": {
    "batch_limit": 50,
    "max_jobs": 50,
    "continuous": false,
    "time_budget_seconds": 60,
    "stop_reason": "single_batch_complete"
  },
  "job_counts_before": {"pending": 1530},
  "job_counts_after": {"pending": 1480},
  "more_remain": true
}
```

`stop_reason` 候选：

- `single_batch_complete`
- `max_jobs_reached`
- `time_budget_reached`
- `no_ready_jobs`
- `llm_unavailable`
- `empty_day`

## 前端 UX

RelationshipGraphPage 的「历史与抽取」区域新增：

- 每批处理数量 select：20 / 50 / 100，默认 50。
- 连续处理当前日期 checkbox。
- 本轮最多处理 input：默认 200，仅连续处理时启用；单批时等于 batch。
- 显示估算：`待处理 / batch ≈ 需要点击次数`。
- 运行结果显示：
  - 本轮 claimed/succeeded/failed/dead。
  - stop_reason。
  - before/after job status。
  - more_remain。
  - 仍然提示：不展示原始聊天内容。

## 验收标准

Backend:

- `MemoryDailyRelationshipExtractionRequest` 支持新字段且兼容旧 `limit`。
- `claim_llm_extraction_jobs_for_day` 支持 batch up to 100。
- `run_daily_group_relationship_extraction` 可按 batch/max/time budget 循环 drain。
- 响应包含 `controls`、`jobs.batches`、`stop_reason`。
- 无 LLM 或无 ready jobs 时安全返回，不暴露 raw text。
- focused tests 覆盖：旧 limit 兼容、single batch、continuous max、time budget/no jobs、router 传参。

Frontend:

- 可选择 20/50/100。
- 可开启连续处理并设置本轮 max_jobs。
- 默认 batch=50，continuous=false。
- 输出摘要展示 controls/job counts。
- build 通过。

## 后续 Phase 2：真正的 window 关系抽取

当 runner 稳定后，再设计 dedicated `group_relationship_window_job`：

- 按日期把消息分成 window：30-80 条或 token cap 6k-10k。
- 单个 LLM 请求输出 relation candidates：subject/object/predicate/confidence/evidence_event_ids。
- merge/deduplicate 后进入 edge review。
- 优点：比逐条消息更适合关系理解，成本更低。
- 风险：需要重新设计 prompt、合并策略、冲突处理和质量评估。

本次先实现 Phase 1：受控批处理 runner + 前端控制。

## Phase 2 Detailed Implementation Plan — Window/Chunk Relationship Extraction

### Objective

Move beyond per-message LLM jobs by adding a **window-level extraction path** for group relationship graphs. A window is a bounded slice of imported group events for one date/session, usually 30-80 messages or a token/character budget. The LLM sees a compact sender-prefixed transcript and returns structured relation candidates with evidence event ids.

This does **not** replace Phase 1 immediately. It complements it:

- Phase 1: safe per-event job drain, good for compatibility and retry.
- Phase 2: window semantic extraction, better relationship quality and lower overhead for group chats.

### API Design

Add admin-only endpoint:

`POST /plugins/memory/group-graph/extract-window`

Request:

```json
{
  "tenant_id": "default",
  "channel": "wechat",
  "source_key": "wxbot",
  "session_id": "xxx@chatroom",
  "date": "2026-05-11",
  "user_id": "__group__",
  "window_size": 50,
  "max_windows": 3,
  "cursor_event_id": 0,
  "dry_run": false
}
```

Controls:

- `window_size`: default 50, clamp 10..100.
- `max_windows`: default 1, clamp 1..10.
- `cursor_event_id`: optional; process events with id greater than cursor inside date window.
- `dry_run`: if true, build windows and return safe metadata without writing graph items.

Response must be safe metadata only:

```json
{
  "ok": true,
  "status": "completed|partial|skipped",
  "scope": {"tenant_id":"...", "session_id":"...", "user_id_scope":"__group__"},
  "date": "2026-05-11",
  "controls": {"window_size":50, "max_windows":3, "dry_run":false},
  "windows": [
    {
      "index": 1,
      "event_count": 50,
      "first_event_id": 100,
      "last_event_id": 149,
      "sender_count": 12,
      "candidate_count": 6,
      "applied_count": 4,
      "skipped_count": 2
    }
  ],
  "totals": {"events":150, "windows":3, "candidates":18, "applied":12, "skipped":6},
  "next_cursor_event_id": 149,
  "more_remain": true,
  "generated_from": ["plugin_memory_event", "llm_window_extractor"]
}
```

### LLM Input Safety

The LLM needs message text to infer relationships, but API/front-end output must not return raw text. The window prompt may contain bounded, sender-prefixed lines from `plugin_memory_event.user_text` because this is an internal admin-triggered extraction step.

Safety constraints:

- Truncate each message line, e.g. 500 chars.
- Cap whole window prompt, e.g. 12k chars.
- Use event ids and sender ids; do not ask the LLM to output full message text.
- Store relation evidence as event ids and sanitized summaries only.
- Response scrubber must remove raw fields.

### Candidate Schema

LLM should return JSON with candidates:

```json
{
  "relations": [
    {
      "subject": "wxid_a",
      "subject_type": "person",
      "predicate": "asked",
      "object": "wxid_b",
      "object_type": "person",
      "confidence": 0.72,
      "evidence_event_ids": [101, 104],
      "reason": "brief safe reason, no raw quote"
    }
  ]
}
```

Allowed predicates should initially reuse existing group graph edge types:

- `mentioned`, `replied_to`, `asked`, `answered`, `co_participated`, `requested`, `provided_resource`, `collaborated_with`, `works_on`, `interested_in`, `maintains`, `reported_issue`, `fixed_issue`, `tested`

### Persistence Strategy

Keep this first implementation low-risk by writing candidates through existing memory item → graph sync path:

- Create/update `plugin_memory_item` with:
  - `source_type="llm_group_window"`
  - `memory_type="note"`
  - `scope_type="session"`
  - `user_id="__group__"`
  - `normalized_key = group-window-rel:<date>:<predicate>:<subject>:<object>:<evidence-hash>`
  - `content = "Group window relation: <subject> <predicate> <object>"`
  - `value_json` contains candidate metadata and evidence event ids, no raw text.
  - `status="pending"`, `acceptance_status="needs_review"` or candidate via acceptance policy.
- Then call existing graph sync safe path. If graph sync does not understand this memory type well enough, add a small mapping for `llm_group_window` relation items.

### Idempotency

A repeated run over the same window/candidate must not duplicate edges/items:

- Candidate normalized key includes date, predicate, subject, object, and sorted evidence ids hash.
- Running the same date/window twice touches existing item.
- `next_cursor_event_id` allows manual paging.

### Frontend UX

Add a second action area in RelationshipGraphPage:

- Button: `运行窗口关系抽取`
- Controls:
  - window size: 30 / 50 / 80
  - max windows: 1 / 3 / 5
  - dry run checkbox
- Output:
  - windows processed
  - candidates/applied/skipped
  - next cursor
  - more_remain
  - no raw chat text note

### Acceptance Criteria

Backend:

- Admin-only route exists.
- Store method builds windows from imported events for date/session/user scope.
- Dry run returns window metadata and does not write items.
- With fake LLM output, candidates create/touch memory items with evidence ids and no raw text in response.
- Repeated run is idempotent.
- Unsupported/no LLM returns safe skipped status.
- Tests cover dry_run, fake LLM apply, idempotency, router admin guard, response scrub.

Frontend:

- Controls for window_size/max_windows/dry_run.
- Calls new endpoint.
- Output safe metadata only.
- Build passes.

### Non-goals For This Batch

- No fully automated background all-day worker.
- No complex multi-day confidence accumulation yet.
- No raw transcript display.
- No replacing existing per-event job runner.

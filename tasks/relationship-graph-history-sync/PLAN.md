# Relationship Graph Raw Messages Date Sync Plan

## Goal
Refine the Relationship Graph manual sync feature so it matches the Raw Messages workflow: select one specific date for a group chat, show whether that date has already been extracted/imported, and sync only that date.

## User Feedback
- Data source should align with Raw Messages.
- Remove `max_messages_per_session` from the graph sync UI.
- No time range selection; choose a specific date.
- Dates already extracted for the group should have a visible marker.
- Operator wants to process previous days one by one and observe graph increments.

## Current Context
- Raw Messages page uses wxbot report messages endpoint: `GET /plugins/wxbot/admin/reports/messages/{session_id}`.
- Memory backfill currently supports `days_limit` and `max_messages_per_session`, collecting from SDK message table.
- Relationship Graph sync button currently calls `/plugins/memory/backfill` with `days_limit` and max messages.

## Target Design
Backend:
- Extend memory backfill to support `target_date` (`YYYY-MM-DD`) for a single calendar day.
- When `target_date` is set, collect all text messages in that local day for the target session/user/group member instead of a rolling range/limit.
- Add a safe date-status endpoint for Relationship Graph UI, e.g. `/plugins/memory/group-graph/history-dates`, returning recent date rows with raw message count, imported count, and status (`not_extracted`, `partial`, `extracted`).
- Keep admin/privacy boundaries; do not return raw message content in date-status response.

Frontend:
- Replace `days_limit` + `max_messages_per_session` controls on RelationshipGraphPage with a specific date selector/list.
- Show status badges for dates already extracted/partial/not extracted.
- Sync button sends `target_date` and current graph scope.
- Keep `enqueue_llm_jobs` option.
- Refresh date statuses and graph after successful sync.

## Acceptance Criteria
- RelationshipGraphPage lets user select a date, see extraction status, and sync that exact date.
- `max_messages_per_session` no longer appears on RelationshipGraphPage sync panel.
- Date status response displays counts only, no raw chat content.
- Build/tests pass.
- Deploy frontend/api if implementation changes backend and user needs immediate use.

## Verification Plan
- Backend unit tests for target_date collection/status if backend changed.
- Frontend `npm run build`.
- Smoke checks after deploy: `/healthz`, `/readyz`, `/relationship-graph`.

## Progress Log
- 2026-05-15 16:50: Initial sync button shipped.
- 2026-05-15 17:00: User clarified Raw Messages/date-based workflow. Plan updated.

## Follow-up: 中文友好型页面优化

### 背景
用户反馈 Relationship Graph 页面仍然偏工程字段，无法直观看懂如何操作同步历史。

### 目标
让页面中文优先、操作步骤清晰，不需要理解 API 字段名即可完成“按日期同步群聊历史并抽取关系图”。

### 验收标准
- History sync 区块改为中文主文案：按日期同步群聊历史。
- 给出 1-2-3 操作步骤：填写群ID/用户ID → 选择日期/查看状态 → 点击同步并等待AI抽取。
- 字段中文标签和说明：群聊ID(session_id)、当前用户ID(user_id)、同步日期(target_date)、自动AI抽取(enqueue_llm_jobs)。
- 缺字段、未发送请求、提交中、成功、失败都有中文提示。
- 保留技术字段在辅助文本/小字中，便于排查。
- 不展示 raw 聊天内容。
- frontend build 通过并部署。

## Follow-up: 群聊图谱不要求手填 user_id

### 背景
用户反馈：目标是画群聊成员关系图谱，不是基于某个人，因此页面要求填写 user_id 不符合产品语义。

### 目标
Relationship Graph 历史同步入口应面向群聊：用户只需填写群聊ID、选择日期、决定是否自动AI抽取。

### 验收标准
- 前端 History sync 不再阻止缺 user_id 的同步。
- 页面不再把 user_id 作为主要必填字段；如需保留则作为高级/自动 scope 字段。
- 后端 backfill/history-dates 对缺 user_id 有安全 fallback（例如 `*`/system/default），不影响群聊消息读取和关系抽取。
- 日期状态和 backfill 仍按 tenant/channel/source_key/session_id/date 统计群聊范围。
- 不展示 raw 聊天内容。
- backend tests + frontend build 通过并部署。

# Memory Module Architecture Audit & Optimization Plan

## Goal
继续完成记忆模块：先审计当前架构欠缺，整理成文档，再基于文档选择高优先级问题优化。

## Scope
- Backend memory plugin: router/store/extraction/job/backfill/graph/runtime/profile/eval/vector indexing.
- Frontend memory-related pages: MemoryPage, RelationshipGraphPage, supporting API types/styles.
- Workers/queues/config/migrations/tests relevant to memory.
- Documentation and task status.

## Deliverables
- `tasks/memory-module-architecture-audit/REPORT.md`: architecture map, data flows, gaps, prioritized roadmap.
- `tasks/memory-module-architecture-audit/STATUS.md`: live progress.
- Follow-up implementation commits for selected high-priority optimizations.

## Acceptance Criteria
- 文档能说明：现有架构、核心数据表/对象、主要 API、消息到记忆/图谱的链路、异步队列、前端操作面、测试覆盖。
- 欠缺要分级：P0/P1/P2，并给出影响、证据、建议方案、落地范围。
- 基于文档至少完成一批低风险高收益优化，并通过 focused tests/build。
- 不泄露 raw 聊天内容到用户可见报告；必要示例脱敏。

## Phases
1. Inventory: collect files, endpoints, tests, configs, recent commits.
2. Architecture report: write REPORT.md with gaps and roadmap.
3. Review: main session validates report against code.
4. Implementation batch: choose highest-priority safe optimizations.
5. Verify/deploy/summary.

## Risks
- 记忆模块代码集中在大文件 `plugins/memory/store.py`，审计需避免遗漏。
- 图谱/抽取/回填/运行时 profile 数据流交叉，需区分用户记忆、群聊图谱、向量检索。
- 优化前必须先有文档，避免盲目改动。

## Full Remediation Backlog Update — 2026-05-15

用户明确要求：不只修 P0，`REPORT.md` 文档内列出的 P0/P1/P2 都需要修复。后续按以下批次推进，除非遇到高风险动作，否则自动继续。

### Completed
- Batch A：P0 backfill graph-only enqueue policy 已完成并部署。
- Batch D：Relationship Graph safe edge evidence panel 已完成并部署。

### Remaining Required Batches

#### Batch B：读接口权限和 DTO 策略固化
Scope：`plugins/memory/router.py` memory read surfaces + focused tests。
Acceptance：
- 非 admin `/events` 不返回 `user_text/assistant_text`。
- 非 admin `/items` 不返回 `content/original_text/value_json`。
- 非 admin 对 group graph 候选/待审/拒绝态查询有集中策略：拒绝或仅 safe metadata。
- admin/debug 行为保持可用。
- tests 覆盖 endpoint families。

#### Batch C：`__group__` history sync 闭环测试
Scope：store fake SDK/raw history + router group graph/history dates tests。
Acceptance：
- 群 session 留空 user_id 返回 `user_id_scope="__group__"`。
- backfill event scope 为 `__group__`。
- 同日重复 backfill duplicate，不重复插入。
- history-dates 正确给出 partial/extracted/not_extracted。
- 不暴露原始正文。

#### Batch E：`MemoryStore` 低风险内部边界拆分
Scope：不改 public API，提取/固化纯 helper：job enqueue eligibility、safe graph payload shaping、group scope resolver、DDL/migration consistency guard/comment。
Acceptance：
- public methods 签名不变。
- helper 有 focused tests。
- 现有 tests 全过。
- 除已明确修复项外无产品行为变化。

#### Batch F：observable-first daily relationship extractor MVP
Scope：新增可测试的每日窗口关系抽取骨架，优先 rule/stat extraction，LLM 可选，接入现有 graph/event/fact/episode 或新 service 层，避免 raw 内容外泄。
Acceptance：
- 能按 tenant/channel/source/session/date 聚合群消息窗口。
- 产生 explainable evidence metadata：message counts/senders/date/window，不输出 raw text。
- 支持幂等重复运行。
- review gating 仍默认安全。
- focused tests 覆盖 daily extractor。

#### Batch G：edge-centric review lifecycle MVP
Scope：关系图边上的 review 操作，映射到底层 backing memory items 或新增 edge review service；前端可选最小接入。
Acceptance：
- operator 能从 edge 维度 accept/reject/supersede 或批量处理 backing items。
- audit trail 能追溯 edge/action/item。
- 非 admin 不可写。
- tests 覆盖权限和状态流转。

#### Batch H：DDL dual-track consistency guard
Scope：ensure_tables 与 Alembic migration 漂移防护。
Acceptance：
- 新增开发检查/测试或文档化 guard，列出 memory DDL 关键表字段/索引。
- 修改 ensure_tables 时能提醒同步 migration。
- 不影响运行时启动。

#### Batch I：RelationshipGraphPage 可视化/操作 UX 增强
Scope：搜索、时间范围、loading 状态、pan/zoom 或至少节点/边搜索过滤；继续保持脱敏。
Acceptance：
- 可按节点/边 label 搜索。
- 可设置 from/to 时间范围查询。
- 加载/空态/错误态更清晰。
- 不显示 raw chat。

#### Batch J：MemoryPage 降复杂度和权限提示
Scope：MemoryPage graph/debug/backfill/acceptance 区块提示与安全选择 payload，必要时拆子组件。
Acceptance：
- debug/raw 区块显示 admin/debug only / no raw chat export 提示。
- 普通视图使用 safe payload。
- 组件复杂度有所下降或至少安全提示明确。

#### Batch K：运维指标增强
Scope：extraction/job/graph metrics。
Acceptance：
- 增加抽取延迟、dead jobs by scope、graph error/result_json 分支统计测试。
- 前端或 API 能看到关键 counters。

#### Batch L：provider-neutral history sync adapter sketch
Scope：抽象 WeChat SDK backfill 依赖，先定义接口和 WeChat adapter，不强行实现其他 provider。
Acceptance：
- backfill_from_sdk 保持兼容。
- 新 adapter/service 层可替换数据源。
- tests 覆盖 WeChat adapter 与 unsupported provider error。

### Execution Policy
- 每批 Codex 实施 → 主会话复核 → focused tests/build → commit → 必要时 docker deploy/health check → 频道进度。
- 优先顺序：B/C/E/H/K（安全与测试）→ F/G（关系抽取/review）→ I/J/L（UX/架构演进）。
- 每批结束更新 `STATUS.md`，最终更新 `REPORT.md` 的 completed/remaining 状态。

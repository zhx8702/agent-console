# 记忆模块架构审计报告

日期：2026-05-15

本报告只基于代码、迁移、测试、配置和文档审计，不包含原始聊天内容。重点文件包括 `plugins/memory/router.py`、`plugins/memory/store.py`、`frontend/src/pages/MemoryPage.tsx`、`frontend/src/pages/RelationshipGraphPage.tsx`、`frontend/src/lib/api.ts`、`app/workers/inbound_worker.py`、`app/common/config.py`、memory 相关迁移与测试。

## 1. 当前架构概览

记忆模块是一个插件化子系统。`plugins/memory/plugin.py:28` 初始化 `MemoryStore`，挂载 REST router，注册 pipeline hooks、flow steps 和 `save_memory` effect handler。运行时入口分为三条：

- 对话链路：`MemorySaveEffectHandler.__call__` 从 effect/上下文取 `tenant_id/channel/source_key/user_id/session_id/user_text/assistant_text`，调用 `MemoryStore.remember_interaction` 保存事件、画像、候选记忆，并可能入队 LLM 抽取任务（`app/orchestrator/effect_handlers.py:162`，`plugins/memory/store.py:6573`）。
- 管理 API：`build_memory_router` 暴露 profile、event、item、acceptance、extraction job、graph、group graph、backfill、vector rebuild/smoke 等接口（`plugins/memory/router.py:547` 起）。
- 后台抽取：`InboundWorker` 在开关启用时周期性和消息处理后调用 memory 插件 `drain_extraction_jobs`，再由 `MemoryStore.claim_llm_extraction_jobs/process_llm_extraction_job` 执行结构化记忆与图谱抽取（`app/workers/inbound_worker.py:75`，`plugins/memory/store.py:7191`，`plugins/memory/store.py:7291`）。

`MemoryStore` 是当前架构的中心，职责较重：建表、CRUD、运行时 profile、确定性抽取、LLM job、acceptance、图谱投影、group graph 聚合、WeChat SDK 历史回填、向量索引协调都在同一个类内（`plugins/memory/store.py:2122`）。这降低了跨模块调用复杂度，但也让变更边界、测试定位和权限审计变难。

配置默认偏保守：LLM 抽取、job drain、向量索引、图谱检索、图谱 LLM 抽取默认关闭；job 表默认可用但无人 drain，除非打开 worker drain（`app/common/config.py:76` 至 `app/common/config.py:114`）。

## 2. 数据模型 / 表映射

| 表/存储 | 主键/范围 | 主要字段 | 主要写入方 | 主要读取方 |
| --- | --- | --- | --- | --- |
| `plugin_memory_identity_profile` | `(tenant_id, channel, source_key, user_id)` | long-term cache、manual notes、message/import counters、last_session_id | `remember_interaction`、profile upsert、backfill | runtime profile、MemoryPage |
| `plugin_memory_session_profile` | `(tenant_id, channel, source_key, session_id, user_id)` | short-term cache、session_summary、open_items、decisions、recent_turns、counters | `remember_interaction`、session upsert、backfill | runtime profile、MemoryPage |
| `plugin_memory_event` | `id`，另有可选唯一 `event_key` | scoped event、user_text、assistant_text、trace_id、created_at | runtime save、backfill | extraction job、event list、evidence |
| `plugin_memory_item` | `id`，唯一去重 `(tenant/channel/source/user/scope/session/source_type/normalized_key)` | content/value_json、scope/source/memory/status、acceptance metadata、evidence ids、sensitivity | manual CRUD、deterministic/LLM extraction、backfill、graph backing items | retrieval、profile cache、graph sync、MemoryPage |
| `plugin_memory_acceptance_audit` | `id` | item/action/status/actor/reason/supersede/time | `append_memory_acceptance_audit` / review path | audit/debug |
| `plugin_memory_extraction_job` | `id`，唯一 `idempotency_key` | scope、source_event_id、status、attempts、locks、last_error、result_json | runtime save/backfill enqueue | worker drain、MemoryPage job panel |
| `plugin_memory_entity` | `id`，唯一 `(tenant/channel/source/user/entity_type/normalized_name)` | entity type/name/aliases/confidence/status | item graph sync、LLM graph extraction | graph APIs、retrieval |
| `plugin_memory_fact` | `id`，唯一 `memory_item_id` | subject/object/predicate、backing memory item、event、confidence/status | item graph sync、LLM graph extraction | graph APIs、relationship graph、retrieval |
| `plugin_memory_episode` | `id`，唯一 `memory_item_ids_json` | title/summary、event_ids、memory_item_ids、importance/status | episodic item sync、LLM graph extraction | graph APIs、relationship graph、retrieval |
| Qdrant/vector collection | point ids `memory_item:*`、`memory_fact:*`、`memory_episode:*` | embeddings + scoped payload | vector sync/rebuild | hybrid retrieval |

建表逻辑在 `MemoryStore.ensure_tables` 中内联执行（`plugins/memory/store.py:2148`）。迁移中补充了 session rolling state（`migrations/versions/20260510_0005_memory_session_state.py`）、graph projection tables（`migrations/versions/20260511_0006_memory_graph.py`）和 extraction job `result_json`（`migrations/versions/20260512_0010_memory_extraction_job_result.py`）。注意：运行时代码和迁移都维护 DDL，需持续保持一致。

## 3. 入站消息到关系图的数据流

1. Inbound worker 消费 bus message，转成 `InboundEvent` 后交给 orchestrator；memory 插件在 flow/hook/effect 路径中参与 load/save。
2. `MemorySaveEffectHandler.__call__` 解析当前消息与回复，调用 `MemoryStore.remember_interaction`。
3. `remember_interaction` 读取/导入 legacy identity/session items，写 `plugin_memory_event`，更新 identity/session profile cache，执行确定性候选抽取 `_extract_long_term_candidates` / `_apply_structured_memory_action`，刷新 legacy cache。
4. 每个 memory item 创建/更新后触发 `_sync_memory_graph_for_item_safe`，把 preference/constraint/note/episodic 等映射到 `plugin_memory_entity`、`plugin_memory_fact`、`plugin_memory_episode`，并同步图谱向量（`plugins/memory/store.py:3283`）。
5. 如果 LLM structured 或 graph extractor 启用且有 llm service，`remember_interaction` 调用 `enqueue_llm_extraction_job` 写 `plugin_memory_extraction_job`。
6. worker drain claim job 后，`process_llm_extraction_job` 读取 `plugin_memory_event`，分别调用 `_enhance_memory_with_llm` 和 `_enhance_memory_graph_with_llm`。结构化抽取生成/更新 memory item；图谱抽取生成 entity、fact、episode、invalidation/conflict backing item，并更新 job `result_json`。
7. `get_group_relationship_graph` 从 entity/fact/episode 和 sanitized backing memory items 聚合 nodes/edges，默认仅展示 active/accepted backing item；显式 `acceptance_status` 可拉候选/待审状态（`plugins/memory/store.py:7996`）。
8. `RelationshipGraphPage` 调 `getGroupGraph` 展示只读投影；`MemoryPage` 调 `/graph/entities|facts|episodes|preview` 展示更偏诊断/审核的图谱表格。

关键隔离点：正常 group graph router 会调用 `_scrub_group_graph_payload` 删除 `content/value_json/original_text/object_value/raw_*` 等原始字段（`plugins/memory/router.py:43`）。store 的 evidence payload 也选择 ids、scope、状态、时间等安全字段，避免展示消息正文（`plugins/memory/store.py:8252`）。

## 4. Backfill / History Sync 流程与 `__group__`

后端历史同步入口是 `/plugins/memory/backfill`，调用 `MemoryStore.backfill_from_sdk`（`plugins/memory/router.py:1371`，`plugins/memory/store.py:9027`）。当前只支持 `channel=wechat`，数据源来自 SDK/raw message 表查询辅助函数。

范围规则在 `_group_history_user_scope`：当 `session_id` 是群会话且未指定 `user_id`，或显式传 `__group__`，返回 group-scoped `user_id="__group__"` 并标记 auto scope（`plugins/memory/store.py:58`，`plugins/memory/store.py:1933`）。这意味着群聊历史导入不是落到某个成员 wxid，而是落到该群的聚合范围。

回填主流程：

1. 校验 WeChat channel、session_ids、user scope。群会话自动用 `__group__`，私聊必须显式 user_id。
2. `target_date` 存在时扩大单日上限到 10000 条；否则按 `days_limit/max_messages_per_session` 控制范围。
3. `_collect_session_history` 读取 raw history，群范围下会解析群消息发送者前缀/私聊 sender map，只形成导入事件与特征，不在报告或普通 graph API 暴露正文。
4. `_insert_backfill_event` 写 `plugin_memory_event`，使用 `_backfill_event_key` 去重。
5. 对新事件执行确定性候选抽取，`source_type_override="backfill"`，因此 acceptance source reliability 低于 manual/explicit/current-turn。
6. 如果 `enqueue_llm_jobs=true` 且 structured extractor 可用，入队 LLM job。当前代码的 `should_enqueue_jobs` 只检查 structured extractor，而不是 graph extractor；这会影响“只启用 graph LLM 抽取”的历史关系图同步。
7. `_apply_backfill_session_messages` 更新群 session profile；`_apply_backfill_identity_messages` 更新 `__group__` identity profile；返回 counters、session profiles、identity profile 和 `user_id_scope/user_id_auto`。

日期状态 API `/group-graph/history-dates` 也走 `_group_history_user_scope`，对最近 N 天计算 raw count 与已导入 count，返回 `extracted/partial/not_extracted`（`plugins/memory/router.py:777`，`plugins/memory/store.py:8773`）。前端 `RelationshipGraphPage` 默认“群聊模式：无需填写用户ID”，留空即走 `__group__`（`frontend/src/pages/RelationshipGraphPage.tsx:289`，`frontend/src/pages/RelationshipGraphPage.tsx:355`）。

## 5. 前端操作面

`frontend/src/lib/api.ts` 定义了 group graph、history dates、backfill 请求/响应类型和通用 `apiRequest`（`frontend/src/lib/api.ts:189` 至 `frontend/src/lib/api.ts:225`）。

`MemoryPage` 是操作员工作台，覆盖面很广：

- profile/session/runtime profile 加载与保存；
- events、memory items 列表、创建、编辑、删除；
- acceptance stats/audit/legacy backfill、单 item review/supersede；
- extraction job stats/maintenance；
- graph entities/facts/episodes/preview 诊断视图；
- WeChat 群/成员选择与批量 backfill。

证据：API 调用集中在 `frontend/src/pages/MemoryPage.tsx:1226` 到 `frontend/src/pages/MemoryPage.tsx:1962`。

`RelationshipGraphPage` 是独立只读关系图页面：可填 scope/filter，加载 `/group-graph`，按日期查看 history status，同步单日历史并可选择入队 AI 抽取；展示 SVG 环形布局、nodes/edges 列表、详情面板和脱敏调试摘要（`frontend/src/pages/RelationshipGraphPage.tsx:247`，`frontend/src/pages/RelationshipGraphPage.tsx:289`，`frontend/src/pages/RelationshipGraphPage.tsx:355`）。路由在 `/relationship-graph`，导航名为“群聊关系图”（`frontend/src/App.tsx:34`，`frontend/src/App.tsx:162`）。

风险点：前端两页职责有重叠。`MemoryPage` 仍承担 graph 诊断与 review；`RelationshipGraphPage` 只有只读投影和历史同步，没有 edge evidence API 调用、搜索、时间范围、review action、权限感知模式。

## 6. 测试覆盖图

| 测试文件 | 主要覆盖 |
| --- | --- |
| `tests/unit/test_memory_store.py`、`test_memory_store_compat.py` | profile/item 兼容、运行时缓存、基础 store 行为 |
| `tests/unit/test_memory_hooks.py` | memory control intent、hook load/save、prompt/session 注入 |
| `tests/unit/test_memory_router.py` | router payload、安全字段过滤、backfill/group graph/extraction job/acceptance API 行为 |
| `tests/unit/test_memory_graph.py` | item->graph 投影、scoped joins、group graph 默认 accepted gating、日期过滤、edge evidence 脱敏、prompt graph retrieval gating、acceptance audit |
| `tests/unit/test_memory_p0.py` | P0 hardening / prompt 安全基础场景 |
| `tests/unit/test_memory_p1b.py` | LLM structured extractor、acceptance scoring/review/history/supersede、legacy acceptance audit/backfill |
| `tests/unit/test_memory_p1c.py`、`test_memory_p1d.py`、`test_memory_p1f.py` | 后续 P1 acceptance/retrieval/job 相关场景 |
| `tests/unit/test_memory_p2a.py`、`test_memory_p2b.py` | P2 功能演进场景 |
| `tests/unit/test_memory_vector_index.py` | item/graph vector point、indexability、search fallback/graph ids |
| `tests/unit/test_memory_eval.py` + `tests/fixtures/memory_eval_cases.json` | 抽取/评估 fixtures |
| `tests/unit/test_inbound_worker.py` | memory job drain 开关、周期 drain、消息后 drain、max_claims、并发锁 |
| `tests/integration/test_session_persistence.py`、`tests/e2e/test_full_flow.py` | 较高层消息/会话流，与 memory 有间接覆盖 |

已覆盖较好：acceptance state、图谱投影、group graph 脱敏、job maintenance、worker drain、向量索引条件、prompt retrieval gating。

明显不足：前端交互没有自动化覆盖；group graph 权限/非 admin 候选态访问缺少明确测试；backfill 只启用 graph extractor 时是否 enqueue job 缺少测试；`__group__` scope 的端到端 history sync 到 graph 展示缺少集成测试；MemoryPage 的 raw graph/debug 展示边界缺少 UI/权限测试。

## 7. gaps / risks

### P0

1. **Backfill 入队条件遗漏 graph-only 抽取。**  
   证据：`backfill_from_sdk` 的 `should_enqueue_jobs` 同时要求 `enqueue_llm_jobs`、`structured_extractor.config.enabled`、`structured_extractor.llm_service is not None`，未考虑 `graph_extractor`（`plugins/memory/store.py:9027` 附近）。如果只打开 `memory_graph_llm_extraction_enabled`，RelationshipGraphPage 的“自动AI抽取”可能导入事件但不排队图谱抽取。验收应覆盖 structured-only、graph-only、both。

2. **诊断/读接口权限边界仍复杂，容易误用。**  
   证据：router 对非 admin 有 safe field projection，但 `/events`、`/items`、`/graph/*`、`/group-graph`、acceptance stats/audit 的读权限模型分散在 `_current_user_for_read`、`_require_admin_request` 和 safe row pick 之间（`plugins/memory/router.py:664`、`:685`、`:798`、`:912`、`:941`）。风险是候选/待审图谱或 event ids 被普通上下文枚举。需要把“普通用户可读”和“admin/debug 可读”写成集中策略并补测试。

3. **`MemoryStore` 职责过载，安全关键逻辑分散。**  
   证据：同类同时处理 DDL、回填 SDK、抽取、job、acceptance、graph、vector、REST 支撑（`plugins/memory/store.py:2122` 至文件末尾）。P0 不是立刻大拆，但需要先把安全策略、脱敏 DTO、job enqueue policy 抽成小的可测函数，降低后续误改概率。

### P1

1. **group relationship 仍是 memory graph 投影，不是文档要求的 observable-first 日增量关系抽取。**  
   证据：`get_group_relationship_graph` 从 `plugin_memory_entity/fact/episode` 聚合，`docs/group-relationship-memory.md` 要求 rule/stat extraction 先于 LLM、每日窗口、证据累计、review gating。当前还没有 dedicated daily relationship extractor。

2. **`__group__` 端到端覆盖不完整。**  
   证据：store 有 `_group_history_user_scope` 与 history dates/backfill；测试中多为 mocked graph/query，缺少“留空 user_id -> `__group__` -> event/job/item/profile -> group graph”闭环测试。

3. **关系图 evidence API 后端已存在，前端未接入。**  
   证据：router 有 `/group-graph/evidence/{edge_id}`（`plugins/memory/router.py:745`），store 有 safe evidence payload（`plugins/memory/store.py:8252`）；`RelationshipGraphPage` 只展示 edge 上的 ids，不调用 evidence endpoint。

4. **Graph review 生命周期仍借 backing memory item，缺少 edge-centric review。**  
   证据：acceptance review 是 `/items/{item_id}/acceptance-review`，group graph edge 只有 `memory_item_ids`；前端关系图没有直接 accept/reject edge。对运营人员来说“边”是对象，但系统审核对象仍是 memory item。

5. **DDL 双轨维护有漂移风险。**  
   证据：`ensure_tables` 和 Alembic migrations 都创建/alter memory 表。短期可接受，但新增字段/索引要强制测试或文档检查，避免本地 auto ensure 与生产 migration 不一致。

### P2

1. **RelationshipGraphPage 可视化是 MVP 环形布局。**  
   缺少搜索、pan/zoom/drag、聚类、时间范围、edge evidence detail、review 模式、加载状态细分。

2. **MemoryPage 过宽。**  
   profile、items、graph、job、backfill、acceptance 混在一个页面，后续维护与权限感知 UI 会越来越重。

3. **运维指标不足。**  
   有 extraction job stats/maintenance，但缺少抽取延迟、dead job by scope 趋势、auto-accept/review/reject 比率、vector drift、graph edge churn。

4. **provider-neutral history sync 尚未完成。**  
   关系图文档强调 source-plugin-neutral，当前 backfill 明确只支持 WeChat SDK。

## 8. 推荐路线图

### Batch A：P0 小修，保证历史同步能驱动图谱

范围：`MemoryStore.backfill_from_sdk` job enqueue policy + 单元测试。

验收：
- structured-only 启用时会 enqueue；
- graph-only 启用时也会 enqueue；
- 两者都关闭或无 llm service 时不 enqueue；
- `RelationshipGraphPage` 的 `enqueue_llm_jobs=true` 对 graph-only 配置不会静默无效。

### Batch B：读接口权限和 DTO 策略固化

范围：`plugins/memory/router.py` 中 memory read surfaces；新增集中 helper，例如 safe/admin/debug/read-current-user 策略。

验收：
- 非 admin `/events` 不返回 `user_text/assistant_text`；
- 非 admin `/items` 不返回 `content/original_text/value_json`；
- 非 admin 显式请求 `acceptance_status=candidate,needs_review,rejected` 的 group graph 要么拒绝，要么只返回当前用户/允许范围的 safe metadata；
- admin/debug 行为保持可用；
- 测试覆盖每个 endpoint family。

### Batch C：`__group__` history sync 闭环测试

范围：store 层 fake SDK/raw history + router 层 group graph/history dates。

验收：
- 群 session 留空 user_id 返回 `user_id_scope="__group__"`；
- backfill 插入 event 的 scope 是 `__group__`；
- 同一日重复 backfill 走 duplicate，不重复插入；
- history-dates 从 raw/imported count 得出 partial/extracted/not_extracted；
- 不断言或暴露原始正文。

### Batch D：Relationship Graph evidence panel

范围：`frontend/src/lib/api.ts` 类型 + `RelationshipGraphPage` 点击 edge 后调用 evidence endpoint。

验收：
- edge detail 展示 evidence counts、memory item ids、event ids、episode ids；
- 不显示 raw text、content、summary；
- API 失败时保留当前 graph；
- 前端输出摘要继续脱敏。

### Batch E：拆分 `MemoryStore` 的低风险内部边界

范围：不改外部 API，先提取纯函数/小 helper：job enqueue eligibility、safe graph payload shaping、group scope resolver、DDL/migration consistency comments。

验收：
- 现有测试全过；
- 新 helper 有 focused tests；
- store public methods 签名不变；
- 没有产品行为变化，除 Batch A 明确修复项外。

## 9. 立即可做的低风险优化候选

- 修正 `backfill_from_sdk` 的 `should_enqueue_jobs`，纳入 graph extractor enabled + llm service。
- 给 `_group_history_user_scope`、job enqueue eligibility、`_scrub_group_graph_payload` 加 focused tests，防止回归。
- 在 `RelationshipGraphPage` 的 history sync 成功摘要中展示 `user_id_scope/user_id_auto`，让 `__group__` 是否生效可见。
- 在 `MemoryPage` graph raw/diagnostic 区域增加“admin/debug only / no raw chat export”提示，并尽量使用 safe selection payload。
- 给 `/group-graph/evidence/{edge_id}` 增加前端类型和只读 evidence panel，不接入写操作。
- 在 `ensure_tables` 附近补一段开发注释或测试，要求新增表/字段同时更新 Alembic migration。
- 增加 extraction job stats 中 graph error 分支的测试，覆盖 `result_json.graph.error_type` 统计。

## 审计结论

模块已经具备可用的用户/会话记忆、acceptance、异步抽取、图谱投影、回填和前端操作面；安全设计也明显不是“直接存取原文”的简单实现。当前最大架构缺口不是单点功能缺失，而是三条边界需要收紧：回填到图谱的 graph-only 抽取链路、读接口权限/脱敏策略、以及 group relationship 从“memory graph 投影”演进到“可解释的日增量关系抽取系统”。建议先用小批次修复 P0，再扩展 evidence/review UX，最后再拆分 `MemoryStore`。

## Remediation Completion Update — 2026-05-15

用户确认要求：不只修 P0，文档内 P0/P1/P2 全部需要修复。以下批次已完成并验证：

### Completed Batches

- Batch A / P0: backfill graph-only enqueue policy
  - Commit: `0489105 fix(memory): enqueue graph-only backfill extraction jobs`
  - Result: structured-only / graph-only 均可排 LLM extraction jobs；job switch/service absence 时安全跳过。

- Batch B: read endpoint permission and safe DTO hardening
  - Commit: `a41e2c4 test(memory): harden read DTOs and group history sync`
  - Result: non-admin `/events`、`/items` 不返回 raw chat/text/value payload；group graph candidate/review/rejected 非 admin 访问受控。

- Batch C: `__group__` history sync closed-loop tests
  - Commit: `a41e2c4 test(memory): harden read DTOs and group history sync`
  - Result: group session blank user_id 自动走 `__group__` scope；event/item/job scope、重复 backfill 去重、history-dates 状态均有测试。

- Batch D: Relationship Graph safe edge evidence panel
  - Commit: `3f00367 feat(memory): show safe relationship edge evidence`
  - Result: 图谱边可查看 evidence metadata；前端过滤 raw/chat fields；失败不清空图。

- Batch E: MemoryStore low-risk boundary helpers
  - Commit: `3b26568 feat(memory): add store guards and extraction metrics`
  - Result: 提取/固化 job enqueue eligibility、group scope resolver、safe evidence payload helpers。

- Batch F: observable-first daily relationship extractor MVP
  - Commit: `84c2cd1 feat(memory): add daily relationship extraction and edge review`
  - Result: admin-only daily group extraction stats-only MVP；按 tenant/channel/source/session/date + `__group__` 幂等生成 metadata item；无 LLM 时 rule-only/no_llm，不泄露 raw text。

- Batch G: edge-centric review lifecycle MVP
  - Commit: `84c2cd1 feat(memory): add daily relationship extraction and edge review`
  - Result: admin-only edge review route；支持 `fact:<id>` accept/reject 并映射到 backing memory item review/audit。

- Batch H: DDL dual-track consistency guard
  - Commit: `3b26568 feat(memory): add store guards and extraction metrics`
  - Result: `MEMORY_DDL_CONSISTENCY_GUARD` + tests 让 ensure_tables 与 Alembic 关键字段/索引漂移可见。

- Batch I: RelationshipGraphPage UX enhancement
  - Commit: `5a91ed3 feat(memory): improve graph UX and history adapter`
  - Result: from/to 时间范围、节点/关系搜索、本地过滤、中文 loading/empty/error 状态。

- Batch J: MemoryPage safety hints and safe outputs
  - Commit: `5a91ed3 feat(memory): improve graph UX and history adapter`
  - Result: debug/backfill/acceptance/raw 区块增加 admin/debug-only 提示；runtime/events/backfill/memory item 输出改 safe metadata；事件正文、episode summary、memory item list content 脱敏。

- Batch K: operational metrics enhancement
  - Commit: `3b26568 feat(memory): add store guards and extraction metrics`
  - Result: extraction job stats 增强 retry/exhausted/ready/delayed/dead scope/latency/graph result/error counters。

- Batch L: provider-neutral history sync adapter sketch
  - Commit: `5a91ed3 feat(memory): improve graph UX and history adapter`
  - Result: 新增 `HistorySyncAdapter` / `WeChatHistorySyncAdapter`，`backfill_from_sdk` 保持兼容，unsupported provider 明确错误并有测试。

### Final Verification

- Python compile:
  - `python3 -m py_compile plugins/memory/store.py plugins/memory/router.py`
- Focused pytest:
  - `pytest tests/unit/test_memory_store_compat.py tests/unit/test_memory_router.py tests/unit/test_memory_graph.py -q` → 90 passed
  - Earlier combined safety/metrics suite included `tests/unit/test_memory_p1c.py` → 106 passed
- Frontend:
  - `npm run build` passed
- Deployment smoke:
  - API `/healthz` ok
  - API `/readyz` ready/errors=[]
  - frontend served new bundle `assets/index-B0-ol3nj.js`
  - bundle strings confirm search/safety markers
  - admin-only daily extraction route returns 403 without admin token

### Remaining Non-blocking Risks

- `MemoryStore` 仍是大文件；Batch E/L 已降低部分风险，但进一步拆 repository/service 仍可作为后续架构债处理。
- Daily relationship extractor 当前是 stats-only/rule-only MVP，尚未做高阶 LLM relation extraction quality tuning。
- Frontend bundle 仍有 Vite large chunk warning；不影响功能，可后续 code split。
- 本地部署覆盖文件不属于版本化产品配置，也不纳入审计结论。

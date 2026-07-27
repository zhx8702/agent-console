# 人物-人物关系图修复计划（2026-05-17）

## 背景
用户反馈：当前群聊关系图主要只有人物与产品/主题/值之间的关系，缺少人物和人物之间的关系。

脱敏后的代表性回归样本显示：图谱已有多类人物到主题、事物和产品的关系，但没有
person-person 边。原线上会话标识和样本规模等运营数据不在仓库文档中保留。

结论：问题主要在后端图谱抽取/写入层，不是前端过滤导致。

## 目标
让群聊关系图能展示“谁和谁之间的关系”，至少包括：
1. 直接回复/引用/点名互动：A replied_to / mentioned / addressed B
2. 同一话题窗口共同讨论：A co_discussed B（低权重/可聚合）
3. 明确协作/支持/反对/建议等语义关系：A agreed_with / disagreed_with / advised / asked B

## 范围
- 检查现有 memory graph 抽取 prompt/schema/store。
- 确认 `replied_to`、`co_participated`、`collaborated_with` 是否只是 schema 支持但抽取器未产出。
- 增加人物-人物边抽取/构造逻辑。
- 增加单元测试，避免只产出 person-value/person-product。
- 验证 graph API 样本里 person-person 边数量 > 0。

## 不做
- 不引入外部 LLM 新服务。
- 不做大规模 UI 重构。
- 不改变数据库破坏性结构；如需要迁移，先评估。

## 阶段

### Phase 1: 代码定位与方案确认
- [x] 找到抽取入口、schema、prompt、store 写入逻辑。
- [x] 找到测试覆盖。
- [x] 判断人物-人物关系应在 LLM 抽取还是 deterministic post-process 中生成。

验收：给出明确实现点。

实现点：`plugins/memory/store.py` 的 `run_group_relationship_window_extraction` 是群聊窗口关系入口；`_apply_group_relationship_window_candidate` 写入 `plugin_memory_item`，`_memory_graph_mapping_for_item` 再投影到 `plugin_memory_fact`，`get_group_relationship_graph` 聚合公开图谱。最小安全方案选 deterministic window post-process，保留 LLM 候选作为补充。

### Phase 2: 实现人物-人物边
- [x] 增强 prompt/schema 或增加 post-process。
- [x] 对群聊窗口构造关系：reply/mention 优先，co_discussed 其次。
- [x] 确保边包含 evidence/confidence/source window。

验收：测试样本能产出 person-person 边。

实现说明：新增 deterministic group-window candidates：`addressed`（@/点名）、`replied_to`（相邻不同发送者）、`co_participated`（同窗口共同参与，低置信度）。写入 `deterministic_group_window` source type，payload 只保存 participant ids、predicate、confidence、event ids、window ids，不保存 raw text；deterministic 边标记 accepted/active，使默认 group graph 能展示 person-person 边。

### Phase 3: 测试与接口验证
- [x] 跑 memory graph 单测。
- [x] 跑相关 focused tests。
- [x] 调用本地 API 或 store smoke 验证。

验收：person-person 边 > 0，且无 raw text 泄漏。

验证：
- `python3 -m py_compile plugins/memory/store.py` 通过。
- `python3 -m pytest tests/unit/test_memory_graph.py tests/unit/test_memory_store_compat.py -q` 通过。
- `npm --prefix frontend run build` 通过。
- `git diff --check` 通过。
- 初次 `python -m ...` 失败，因为环境没有 `python` 命令；已改用 `python3`。
- 本轮补充验证：`pytest tests/unit/test_memory_graph.py -q` 通过（50 tests）。
- 本轮补充验证：`pytest tests/unit/test_memory_router.py -q` 通过（27 tests）。
- 增加覆盖：no-LLM window extraction 生成 person-person edges；deterministic `addressed` edge 能出现在默认 group graph；graph payload 不含 raw `user_text`/`content`/`original_text`。

### Phase 4: 提交与部署建议
- [x] 更新文档。
- [ ] commit。（supervisor 正在复核并提交）
- [ ] 如用户要求，重建 API/worker 并触发补抽历史窗口。

## 风险
- `co_discussed` 关系如果过多会变成噪音，需要低 confidence/可过滤。
- 人物身份映射仍依赖 wxbot roster/contact map。
- 历史图谱需要重新抽取/回填才会看到新关系。

## 当前状态
Phase 1-3 已完成；Phase 4 文档已更新，待 supervisor commit。未部署。历史图谱需要重新跑 `group-graph/extract-window` 或 catchup 后才会出现新 person-person 边。

## 变更文件
- `plugins/memory/store.py`
- `frontend/src/pages/RelationshipGraphPage.tsx`
- `tests/unit/test_memory_graph.py`
- `tasks/person-person-graph-2026-05-17/PLAN.md`

## 剩余风险
- `co_participated` 低权重边仍可能偏多；当前限制每窗口最多 8 个 sender / 20 对关系。
- `replied_to` 使用相邻发言作为保守近似，不等价于平台级 reply metadata。
- 已导入历史需要重新跑 window extraction/catchup，旧图谱不会自动出现新 deterministic 边。

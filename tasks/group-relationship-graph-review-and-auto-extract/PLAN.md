# 群聊关系图：边审核 + 自动抽取

## 目标

把 `/relationship-graph` 从“只能看、只能手点抽取”补成可运营的半成品闭环：

1. 在详情面板接受或拒绝一条关系（后端 API 已存在）。
2. 调度进程按已导入的 `plugin_memory_event` 自动做窗口追平，默认同时跑确定性规则和语义 LLM。

不提交、不部署，除非另外明确要求。

## 不做什么

- 不展示聊天原文；不把 `content` / `original_text` / `user_text` 带回 UI。
- 不改远程 delay/timeout，不上传未配套的 restyle 页面。
- 不做力导向布局、跨昵称自动身份合并、矛盾自动消解、衰减引擎、聚类算法。
- 不做角色权限矩阵、重置/清空端点。
- 窗口抽取的手动 API 行为保持兼容：未显式关闭时仍可走 LLM。

## 阶段 1 — 边审核 UI

### 已有能力

- `POST /plugins/memory/group-graph/edges/{id}/acceptance-review`
- `MemoryStore.review_group_relationship_edge` 映射到记忆验收模型
- 默认图查询只返回已接受边；`acceptance_status=needs_review` 才能看到待审边

### 要做

- `frontend/src/lib/api.ts` 增加 `reviewGroupGraphEdge`
- 控制器增加 `reviewEdge(action)`、`showPendingReview()`、`reviewing`
- 详情面板对选中边提供「接受 / 拒绝」，走现有 `DangerAction` 确认
- 页头增加「查看待审核」，切到 `needs_review` 并刷新
- 测试：模块确认框会调用 controller；controller 会打审核 API 并刷新图

### 验收

- 待审核边可在 UI 中接受或拒绝，刷新后状态与默认过滤一致
- 确认框展示群/关系摘要，不含原文
- 拒绝后默认视图不再显示该边

## 阶段 2 — 自动窗口追平

### 约束

- 只读已导入事件，不在 tick 里打 wxbot 历史接口
- 默认 `include_llm=True`，并打开图/结构化抽取与任务 drain
- 不按额度做额外限流；调度仍有每轮窗口和时间预算，避免单 tick 占死
- 仅 `scheduler` 进程跑循环，避免 api/worker 重复写
- 新边写入 `needs_review`

### 要做

- Settings / `.env.example` / `docker-compose.yml` 增加开关与限额
- `run_group_relationship_window_extraction` / `catchup` 增加 `include_llm`
- `list_imported_group_graph_targets` 从 `plugin_memory_event` 列出群日
- `run_group_graph_auto_extract_tick` 做范围门控后追平
- `MemoryPlugin` 增加可取消循环，镜像治理循环的失败隔离

### 验收

- 关闭开关或非 scheduler 角色不启动循环
- `include_llm=False` 时即使 extractor 可用也不调用 LLM
- tick 只处理 `@chatroom` + `__group__` 已导入日
- 范围门控拒绝的租户/群被跳过，不让整轮失败

## 阶段 3 — 验证

- 后端已跑：`test_memory_group_graph_auto_extract.py`、`test_config_precedence.py`、`test_memory_graph.py`、`test_memory_router.py -k edge_review or group_graph`
- 前端已跑：`RelationshipModules.test.tsx`、`useRelationshipGraphController.test.tsx`
- 本机没有已登录的控制台会话，未做浏览器点击验收
- 概览默认最近 7 天；页头展示待审核数量；手动窗口抽取显式带 `include_llm=true`

## 阶段 4 — 补全可运营缺口

已补：

- 待审核队列（含 `needs_review,candidate`）、过期 / 退回待审、刷新后尽量保留选中
- 画布点选、平移、滚轮缩放、适应画布；待审虚线、证据线宽、新旧透明度、抽取方式标签
- 近 7/14/30/全部时间范围；「同步并抽取近 7 天」；自动抽取回看默认 7 天
- 默认打开 `memory_graph_retrieval_enabled`，已接受关系可进入提示词

阶段 5 已补：按日回放、并行边曲线、验收分/别名/替代关系、治理保留摘要、调度对已知群补同步后再抽取。

仍不做：跨昵称自动身份合并、矛盾自动消解、衰减/评测引擎、力导向聚类、角色权限矩阵、提交部署

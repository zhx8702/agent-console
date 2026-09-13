# 主分支后端与工程优化评估（2026-09-13）

- 评估对象：`origin/main` @ `ed686d9`（PR #6 合并后，55 次提交，3 位贡献者）
- 方法：源码规模与 ruff 圈复杂度统计、mypy / CI 配置审计、执行门禁与前端构建产物实测、单测全量运行（3,247 项）
- 用途：作为 `refactor/backend-optimization` 分支的工作清单；每一项都附带证据位置，可直接定位到代码

## 一览

| 指标 | 数值 |
| --- | --- |
| 后端 Python 行数（`app` + `plugins`） | 65,719 + 94,322 ≈ 160K，342 个 `.py` |
| 前端 TS/TSX 行数 | 48,192，169 个文件 |
| 单测 / 集成 / e2e 行数 | 118,003 / 794 / 537 |
| 圈复杂度 > 25 的函数 | 31 个 |
| mypy strict 覆盖的源文件 | 16 个（约 5%） |
| `Any` 出现次数 | wxbot 626、memory 590、app/admin 205、app/plugin 175 |
| macOS 上失败的单测 | 43 / 3,247（见 P0） |

## P0 — 正确性

### `app/common/config.py`：未知环境变量的模糊匹配在 macOS 上拒绝构造 prod 配置

- 位置：`app/common/config.py:72-88` `_looks_like_settings_env`，使用 `difflib.get_close_matches(normalized, known_names, n=1, cutoff=0.82)`
- 现象：macOS 由 launchd 启动的进程（含 Cursor / VS Code 的终端）带有 `XPC_SERVICE_NAME`，被判为 `APP_SERVICE_NAME` 的拼写错误；任何 `Settings(app_env="prod")` 抛出  
  `ValueError: Unknown Agent Console setting(s): process environment: XPC_SERVICE_NAME (did you mean APP_SERVICE_NAME?)`
- 影响：本地 3,247 个单测中 43 个失败（`test_main.py`、`test_config_precedence.py`、`test_admin_authorization_audit.py`、`test_amap_router.py`、`test_router.py`、`test_runtime_llm_config.py`、`test_worker_readiness.py`），`env -u XPC_SERVICE_NAME` 后 0 failed；Linux CI 看不到。生产若跑在非容器的 macOS 主机上会直接拒绝启动
- 该文件 838 行、344 个字段，是近 200 次提交里改动最多的源文件（15 次）
- 建议：
  1. 只对已知前缀命名空间做精确校验，取消 `get_close_matches` 或把 `XPC_`、`__CF`、`TERM_`、`SHELL_` 等系统前缀加入 `_NON_SETTINGS_ENV_PREFIXES`
  2. 增加回归测试：携带典型 macOS / Linux / CI 环境变量集合时 `Settings(app_env="prod")` 必须可构造
  3. 把 `Settings` 按域拆成子模型（llm / plugin / wxbot / orchestrator / admin），降低单文件改动率

## P1 — 热路径性能

### 插件执行门禁：每个 hook / tool / effect 边界一次数据库往返，且只缓存拒绝结果

- `app/plugin/hooks.py:177` `HookRunner.run` 顺序遍历同一 hook point 的全部 entry，每个 entry 调 `evaluate_owner_execution`
- `app/orchestrator/owner_gate.py:76`：注释明确 “Cache only denials; positive decisions are rechecked” —— 允许结果每次重查
- `app/plugin/registry.py:1712` `execution_allowed` → `app/plugin/state.py:431` `execution_snapshot_allowed` → `engine.begin()` 一条 SQL（`state.py:1448` `_fetch`）
- 规模：全仓 96 处 `execution_allowed` 调用点；8 个 hook point 上约 25 个已注册 hook；一条走完全程的消息在门禁上有数十次 SQL 往返，且与 LLM 调用串行
- 状态层已支持多 owner 单语句 union snapshot，但调用侧没有合并同一 hook point 的 owner，也没有消息内 memo
- 建议：
  1. 在 `PipelineContext` 内对同一 `(owner, tenant, session)` 的允许结果做单次消息内 memo（`owner_gate.py` 的 `_decision_cache` 已有结构，只需对 allowed 也缓存并绑定 ctx 生命周期）
  2. 进程级短 TTL 缓存（1–3 秒），用 Redis pub/sub 或 `plugin_state` 版本号做失效
  3. 一个 hook point 下多个 owner 合并成一次 `execution_snapshot_allowed` 查询
  4. 回归依赖现有 `tests/unit/test_owner_execution_gate.py`、`test_plugin_runtime.py`

### 前端首屏：没有路由级懒加载，预加载全部 21 个页面 chunk

- `frontend/src/App.tsx:6-26` 静态 import 全部 21 个页面
- `frontend/vite.config.ts` 的 `manualChunks` 按页切分，但 `dist/index.html` 含 21 条 `modulepreload`，切分只改变了文件数
- 构建产物 1.2 MB：`page-MemoryPage` 168 KB、`vendor` 161 KB、`page-WxbotPage` 127 KB、`page-GroupBehaviorPage` 91 KB、`page-PluginsPage` 67 KB、`index` 66 KB、`page-RelationshipGraphPage` 61 KB
- 建议：页面组件改 `React.lazy` + `Suspense`，保留 manualChunks；首屏只剩 vendor + index + 当前页（约 -70%）

## P1 — 可维护性

### `plugins/memory`

- `store.py` 10,295 行；`MemoryStore`（`store.py:2843` 起）一个类 91 个方法，约 7,400 行
- 已拆出 `store_jobs.py`（2,025）、`store_group_graph.py`（2,014）、`store_backfill.py`（1,591）、`store_retrieval.py`（1,555）、`store_mutations.py`（975）作为 mixin，但主文件仍承担 `HistorySyncAdapter` / `WeChatHistorySyncAdapter`（`store.py:2404-2842`）、过期清理、遗忘等
- 复杂度 > 15 的函数 28 个：`_run_physical_expiry_sweep` 50（`store.py:3242`）、`forget_member_detailed` 48（`store.py:7480`）、`get_group_relationship_graph` 50（`store_group_graph.py:233`）、`_apply_structured_memory_action` 31（`store.py:8510`）
- `Any` 590 处，`noqa` 14 处
- 建议：按 profile / item / episode / erasure / history-sync 继续切分为独立 repository；把 `HistorySyncAdapter` 迁到 wxbot 边界；三个复杂度 ≥ 48 的方法先做提取函数级重构再补类型

### `plugins/wxbot`

- 31,252 行，最大插件；`store.py` 3,581 行覆盖约 20 张表；`agent_tool_service.py` 3,446 行；`router.py` 3,323 行
- 复杂度 > 15 的函数 28 个：`build_wxbot_router` 110（`router.py:1811`）、`register_report_routes` 59（`admin_report_routes.py:44`）、`ReplyQueueHook.run` 52（`reply_queue_hook.py:84`）
- `Any` 626 处；23 处 `asyncio.sleep` 自轮询循环分散在各服务
- 建议：store 按 reply_queue / group_observation / policy / report 拆 repository；后台循环统一收敛到 scheduler worker 的 job 框架；agent tool 定义与执行分层

### Router builder 闭包模式

把几十个端点写成一个 `build_*_router` 函数内的嵌套闭包，无法单独测试和类型检查：

| 函数 | 位置 | 圈复杂度 | 路由数 |
| --- | --- | --- | --- |
| `build_memory_router` | `plugins/memory/router.py:1095` | 162 | 47 |
| `build_admin_router` | `app/admin/kb_router.py:630` | 155 | 47 |
| `build_wxbot_router` | `plugins/wxbot/router.py:1811` | 110 | 31 |
| `build_persona_extract_router` | `plugins/persona_extract/router.py:246` | 57 | 14 |
| `build_social_admin_router` | `app/social/router.py:53` | 44 | 19 |

- `app/admin/kb_router.py` 名不副实：/plugins 15、/message-flows 8、/kb 7、/faqs 5、/streams 4、/dlq 4、/runtime 3、/tenants 1 个端点混在一起
- 建议：改为模块级 handler + FastAPI `Depends` 注入 store；`kb_router.py` 按前缀拆成 `plugins_router` / `flows_router` / `kb_router` / `queues_router`；拆出的 router 逐个纳入 mypy `files`

### 前端状态与数据层

- 没有统一数据请求层：全站 634 个 `useState`、97 个 `useEffect`、76 处手写 `setLoading` / `setError`
- `frontend/src/pages/wxbot/useWxbotPageController.tsx` 1,926 行、74 个 `useState`、19 个 `useEffect`、返回约 200 个字段
- `frontend/src/lib/api.ts` 1,133 行手写 fetch 封装；`App.tsx` 707 行
- 建议：引入 TanStack Query 或自写 `useResource` 统一缓存 / 重试 / 并发去重；wxbot 控制器按 tab（policy / agent / reports / queue）拆成多个 hook；`api.ts` 按域拆分或从 `openapi.json` 生成

## P2 — 工程效率与质量门禁

### 类型门禁

- `pyproject.toml` 的 `[tool.mypy] files` 只列 16 个文件；CI 再对 7 个 worker 入口用 `--follow-imports=skip`；`app` + `plugins` 共 342 个 `.py`，覆盖率约 5%
- `type: ignore` 28 处（`app/main.py` 占 12）
- 建议：“新文件必须进 mypy 列表”作为 PR 门禁；先纳入 `app/plugin`、`app/orchestrator`、`app/llm`，再逐插件推进；`main.py` 的 ignore 通过给 container 明确协议类型清掉

### CI（`.github/workflows/backend-ci.yml`）

- 单个 `quality-and-tests` job、45 分钟超时，串行执行 lint → mypy → 迁移回滚 → pytest → e2e → 前端 → Playwright → Docker 构建；前端检查排在后端 e2e 之后
- 本地 3,247 个单测约 75 秒，CI 一轮却要几十分钟
- 建议：拆成 lint+mypy、backend-unit、backend-integration+e2e、frontend、compose-contract 五个并行 job；pytest 加 `-n auto`（pytest-xdist）

### 测试金字塔倒置

- unit 236 文件 118,003 行 vs integration 7 文件 794 行、e2e 3 文件 537 行
- 最大测试文件 `tests/unit/test_wxbot_router.py` 4,207 行、`test_memory_graph.py` 3,945 行
- moderation 源码 2,080 行只有 307 行测试；speaker_portrait 2,064 行 / 479 行；local_agent 2,165 行 / 465 行
- store 层大量 SQL 只在 aiosqlite 上验证，与生产 PostgreSQL 的差异靠 `app/infra/runtime_schema.py` 兜底
- 建议：memory / wxbot 的 store 层测试改跑真实 PostgreSQL（CI 已有 service）；拆分 > 2,000 行的测试文件；给 moderation / speaker_portrait / local_agent 补关键路径测试

### `wxbot_client`（Windows companion）

- `wxbot_client/api/server.py:41` `create_app` 圈复杂度 147，全仓最高单函数，865 行 Flask 应用工厂
- 单测 `tests/unit/test_wxbot_api_server.py` 因缺 `flask` 依赖被 SKIPPED，实际没有跑
- 建议：用 Flask Blueprint 按 ingest / media / queue 拆分；把 `flask` 放进 dev extras 让测试真正执行

### 开发脚本在 macOS 不可用

- `scripts/dev-stack.sh:222` 使用 `setsid`，macOS 没有该命令；README 声称的 macOS 源码模式 `make dev-start` 实际无法启动
- 建议：用 `python -c "subprocess.Popen(..., start_new_session=True)"` 或 `nohup` + `disown` 替代

## 附：规模与复杂度数据

### 最大的 Python 源文件

| 文件 | 行数 |
| --- | ---: |
| `plugins/memory/store.py` | 10,295 |
| `plugins/wxbot/store.py` | 3,581 |
| `plugins/wxbot/agent_tool_service.py` | 3,446 |
| `plugins/wxbot/router.py` | 3,323 |
| `app/admin/kb_router.py` | 3,070 |
| `plugins/persona_extract/store.py` | 2,959 |
| `plugins/memory/router.py` | 2,843 |
| `plugins/draw/store.py` | 2,618 |
| `app/plugin/manager.py` | 2,298 |
| `plugins/memory/hooks.py` | 2,296 |
| `plugins/draw/hooks.py` | 2,275 |
| `app/agent/engine.py` | 2,031 |

### 圈复杂度 > 15 的函数数（`ruff --select C901`，阈值 15）

| 模块 | 数量 |
| --- | ---: |
| `plugins/wxbot` | 28 |
| `plugins/memory` | 28 |
| `app/plugin` | 9 |
| `app/orchestrator` | 7 |
| `plugins/persona_extract` | 4 |
| `plugins/draw` | 4 |
| `app/main.py` | 4 |
| `app/admin` | 4 |

### 其余复杂度 ≥ 32 的函数

| 函数 | 位置 | 圈复杂度 |
| --- | --- | ---: |
| `create_app` | `wxbot_client/api/server.py:41` | 147 |
| `_run_async_draw_job` | `plugins/draw/hooks.py:1213` | 42 |
| `run_worker_process` | `app/workers/runtime.py:266` | 40 |
| `dispatch_command` | `plugins/commands/hooks.py:336` | 39 |
| `_process_session` | `plugins/group_activity/service.py:452` | 37 |
| `run_extraction` | `plugins/persona_extract/store.py:2516` | 36 |
| `AgentEngine.answer` | `app/agent/engine.py:174` | 33 |
| `DialogOrchestrator._run` | `app/orchestrator/engine.py:593` | 32 |

### 近 200 次提交的改动热点

`app/common/config.py` 15 次、`app/agent/engine.py` 12、`app/common/prompting.py` 10、`plugins/wxbot/agent_intent_hook.py` 9、`plugins/persona_extract/store.py` 9、`frontend/src/pages/persona/PersonaWorkspace.tsx` 9

## 建议的推进顺序

1. 修 `config.py` 环境变量误判（半天，立刻恢复 macOS 开发机上的 43 个单测）
2. 执行门禁加消息内 memo + 短 TTL 缓存（改动集中在 `owner_gate.py` / `registry.execution_allowed`）
3. 前端 `React.lazy`（`App.tsx` 一处改动）
4. CI 拆并行 job（不改业务代码）
5. Router 去闭包 + `kb_router.py` 按域拆分，顺带纳入 mypy strict
6. memory / wxbot store 分层（最重；先在真实 PostgreSQL 上补集成测试兜底）

## 检查过、暂不需要优先处理的部分

Redis Streams 消费（批量 16，有重试提升与 reclaim）、BM25 检索（`app/rag/retriever.py` 已有缓存）、SQL 注入面（5 处 f-string 均为常量表名）、Dockerfile 与 Compose 安全不变量、DB 连接池配置（`pool_size=10` / `max_overflow=10`，lifecycle fence 使用独立 `NullPool` 属刻意设计）。

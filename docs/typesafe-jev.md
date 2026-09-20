# TypeSafe / Jev 群关系旁路评估

群关系窗口抽取可调用独立的 TypeSafe System One 客户端。当前只提供
shadow 评估：结果用于审计，关系的接受、拒绝和持久化仍由原有策略决定。
`TYPESAFE_SHADOW_ONLY=false` 也不会开启自动裁决；当前版本没有提供该功能。

## 配置

本地运行使用 `TYPESAFE_*` 环境变量。Docker Compose 部署在服务器 `.env`
中使用对应的 `COMPOSE_TYPESAFE_*` 变量，由 compose 传入运行 memory 插件的后端进程。

```dotenv
COMPOSE_TYPESAFE_API_KEY=<server-side-secret>
COMPOSE_TYPESAFE_ENABLED=true
COMPOSE_TYPESAFE_SHADOW_ONLY=true
COMPOSE_TYPESAFE_MODEL=jev-latest
COMPOSE_TYPESAFE_TIMEOUT=10
COMPOSE_TYPESAFE_MAX_RETRIES=1
COMPOSE_TYPESAFE_MIN_CONFIDENCE=0.8
```

默认禁用。API key 仅保存在服务端配置中，不进入仓库或前端。
`MIN_CONFIDENCE` 是客户端提供的判断阈值，当前群关系旁路不会用它改变接受状态。
每个候选请求受超时限制（旁路最多 30 秒）；请求失败只记录错误类别，抽取继续。
禁用后不调用 TypeSafe。停止插件会取消后台任务并关闭客户端。

## 输入与审计

输入包括参与者哈希、关系谓词、信号计数、证据数量和窗口规模，不含原始聊天内容。
因此评估只反映结构化信号是否支持候选，不能替代基于原文的语义核实。
memory event 与 observation 的 ID 空间独立，证据数量分别去重后相加。

成功结果保存在 memory item 的 `value_json.relation.typesafe_shadow` 中：

- `status`：`completed`、`error` 或 `unavailable`。
- `model`、`usage`：实际返回的模型版本和用量。
- `decision`、`supported`、`quality`：选择、概率、评分和置信度。
- 上游提供时保留 `request_id`、`trace_id`；未提供时不生成虚构 ID。

数值保留 JSON 数字类型，审计对象限制大小。窗口运行结果中的
`typesafe_shadow.attempted/completed/failed` 提供汇总计数。
只在新的窗口候选生成时执行，不自动重跑历史数据。

## 验证与部署

```bash
uv sync --frozen --extra dev
uv run pytest tests/unit/test_typesafe.py tests/unit/test_typesafe_group_shadow.py \
  tests/unit/test_memory_graph.py tests/unit/test_memory_group_graph_auto_extract.py \
  tests/unit/test_config_precedence.py tests/unit/test_builtin_plugin_background_lifecycle.py
sudo AGENT_CONSOLE_ENV_FILE=.env bash scripts/deploy-server.sh
```

部署前备份服务器配置与代码。部署后检查 `/healthz`、`/readyz`、所有后端进程
的客户端配置、实际 TypeSafe 请求，以及 scheduler 到微信 SDK 的连接。
停用时设置 `COMPOSE_TYPESAFE_ENABLED=false`，并重新创建后端容器使环境变量生效。

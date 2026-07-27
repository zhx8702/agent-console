# 消息流编排演进方案

本文档记录当前消息处理链路从“固定 Pipeline + 插件 Hook”向“平台内核 + 可编排 Message Flow”演进的评估方案。目标不是马上引入重型工作流系统，也不是立刻推翻现有 `DialogOrchestrator`，而是在项目还未臃肿时提前定义边界，避免后续所有能力都继续堆到线性 pipeline 和 `ctx.extras` 里。

## 背景

当前平台本质是消息处理平台：

```text
消息入站 -> 归一化 -> 队列 -> 会话/预处理/路由/能力执行/后处理 -> 队列 -> 消息出站
```

现有主链路：

- 入站 HTTP、验签、限流、幂等、发布 inbound stream：`app/ingress/router.py`
- Inbound Worker 消费消息并调用 orchestrator：`app/workers/inbound_worker.py`
- 消息主处理逻辑：`app/orchestrator/engine.py`
- Pipeline 上下文：`app/orchestrator/pipeline.py`
- 插件 Hook 点：`app/plugin/hooks.py`
- 出站派发：`app/egress/dispatcher.py`

`DialogOrchestrator._run()` 当前按固定顺序执行：

```text
load session
-> before_preprocess hooks
-> preprocess
-> after_preprocess hooks
-> append user turn
-> handoff short-circuit
-> input safety
-> before_route hooks
-> faq preview / router signals
-> route
-> after_route hooks
-> before_capability hooks
-> capability dispatch
-> after_capability hooks
-> output safety
-> before_postprocess hooks
-> postprocess
-> after_postprocess hooks
-> append assistant turn
-> state transition
-> publish outbound
```

这个设计在当前阶段清晰、直接、可测试。但随着插件市场、Agent tools、命令中心、积分、审核、记忆、渠道回复策略、绘图长任务等能力增加，固定 pipeline 的扩展压力会持续上升。

### 当前行为等价边界

后续引入 FlowRunner 时，第一目标是行为等价，而不是顺手优化现有顺序。当前 `_run()` 有几个容易在抽象时被抹平的细节：

- `append_user_turn` 发生在 `handoff_short_circuit`、`input_safety`、`BEFORE_ROUTE` hooks 之前。也就是说，当前命令消息、群聊不回复消息、复读消息在多数情况下都会先写入 user turn，再被后续 hook 短路。
- `handoff_short_circuit` 当前不走 `_finalize()`，而是直接 `_emit_canned()`。它不会 append assistant turn、不会 state transition、不会 output safety，只会构造 canned reply 并运行 `AFTER_POSTPROCESS` hooks 后发布或被微信 hook suppress。
- `HookAbort` 在不同阶段隐含不同 finalize 策略。当前 preprocess / route / before_capability / after_capability 的 `HookAbort` 都会转成 canned result，并以 `skip_output_safety=True` 进入 finalize；普通 capability 结果则会继续走 output safety。
- 渠道正式回复不一定都由 generic outbound bus 最终投递。微信渠道当前仍通过 `plugin_wxbot_reply_queue` 和 wxbot bridge 发送正式回复，并设置 `suppress_outbound=True` 避免同一回复同时进入 generic outbound stream；绘图、地图等业务插件已经可以通过 `ChannelRegistry` 调用当前渠道 outbound provider，未来飞书、Discord 也应注册自己的 provider。

因此阶段 2 的默认 flow 必须完整复刻当前 `_run()` 顺序和短路语义。任何把 `command_dispatch`、`reply_policy`、`repeater` 移到 `append_user_turn` 前的调整，都应视为阶段 4/5 的显式行为变更，必须配套 golden case 和迁移说明。

## 当前问题

### 线性 Pipeline 的局限

固定 pipeline 适合简单顺序处理，但后续会遇到以下场景：

- 不同渠道需要不同流程，例如 Web、微信私聊、微信群、系统事件、API 事件。
- 不同租户或群可能需要不同流程，例如启用记忆、禁用 Agent、只允许命令、不自动回复。
- 某些能力天然是前后成对的事务，例如积分 reserve / capture / release。
- 某些处理适合并行准备，例如群配置、记忆画像、FAQ preview、风控信号、插件运行状态。
- 某些能力需要短路，但不是异常，例如命令已处理、审核拦截、群聊回复策略决定不回复。
- 某些能力需要子流程，例如绘图、地图任务、异步进度消息、多轮表单、人工接管。
- 插件市场上线后，插件不只会贡献 hooks/tools/commands，也会希望贡献消息处理步骤。

如果继续只增加 hook point，系统会变成：

```text
更多 HookPoint + priority 排序 + ctx.extras 私有约定 + HookAbort 短路
```

这会导致插件之间的依赖和副作用越来越难分析。

### `ctx.extras` 已经承担隐式协议

当前 `PipelineContext.extras` 中已经出现大量隐式控制字段，例如：

- `suppress_outbound`
- `skip_assistant_turn`
- `skip_state_transition`
- `router_signals`
- `agent_tool_scope`
- `_billing_command_reservation`
- `_credits_cost`
- `reply_policy_override`
- `draw_result`
- `repeater`

这些字段让插件可以快速扩展链路，但它们没有统一 schema、权限、owner、输入输出声明和冲突检测。短期可接受，长期会削弱可维护性。

### 当前 `extras` 协议盘点

迁移前需要先把隐式字段登记为兼容协议，避免 FlowStep 化时遗漏行为。建议按“公开控制字段”和“插件私有字段”分层处理：公开控制字段迁入 `signals`、`effects` 或 `StepResult`；插件私有字段只允许在同 owner step 内传递，跨 owner 读取必须升级为 typed signal。

| 字段 | 当前 owner | 当前用途 | 建议迁移目标 | 兼容期处理 |
|---|---|---|---|---|
| `router_signals` | core / wxbot / faq preview | 给 router 追加 FAQ、工具可用性等信号 | `ctx.signals.router.*` | FlowRunner 读取旧字段并合并到 `signals.router` |
| `agent_tool_scope` | channel / credits / agent | 限定 Agent 可用工具集合 | `ctx.signals.agent.tool_scope` | `core.route` 同时读取 signal 和旧 extras；scope 名称使用 `group_*` canonical scope |
| `suppress_outbound` | channel / agent / repeater | 阻止最终 generic outbound publish | `StepResult(action="suppress_outbound")` 或 `effects.publish_outbound.enabled=false` | finalize 阶段保留旧字段优先级；不等于“渠道不发消息” |
| `skip_assistant_turn` | channel / agent | 不写 assistant turn | `MessageEffect(type="append_assistant_turn", enabled=false)` | 与 suppress outbound 分开，避免误删审计轨迹 |
| `skip_state_transition` | channel | 不推进会话状态 | `MessageEffect(type="set_session_state", enabled=false)` | 仅 core step 可消费 |
| `reply_policy_override` | repeater / channel | 通用回复策略覆盖，例如强制发送、不 @ 发送者 | `ctx.signals.reply_policy.override` | 已从旧 `wxbot_force_*` 迁到 `app.channel.reply_policy`；必须记录 owner 和 reason |
| `wxbot_reply_policy` | wxbot | 微信群聊是否回复、原因、命中规则 | `ctx.signals.reply_policy` | 由 `plugin.wxbot.reply_policy` 输出；未来其他渠道应输出同一 reply policy signal |
| `wxbot_map_progress_enqueued` | wxbot | 标记地图进度消息已入队 | `MessageEffect(type="emit_progress_message")` | 迁为 effect 后用 idempotency_key 去重 |
| `_command_token` / `_command_plugin` / `_command_canonical` | commands | 记录命中命令，供 draw、wxbot 等后续 hook 判断 | `ctx.signals.command.*` | 私有下划线字段只保留给兼容 adapter |
| `_billing_command_reservation` / `_billing_command_deferred` | commands / draw | 命令级计费预留和延迟结算 | `effects.reserve_credits` + `effects.capture_credits` | 优先迁入 credits step，禁止跨插件直接读写 reservation |
| `_credits_cost` / `_credits_cfg` / `_credits_reservation_id` / `_credits_deducted` | credits | 计费成本、配置、预留编号、扣减状态 | `ctx.signals.billing.*` + billing effects | settle step 消费自己 owner 的 signal/effect |
| `_credits_agent_billing` | credits | 标记 Agent 工具计费路径 | `ctx.signals.billing.scope` | 与 `agent_tool_scope` 解耦 |
| `_credits_auto_checkin_done` / `_credits_auto_checkin_result` | credits | 自动签到去重和结果复用 | `ctx.signals.credits.auto_checkin` | 仍属于 credits owner 私有状态 |
| `user_memory_profile` | memory | 注入用户记忆画像 | `ctx.signals.memory.user_profile` | prompt 构造读取 typed signal |
| `_moderation_*` | moderation | 审核命中、提醒模式和提醒文本 | `ctx.signals.moderation.*` | block/replace/append 由 StepResult 表达 |
| `draw_result` | draw | 绘图结果转图片出站结构 | `ctx.signals.draw.result` + `MessageEffect(type="publish_media")` | postprocess adapter 兼容旧字段 |
| `repeater` | repeater | 群复读触发状态和审计信息 | `ctx.signals.repeater` | 触发回复使用 `stop` + canned result/effect |

命名约定建议：

- `signals.<domain>.*`：可被后续 step 读取的结构化事实，只描述状态，不直接执行副作用。
- `effects.<type>`：需要 commit、审计、补偿或出站派发的副作用。
- `scratch.<owner>.*`：单 owner 兼容字段，不进入跨插件协议，也不允许 flow 条件引用。
- 下划线开头的旧字段视为 legacy-private，只能由兼容 adapter 读写，新 FlowStep 不新增此类字段。

`signals` 第一版可以使用 `dict[str, Any]` 承载，但跨 owner 的公开 signal 必须登记 schema，避免把 `ctx.extras` 的问题搬到 `ctx.signals`：

| 登记项 | 说明 |
|---|---|
| key | 例如 `signals.router.faq_similarity`、`signals.agent.tool_scope` |
| owner | 负责生产和维护 schema 的 core 或插件 |
| schema_version | signal payload 版本 |
| payload_schema | 字段类型、必填项、默认值 |
| write_policy | `create_only`、`merge`、`replace` |
| consumers | 已知消费 step，用于禁用插件或升级时影响分析 |
| trace_policy | 哪些字段允许进入 trace/log |
| deprecated_after | 兼容字段计划下线时间或版本 |

### 当前非幂等副作用盘点

FlowRunner 引入 timeout、retry、DLQ 或 replay 后，不能重复执行已成功的非幂等副作用。迁移前需要把现有 hook 内直接执行的外部副作用先登记为 effect，至少保证 idempotency key、审计和补偿策略清楚。

| owner | 当前位置 | 外部副作用 | 当前幂等依据 | 目标 effect | 迁移注意事项 |
|---|---|---|---|---|---|
| wxbot | `BEFORE_ROUTE` / `wxbot.agent_intent` | 显式地图生成请求会提前入队一条“正在生成地图”的进度消息 | `wxbot-progress:{tenant_id}:{source_message_id}:amap-map` | `emit_progress_message` 或 `enqueue_wxbot_reply(progress=true)` | 进度消息和正式回复必须使用不同 effect type / idempotency_key，不能被 `suppress_outbound` 误吞 |
| wxbot | `AFTER_POSTPROCESS` / `wxbot.reply_queue` | 正式微信回复入 `plugin_wxbot_reply_queue`，随后 suppress generic outbound | `wxbot-reply:{tenant_id}:{source_message_id}:{index}` | `enqueue_channel_reply(channel=wechat)` 或兼容期 `enqueue_wxbot_reply` + `publish_outbound.enabled=false` | 入队成功但 generic publish 失败不应重放微信入队；入队失败才按出站失败处理 |
| moderation | `AFTER_PREPROCESS` / `moderation.audit` | 写审核事件，并可能调用外部 webhook | 当前依赖 store 事件记录，webhook 无统一幂等 | `write_audit_event` + `send_webhook` | webhook 应可 fail-open，并记录 status；重试不得重复创建审核事件 |
| credits | `BEFORE_CAPABILITY` / `credits.deduction` | 自动签到、余额检查、积分预留 | reservation id / trace_id | `auto_checkin` + `reserve_credits` | 余额不足是业务拒绝；reserve 成功后任何短路、异常、超时都必须 release 或进入补偿队列 |
| credits | `AFTER_CAPABILITY` / `credits.settlement` | capture / release 预留积分，或直接 adjust | reservation id / `_credits_deducted` | `capture_credits` / `release_credits` | settle 失败不能静默；需要审计并支持幂等重试 |
| commands | `BEFORE_ROUTE` / `commands.center` | 命令级计费 reserve/capture/release，执行命令 handler | billing reservation / command handler 自身约束 | `reserve_credits(command)` + `execute_command` + `capture_credits(command)` | draw 等长任务可能设置 `_billing_command_deferred`，capture 时机不能由跨插件 extras 隐式决定 |
| draw | command handler / postprocess hook | 调用绘图服务、保存图片结果、通过 `ChannelRegistry` 发回当前渠道 | image id / command id / store 约束 | `start_draw_task` + `publish_media` / `enqueue_channel_reply(media)` | 绘图成功但出站失败时不能重复生成图片；媒体 payload 需要 typed schema；微信只是其中一个 channel provider |
| memory | `AFTER_POSTPROCESS` / memory save | 保存用户记忆画像或对话摘要 | 当前由插件实现决定 | `save_memory` | 最好在 commit 成功后执行，或保证可幂等重试 |

阶段 2 可以暂时保留这些 hook 的直接副作用，但 FlowRunner 必须把它们标记为 legacy side effect，并避免自动重试已执行过的 hook step。对这类 legacy Hook adapter，阶段 2 不应引入会中断正在执行代码的硬 timeout，也不应在 step 抛错、worker 重启或 DLQ replay 时重新执行该 hook；只能按当前 worker 语义重放整条消息，且必须依赖现有幂等约束。真正的自动 retry / replay 只能发生在副作用迁移为带 `idempotency_key` 的 `MessageEffect` 之后。阶段 4 迁移插件时，再逐步改为 `MessageEffect` 提交。

### Hook 不是编排

Hook 适合做局部增强，例如“在路由前补充信号”“在回复后做持久化”。但 Hook 不适合表达完整流程：

- 无法显式声明输入输出。
- 无法清晰表达分支。
- 无法静态校验依赖。
- 无法在前端展示流程拓扑。
- 无法可靠判断禁用插件后哪些流程会失效。
- 无法表达成对补偿逻辑。

因此，Hook 应保留为低成本扩展点，但不能成为长期唯一扩展模型。

## 目标

第一阶段目标是提前设计“可编排消息流”的架构边界，并为后续逐步落地留出接口：

- 将平台不可绕过的处理抽象为 `Message Kernel`。
- 将业务处理链路抽象为 `Message Flow`。
- 定义可验证的 `FlowStep`、`StepResult`、`MessageEffect`。
- 将现在的固定 pipeline 迁移为默认线性 flow，保持行为不变。
- 支持按 channel、tenant、session 或 message type 选择不同 flow profile。
- 支持插件贡献 flow step，并和插件市场 owner、权限、启停状态绑定。
- 减少 `ctx.extras` 私有约定，逐步迁移到 typed signals/effects。
- 为后续分支、并行 enrich、子流程、长任务提供演进路径。

## 非目标

第一阶段不做：

- 不引入 Temporal、Airflow、BPMN 等重型工作流引擎。
- 不做前端拖拽式流程编排。
- 不允许管理员任意绕过安全、审计、出站、会话锁等平台内核。
- 不立即把所有插件 Hook 改成 FlowStep。
- 不支持任意循环或无限 DAG。
- 不把每条消息都拆成多条跨进程 workflow task。
- 不牺牲当前消息处理延迟和可观测性。

## 设计原则

1. 内核固定，业务可编排

   平台安全边界、消息可靠性、幂等、会话锁、审计、队列、DLQ、最低安全策略等属于内核，不进入用户级自由编排。

2. 先兼容，再抽象

   默认 flow 必须完整复刻当前 `DialogOrchestrator._run()` 行为。第一版可以只是“用 FlowRunner 执行固定线性步骤”，不改变业务结果。

3. 受控编排，不做任意代码流程

   Flow 只允许引用已注册的 step kind。每个 step kind 都有 owner、权限、输入输出、错误策略和 timeout。

4. 显式结果代替异常控制流

   命令命中、审核拦截、忽略回复、降级回复、异步延迟等结果应尽量通过 `StepResult` 表达，而不是全部通过 `HookAbort` 和 `ctx.extras`。

5. 插件能力 owner 化

   插件贡献的 FlowStep 必须带 owner。禁用插件时，引用该 owner step 的 flow 必须进入 invalid、degraded 或 pending_restart 状态。

6. Flow 可观测

   每个 step 都要有 trace span、耗时、结果、错误、owner 和 route 标签。否则编排只会让排障更困难。

7. 可渐进迁移

   Hook 继续存在。核心插件可以逐步从 Hook 迁到 FlowStep，不要求一次性重写。

## 总体架构

建议拆成两层：

```text
Ingress / Bus / Worker
        |
        v
Message Kernel
        |
        v
FlowResolver -> FlowRunner -> MessageFlow
        |
        v
Commit / Outbound Bus
        |
        v
Outbound Worker / Dispatcher
```

### Message Kernel

`Message Kernel` 是平台不变量，不提供普通插件或租户级自由编排。

职责：

- 入站验签、限流、幂等。
- inbound/outbound stream publish。
- worker 消费、重试、DLQ。
- trace、tenant、session context。
- session lock。
- flow profile 解析。
- flow 编译校验。
- step timeout 和错误兜底。
- 最终 commit 和 outbound publish 的平台级保护。
- 基础审计和指标。

不建议编排的内核步骤：

- HMAC 验签。
- 入站幂等。
- Redis stream ack / retry / DLQ。
- session lock。
- trace context。
- 插件启停状态检查。
- flow schema 校验。
- 系统级输出安全底线。

### Message Flow

`Message Flow` 是可配置的业务处理链路。

可以进入 flow 的步骤：

- load session
- preprocess
- append user turn
- reply policy
- command dispatch
- moderation
- memory load
- router signal enrichment
- route decision
- capability dispatch
- billing reserve / capture / release
- output safety
- postprocess
- memory save
- append assistant turn
- state transition
- publish decision
- plugin-provided custom steps

注意：第一阶段可以仍然把 `commit_turns_and_publish` 作为 core step，但它必须受 Kernel guardrail 保护。

## 核心模型

### MessageFlowContext

建议逐步替代当前 `PipelineContext`，或先让 `PipelineContext` 兼容扩展为同等语义。

```python
@dataclass
class MessageFlowContext:
    event: InboundEvent
    trace_id: str
    session: Session | None = None
    pre: PreprocessedMessage | None = None
    route: RouteDecision | None = None
    result: CapabilityResult | None = None
    reply: OutboundReply | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    effects: list[MessageEffect] = field(default_factory=list)
    scratch: dict[str, Any] = field(default_factory=dict)
```

字段语义：

- `signals`：给后续步骤消费的结构化信号，例如 `faq_similarity`、`agent_tool_scope`、`reply_policy`。
- `effects`：需要在 commit 阶段统一处理或审计的副作用。
- `scratch`：临时兼容空间，替代当前 `extras`，但不鼓励长期依赖。

### FlowStep

```python
class FlowStep(Protocol):
    kind: str
    owner: str
    name: str
    permissions: list[str]
    inputs: set[str]
    outputs: set[str]
    timeout_seconds: float

    async def run(self, ctx: MessageFlowContext) -> StepResult:
        ...
```

每个 step 必须声明：

- `kind`：稳定唯一标识，例如 `core.preprocess`、`plugin.memory.load`。
- `owner`：`core` 或插件名。
- `permissions`：需要的权限，用于安装、启用和 flow 编译时校验。
- `inputs`：依赖的上下文字段。
- `outputs`：会写入的上下文字段或 signals/effects。
- `timeout_seconds`：单步超时。

### StepResult

```python
@dataclass
class StepResult:
    action: str = "continue"
    reason: str = ""
    route_label: str = "unknown"
    result: CapabilityResult | None = None
    finalize: bool = False
    skip_output_safety: bool = False
    append_assistant_turn: bool | None = None
    publish_outbound: bool | None = None
    effects: list[MessageEffect] = field(default_factory=list)
    error: str = ""
```

建议 action 枚举：

| action | 含义 |
|---|---|
| `continue` | 继续执行后续 step |
| `stop` | 正常停止 flow，不再执行后续 step |
| `replace_result` | 替换 `ctx.result`，继续或进入 finalize |
| `suppress_outbound` | 不发布出站消息 |
| `defer` | 当前消息转为异步任务或长任务 |

`StepResult.action` 只表达 step 正常完成后的业务控制流，不表达异常处理策略。`fail_open`、`fail_closed`、`degrade`、`retry` 属于 `error_policy`，只在 step 抛异常、超时或外部依赖失败时由 Runner 使用，不能作为普通业务短路结果返回。

第一版可以只实现 `continue`、`stop`，并为兼容 adapter 支持有限的 `replace_result` / `suppress_outbound` 翻译；其他 action 先预留。

辅助字段语义：

- `result`：step 产出的 canned 或替换后的 `CapabilityResult`。命令、审核替换、复读、降级都应显式放在这里，而不是只写 `ctx.result`。
- `finalize`：当前 step 结束后是否跳过后续业务 step，直接进入 finalize / commit guardrail。
- `skip_output_safety`：进入 finalize 时是否跳过 output safety。兼容期内用于复刻旧 `HookAbort` 行为；长期应只允许系统认证 canned reply 或已有审计 reason 的安全阻断使用。
- `append_assistant_turn`：覆盖是否写 assistant turn。`None` 表示沿用默认 finalize 策略，`False` 表示明确不写。
- `publish_outbound`：覆盖是否发布 generic outbound。微信队列入队成功后通常应设为 `False`，但不等于“不发消息”。

### StepResult 执行语义

`StepResult` 不能只作为返回枚举，还需要有明确的 runner 语义：

| 场景 | Runner 行为 | 记录要求 |
|---|---|---|
| `continue` | 合并 effects，执行下一个 step | step span 标记 `action=continue` |
| `stop` | 停止后续业务 step，进入 finalize 或 commit guardrail | 必须记录 `reason` 和停止 owner |
| `suppress_outbound` | 继续执行必要的安全和审计 step，但最终不发布正式回复 | 必须保留是否写 assistant turn 的独立决策 |
| `replace_result` | 替换 `ctx.result`，后续 output safety 和 postprocess 仍执行 | 必须记录原 route 和替换 owner |
| `defer` | 写入异步任务 effect，停止同步回复或发送进度消息 | 必须有 idempotency_key 和恢复入口 |

短路不是异常。命令命中、群聊不回复、审核替换、复读触发这类可预期结果应返回 `StepResult`；只有代码错误、外部依赖异常、超时才走 error policy。

Finalize 语义需要单独固定：

- `stop` 不一定等于“什么都不做”。如果 `finalize=True` 且带 `result`，Runner 仍应执行必要的 output safety、postprocess、assistant turn、state transition 和出站决策，除非 `StepResult` 明确覆盖。
- `stop` 也不一定等于“有回复”。静默停止必须显式表达为 `finalize=True`、`result=None`、`append_assistant_turn=False`、`publish_outbound=False`，并保留 reason；典型场景是群聊策略不回复、自发消息 audit-only、重复消息去重。
- `skip_output_safety=True` 只兼容旧行为，不应成为普通插件默认能力。Flow compiler 需要校验该字段只能由 core step 或具备安全权限的插件 step 设置。
- `suppress_outbound` / `publish_outbound=False` 只表示不发 generic outbound，不应自动跳过 `append_assistant_turn`、`save_memory`、`capture/release credits` 或渠道队列投递。
- `append_assistant_turn=False` 必须有 reason，例如群聊策略不回复、自发消息 audit-only、result metadata 明确要求 suppress final reply。
- handoff 需要作为特殊兼容路径登记：当前行为等价于 `result=HANDOFF_PENDING`、`finalize=True`、`skip_output_safety=True`、`append_assistant_turn=False`，且只运行 `AFTER_POSTPROCESS` 兼容 hook。

冲突处理建议：

- 多个 step 写同一个 `signals` key 时，默认禁止覆盖，除非 step definition 显式声明 `writes: replace`。
- 多个 step 同时要求 `suppress_outbound` 时可以合并，最终 reason 保留列表。
- `suppress_outbound` 不自动等于 `skip_assistant_turn`，两者必须分别表达。
- `replace_result` 只能发生在 capability 之后、postprocess 之前，第一版不要允许任意位置替换。
- error policy 产生的阻断优先级高于 `suppress_outbound`，因为系统异常和安全阻断不能被静默吞掉。
- billing effects 必须按 `reserve -> capture | release` 成对出现；Flow compiler 可以先做静态顺序检查，运行期再用 idempotency_key 保证幂等。

### MessageEffect

副作用建议显式化：

```python
@dataclass
class MessageEffect:
    type: str
    owner: str
    payload: dict[str, Any]
    idempotency_key: str = ""
```

典型 effect：

- `append_user_turn`
- `append_assistant_turn`
- `publish_outbound`
- `enqueue_channel_reply`
- `enqueue_wxbot_reply`
- `reserve_credits`
- `capture_credits`
- `release_credits`
- `save_memory`
- `emit_progress_message`
- `write_audit_event`
- `set_session_state`

第一版不一定要把所有副作用都延后到统一 commit，但应该先定义 effect 结构，为后续补偿和审计留出口。

建议把当前 `_finalize()` 概念拆成三个语义层，哪怕第一版仍在一个方法里实现：

| 层级 | 职责 | 例子 |
|---|---|---|
| finalize | 把业务结果变成可提交的回复和 effects | output safety、postprocess、构造 reply、决定是否写 assistant turn |
| commit | 按顺序提交可审计、可幂等的 effects | append turns、set state、reserve/capture/release credits、save memory |
| dispatch | 把回复交给具体出站通道 | publish generic outbound、enqueue channel reply、emit progress message |

这样可以避免把“构造回复”“写状态”“发送到渠道”混成一个不可补偿的大步骤。

出站 effect 需要区分三类：

| effect | 含义 | 幂等要求 |
|---|---|---|
| `publish_outbound` | 发布到平台 generic outbound stream，由 outbound worker / dispatcher 处理 | `trace_id + session_id + reply_index` 或显式 `idempotency_key` |
| `enqueue_channel_reply` | 通过 `ChannelRegistry` 交给当前渠道 outbound provider；provider 可直接发、入队或转 bridge | 必须包含 `channel`、`target`、`idempotency_key`；同一正式回复应与 generic outbound 互斥 |
| `enqueue_wxbot_reply` | 兼容期微信专用 effect，写入 `plugin_wxbot_reply_queue`，由 wxbot SDK bridge 发送 | 必须包含 `command_id` / `idempotency_key`，并与 generic outbound 互斥；长期应收敛到 `enqueue_channel_reply(channel=wechat)` |
| `emit_progress_message` | 发送长任务进度、处理中提示等非最终回复 | 必须和最终回复使用不同 key，且不受最终回复 `suppress_outbound` 影响 |

微信正式回复的目标语义应是：

```text
postprocess
-> enqueue_channel_reply(channel=wechat)
-> publish_outbound.enabled=false
```

因此 `suppress_outbound` 在渠道链路上不代表“用户不会收到消息”，只代表“不再发布 generic outbound stream”。具体是否发消息由 `enqueue_channel_reply` / `emit_progress_message` 等 effect 决定。

## Flow 定义

第一版建议支持 YAML 或内置 Python definition。为了便于评估，可以先用 YAML 描述目标形态：

```yaml
name: default_message_flow
version: 1
description: Default flow equivalent to current DialogOrchestrator behavior.

match:
  channels: ["*"]
  message_types: ["*"]

steps:
  - id: load_session
    kind: core.load_session

  - id: preprocess
    kind: core.preprocess
    hooks:
      before: before_preprocess
      after: after_preprocess

  - id: append_user_turn
    kind: core.append_user_turn
    when:
      metadata_not:
        is_self_sent: true

  - id: handoff_short_circuit
    kind: core.handoff_short_circuit

  - id: input_safety
    kind: core.input_safety

  - id: command_dispatch
    kind: plugin.commands.dispatch
    when:
      message_type: text

  - id: reply_policy
    kind: plugin.channel.reply_policy
    when:
      session_kind: group

  - id: moderation
    kind: plugin.moderation.check

  - id: memory_load
    kind: plugin.memory.load

  - id: route
    kind: core.route

  - id: credits_reserve
    kind: plugin.credits.reserve
    when:
      route_in: [agent, llm]

  - id: capability
    kind: core.capability_dispatch

  - id: credits_settle
    kind: plugin.credits.settle

  - id: output_safety
    kind: core.output_safety

  - id: postprocess
    kind: core.postprocess

  - id: memory_save
    kind: plugin.memory.save

  - id: commit
    kind: core.commit_turns_and_publish
```

### 兼容 Flow 与目标 Flow

阶段 2 的 default flow 是兼容层，不是最终插件化目标形态：

- `hooks.before` / `hooks.after` 只允许出现在 core step 的 legacy adapter 上，用于复刻当前 HookManager 调用点。
- 兼容 flow 可以保留 `BEFORE_*` / `AFTER_*` 命名，但 UI 应标记为 legacy hook slot，而不是普通插件 step。
- 目标 flow 中插件必须通过 `kind` 注册独立 `FlowStepDefinition`，声明 inputs、outputs、permissions、error_policy 和 effects。
- 阶段 4 之后新插件不应再依赖 hook slot 插入关键流程；hook 只保留为局部观察或低风险增强。
- 同一个插件不能同时在 legacy hook slot 和目标 step 中执行同一副作用，迁移时需要用 feature gate 或 flow version 做互斥。

### FlowResolver

Flow 选择规则：

```text
tenant override > channel/session override > channel default > global default
```

匹配细则建议：

- `tenant_id` 精确匹配优先于空 tenant。
- `channel` 精确匹配优先于 `*`。
- `session_kind` 精确匹配优先于空 kind。群聊/频道建议统一归一化为 `group`，私聊/DM 统一为 `private`。
- `session_id_pattern` 精确匹配优先于 glob；glob 优先于空 pattern。
- `message_type` 精确匹配优先于 `*`。
- `priority` 值越小优先级越高；priority 相同时按匹配 specificity 排序。
- specificity 仍相同时按 `updated_at`、`id` 或配置文件顺序做稳定排序，避免同一消息在不同进程命中不同 flow。
- 第一版 `session_id_pattern` 建议只支持 glob，不支持任意 regex，降低误配和 ReDoS 风险。

Resolver 需要在 trace/log 中记录最终命中的 binding 和未命中原因，便于解释“为什么这条消息走了这个 flow”。

示例：

```yaml
profiles:
  - name: wechat_group_flow
    match:
      channel: wechat
      session_kind: group
      session_id_pattern: "*@chatroom"
    flow: default_wechat_group

  - name: discord_group_flow
    match:
      channel: discord
      session_kind: group
    flow: default_group_channel

  - name: web_flow
    match:
      channel: web
    flow: default_web
```

第一版可以只实现 global default，不做多 profile。文档先定义未来目标。

## Flow 编译与校验

Flow 运行前必须编译校验，不能每条消息临时做复杂判断。

校验项：

- Flow 无环。
- Step id 唯一。
- Step kind 必须已注册。
- Step owner 对应插件必须 installed/enabled，或 Flow 标记为 degraded/invalid。
- Step permissions 必须被插件 manifest 声明并授权。
- required inputs 必须由前置 step 产生，或由 Kernel 提供。
- core required steps 不得缺失。
- 禁止普通插件替换或绕过 Kernel guardrail。
- timeout、error_policy 必须有默认值。
- `when` 条件必须只引用允许的字段。
- `signals` 写入不能冲突，除非 step 明确声明允许覆盖。
- `effects` 必须有 owner、type 和可审计 payload，高风险 effect 必须有 idempotency_key。
- billing 类 effect 必须满足 `reserve` 后只能 `capture` 或 `release` 一次。
- `suppress_outbound` 后仍必须保留必要的审计、安全和补偿 step。
- `replace_result` 不能跳过 output safety，除非结果类型是系统认证 canned reply 且有 audit reason。

编译结果：

```python
@dataclass
class CompiledFlow:
    name: str
    version: int
    steps: list[CompiledStep]
    status: str  # active / degraded / invalid
    warnings: list[str]
```

状态语义：

- `active`：可正常运行。
- `degraded`：部分可选 step 不可用，仍可运行。
- `invalid`：缺少核心 step、必需插件禁用、输入输出不满足，禁止启用。

## Kernel Guardrails

可编排必须有硬边界：

1. 入站安全不可绕过

   验签、限流、幂等不进入普通 flow。

2. 会话锁不可绕过

   同一 `session_id` 内消息仍必须串行处理，避免状态乱序。

3. 最低输出安全不可绕过

   普通回复必须经过 output safety。系统认证 canned reply 可以有显式 skip 标记和审计。

4. 出站发布受控

   插件不能直接绕过 outbound bus 发正式回复。进度消息也应通过受控 effect 或 dispatcher。

5. 权限与 owner 绑定

   插件 step 只能使用 manifest 声明的权限。禁用插件后 flow 编译状态必须变化。

6. 无界循环禁止

   第一版只支持线性 flow。后续如支持分支和循环，循环必须有最大次数、预算和审计。

7. 每步有预算

   每个 step 有 timeout；整个 flow 仍有 total timeout。

8. 错误策略显式

   每个 step 的错误策略必须是 `fail_open`、`fail_closed`、`degrade` 或 `retry` 之一。

## 插件集成

插件市场文档中已经规划插件 owner、权限、启停、runtime status。消息流编排应和插件市场共享同一套 owner 和权限模型。

插件 manifest 后续可扩展：

```yaml
capabilities:
  hooks: []
  agent_tools: []
  commands: []
  flow_steps:
    - kind: plugin.memory.load
      display_name: 读取用户记忆
      inputs: ["event", "session", "pre"]
      outputs: ["signals.user_memory_profile"]
      permissions: ["storage:shared"]
      timeout_seconds: 1.5
      error_policy: fail_open
    - kind: plugin.memory.save
      display_name: 保存用户记忆
      inputs: ["event", "session", "reply"]
      outputs: ["effects.save_memory"]
      permissions: ["storage:shared"]
      timeout_seconds: 2.0
      error_policy: fail_open
```

插件接口可增加：

```python
class Plugin:
    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return []
```

注册要求：

- `FlowStepDefinition.kind` 必须以 `plugin.{plugin_name}.` 开头。
- `owner` 必须等于插件名。
- Step 权限必须是插件 manifest permissions 的子集。
- 插件 disabled 时，FlowStepRegistry 必须 unregister owner 或标记不可用。
- `kind` 必须稳定，不能因展示名称或内部实现调整而变化。
- Step definition 需要带 `schema_version` 或 `definition_version`。插件升级后如果 inputs/outputs、permissions、effect 类型或 error policy 发生 breaking change，引用该 step 的 flow 必须重新 validate。
- 如果插件升级导致旧 flow 无法满足新 step schema，flow 状态应进入 `invalid` 或 `pending_migration`，不能静默继续运行。

## 与现有 Hook 的关系

Hook 不立即删除。建议分三类处理：

| 类型 | 处理策略 |
|---|---|
| 简单增强 Hook | 保留，例如补充 router signals、记录审计 |
| 改变流程走向的 Hook | 逐步迁为 FlowStep，例如 commands、reply policy、moderation |
| 成对事务 Hook | 优先迁为 FlowStep + Effect，例如 credits |

迁移优先级建议：

1. `commands`：从 `BEFORE_ROUTE` Hook 迁为 `plugin.commands.dispatch`。
2. `reply policy`：从多个 Hook/extras 迁为通用 `plugin.channel.reply_policy`，微信实现可先由 `plugin.wxbot.reply_policy` 适配。
3. `moderation`：迁为 `plugin.moderation.check`，明确 block/append/remind。
4. `memory`：拆为 `plugin.memory.load` 和 `plugin.memory.save`。
5. `credits`：拆为 `reserve`、`settle`，并补偿失败路径。
6. `draw`：命令仍归 commands，但结果后处理可迁为 dedicated step。

### Hook 到 FlowStep 迁移映射

| 现有 HookPoint / 插件 | 当前行为 | 目标 FlowStep | 目标结果语义 | 迁移注意事项 |
|---|---|---|---|---|
| `BEFORE_PREPROCESS` / wxbot normalize | 归一化微信事件、自发消息处理 | `plugin.wxbot.normalize_event` | 输出 normalized metadata 或 `stop` | 自发消息 audit-only 不能丢，仍要允许 after_postprocess 类审计 |
| `AFTER_PREPROCESS` / moderation inspect | 记录命中词和提醒模式 | `plugin.moderation.inspect_input` | 输出 `signals.moderation.*` | 不在此步直接阻断，阻断交给 capability 前 step |
| `BEFORE_ROUTE` / commands | 解析命令、鉴权、执行命令并 `HookAbort` | `plugin.commands.dispatch` | 命中后 `stop` + canned result，或输出 `signals.command.*` | 命令回复仍需经过 postprocess 的渠道适配，但可跳过 route/capability |
| `BEFORE_ROUTE` / channel reply policy | 群聊/频道是否回复、mention、关键词和渠道特定回复策略 | `plugin.channel.reply_policy`；微信兼容实现为 `plugin.wxbot.reply_policy` | `continue`、`suppress_outbound` 或 `stop` | 命令是否绕过“不 @ 不回复”必须由 command step 和 policy step 的顺序明确；渠道特有字段只能留在渠道 step 内 |
| `BEFORE_ROUTE` / agent scope enrich | 根据群/频道能力写入工具范围和 router signals | `plugin.agent.scope_enrich` 或渠道插件贡献的 enrich step | 输出 `signals.agent.tool_scope`、`signals.router.tools_available` | 不应直接改 route hints，统一由 route step 合并 signals；具体工具还必须按 `channels` / `session_kinds` metadata 过滤 |
| `BEFORE_ROUTE` / repeater | 检测连续复读并 `HookAbort` | `plugin.repeater.detect` | 触发后 `stop` + canned result + audit effect | 需要在 append user turn 前后重新确认，因为当前逻辑依赖历史 turns |
| `AFTER_ROUTE` / 预留 | 路由后补充或审计 | `plugin.*.after_route` 或保留 Hook | 输出 typed signals | 简单审计 Hook 可以长期保留 |
| `BEFORE_CAPABILITY` / persona_extract | 写入 persona skill 变量 | `plugin.persona.skill_enrich` | 输出 `signals.persona.*` 或更新 session effect | 需要避免与 memory prompt 注入重复 |
| `BEFORE_CAPABILITY` / memory load | 读取用户记忆并写 prompt 相关字段 | `plugin.memory.load` | 输出 `signals.memory.user_profile` | 失败默认 fail-open |
| `BEFORE_CAPABILITY` / credits reserve | 检查余额、预留积分、查询积分命令短路 | `plugin.credits.reserve` / `plugin.credits.query_command` | 正常为 `continue` 或 `stop`；余额/一致性异常按 `error_policy=fail_closed` | 查询命令应归 commands 或 credits command step；付费能力不能 fail-open 免费放行 |
| `BEFORE_CAPABILITY` / moderation replace | 审核命中时替换为提醒 | `plugin.moderation.enforce_input` | `stop` 或 `replace_result` | 安全阻断要有 route label 和 audit effect |
| `AFTER_CAPABILITY` / credits settle | 扣减或释放积分 | `plugin.credits.settle` | 输出 capture/release effects | 必须覆盖 capability 异常、degrade、suppress outbound 的路径 |
| `AFTER_CAPABILITY` / moderation append | 在结果后追加提醒 | `plugin.moderation.decorate_output` | `replace_result` | 仅允许修改文本，不允许绕过 output safety |
| `BEFORE_POSTPROCESS` / draw result | 把绘图结果转成图片回复 | `plugin.draw.postprocess_result` | `replace_result` 或 `effects.publish_media` | 媒体发布需要 dispatcher 支持 typed payload |
| `AFTER_POSTPROCESS` / memory save | 保存对话记忆 | `plugin.memory.save` | `effects.save_memory` | 最好在 commit 成功后或可幂等重试 |
| `AFTER_POSTPROCESS` / wxbot outbound policy | 应用群聊 @、静默、图片/地图进度策略 | `plugin.wxbot.outbound_policy` | 修改 publish effect 或 `suppress_outbound` | 不应直接发布正式回复，进度消息也走受控 effect |

兼容期内，FlowStep 可以内部继续调用现有 Hook 逻辑，先改编排模型，再重构插件实现。兼容 adapter 的边界是“把 HookAbort/extras 翻译成 StepResult/signals/effects”，不能让新的 flow 继续扩大隐式 extras 协议。

## 默认 Flow 草案

阶段 2 的默认 flow 必须以当前 `_run()` 行为等价为准。下面先给出“兼容默认 flow”，后面的 web / group channel / wechat 拆分只作为阶段 5 以后候选形态。

### default_compatible_flow

```text
load_session
-> before_preprocess hooks
-> preprocess
-> after_preprocess hooks
-> append_user_turn
-> handoff_short_circuit
-> input_safety
-> before_route hooks
-> faq_preview / router_signal_merge
-> route
-> after_route hooks
-> before_capability hooks
-> capability_dispatch
-> after_capability hooks
-> output_safety
-> before_postprocess hooks
-> postprocess
-> after_postprocess hooks
-> append_assistant_turn
-> state_transition
-> publish_outbound
```

兼容要求：

- `append_user_turn` 仍在 `BEFORE_ROUTE` hooks 之前，避免 command、reply policy、repeater 行为变化。
- `before_route hooks` 兼容期内继续承载 commands、channel/wxbot reply policy、repeater、agent intent 等现有 hook。
- `handoff_short_circuit` 保持当前特殊 finalize 行为，不自动写 assistant turn。
- `after_postprocess hooks` 兼容期内继续允许 channel reply queue、wxbot reply queue、memory save 等 hook 执行；其中 wxbot 入队后仍 suppress generic outbound。
- 阶段 2 不拆多 profile，可以只把这个线性顺序注册为内置 default flow。

### default_web_flow 候选

```text
load_session
-> preprocess
-> append_user_turn
-> handoff_short_circuit
-> input_safety
-> memory_load
-> route
-> capability_dispatch
-> output_safety
-> postprocess
-> memory_save
-> commit_turns_and_publish
```

### default_wechat_private_flow 候选

```text
load_session
-> preprocess
-> append_user_turn
-> handoff_short_circuit
-> input_safety
-> command_dispatch
-> moderation
-> memory_load
-> route
-> credits_reserve
-> capability_dispatch
-> credits_settle
-> output_safety
-> postprocess
-> memory_save
-> commit_turns_and_publish
```

### default_group_channel_flow 候选

```text
load_session
-> preprocess
-> append_user_turn
-> handoff_short_circuit
-> input_safety
-> command_dispatch
-> repeater
-> channel_reply_policy
-> moderation
-> agent_scope_enrich
-> memory_load
-> route
-> credits_reserve
-> capability_dispatch
-> credits_settle
-> output_safety
-> postprocess
-> memory_save
-> commit_turns_and_publish
```

说明：

- 候选 group channel flow 适用于微信群、飞书群、Discord channel/guild 等 `session_kind=group` 场景。微信可以在该 flow 上挂 `plugin.wxbot.reply_policy` 和 `plugin.wxbot.normalize_event`，Discord/飞书则挂自己的渠道 step。
- 候选 group channel flow 中 `command_dispatch`、`channel_reply_policy`、`repeater` 的顺序需要评估。当前 hook priority 是 commands 先于 repeater，repeater 先于 wxbot reply policy；若调整顺序，必须定义命令是否绕过“不 @ 不回复”策略。
- `repeater` 当前依赖 session turns，因此不应在没有兼容 adapter 的情况下移到 append user turn 前。
- 群聊不回复是否仍写 user turn，是产品语义，不应作为技术重构的隐式副作用改变。
- 第一版不要急着拆多个 flow，可以先保留 default flow，然后用条件 step 模拟差异。

### default_wechat_group_flow 候选

微信 group flow 可以作为 `default_group_channel_flow` 的渠道绑定特化版本，差异只应集中在：

- `plugin.wxbot.normalize_event`：解析 `sender_wxid`、`msg_svr_id`、引用消息、群成员事件等微信 SDK 字段。
- `plugin.wxbot.reply_policy`：实现微信群的 @、关键词、mention sender、reply queue 策略。
- `enqueue_channel_reply(channel=wechat)`：兼容期落到 `plugin_wxbot_reply_queue`，由 wxbot bridge 发送。
- 微信专属 agent tools：只能由 `plugins.wxbot` 注册，并带 `channels=["wechat"]`、`session_kinds=["group"]` metadata。

## 状态与持久化

后续如果支持 flow 配置，需要状态表。

### message_flow_profile

```sql
CREATE TABLE message_flow_profile (
    id               BIGSERIAL PRIMARY KEY,
    name             VARCHAR(128) NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    lifecycle        VARCHAR(32) NOT NULL DEFAULT 'draft',
    schema_version   INTEGER NOT NULL DEFAULT 1,
    flow_json        JSONB NOT NULL DEFAULT '{}',
    flow_hash        VARCHAR(128) NOT NULL DEFAULT '',
    status           VARCHAR(32) NOT NULL DEFAULT 'active',
    last_error       TEXT NOT NULL DEFAULT '',
    validated_at     TIMESTAMPTZ NULL,
    published_at     TIMESTAMPTZ NULL,
    created_by       VARCHAR(128) NOT NULL DEFAULT '',
    updated_by       VARCHAR(128) NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);
```

### message_flow_binding

```sql
CREATE TABLE message_flow_binding (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        VARCHAR(128) NOT NULL DEFAULT '',
    channel          VARCHAR(64) NOT NULL DEFAULT '',
    session_kind     VARCHAR(64) NOT NULL DEFAULT '',
    session_pattern  VARCHAR(256) NOT NULL DEFAULT '',
    message_type     VARCHAR(64) NOT NULL DEFAULT '',
    profile_name     VARCHAR(128) NOT NULL,
    priority         INTEGER NOT NULL DEFAULT 100,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

第一版不建议立即建表。可以先使用内置 default flow，等 FlowRunner 稳定后再加入配置持久化。`session_kind` 应使用归一化后的值，例如 `group`、`private`，不要把微信的 `@chatroom`、Discord 的 channel/guild、飞书的 chat 类型直接泄漏为 flow 匹配维度。

如果进入可编辑阶段，profile 必须版本化发布，不能直接覆盖 active flow：

- `draft`：可编辑、可 validate，但不会被 resolver 命中。
- `published`：通过 validate 后可绑定到 flow binding。
- `archived`：历史版本，只读，可用于回滚参考。
- 同一 `name` 只能有一个 active/published binding 生效；发布新版本需要写审计事件。
- `flow_hash` 应覆盖规范化后的 flow definition，用于确认 validate 的内容和发布内容一致。
- 插件升级、禁用、权限变化后，引用相关 step 的 profile 必须重新 validate，必要时状态变为 `degraded`、`invalid` 或 `pending_migration`。

## API 设计草案

第一阶段可先只做只读接口。

### 查看当前 Flow

```http
GET /v1/admin/message-flows
```

返回：

```json
{
  "items": [
    {
      "name": "default_message_flow",
      "version": 1,
      "status": "active",
      "bindings": [{"channel": "*", "tenant_id": ""}],
      "steps": [
        {
          "id": "preprocess",
          "kind": "core.preprocess",
          "owner": "core",
          "enabled": true,
          "error_policy": "fail_closed"
        }
      ],
      "warnings": []
    }
  ]
}
```

### 校验 Flow

```http
POST /v1/admin/message-flows/validate
```

返回：

```json
{
  "valid": true,
  "status": "active",
  "warnings": [],
  "errors": []
}
```

### Flow 运行轨迹

后续可提供单条消息 trace：

```http
GET /v1/admin/message-flows/traces/{trace_id}
```

第一版可以先只依赖 OpenTelemetry 和日志，不做专门存储。

## 可观测性

每个 step 应产生：

- trace span：`flow.step.{kind}`
- 日志字段：`flow_name`、`step_id`、`kind`、`owner`、`action`、`duration_ms`
- metrics：
  - `cs_flow_step_duration_seconds`
  - `cs_flow_step_errors_total`
  - `cs_flow_runs_total`
  - `cs_flow_invalid_total`

Flow 级别需要记录：

- flow name/version
- resolved binding
- step count
- final action
- route label
- suppressed outbound 与否
- degraded reason

日志和 trace payload 需要脱敏：

- 默认不记录完整用户消息、reply 正文、工具参数、webhook URL、PII、billing subject 明细。
- 可记录文本长度、hash、message type、route、owner、reason、effect type、idempotency key、duration。
- 管理 trace 页面如果展示 step input/output，只能展示 schema 化摘要；需要查看原文时必须走已有会话/审计权限边界。
- `signals` 和 `effects` 中的 payload 需要按字段标记 `safe_for_trace`，未标记字段默认不进 trace。

### Admin 运行视图维护约定

当前插件页里的 `Message Flow Runtime / Flow / Effect 运行视图` 已经从迁移期调试页收敛为线上排障视图。后续维护时按以下边界处理，避免把临时 probe 和灰度 checklist 再堆回主界面。

当前主视图应保留：

- `Runtime` / `Shadow` / `Commit` / `Handlers` / `Audit` 状态卡，用来确认真实流量是否由 `FlowRunner` 接管、副作用是否经过 commit gate、handler 是否启用、audit log 是否写入。
- `Effect 链路风险`，只突出需要处理的状态：`handler_error`、`no_handler`、`commit_error`、`audit_error`。`duplicate` 可以展示，但应解释为幂等命中，通常不是故障。
- `Effect Filters`，支持按 `trace_id`、owner、type、status、dry_run 过滤 audit log。排查“某条消息为什么没回复”“某个 effect 是否重复提交”时必须能快速筛到同一 trace。
- `Effect Handlers`，展示 owner/type/handler、allowlist 是否允许、fallback handler 是否存在。迁移新 effect 时先看这里确认有没有 handler 和是否进入 allowlist。
- `Effect Summary` 和 `Effect Audit`，展示最近 effect 的状态分布和持久化记录。这里是判断 Redis gate、Postgres audit、handler dispatch 是否串起来的主入口。
- `最近 Runtime` 和 `最近 Shadow` 的 step trace、effect commit、effect dispatch。排查短路类问题时优先看 step `reason`、`action`、`stop_reason` 和 finalize 后是否执行渠道 outbound policy。
- `单条 Trace 聚合`，从最近消息点击 `trace_id` 后聚合同一条消息的入站、Flow step、Effect、Handler、reply queue、outbound 信息。这里优先读取 Redis trace snapshot，再回退到进程内最近一次 runtime/shadow 结果。

Flow trace snapshot 约定：

- 当前采用 Redis snapshot + TTL，不引入重型历史 trace 表。默认 key 前缀为 `cs:flow:trace`，形如 `cs:flow:trace:{trace_id}:runtime` 和 `cs:flow:trace:{trace_id}:shadow`。
- 默认 TTL 与 effect commit TTL 一致，为 7 天。可通过 `ORCHESTRATOR_FLOW_TRACE_SNAPSHOT_ENABLED`、`ORCHESTRATOR_FLOW_TRACE_SNAPSHOT_TTL_SECONDS`、`ORCHESTRATOR_FLOW_TRACE_SNAPSHOT_KEY_PREFIX` 调整。
- `FlowRunner` 完成 runtime 或 shadow 后写入脱敏 snapshot；写入失败只打 warning，不阻断真实消息处理。
- snapshot 只保存 `flow_name/version`、状态、`trace_id`、tenant/session、step 元数据、effect commit/dispatch 元数据。不保存用户原文、reply 正文、图片 URL、完整 payload、工具参数、webhook URL 或 billing 明细。
- Admin 读取接口为 `GET /v1/admin/message-flows/traces/{trace_id}`。运行视图的单条 trace 聚合应优先使用该接口补齐历史 step/handler dispatch。

主视图不再展示：

- `Probe Memory Dry-run`、`Probe Wxbot Dry-run` 这类迁移期按钮。
- “最近 Probe”结果面板。
- “上线开关顺序”或“先开 Shadow / 准备灰度”这类灰度 checklist。
- 把 `dry_run` 作为风险项展示。`dry_run` 是执行模式或历史记录维度，不应和 handler 错误放在同一级风险里。

保留但不放主界面的开发者能力：

- `POST /v1/admin/message-flows/effects/probe` 可以暂时保留为开发者接口，用于最小化验证 effect committer、handler dispatch 和 audit log。
- 该接口不应作为日常验证入口，也不应让业务用户在 UI 上主动触发。真实消息已经能产生大量 runtime/effect 数据时，应优先用真实 trace 排障。
- 当 Redis committer、Postgres audit、handler allowlist 和主要 effect handler 稳定后，可以删除 probe endpoint、`FlowEffectProbeRequest`、前端相关类型和测试。

已落地并需要继续保持的增强方向：

- 从最近消息列表点击 `trace_id`，直接跳转到运行视图并自动应用 trace 过滤。
- 单条 trace 聚合展示：入站消息、resolved flow、step trace、effect commit、handler dispatch、wxbot reply queue / channel outbound 结果。
- 风险说明白话化：`no_handler` 直接显示缺少哪个 owner/type，`handler_error` 直接显示 error，`audit_error` 显示 Postgres 或查询错误来源。
- 对 payload 继续保持脱敏，只展示 `payload_keys` 和必要摘要，避免把用户原文、图片 URL、私有 webhook、billing 明细直接打进运行视图。

## 错误策略

FlowRunner 需要区分三类失败：

- 业务拒绝：例如命令无权限、审核拦截、群聊策略不回复。返回 `StepResult`，不记为系统异常。
- 可降级依赖失败：例如 memory load、FAQ preview、外部画像服务超时。按 `fail_open` 或 `degrade` 继续。
- 关键一致性失败：例如 load session、billing reserve、commit publish 失败。按 `fail_closed`、retry 或 DLQ 处理。

补偿原则：

- 已 reserve credits 但 capability 未成功完成时，必须 release。
- capability 成功但 outbound 被 suppress 时，是否 capture 由 step definition 明确配置，不能隐式决定。
- postprocess 失败使用 fallback reply 时，credits settle 应基于原 capability 结果还是 fallback 结果需要写入 billing metadata。
- progress message 和正式 reply 使用不同 effect type，避免 suppress 正式回复时误吞进度消息。
- commit 阶段失败后不得重复执行非幂等插件 step，只能重放带 idempotency_key 的 effects。

建议为每个 step 定义 `error_policy`。`error_policy` 是 Runner 对异常、超时、外部依赖失败的处理规则，不参与正常业务分支；正常分支必须通过 `StepResult.action`、`result`、`finalize`、`effects` 表达。

Legacy Hook adapter 的 `error_policy` 需要更保守：阶段 2 只能记录、降级或按现有行为中断，不能对已经进入 hook 代码的调用做自动 retry，也不能用硬 timeout 强杀正在执行外部副作用的 hook。需要 timeout 时只能做软超时观测和告警，等副作用 effect 化后再启用可重放 retry。


| error_policy | 含义 | 示例 |
|---|---|---|
| `fail_closed` | 失败后阻断或降级 | input safety、output safety、core route |
| `fail_open` | 失败后记录错误并继续 | memory load、FAQ preview |
| `degrade` | 失败后替换为 canned result | capability dispatch |
| `retry` | step 内短重试 | 外部短调用 |

默认策略：

- `core.load_session`：`fail_closed`
- `core.preprocess`：`degrade`
- `core.input_safety`：`fail_open` 或 `fail_closed` 需按产品策略决定；当前实现偏 fail-open。
- `core.route`：`degrade`
- `core.capability_dispatch`：`degrade`
- `core.output_safety`：建议 `fail_open` 保持现状，但高风险租户可改 `fail_closed`。
- `plugin.memory.*`：`fail_open`
- `plugin.credits.reserve`：`fail_closed` 或 `degrade`，需避免免费绕过。
- `plugin.credits.settle`：失败必须写审计并可补偿。

## 安全与权限

Flow 编排新增风险：

- 管理员误删安全步骤。
- 插件贡献恶意 step 绕过出站。
- flow 引用已禁用插件。
- step 顺序错误导致先扣费后阻断。
- 并行或短路导致补偿逻辑没执行。

最低安全要求：

- 普通管理员第一版只能查看 flow，不能编辑。
- Flow 编辑如果开放，必须有 preview/validate。
- Flow 修改写审计事件。
- Flow 启用必须校验 required core steps。
- 插件 step 必须受插件启停控制。
- 高风险 step 需要权限声明，例如 `billing`、`storage:shared`、`runtime:publish`。
- `runtime:publish` 第一版不开放给普通插件，进度消息必须走受控 effect。

## 实施阶段

### 阶段 0：设计文档和边界确认

目标：先完成架构评估，不改运行时代码。

任务：

- 明确 Message Kernel / Message Flow 边界。
- 明确 FlowStep / StepResult / MessageEffect 草案。
- 明确插件市场如何声明 flow steps。
- 明确哪些现有 Hook 优先迁移。

验收：

- 文档可用于评审。
- 不影响现有功能。

### 阶段 1：拆分 Orchestrator 内部步骤

目标：不改变行为，降低 `_run()` 单方法复杂度。

任务：

- 把 `DialogOrchestrator._run()` 拆为内部 step 方法。
- 每个 step 保留当前 trace span 和错误策略。
- `PipelineContext` 暂时继续使用。
- Hook 行为不变。

验收：

- 现有单元、集成、e2e 测试通过。
- 消息处理结果不变。
- trace span 不减少。

### 阶段 2：引入 FlowRunner，默认线性 Flow

目标：让当前固定顺序由 FlowRunner 执行，但仍只支持内置 default flow。

任务：

- 新增 `app/orchestrator/flow.py`。
- 新增 `FlowStepDefinition`、`StepResult`、`CompiledFlow`。
- 将阶段 1 的内部 step 注册为 core step。
- 内置 default flow 等价当前顺序。
- FlowRunner 负责 step 日志、metrics，并对 core / effect 化 step 支持 timeout；legacy Hook adapter 阶段只做软超时观测，不强杀、不自动重试。

验收：

- 默认 flow 行为与原 orchestrator 一致。
- 可以通过 admin 只读接口查看 default flow。
- 单 step 失败策略与当前行为一致。

### 阶段 3：插件 FlowStep Registry

目标：让插件可以声明 flow steps，但先不强制迁移所有插件。

任务：

- 新增 `FlowStepRegistry`。
- 插件基类增加 `get_flow_steps()` 默认实现。
- FlowStep 带 owner 注册和反注册。
- 插件 disabled 后 flow 编译能发现缺失 step。
- 插件市场 manifest 支持 `capabilities.flow_steps`。

验收：

- 禁用插件后，对应 flow step 不可用。
- Flow validation 能返回 degraded/invalid。
- 不影响现有 Hook 插件。

### 阶段 4：迁移关键插件

目标：把最影响流程走向的插件从 Hook 迁为 FlowStep。

建议顺序：

1. commands
2. channel reply policy（微信先由 wxbot 适配）
3. moderation
4. memory load/save
5. credits reserve/settle
6. channel outbound dispatch：把 wxbot reply queue、draw/amap 的 `ChannelRegistry` 发送、generic outbound publish 收敛到统一 effect 语义。

验收：

- 命令处理不再依赖 `HookAbort` 作为主要控制流。
- 回复抑制从 `ctx.extras["suppress_outbound"]` 迁到 typed result/effect。
- credits 有明确补偿路径。

### 阶段 5：多 FlowProfile

目标：支持按渠道或租户选择不同 flow。

任务：

- FlowResolver 支持 channel / tenant / session pattern。
- 增加只读管理接口。
- 支持配置文件或数据库存储 flow profile。
- Flow 修改需 validate 后才能启用。

验收：

- web 和 wechat 可以走不同 flow。
- 禁用某个插件后，引用它的 flow 显示 degraded/invalid。
- 回滚到 default flow 可用。

### 阶段 6：受控分支和并行

目标：在 step 协议稳定后，再考虑更强编排能力。

候选能力：

- 条件分支。
- 并行 enrich。
- 子流程。
- bounded loop。
- 长任务 defer/resume。

验收：

- 有明确预算和超时。
- trace 能展示分支。
- 错误策略可预测。

## 测试矩阵

### 单元测试

Flow compiler：

- 合法线性 flow 编译为 `active`。
- 未知 step kind 返回 invalid，并包含 step id。
- 缺 required core step 返回 invalid。
- 引用 disabled plugin 的 required step 返回 invalid。
- 引用 disabled plugin 的 optional step 返回 degraded。
- required inputs 未由前置 step 或 Kernel 提供时报错。
- 两个 step 写同一 signal 且未声明 replace 时报错。
- 普通插件声明 Kernel-only effect 时报错。
- billing effect 顺序不满足 reserve/settle 时报错。
- `when` 引用 scratch 或未知字段时报错。

FlowRunner：

- 按 compiled step 顺序执行。
- `when=false` 的 step 跳过且记录 skipped。
- step timeout 按 `fail_open` 继续。
- step timeout 按 `fail_closed` 阻断。
- `stop` action 不执行后续业务 step，但仍进入 finalize guardrail。
- `suppress_outbound` 不发布正式回复，但不自动跳过 assistant turn。
- `replace_result` 后仍执行 output safety。
- step result effects 被收集并按 idempotency_key 去重。
- step 抛异常时不会重复执行已成功的非幂等 step。

FlowStepRegistry：

- 按 owner 注册和反注册。
- 同 owner 同 kind 覆盖旧定义。
- 不同 owner 注册同 kind 被拒绝。
- 插件 disabled 后 registry 标记 unavailable 或 unregister。
- manifest permissions 与 step permissions 不一致时报错。

兼容 adapter：

- 旧 HookAbort 被翻译为 `stop` + canned result。
- 旧 HookAbort 的 `skip_output_safety=True` 语义按触发阶段保留，并带 audit reason。
- 旧 `ctx.extras["router_signals"]` 合并进 `signals.router`。
- 旧 `suppress_outbound`、`skip_assistant_turn`、`skip_state_transition` 保持原 finalize 行为。
- 下划线 legacy-private 字段不会暴露给 flow `when` 条件。
- legacy side effect step 不会被 FlowRunner 自动重复执行；重试只能重放带 idempotency_key 的 effects。

### 集成测试

默认行为等价：

- 默认 flow 与当前 orchestrator 在 web 文本消息上的 route、reply、turns、outbound 一致。
- 阶段 2 `default_compatible_flow` 的 step 顺序与当前 `_run()` 一致，尤其是 `append_user_turn` 仍早于 `BEFORE_ROUTE` hooks。
- preprocess 失败仍返回 degradation reply。
- route 失败仍返回 degradation reply。
- capability 失败仍走 FAQ fallback 或 canned degradation。
- output safety 拦截仍生效。
- handoff escalated session 仍短路并发布 handoff canned reply。
- handoff escalated session 不写 assistant turn、不推进 state，且仍允许 `AFTER_POSTPROCESS` hook suppress 微信出站。
- self-sent 微信消息仍不追加 user turn，且按现有策略审计或静默。

插件行为等价：

- command 消息仍优先处理，不进入普通 route/capability。
- command 消息是否写 user turn 保持现状；如果后续改为不写，必须作为显式行为变更。
- command 鉴权失败仍返回拒绝回复。
- wxbot 群聊不应回复的消息仍 suppress outbound。
- wxbot 群聊不回复消息是否写 user turn 保持现状。
- wxbot 群聊命令、@ 机器人、白名单、机器人关闭策略保持现状。
- repeater 触发时仍返回复读内容，并保持 cooldown/dedupe。
- repeater 仍能基于已追加的当前 user turn 找到上一条 user turn。
- moderation replace/append 两种提醒模式保持现状。
- memory load/save 行为不变，memory 失败不阻断主链路。
- credits reserve/capture/release 行为不变，余额不足不能免费放行。
- draw 成功后仍能生成图片 payload，失败时仍释放或不扣积分。
- Agent 工具 scope 仍能从 group/channel 信号传到 route hints 和 agent engine。
- Agent 工具 metadata 过滤生效：`channels=["wechat"]` 的 wxbot 工具不会暴露给 Discord/飞书 group session；无渠道限制或匹配当前渠道的工具可以使用。
- 微信正式回复只入 `plugin_wxbot_reply_queue` 或 `enqueue_channel_reply(channel=wechat)` 对应 provider，不进入 generic outbound stream；web 回复仍进入 generic outbound stream。
- Discord/飞书这类非微信 group session 可以触发通用命令和通过当前渠道 outbound provider 回发业务插件结果，例如 `/draw` 文本和图片。

补偿与幂等：

- reserve 成功、capability 失败时 release。
- reserve 成功、postprocess 失败时 settle 策略可观测。
- outbound publish 失败重试时不重复扣费。
- wxbot reply queue 入队成功后重试不重复入队。
- progress message 已发送、正式 reply suppress 时不会重复发送进度消息。
- progress message 和正式 reply 使用不同 idempotency_key。
- draw 图片已生成但出站失败时不重复生成图片。

### E2E / 回归测试

- 入站 HTTP 到 outbound bus 的完整链路保持现有 e2e 结果。
- 微信私聊、微信群、web channel、Discord/飞书 fake group channel 至少各有一个 golden case。
- routing baseline cases 在引入 FlowRunner 后结果不变。
- OpenTelemetry span 数量不减少，新增 flow step span 带 owner/kind/action。
- metrics 中 `cs_flow_runs_total`、`cs_flow_step_errors_total` 标签可按 flow 和 owner 聚合。

### 前端/管理测试

- 插件页能展示插件贡献的 flow steps。
- flow 只读页能展示 step owner、kind、status、warnings。
- 禁用插件后 flow 状态变为 degraded/invalid。
- flow validation 能展示缺失 step、权限错误、输入输出错误和 signal 冲突。
- 管理员不能在第一版编辑或启用自定义 flow。
- trace 页面或日志查询能按 trace_id 看到 step 顺序、耗时和 final action。

## 风险与权衡

### 风险 1：过早抽象

如果现在直接做通用 DAG，会增加大量复杂度。应先拆内部 step，再做线性 FlowRunner。

### 风险 2：编排绕过安全

必须坚持 Kernel guardrail。安全、幂等、会话锁、DLQ 不进入自由编排。

### 风险 3：插件 step 副作用不可控

插件 step 必须声明权限、owner、inputs/outputs，并纳入插件启停。

### 风险 4：迁移成本高

Hook 兼容期要足够长。先迁移最能减少复杂度的插件，不追求一次性统一。

### 风险 5：可观测性下降

FlowRunner 必须内置 trace、metrics、step log。否则问题定位会比固定 pipeline 更难。

## 与插件市场方案的关系

插件市场负责：

- 插件安装、升级、卸载。
- 插件启停状态。
- 插件权限和 manifest。
- 插件 owner 化注册与反注册。

消息流编排负责：

- 消息处理步骤声明。
- Flow 编译和执行。
- 插件 step 引用校验。
- 不同 channel/tenant 的 flow profile。

两者共享：

- plugin owner。
- permissions。
- runtime status。
- disable_mode。
- restart_required。
- 审计事件。

插件市场第一阶段可以先不实现 flow_steps，但 manifest 字段应预留，避免后续 schema 大改。

## 建议结论

需要提前考虑可编排，但不建议马上推翻现有 pipeline。

推荐路线：

1. 保留当前固定主链路，先写清 Message Kernel 和 Message Flow 边界。
2. 下一轮重构先拆 `DialogOrchestrator._run()`，不改行为。
3. 再引入只支持线性的 FlowRunner。
4. 插件市场 owner 化完成后，把插件能力扩展到 `flow_steps`。
5. 先迁移 commands、channel reply policy（微信先由 wxbot 适配）、moderation、memory、credits。
6. 最后再做多 flow profile、条件分支、并行 enrich 和子流程。

这样可以避免两个极端：

- 继续把所有能力塞进固定 pipeline，后续扩展越来越难。
- 过早引入通用工作流系统，让当前项目复杂度失控。

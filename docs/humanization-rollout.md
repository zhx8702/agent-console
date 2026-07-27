# 群聊自然参与发布门与灰度手册

## 1. 当前状态

代码已经具备分阶段 rollout、global/tenant/group kill switch、统一群发言 ledger、发送时复验、风格/近似重复守卫、成员隐私和生产 SLO evaluator。

当前只验证了仓库内十场景 coverage fixture：十个必需场景齐全且命令退出 0，但报告明确为：

```json
{
  "mode": "scenario_contract_only",
  "scenario_contract_passed": true,
  "production_slo_evaluated": false,
  "production_slo_passed": null
}
```

因此现状是**合约可用，生产 SLO 未评估**。不得把这 10 条 fixture 的表观结果写成 must-reply 100%、泄漏 0 或 canary 已通过。

支撑灰度操作的第二轮本地控制台回归已执行：typecheck `exit 0`，Vitest 31 files / 144 tests 全通过，production build `exit 0`（156 modules）。当前轮用 `agent-browser` 复核真实 API 离线登录与真实前端 + 临时模拟 API 的 Overview、Connection、Plugin、Memory、Relationship Graph；这些 UI 结果不能证明 live backend，也不改变 `production_slo_evaluated=false`。

本地后端代码门也已收口：完整 pytest 共收集 2567 项，**2547 passed / 20 skipped / 0 failed**（58 warnings），全树 Ruff、CI-scoped mypy typed-core 15 files + runtime-role 7 files（`--follow-imports=skip`）、compileall 均通过，Alembic 单 head 为 `0036_wxbot_report_attempt_fencing`。20 项 skip 全部来自可选 Flask、真实 Anthropic key、Redis/PostgreSQL/Redis-e2e 环境缺失，不能据此放行生产。

58 条 warning 保持为显式 P2：1 条第三方 FastAPI/Starlette `TestClient` deprecation，55 + 2 条 aiosqlite/stdlib SQLite Python 3.12 date/datetime adapter deprecation。它们不改变本轮 0 failed，也不应在后续依赖升级中被忽略。

source-bound 异步群结果必须携带请求时捕获的 `participation_status`、`source_message_id`、`participation_policy_version` 与 `send_revalidation_enabled`；发送时不得用 current policy version 补齐缺失 capture。地图进度、AMap、draw 和绑定最近群观察的 group-activity 路径已纳入；`tibo_reset` 是无成员 source observation 的定时外部 feed，不适用“成员先答/改口/话题切换”语义复验，也不得被统计为该门已覆盖。

## 2. 两种 replay 模式

### 2.1 场景合约模式

用于 CI 检查 schema 和十个必需场景是否齐全，不评估生产 SLO：

```powershell
uv run python scripts/evaluate_social_replay.py `
  --validate-scenarios-only `
  tests/fixtures/social_replay_scenario_contract.jsonl
```

十个场景为：direct mention、inline mention、rapid multi-party chat、member answer exists、identity inquiry、private-memory inducement、sensitive repeater、quiet hours、memory correction、topic changed before send。

### 2.2 生产 SLO 模式

只对真实 shadow/canary 导出的脱敏结构化 observation 使用：

```powershell
uv run python scripts/evaluate_social_replay.py artifacts/social-replay.jsonl
```

该模式只有在运行时字段完整、样本量满足且所有门槛同时通过时才返回 0。缺字段、缺样本或任何门失败都应 fail-closed。

`HumanizationObservation` 不接受聊天正文、tenant ID、session ID 或成员 ID 字段。导出数据只保留场景标签、布尔结果、stage/cohort、最终发送状态、低基数比例/延迟和配对人工评分；未知字段会被加载器拒绝。导出链路仍应执行访问控制、最小保留期和审计，不能因为 schema 不含正文就跳过数据治理。

## 3. 生产晋级阈值

所有比率阈值使用 95% Wilson 区间，而不是只看点估计。一次晋级必须同时满足下表全部门：

| 指标 | 生产门槛 | 样本口径 |
|---|---:|---|
| 直接点名召回 | Wilson 下界 `>= 99%` | `directly_addressed=true`，至少 1000 条 |
| 非点名误插话 | Wilson 上界 `<= 1%` | 非直接点名且期望沉默，至少 1000 条 |
| 跨受众记忆泄漏 | `0` | 所有 observation；任一事件立即阻断 |
| 成员已回答后仍发送 | Wilson 上界 `< 0.5%` | `valid_member_answer_exists=true`，至少 1000 条 |
| 24 小时近似重复 | Wilson 上界 `< 1%` | 实际发送样本，至少 1000 条 |
| 最近 40 条机器人占比 | p95 `<= 15%` | 每条 observation 的 `bot_ratio_last_40` |
| 连续三条机器人消息 | `0` | 所有 observation；任一事件立即阻断 |
| 新增调度延迟 | p95 `< 8s` | 实际发送样本 |
| 自然度改善 | 平均配对提升 `>= 0.5` | 同一任务 before/after 人工评分，至少 100 对 |
| 任务完成率 | 相对下降 `<= 2%` | 同一任务 before/after 完成结果，至少 100 对 |

生产 evaluator 还要求：

- direct、non-call、answered context、sent 四个关键 rate 分母各不少于 1000；
- 总 observation、naturalness pairs、task-completion pairs 均不少于 100；
- 每条记录包含合法 `final_delivery_state`、`rollout_stage`、非空 `cohort`、成员回答/泄漏/bot ratio/连续消息字段；
- 实际发送记录还必须包含近似重复、duplicate-guard、added scheduling delay 和 actual delivery delay；
- 十个场景全部覆盖，并能按 stage/cohort 输出分段摘要。

`actual_delivery_delay_p95_seconds` 和 `duplicate_guard_trigger_rate` 当前是诊断指标，不替代上表的发布门。不要擅自把“实际延迟”与“新增拟人化调度延迟”混为一个阈值。

## 4. 固定灰度顺序

灰度顺序不得跳级：

1. **`shadow`**：新策略只计算决策、原因和指标，不改变实际发送；保留基线参与行为。先验证事件持久化、字段完整、十场景覆盖和 label cardinality。
2. **`privacy_5`**：仅对明确 opt-in 且稳定哈希桶 `<5%` 的群启用隐私控制、发送时复验和近似重复守卫；参与决策仍保持基线。
3. **`style_10`**：仅对明确 opt-in 且稳定哈希桶 `<10%` 的群继续启用 privacy 能力并加入 style guard；参与决策仍保持基线。
4. **`contextual`**：前序 cohort 的生产 SLO 全部通过后，才启用 contextual soft participation、统一 speech budget、privacy/revalidation/style/duplicate guard；主动暖场仍关闭。
5. **`proactive`**：最后单独开放主动暖场。只有 group opt-in、稳定桶低于 `proactive_rollout_percent`、三级 kill switch 全开且共享预算允许时才可发送。

稳定桶按 `(tenant_id, session_id)` 的 SHA-256 计算，重启、扩容和 replay 不改变群归属。stage 和 `proactive_rollout_percent` 保存于版本化 participation policy；修改必须使用 `If-Match` 与 `Idempotency-Key`。

## 5. 每阶段晋级清单

每次从阶段 N 晋级到 N+1，发布负责人必须保存以下证据：

1. 当前策略版本、目标 stage、cohort 定义、opt-in 清单摘要和变更审计 ID；
2. 新鲜 production observation 的导出时间窗、字段完整性和样本量报告；
3. production SLO evaluator 的完整 JSON 输出与进程退出码；
4. 按 stage/cohort 的分段结果，而不只看全局平均；
5. global、tenant、group kill switch 和 `rollback_to_version` 的演练记录；
6. identity disclosure、跨受众泄漏、重复/连续机器人消息、lost/duplicate send 的零容忍审查；同时抽检未寻址身份/转人工文本没有触发抢答，`no_group_mentions` 没有被任何插件 mention override 绕过，source-bound 异步插件结果没有缺 contract、用 current version 补 capture 或绕过 SDK 前复验；
7. 值班人、观察窗口、回滚负责人和发布制品 digest。

任一项缺失都保持当前阶段。十场景 contract pass 只能满足第 2 项中的 coverage 子检查，不能替代真实样本和 SLO 输出。

## 6. 中止与回滚

- global、tenant、group 任一 kill switch 关闭，都会禁用对应范围的 live humanization feature。
- 影响范围明确时先关闭最窄 scope；范围不明、跨 tenant 或身份/隐私事件时先关闭 global，再调查。
- 发现跨受众记忆泄漏、连续三条机器人消息、身份冒充、不可解释的重复发送或幂等冲突时立即停止晋级并回滚，不等待样本窗口结束。
- 明确寻址的身份/转人工请求必须说明 AI 身份和真实能力边界；未点名、未回复机器人且无明确机器人称呼的成员对话不得仅凭“你是谁/转人工”等关键词强制插话。
- 成员 `no_group_mentions` 必须在所有插件 `mention_sender` override 之后仍保持生效；发现任何绕过即按隐私事件停止晋级并回滚。
- 控制台使用 `rollback_to_version` 和当前 ETag 回滚；回滚是创建一个新的已审计版本，不覆盖或删除历史版本。
- 策略回滚不会撤回已成功发送的消息；下游发送继续使用 message/effect/speech idempotency key，indeterminate delivery 必须走对账而不是盲目重发。
- 如果发送时复验或 speech ledger 依赖不可用，live 路径必须 fail-closed；不得为了维持发送量绕过。
- source-bound 异步结果出现缺失 source/policy capture、policy version/rollout flag 漂移或 kill switch 关闭时必须抑制；不得把无 source observation 的 scheduler feed 伪装成已通过成员回答/话题复验。

## 7. 线上观测与离线评审

Prometheus 指标不得带 tenant/member/message 等高基数或可识别标签。当前代码提供：

- `cs_social_participation_decisions_total`
- `cs_social_send_revalidations_total`
- `cs_social_added_scheduling_delay_seconds`
- `cs_social_bot_ratio_last_40`
- `cs_social_privacy_actions_total`
- `cs_social_runtime_event_persistence_total`
- `cs_social_final_deliveries_total`
- `cs_social_actual_delivery_delay_seconds`
- `cs_social_duplicate_guard_total`

speech ledger 另记录 reservation/transition/observation 的低基数计数。线上指标负责实时止损和数据完整性；自然度、任务完成率、24 小时近似重复和跨受众泄漏仍以脱敏 observation + 人工配对评审为晋级依据。两者必须同时存在，不能用在线 counter 代替人工评分，也不能用离线 replay 代替真实发送结果。

## 8. 当前放行结论

截至本快照：十场景 contract pass，`production_slo_evaluated=false`，没有满足样本量的真实 shadow/canary 数据。因此阶段只能视为**代码就绪、生产未放行**；在真实 SLO、容器/依赖和回滚演练完成前，禁止把 rollout 状态写成 contextual 或 proactive 已上线。

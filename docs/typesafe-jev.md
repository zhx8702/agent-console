# TypeSafe / Jev 评估与群内答疑

Jev 通过独立的 TypeSafe System One 客户端复核结构化判断，不替换回复用的 LLM。
当前接入五个场景：群关系、自动长期记忆（含旧每日抽取）、意图复核、内容审核、群内求助判断。

## 配置与模式

本地使用 `TYPESAFE_*`，Compose 使用服务器 `.env` 中对应的 `COMPOSE_TYPESAFE_*`。
密钥只保存在服务端。`TYPESAFE_ENABLED=false` 禁用全部调用；数据库租户策略不能越过这个开关。

```dotenv
COMPOSE_TYPESAFE_API_KEY=<server-side-secret>
COMPOSE_TYPESAFE_ENABLED=true
COMPOSE_TYPESAFE_SHADOW_ONLY=true
COMPOSE_TYPESAFE_MODEL=jev-latest
COMPOSE_TYPESAFE_TIMEOUT=10
COMPOSE_TYPESAFE_MAX_RETRIES=1
COMPOSE_TYPESAFE_MIN_CONFIDENCE=0.8
COMPOSE_TYPESAFE_ONLINE_TIMEOUT=1.5
COMPOSE_TYPESAFE_WORKER_CONCURRENCY=4
COMPOSE_TYPESAFE_JOB_MAX_ATTEMPTS=3
COMPOSE_TYPESAFE_RELATIONSHIP_ENABLED=true
COMPOSE_TYPESAFE_MEMORY_ENABLED=true
COMPOSE_TYPESAFE_INTENT_ENABLED=true
COMPOSE_TYPESAFE_MODERATION_ENABLED=true
COMPOSE_TYPESAFE_PARTICIPATION_ENABLED=true
```

控制台“记忆 → Jev 评估”按租户管理开关、观察/参与决策模式、最低置信度、采样比例和场景开关。
群内求助使用独立的 `participation_shadow_only` 和 `help_sessions` 白名单，允许在记忆仍为观察模式时
只对指定群启用主动答疑。会话 ID 使用消息中的规范 ID（`cx1:c:…@chatroom`）；微信群参与策略
仍按管理界面的外部群 ID 配置。该群还需开启参与策略的 proactive_enabled、主动阶段和 rollout opt-in。
用户明确指定需要启用的单个群，应将该群的 `proactive_rollout_percent` 设为 100；默认 5% 仍会
按规范会话 ID 分桶，即使开关已开启也可能不生效。此设置只改变该群，不扩大 Jev 群白名单。

明确 @ 或回复机器人仍按原流程处理。非点名消息由 Jev 判断是否求助，达到阈值才提供软参与信号，
经过原有安静时段、频率、成员退出、已有人解答、发送前复核等限制后回答。不会把群内任意聊天都当成问题。
Jev 不生成聊天回复。开启答疑的群会检索当前群和租户可见的知识库，用原 LLM 组织回答；
检索失败时继续正常回答。第三方总结必须标注来源和核验状态，不能视作官方文档。

## 持久化队列与故障行为

群关系/记忆先保存，再在数据库中排队；抽取循环不等待 Jev。队列失败使用事务保存点隔离，
不回滚主记录。scheduler 每 2 秒取最多 WORKER_CONCURRENCY 个任务，SKIP LOCKED 加租约令牌
防止重复领取和旧进程覆盖新任务。超时和错误按指数退避最多 JOB_MAX_ATTEMPTS 次；崩溃后租约过期可接管。

每分钟扫描最近变更的 100 条自动记忆作为入队补偿，扫描游标从进程启动前一天开始；这不是无限历史回填。
同一模型/版本/内容按指纹去重，Jev 自己的审计不会形成评估循环。超过 7 天的待处理/失败任务清除输入并过期；
终态审计保留 30 天。手动重试保留累计用量，尝试次数按本轮重置。

意图/审核/求助：观察模式异步入队，参与决策模式上游调用受 ONLINE_TIMEOUT（包含并发槽等待）限制。
策略读取、审计写入各有 1 秒限制，上游超时、配置不可用或连续错误时沿用原流程。
连续 3 次上游失败开启 30 秒熔断。群内求助的失败行为是不主动加入新对话。

## 语义输入、隐私与应用边界

发送脱敏后的候选内容和最多 8 段、每段 600 字的来源证据。人员标识替换为代称，屏蔽识别出的
联系方式、URL 和凭据。自动记忆无引用记录时使用已保存原文的有限片段；缺失的引用不假装有证据。
脱敏并不等于完全匿名，语义判断需要有限文本。发送前及写回前检查所属插件、成员退出和来源有效性。

写回时锁定任务/记忆并核对指纹；删除、过期、内容变更、隐私变更或人工审核后的旧结果不能覆盖新状态。
观察模式只存审计。参与决策模式达到最低置信度后可接受、退回待审或拒绝自动记忆；敏感或证据不足的内容
不能自动接受，人工/显式保存、置顶和人工审核保留。关联图与缓存沿用原有记忆审核流程同步。
意图复核只能撤回不支持的执行意图或改为会话类意图，不能构造新工具参数；语义审核只能增加标记，不能解除敏感词拦截。

## 审计和 API

`jev_evaluation` 保存队列状态、实际模型、判断、分数、置信度、用量、耗时、错误类别和应用标志。
主记录中的 `value.jev` 保存最新结果；群关系同时保留 `relation.typesafe_shadow` 兼容字段。
关系详情显示评估质量，已应用评估可参与前端关系排序；观察结果不改变默认排序。
窗口返回 `typesafe_shadow.mode=queued` 和本次 `queued`；异步总量从控制台查询，不能把入队数当成已完成数。

- GET `/v1/admin/jev?tenant_id=…`：租户策略、全量聚合、按优先级排序的审计列表。
- PUT `/v1/admin/jev/policy?tenant_id=…`：`{version, policy}`，必需 `Idempotency-Key`；版本冲突 409。
- POST `/v1/admin/jev/jobs/{id}/retry?tenant_id=…`：失败且保留可重建输入的任务可重试，必需 `Idempotency-Key`。

写入要求租户管理范围及危险操作权限；群级操作员不能修改整租户策略。审计用量仅含收到的响应，不等同于账单。
`applied` 表示判断已交给业务策略，不等同于微信已送达，最终发送结果应看回复队列和 SDK 回执。

## 验证与部署

```bash
uv run pytest tests/unit/test_jev_service.py tests/unit/test_typesafe.py tests/unit/test_wxbot_hook.py
# 仅对独立测试库运行，先 alembic upgrade head；不要指向生产库。
JEV_TEST_DSN=postgresql+asyncpg://user:password@localhost/test_db uv run pytest tests/integration/test_jev_postgres.py
cd frontend && npm run build && npm test
sudo AGENT_CONSOLE_ENV_FILE=.env bash scripts/deploy-server.sh
```

迁移 0053 新建策略与队列表。部署前保留旧代码/环境/镜像；部署后检查健康、队列领取、实际结果和微信 SDK。
禁用总开关不删除已有记忆；回滚应用时可保留新增表。

# TypeSafe / Jev 评估与群内答疑

Jev 通过独立的 TypeSafe System One 客户端复核结构化判断，不替换回复用的 LLM。
当前接入群关系、自动长期记忆（含旧每日抽取）、意图复核、内容审核、群内求助判断，以及每日群知识与回答质量复盘。

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
COMPOSE_TYPESAFE_ONLINE_TIMEOUT=10
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
保存白名单时会清理前后空格、空行和重复 ID，拒绝非群会话 ID。移出白名单后，尚未执行的求助
评估会在执行前重新读取策略并跳过，在线请求也会在返回结果前复查当前策略。
用户明确指定需要启用的单个群，应将该群的 `proactive_rollout_percent` 设为 100；默认 5% 仍会
按规范会话 ID 分桶，即使开关已开启也可能不生效。此设置只改变该群，不扩大 Jev 群白名单。

明确 @ 或回复机器人仍按原流程处理。非点名消息由 Jev 判断是否求助，达到阈值才提供软参与信号，
经过原有安静时段、频率、成员退出、已有人解答、发送前复核等限制后回答。不会把群内任意聊天都当成问题。
通过 Jev 门槛的明确求助，普通文本答案生成完毕后重新安排发送窗口（120 秒）；不把流式生成耗时
扣进普通软回复的 45 秒有效期。发送前仍会取消已被回答、已改话题或被更新请求取代的回复。
这只调整求助答案的发送窗口；Grok 的流式首事件、空闲和最长运行限制独立生效。
Jev 不生成聊天回复。开启答疑的群会检索当前群和租户可见的知识库，用原 LLM 组织回答；
检索失败时继续正常回答。第三方总结必须标注来源和核验状态，不能视作官方文档。

## 持久化队列与故障行为

群关系/记忆先保存，再在数据库中排队；抽取循环不等待 Jev。队列失败使用事务保存点隔离，
不回滚主记录。scheduler 每 2 秒取最多 WORKER_CONCURRENCY 个任务，SKIP LOCKED 加租约令牌
防止重复领取和旧进程覆盖新任务。超时和错误按指数退避最多 JOB_MAX_ATTEMPTS 次；崩溃后租约过期可接管。

每分钟扫描最近变更的 100 条自动记忆作为入队补偿，扫描游标从进程启动前一天开始；这不是无限历史回填。
同一模型/版本/内容按指纹去重，Jev 自己的审计不会形成评估循环。超过 7 天的待处理/失败任务清除输入并过期；
终态审计保留 30 天。手动重试保留累计用量，尝试次数按本轮重置。

意图/审核/求助：观察模式异步入队。整个在线路径受 ONLINE_TIMEOUT 限制，包括策略读取、所属插件检查、
并发槽等待、上游调用、策略复查和审计写入。超时后只额外尝试最多 250ms 的本地失败审计，然后沿用原流程。
观察模式的入队也受在线时限限制。调用方取消任务时取消继续向上传播。
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
新评估的 `result._audit` 保存本地判定原因、评估模式、当时置信度门槛、在线消息 trace_id；记忆结果还记录
实际执行的接受/待审/拒绝决定。例：模型建议回复但置信度 0.61、门槛 0.80 时显示“置信度未达到阈值”；
模型建议接受但证据或敏感性检查未通过时显示实际待审。追踪元数据不会作为证据发往 Jev。
控制台显示“建议回复／继续旁观”，支持按规范会话 ID 筛选记录及统计。旧记录可能没有新增的本地判定原因。
主记录中的 `value.jev` 保存最新结果；群关系同时保留 `relation.typesafe_shadow` 兼容字段。
关系详情显示评估质量，已应用评估可参与前端关系排序；观察结果不改变默认排序。
窗口返回 `typesafe_shadow.mode=queued` 和本次 `queued`；异步总量从控制台查询，不能把入队数当成已完成数。

- GET `/v1/admin/jev?tenant_id=…&session_id=…`：租户策略、按场景/会话筛选的聚合、按优先级排序的审计列表；session_id 可省略。
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


## 每日群知识候选

租户策略新增 `knowledge`（默认 false）、`knowledge_sessions`（独立白名单）、
`knowledge_daily_hour`（默认 3）、`knowledge_timezone`（默认 Asia/Shanghai）、
`knowledge_min_confidence`（默认 0.90）。不跟随答疑开关自动扩大知识采集范围。
配置会话 ID 必须与归档消息一致，托管连接使用规范 `cx1:c:…@chatroom` ID。

scheduler 独立任务扫描前一天及最近七天的归档消息；50 条一页，保存游标、处理量和候选数。
每个任务租约 15 分钟，失败最多三次；页面成功后重置尝试次数，正常部署取消不消耗失败重试次数。完成日期出现晚到消息时自动继续。
Grok 以流式活动计时整理问题、环境、步骤、结果和原始消息 ID，Jev 独立审核可复用性、
支持度、敏感性和解决状态。机器人发言不能充当成功确认，缺失或被删除的来源不能发表。
提取保留上页上下文，并带入最近 14 天按当前消息相关性排序的最多八个未解决候选的原始证据；新增反馈须另经
Jev 判断确实解决同一个问题，才能关联旧候选。该上限不意味着所有未解决问题都已自动跟进。

达到知识门槛的条目还要检索本群及租户公共知识，逐一判断重复、补充、冲突或无关。
检索失败会重试，不按“没有旧知识”处理；知识库在评估后发生变更时，发布要求重新审核。
控制台“记忆 → Jev 评估 → 每日知识候选”显示最近任务、结构化候选、脱敏原文和判断。
明确通过的候选可由租户管理员填写审核说明后发布到本群知识库；其他候选可排除或重新审核。
不会自动发布或自动覆盖人工文档。对同群旧知识的补充/冲突会生成修订草案，Jev 再次核对原文支持度和限制条件。
管理员可编辑草案；每次编辑后必须重新审核，再批准更新。保留原正文、来源和版本哈希，
发布使用所有知识写入路径共用的锁；原文或知识库变化会拒绝旧审批。崩溃后可识别已完成的修订，避免重复应用。

成员退出在外发前、保存候选的事务内及发布前复查。成员遗忘流程同步清除候选及派生知识的
索引与正文；索引清理失败会交给原有持久化删除流程重试，不报告删除完成。

API：
- GET `/v1/admin/jev/knowledge?tenant_id=…&session_id=…`
- GET `/v1/admin/jev/knowledge/candidates/{id}/evidence?tenant_id=…`
- POST `/v1/admin/jev/knowledge/candidates/{id}?tenant_id=…`：版本、publish/apply_revision/reject/retry、审核说明，需 Idempotency-Key。
- POST `/v1/admin/jev/knowledge/jobs/{id}/retry?tenant_id=…`：保留已处理游标，需 Idempotency-Key。

- POST `/v1/admin/jev/knowledge/candidates/{id}/revision?tenant_id=…`：提交草案及基线哈希，需 Idempotency-Key。
- GET `/v1/admin/jev/knowledge/documents/{id}/history?tenant_id=…&session_id=…`：已批准修订的旧正文与版本。
- GET `/v1/admin/jev/knowledge/findings/{id}/evidence?tenant_id=…`：复盘的脱敏原始证据。

每日任务同时提出漏答、无效回答、不必要回复和有效解决四类复盘候选；Jev 对照原始聊天、
处理结果与实际发送记录独立复核。没有运行证据的“没看到回复”只可待核验，不能确认漏答。
零问题是有效结果，复盘结论不进入问答知识库，也不自动改变频率策略。
控制台单独显示复盘类别、状态、证据及本地追踪 ID。统计是观察发现数，不是完整漏答率。

迁移 `0054_jev_knowledge` 添加每日任务及候选表，`0055_jev_revisions` 添加修订和质量发现。
回滚前先停用知识整理；旧应用可以保留新增表。开发和运行验证见 `docs/jev-knowledge-iteration.md`。


## 历史分页与人工复盘

知识面板支持候选与复盘状态筛选，以及任务、候选、复盘独立继续加载。
列表按创建时间和 ID 排序；游标绑定当前租户、群及筛选条件。GET `/v1/admin/jev/knowledge`
支持 `job_cursor` / `candidate_cursor` / `finding_cursor`、`candidate_status` / `finding_status`，
返回 `pagination.jobs` / `pagination.candidates` / `pagination.findings` 作为下一页游标。
`page_size` 为 1–100，任务单页最多 50 条。统计仍是当前群范围的观察发现数，不是漏答率。

POST `/v1/admin/jev/knowledge/findings/{id}?tenant_id=…` 支持 `confirm` / `dismiss`，
请求包括 `expected_status`、`reason` 与 `Idempotency-Key`。确认必须先调用证据接口，
取得当前原文和处理/发送记录的 `evidence_hash` 并提交；证据变化时返回 409，需要重新查看。
结果分别为 `confirmed`（人工确认）和 `dismissed`（人工排除），保留原 Jev 判断与人工处理信息。
人工确认复盘不会自动调整群策略、发表知识或向群发送消息。

正常安静时段、已有人解答、明确配额限制等不直接确认为漏答；缺少实际发送证据的回复质量结论待核验。
知识比对不截取长文档后作确定判断；超过当前完整评估上限的文档及尚未解决的多文档冲突均保留待核验。

离线 Grok 提取与修订的外层等待跟随流式总时限并保留 30 秒余量（当前 330 秒），
避免固定 120 秒外层窗口截断提供者内部重试；真正的流式首事件、空闲和总时限仍由提供者控制。

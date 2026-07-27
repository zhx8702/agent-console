# 群聊成员记忆链路：让机器人记得和它互动的人

## 背景
用户提出：群聊里面的记忆目前如何解决？如果某个群成员和机器人互动，如何保证机器人下次还记得这个人？

## 目标
梳理并修复群聊成员记忆链路，使机器人在群聊中和具体成员互动时，可以：
- 识别当前说话的真实成员身份（wxid/昵称/备注）。
- 写入该成员相关长期/短期记忆，而不是全部混到群 `__group__`。
- 回复时检索“当前成员个人记忆 + 群聊上下文记忆”。
- 不泄漏其他成员隐私，不把 A 的个人记忆误用到 B。

## 验收标准
1. 文档说明当前链路：消息导入、user_id/session_id、memory event/item、prompt 注入。
2. 找出是否存在：群聊互动只使用 `__group__` 导致无法按成员记忆的问题。
3. 如有缺口，完成最小修复：
   - 当前群成员身份进入 memory scope。
   - 互动时检索当前成员记忆和群记忆。
   - 写入时能按当前成员归属。
4. 增加/更新测试，覆盖群聊中不同成员不串记忆。
5. 验证通过：focused tests + py_compile，必要时本地 smoke。

## 分阶段 TODO
- [x] Phase 1：代码链路分析
- [x] Phase 2：确定修复方案
- [x] Phase 3：实现和测试
- [x] Phase 4：本地验证（不部署）

## 风险
- 群聊隐私边界：不能把某成员个人偏好广播给其他人。
- wxid/昵称映射不稳定：应以 wxid 为主键，昵称只展示。
- 现有 `__group__` 图谱/历史抽取不能直接替代成员个人记忆。

## 当前状态
已完成代码分析与最小修复，已通过 Python 3.11 临时容器验证，待提交/部署。

## 链路分析

### inbound / wxbot 消息解析
- `plugins/wxbot/bridge.py::_publish_legacy_message` 从 SDK/polling 消息构建 `InboundEvent`：
  - `event.user_id = msg.get("sender_wxid") or "unknown"`。
  - `event.session_id = msg.get("session_id")`，群聊为 `*@chatroom`。
  - `metadata.sender_wxid`、`metadata.sender_name`、`metadata.session_kind`、`metadata.mentioned_me`、`metadata.at_wxids` 等随事件进入后续流程。
- `plugins/wxbot/bridge.py::_publish_stream_envelope` 对 unified stream 做同样规范化：
  - `event.user_id = sender.id`。
  - `metadata.sender_wxid = sender.id`，`metadata.sender_name = sender.name`。
- `app/channel/models.py::ChannelTarget.from_event/from_session` 对发消息目标保留：
  - `target.session_id = 群 id`。
  - `target.user_id = event.user_id`。
  - `target.sender_id = metadata.sender_id || metadata.sender_wxid || event.user_id`。
  这保证回复/排队层能知道群会话与当前成员 wxid。

### user_id / session_id 分配
- 在线群聊消息：`session_id` 是群 `*@chatroom`，`user_id` 是当前发言成员 `sender_wxid`，不是群 id，也不是昵称。
- `plugins/memory/hooks.py::_scope_from_ctx` 使用 `ctx.event.user_id` 作为 memory identity scope。
- 因此实时互动不会天然写到 `__group__`，而是写到当前成员 wxid。
- `plugins/memory/store.py::GROUP_HISTORY_USER_ID_SCOPE = "__group__"` 主要用于群历史/群图谱 backfill 的共享群范围。

### 当前说话人/member wxid 提取
- wxbot bridge 在事件 metadata 中保存 `sender_wxid`/`sender_name`。
- prompt/agent 历史渲染使用 `metadata.sender_name || metadata.sender_wxid` 作为显示标签；显示名只用于提示文本，不作为 memory key。
- memory key 仍是 `event.user_id`，即稳定 wxid。

### memory event/item append
- `plugins/memory/hooks.py::MemoryPersistenceHook.run` 和 `MemorySaveStep` 调用 `MemoryStore.remember_interaction(... user_id=ctx.event.user_id, session_id=ctx.event.session_id ...)`。
- `plugins/memory/store.py::remember_interaction`：
  - 写 `plugin_memory_event.user_id = user_id`。
  - 更新 `plugin_memory_identity_profile`，key 为 `(tenant_id, channel, source_key, user_id)`。
  - 更新 `plugin_memory_session_profile`，key 为 `(tenant_id, channel, source_key, session_id, user_id)`。
  - 结构化长期记忆 `_apply_structured_memory_action` 也使用同一个 `user_id`。
- backfill 路径 `plugins/memory/store.py::backfill_from_sdk` 在群历史自动范围下使用 `__group__`，它是群共享历史/图谱，不是在线成员个人记忆。

### prompt/context memory retrieval
- 修改前：`MemoryContextHook.run` 只加载当前 `ctx.event.user_id` 的 runtime profile 和相关 memory items/graph，然后写入 `session.variables["user_memory"]`。
- `app/common/prompting.py::augment_prompt_with_persona_and_memory` 只渲染 `user_memory`，并有规则“群聊只使用当前发言人的记忆”，可以防止 A 的个人记忆注入 B。
- 缺口：如果群共享历史/图谱存为 `__group__`，实时群回复只加载当前成员记忆，不会同时加载共享群上下文。

## 结论
- “群成员直接和 bot 互动只存到 `__group__` 导致记不住个人”的问题：当前实时 wxbot 链路没有这个问题，在线消息按 `sender_wxid` 写入/读取成员个人记忆。
- 实际缺口是另一半：`__group__` 的群共享记忆不会随当前成员个人记忆一起进入 prompt，因此“成员个人记忆 + 群共享上下文”未同时加载。

## 已变更
- `plugins/memory/hooks.py`
  - 群聊 wxbot 当前成员仍使用 `event.user_id` 加载个人记忆。
  - 当 `channel=WECHAT` 且 `session_id.endswith("@chatroom")` 且 `user_id` 是真实成员时，额外加载 `GROUP_HISTORY_USER_ID_SCOPE` (`__group__`)。
  - 共享群记忆写入 `session.variables["group_memory"]`，不混入 `user_memory`。
  - 不会查询其他成员 wxid，因此不会把 A 个人记忆注入 B。
- `app/common/prompting.py`
  - 抽出 memory 渲染 helper。
  - 在群聊 prompt 中单独渲染 `group_memory`，文案明确为“当前群聊的共享记忆”，并声明不能当作当前发言人或其他个人私有偏好。
- `tests/unit/test_memory_hooks.py`
  - 覆盖群成员 A 加载个人 scope 和 `__group__` 共享 scope。
  - 覆盖群成员 B 不加载 A 的个人 scope。
  - 覆盖 item/graph/hybrid retrieval 都对当前成员和 `__group__` 分开调用。
- `tests/unit/test_prompting.py`
  - 覆盖 A prompt 有 A 个人记忆，B prompt 没有 A 个人记忆。
  - 覆盖共享群记忆对 A/B 都可见，且带共享上下文边界说明。

## 验证结果
- `pytest tests/unit/test_prompting.py`：通过，7 passed。
- `python3 -m py_compile app/common/prompting.py plugins/memory/hooks.py tests/unit/test_memory_hooks.py tests/unit/test_prompting.py`：通过。
- `git diff --check -- app/common/prompting.py plugins/memory/hooks.py tests/unit/test_memory_hooks.py tests/unit/test_prompting.py`：通过。
- 本机 `pytest tests/unit/test_memory_hooks.py`：因系统 Python 3.10.12 不支持既有 `datetime.UTC`，收集失败；这不是本次改动导致。
- Python 3.11 临时容器：`python -m pytest tests/unit/test_memory_hooks.py tests/unit/test_prompting.py -q` 通过，17 passed。

## 剩余风险
- 本机默认 Python 仍是 3.10，后续完整测试应继续使用 Python 3.11+ 环境/容器。
- 本修复只为 wxbot/WeChat chatroom 加载 `__group__`，未推广到 Discord/Feishu 等非微信群；这是刻意限制，避免把微信历史 sentinel 误用于其他渠道。
- `__group__` 内容必须只放群共享事实。若历史抽取把个人隐私误归入 `__group__`，prompt 会作为共享群上下文渲染，需要后续治理抽取/审核质量。

# 立即生成复盘 write-to-knowledge 标志失效问题

## 背景
用户反馈：项目里点击“立即生成复盘”按钮时，即使把“写入知识库”设置成 false，仍然会写入知识库。

## 目标
定位并修复复盘生成链路中的写入知识库开关逻辑，确保用户显式设置 false 时绝不写入知识库。

## 验收标准
- 找到前端按钮/表单传参、API request schema、后端处理逻辑、知识库写入函数调用点。
- 当 `write_to_knowledge=false` / 等价字段为 false 时：
  - 复盘可以生成。
  - 不调用知识库写入/导入函数。
  - 返回结果能体现未写入或 skipped。
- 当该字段为 true 时：
  - 保持原有写入行为。
- 增加/更新单测，覆盖 true/false 两种分支。
- 验证：focused tests + lint/compile/diff-check。

## TODO
- [x] Phase 1：定位立即生成复盘入口和字段名。
- [x] Phase 2：定位后端写入知识库分支。
- [ ] Phase 3：修复 false 被忽略/默认覆盖/字符串 truthy 等逻辑问题。
- [ ] Phase 4：补测试并验证。
- [ ] Phase 5：提交/部署（视修复结果）。

## 当前状态
已定位链路：
- 前端入口：`frontend/src/pages/WxbotPage.tsx` 的 “立即生成复盘” 按钮调用 `previewSelfReview()`，请求 `GET /plugins/wxbot/admin/self-review/preview/{session_id}`。
- 前端设置字段：UI 标注 “自动写入知识库”，状态变量 `selfReviewAutoCreateKbDoc`，保存订阅时发送 `auto_create_kb_doc: selfReviewAutoCreateKbDoc === "true"`。
- 问题点 1：手动 “立即生成复盘” preview 请求只发送 `session_name` / `date`，没有发送当前表单里的 `auto_create_kb_doc`，未保存 false 时后端会继续按已保存订阅或默认 true 写 KB。
- 后端 schema/router：`plugins/wxbot/router.py`，`WxbotSelfReviewSubscriptionRequest.auto_create_kb_doc`，`preview_self_review()`。
- 后端处理：`plugins/wxbot/self_review.py`，`WxbotSelfReviewService.run_self_review_job()`。
- 知识库写入点：`WxbotSelfReviewService._write_kb_document()` 调用 `kb_service.add_document(...)`，由 `if auto_create_kb_doc:` 控制。
- 问题点 2：`bool(subscription.get("auto_create_kb_doc", True))` 会把字符串 `"false"` 当成 true；store hydrate/upsert 也存在直接 `bool(...)` 转换。

## 实施结果

### 根因
1. 前端“立即生成复盘”/preview 请求未携带当前 UI 中的 `auto_create_kb_doc` 值，只传 `session_name` 和 `date`。因此用户把页面开关调成 false 但未保存时，后端仍使用已保存订阅/默认配置，可能继续写入知识库。
2. 后端部分路径使用 `bool(value)` 转布尔值，字符串 `"false"` 会被 Python 视为 true，导致配置/请求若以字符串形式进入时被误判。
3. preview route 创建异步 job 时没有在 job payload 中保留“本次立即生成”的显式写入选择，job 执行阶段只能回读订阅配置。

### 修复
- `frontend/src/pages/WxbotPage.tsx`：preview 请求和 preview 轮询都显式传 `auto_create_kb_doc: selfReviewAutoCreateKbDoc === "true"`。
- `plugins/wxbot/router.py`：preview route 接收 `auto_create_kb_doc`，写入 `review_payload.requested_auto_create_kb_doc`。
- `plugins/wxbot/self_review.py`：job 执行时优先使用 `review_payload.requested_auto_create_kb_doc`，没有才回退到订阅配置，并在结果 payload 中返回实际 `auto_create_kb_doc`。
- `plugins/wxbot/store.py`：新增 `coerce_bool()`，修复 `"false"` 被 `bool("false")` 误判为 true 的问题，并用于订阅 hydrate/upsert/self-review 执行路径。
- `tests/unit/test_wxbot_reports.py`：覆盖 false 不写 KB、true 写 KB、字符串 false 不写 KB。
- `tests/unit/test_wxbot_router.py`：覆盖 preview query `auto_create_kb_doc=false` 能保留到 job payload 且不返回 kb_doc_id。

### 验证
- `git diff --check` ✅
- `python3 -m py_compile plugins/wxbot/store.py plugins/wxbot/self_review.py plugins/wxbot/router.py tests/unit/test_wxbot_reports.py tests/unit/test_wxbot_router.py` ✅
- `python3 -m pytest tests/unit/test_wxbot_reports.py -q -k 'self_review and (kb_write or string_false or explicit_false or true_writes)'` ✅ 3/3
- `python3 -m pytest tests/unit/test_wxbot_router.py -q -k 'self_review_preview_preserves_explicit_false_kb_flag'` ✅ 1/1
- `npm --prefix frontend run build` ✅

### 剩余风险
- 完整 `tests/unit/test_wxbot_router.py` 在 Codex runner 中出现长时间等待；主会话 focused router test 已通过。后续如需可单独调查完整 suite 是否有既有慢/阻塞测试。
- 尚未提交/部署。

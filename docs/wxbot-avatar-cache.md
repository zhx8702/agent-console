# Linux 微信 SDK 头像缓存刷新

SDK 发送器用真实数据库头像核对搜索结果。微信窗口能打开群，不代表 SDK 缓存已同步。
若 `/ext/roster/groups` 显示 `avatar.cached=false`，头像接口返回 404，发送器会拒绝未核验的目标。

2026-09-20 检查发现原始 `db_storage/head_image/head_image.db` 已更新，而 SDK 解密头像库仍停留在
7 月 15 日。消息和联系人库的正常同步不能证明头像库也在更新。重新生成真实头像快照后，目标群
`49025625236@chatroom` 的头像接口返回 200、9338 字节 JPEG，`cached=true`。

`scripts/refresh_wxbot_avatar_cache.py` 使用当前账号已有的 `keys.json`，通过 SQLCipher 只读连接原始库，
读取已提交的 WAL，并生成一致的独立快照。源库及快照均通过完整性检查后，使用 SQLite backup 原子更新
SDK 的头像缓存。不会下载替代头像、写入微信原始库或绕过发送器身份校验。

依赖独立安装的 `sqlcipher3-binary==0.6.0`；不加入 Agent Console 的普通运行依赖。示例：

```bash
python3 scripts/refresh_wxbot_avatar_cache.py \
  --source /path/to/account/db_storage/head_image/head_image.db \
  --cache /path/to/account/decrypted/head_image/head_image.db \
  --keys /path/to/account/decrypted/keys.json
```

使用微信桌面账号运行。三个路径必须对应同一个已登录账号；原始库、缓存和密钥文件均须已存在。
可按两分钟周期执行，使用独占锁防止并发；内容未变时不写缓存。首次更新保存
`head_image.before-avatar-refresh.db`（权限 0600）用于回滚，后续不覆盖。密钥错误、完整性失败、
空快照均保留旧缓存并返回失败，日志只输出错误类型，不输出密钥或联系人内容。

服务器已通过 `wxbot-avatar-cache.timer` 定期执行，脚本与依赖保存在 SDK 的持久化 home 卷中。
更新脚本时需重新复制到该目录；切换账号时需同步更新 service 中的账号路径。

验证应分两层：头像 HTTP 200 / cached=true 只证明头像可用；完整会话选择校验仍需观察 SDK 真实回执。
不要为测试自动重放旧失败回复，也不要把 Jev 的求助识别成功当作微信已发送。

```bash
PYTHONPATH=/path/to/sqlcipher-install uv run pytest tests/integration/test_wxbot_avatar_cache.py
```

集成测试使用独立临时 SQLCipher 数据库，覆盖已提交 WAL、原始库不被修改、备份保留、重复执行、
错误密钥、空库以及源/目标路径冲突。

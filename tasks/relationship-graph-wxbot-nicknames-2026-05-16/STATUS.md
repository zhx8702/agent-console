# Relationship Graph wxbot 昵称映射

日期：2026-05-16

## 目标

将群聊关系图中的 wxid/userid 节点映射为普通人能看懂的名称：

优先级建议：
1. 群内昵称 / 群名片
2. 联系人备注名
3. 微信昵称
4. 已有 graph alias / entity name
5. 短 wxid fallback

## 安全边界

- 不返回原始聊天内容。
- 只返回联系人/群成员显示名 metadata。
- 如果 wxbot SDK 不可用或表结构不匹配，图谱 API 应正常 fallback。

## 阶段 TODO

- [ ] 阶段 1：探索 wxbot SDK 数据库、表、字段。
- [ ] 阶段 2：设计最小后端方法：session_id + wxid list -> display name map。
- [ ] 阶段 3：Codex 实现 + 测试。
- [ ] 阶段 4：部署 + smoke 验证真实图谱返回昵称。

## 当前状态

阶段 1 进行中。

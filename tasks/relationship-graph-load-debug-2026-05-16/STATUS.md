# Relationship Graph 无法加载排查

日期：2026-05-16

## 用户现象

前端显示所选范围/日期 AI 抽取任务计数：
- pending: 1475
- running: 10
- succeeded: 95

但图谱无法加载。

## 排查目标

确认问题属于以下哪类：
1. 图谱 API 报错或鉴权失败。
2. 前端 scope/date 参数与抽取任务 scope 不一致。
3. succeeded job 只代表 LLM extraction job 成功，但没有产生可投影 graph facts/edges。
4. graph projection/sync 路径没有覆盖当前 memory items。
5. 数据库中有 running job 卡住，导致状态误导。
6. 前端图谱加载逻辑/空数据处理异常。

## 当前阶段

阶段 1：收集运行状态、API 路由、数据库摘要，不输出原始聊天内容。

## 验收

- 给出明确 root cause 或 narrowed cause。
- 给出可执行修复方案。
- 如需改代码，交给 Codex；主会话负责验证和汇报。

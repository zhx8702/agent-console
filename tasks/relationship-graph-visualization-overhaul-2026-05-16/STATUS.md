# Relationship Graph 可视化专项优化

日期：2026-05-16

## 用户反馈

用户截图显示当前关系图可视化仍然不好读，需要记录并逐项解决。

## 截图暴露的问题

1. **圆环布局不可读**
   - 大量节点被均匀摆成一圈。
   - 关系线跨圆环穿过中心，形成蜘蛛网。
   - 无法看出核心人物/主题/关系簇。

2. **标签严重重叠**
   - 节点 label 与其他节点/线重叠。
   - 多个 `user:wxid...` 仍然挤在一起。
   - 中文昵称、英文 id、类型标签混杂，视觉噪音大。

3. **节点语义不清**
   - USER / PERSON / THING / PRODUCT / BRAND 等类型文字显示在图上，干扰阅读。
   - 真实用户、主题、产品、普通 value 节点没有视觉区分。
   - 很多技术 fallback label 仍然以 `user:` / `wxid` 形式出现。

4. **边/关系不可读**
   - 线很多但没有方向感、没有聚合。
   - 只有选中边才显示关系 label，默认无法理解图的含义。
   - 低价值关系如 `said`/value 类边可能占据大量空间。

5. **缺少交互辅助**
   - 没有“只看核心人物/只看人与人关系/隐藏 value 节点/隐藏低价值边”的快速视图。
   - 没有 hover tooltip / 选中邻居高亮 / 局部展开。
   - 画布不能拖拽、缩放、自动适配。

## 目标

把关系图从“技术节点圆环”改成普通人可读的关系视图。

### P0 必须解决

- [x] 改掉纯圆环布局，使用更可读的分层/力导向/中心辐射布局。
- [x] 默认隐藏或弱化 `value` / `said` / 低价值边，优先展示人与人、人与主题、人与项目关系。
- [x] 标签防重叠：只显示重点节点 label，其他节点 hover/列表显示。
- [x] 节点类型视觉化：颜色/形状区分 person/topic/product/value，不在图上直接堆 USER/THING 字样。
- [x] 技术 ID 进一步清理：图上不显示 `user:` 前缀；wxid fallback 只显示短 ID 或“未知成员#xxxx”。

### P1 应解决

- [x] 增加快速视图：
  - 核心人物
  - 人与人关系
  - 人与主题/项目
  - 全部
- [x] 选中节点时高亮一跳邻居，淡化无关节点/边。
- [x] 边列表与图联动：点击列表定位/高亮。
- [x] 图谱说明/图例：颜色代表什么、隐藏了什么。

### P2 可选

- [ ] 引入成熟图库，例如 d3-force 或 vis-network/cytoscape，而不是手写 SVG 圆环。
- [ ] 支持缩放、拖拽、自动 fit view。
- [ ] 支持社区/簇分组。

## 实施内容

- `frontend/src/pages/RelationshipGraphPage.tsx`
  - 增加 `readable/core/people/topics/all` 五种视图模式，默认 `readable`。
  - 增加低价值边过滤、value 节点过滤、节点重要性排序、重点 label 限制。
  - 将原圆环布局改成按 person/core/topic/value/other 分 lane 的语义布局。
  - 增加节点类型颜色、图例、隐藏计数、选中节点一跳邻居高亮与无关淡化。
  - 列表与图联动，选中边/节点后相关项高亮。
  - value 节点详情不展示原始 value 文本，继续保持 No raw content。
- `frontend/src/styles.css`
  - 增加视图 tab、图例、lane label、节点类型颜色、边/节点 focus/fade 状态、移动端适配样式。

## 验收结果

- [x] `npm --prefix frontend run build` 通过。
- [x] `python3 -m pytest tests/unit/test_memory_graph.py -q` 通过（46 passed）。
- [x] `git diff --check` 通过。
- [x] 代码扫描确认详情区域改用 safe label helper，value 节点不直接展示原始 label/aliases。
- [!] 页面截图验收未执行：当前运行环境没有可用 Chromium/Chrome，`browser.open` 返回 No supported browser found。

## 当前状态

实现与命令验收完成，等待最终提交。

## 相关历史提交

- `b426c1d feat(memory): improve relationship graph readability`
- `67bd5f3 feat(memory): map group graph nodes to wxbot contact names`

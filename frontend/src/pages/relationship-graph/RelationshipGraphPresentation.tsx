import {
  GRAPH_CANVAS_HEIGHT,
  GRAPH_CANVAS_WIDTH,
  GRAPH_VIEW_MODES,
  NODE_TYPE_LEGEND,
  acceptanceClass,
  acceptanceStatusLabel,
  displayEdgeSource,
  displayEdgeTarget,
  edgeConnectsNode,
  edgeKey,
  formatConfidence,
  nodeIsFocused,
  nodeSecondaryLabel,
  nodeTypeLabel,
  nodeVisualType,
  readableRelationType,
  relationLabel,
  safeNodeDisplayLabel,
  selectedEdgeTouchesNode,
} from "./graphModel";
import type { RelationshipGraphController } from "./useRelationshipGraphController";

export type RelationshipGraphPresentationProps = Pick<
  RelationshipGraphController,
  | "modeHiddenNodeCount"
  | "modeHiddenEdgeCount"
  | "graphViewMode"
  | "setGraphViewMode"
  | "hiddenGraphNodeCount"
  | "hiddenGraphEdgeCount"
  | "graphNodes"
  | "graphSummaryText"
  | "loading"
  | "visibleGraphEdges"
  | "layout"
  | "selectedEdge"
  | "selectedNode"
  | "selection"
  | "setSelection"
  | "visibleLabels"
  | "neighborNodeIds"
  | "graph"
  | "graphStateMessage"
  | "graphEdges"
  | "nodesById"
  | "modeFilteredNodes"
>;

export function RelationshipGraphPresentation(controller: RelationshipGraphPresentationProps) {
  const {
    modeHiddenNodeCount,
    modeHiddenEdgeCount,
    graphViewMode,
    setGraphViewMode,
    hiddenGraphNodeCount,
    hiddenGraphEdgeCount,
    graphNodes,
    graphSummaryText,
    loading,
    visibleGraphEdges,
    layout,
    selectedEdge,
    selectedNode,
    selection,
    setSelection,
    visibleLabels,
    neighborNodeIds,
    graph,
    graphStateMessage,
    graphEdges,
    nodesById,
    modeFilteredNodes,
  } = controller;

  return (
    <>
        <section className="panel relationship-canvas-panel">
          <div className="panel-header">
            <div>
              <p className="section-kicker">关系图</p>
              <h3>只读关系视图</h3>
            </div>
            <div className="relationship-graph-badges">
              {!!(modeHiddenNodeCount || modeHiddenEdgeCount) && graphViewMode !== "all" && (
                <span className="relationship-graph-filter-pill">
                  视图过滤 {modeHiddenNodeCount} 节点 / {modeHiddenEdgeCount} 关系
                </span>
              )}
              {!!(hiddenGraphNodeCount || hiddenGraphEdgeCount) && (
                <span className="relationship-graph-limit">
                  摘要隐藏 {hiddenGraphNodeCount} 节点 / {hiddenGraphEdgeCount} 关系
                </span>
              )}
              <span className="relationship-privacy-pill">不展示聊天正文</span>
            </div>
          </div>
          <div className="relationship-graph-toolbar" aria-label="关系图视图模式">
            <div className="relationship-view-tabs" role="group" aria-label="关系图视图模式选项">
              {GRAPH_VIEW_MODES.map((mode) => (
                <button
                  key={mode.value}
                  type="button"
                  className={`relationship-view-tab${graphViewMode === mode.value ? " is-active" : ""}`}
                  onClick={() => setGraphViewMode(mode.value)}
                  title={mode.description}
                  aria-pressed={graphViewMode === mode.value}
                >
                  {mode.label}
                </button>
              ))}
            </div>
            <div className="relationship-graph-legend" aria-label="节点类型图例">
              {NODE_TYPE_LEGEND.map((item) => (
                <span key={item.type}>
                  <i className={`relationship-legend-dot is-${item.type}`} />
                  {item.label}
                </span>
              ))}
            </div>
          </div>
          {!!graphNodes.length && (
            <p className="relationship-graph-summary">
              {graphSummaryText} 画布仅作视觉概览；请选择右侧的关系列表或节点列表查看详情。
            </p>
          )}
          {loading ? (
            <div className="relationship-empty">
              <strong>正在加载关系图</strong>
              <span>后端正在返回安全图谱摘要，不会展示原始聊天内容。</span>
            </div>
          ) : graphNodes.length ? (
            <svg
              className="relationship-canvas"
              viewBox={`0 0 ${GRAPH_CANVAS_WIDTH} ${GRAPH_CANVAS_HEIGHT}`}
              aria-hidden="true"
              focusable="false"
              pointerEvents="none"
            >
              <g className="relationship-lane-labels" aria-hidden="true">
                <text x="94" y="38">人物 / 核心成员</text>
                <text x="520" y="38">主题 / 项目</text>
                <text x="710" y="38">产品 / 工具</text>
                {graphViewMode === "all" && <text x="760" y="410">值 / 其他</text>}
              </g>
              {visibleGraphEdges.map((edge) => {
                const from = layout.get(displayEdgeSource(edge));
                const to = layout.get(displayEdgeTarget(edge));
                if (!from || !to) return null;
                const selected = selectedEdge?.id === edge.id;
                const connectedToSelectedNode = edgeConnectsNode(edge, selectedNode?.id);
                const faded = Boolean(selection) && !selected && !connectedToSelectedNode;
                return (
                  <g
                    key={edgeKey(edge)}
                    className={`relationship-edge${selected ? " is-selected" : ""}${connectedToSelectedNode ? " is-neighbor" : ""}${faded ? " is-faded" : ""}`}
                  >
                    <line
                      x1={from.x}
                      y1={from.y}
                      x2={to.x}
                      y2={to.y}
                      className="relationship-edge-line"
                    />
                    {(selected || connectedToSelectedNode) && (
                      <text x={(from.x + to.x) / 2} y={(from.y + to.y) / 2 - 6} className="relationship-edge-label">
                        {readableRelationType(edge.label || edge.type)}
                      </text>
                    )}
                  </g>
                );
              })}
              {Array.from(layout.entries()).map(([nodeId, point]) => {
                const node = graphNodes.find((item) => item.id === nodeId);
                if (!node) return null;
                const selected = selectedNode?.id === node.id;
                const visualType = nodeVisualType(node);
                const label = visibleLabels.get(node.id);
                const focused = nodeIsFocused(node.id, selectedNode?.id || null, selectedEdge, neighborNodeIds);
                const selectedByEdge = selectedEdgeTouchesNode(selectedEdge, node.id);
                return (
                  <g
                    key={node.id}
                    className={`relationship-node is-${visualType}${selected ? " is-selected" : ""}${selectedByEdge ? " is-neighbor" : ""}${!focused ? " is-faded" : ""}${label ? " has-label" : " is-dot"}`}
                  >
                    <circle cx={point.x} cy={point.y} r={selected ? 16 : label ? 12 : 5.5} />
                    {label && (
                      <text x={label.x} y={label.y} textAnchor={label.anchor} className="relationship-node-label">
                        {label.text}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
          ) : (
            <div className="relationship-empty">
              <strong>{graph === null ? "尚未加载" : "没有可显示的节点"}</strong>
              <span>{graphStateMessage}</span>
            </div>
          )}
        </section>

        <div className="relationship-side-stack">
          <section className="panel relationship-list-panel" aria-labelledby="relationship-edge-list-title">
            <div className="panel-header">
              <div>
                <p className="section-kicker">关系</p>
                <h3 id="relationship-edge-list-title">关系列表</h3>
              </div>
            </div>
            <div className="relationship-list">
              {graphEdges.map((edge) => (
                <button
                  key={edge.id}
                  type="button"
                  className={`relationship-list-item${selectedEdge?.id === edge.id ? " is-selected" : ""}${edgeConnectsNode(edge, selectedNode?.id) ? " is-related" : ""}`}
                  onClick={() => setSelection({ kind: "edge", item: edge })}
                  aria-pressed={selectedEdge?.id === edge.id}
                >
                  <strong>{relationLabel(edge, nodesById)}</strong>
                  <span className={acceptanceClass(edge.acceptance_status)}>{acceptanceStatusLabel(edge.acceptance_status)}</span>
                  <small>{readableRelationType(edge.label || edge.type)} · 置信度 {formatConfidence(edge.confidence)} · {edge.evidence_count ?? 0} 条证据</small>
                </button>
              ))}
              {!graphEdges.length && <p className="muted-copy">{loading ? "正在加载关系..." : "没有关系匹配当前查询、搜索条件或视图模式。"}</p>}
            </div>
          </section>

          <section className="panel relationship-list-panel" aria-labelledby="relationship-node-list-title">
            <div className="panel-header">
              <div>
                <p className="section-kicker">节点</p>
                <h3 id="relationship-node-list-title">节点列表</h3>
              </div>
            </div>
            <div className="relationship-list">
              {modeFilteredNodes.map((node) => (
                <button
                  key={node.id}
                  type="button"
                  className={`relationship-list-item${selectedNode?.id === node.id ? " is-selected" : ""}${neighborNodeIds.has(node.id) || selectedEdgeTouchesNode(selectedEdge, node.id) ? " is-related" : ""}`}
                  onClick={() => setSelection({ kind: "node", item: node })}
                  aria-pressed={selectedNode?.id === node.id}
                >
                  <strong>{safeNodeDisplayLabel(node)}</strong>
                  <span className={acceptanceClass(node.acceptance_status)}>{acceptanceStatusLabel(node.acceptance_status)}</span>
                  <small>{nodeTypeLabel(node.type) || nodeSecondaryLabel(node)} · 置信度 {formatConfidence(node.confidence)} · {node.evidence_count ?? 0} 条证据</small>
                </button>
              ))}
              {!modeFilteredNodes.length && <p className="muted-copy">{loading ? "正在加载节点..." : "没有节点匹配当前查询、搜索条件或视图模式。"}</p>}
            </div>
          </section>
        </div>
    </>
  );
}

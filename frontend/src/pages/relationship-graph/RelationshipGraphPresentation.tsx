import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import { DangerAction } from "../../components/DangerAction";
import {
  GRAPH_CANVAS_HEIGHT,
  GRAPH_CANVAS_WIDTH,
  GRAPH_RANGE_PRESETS,
  GRAPH_VIEW_MODES,
  NODE_TYPE_LEGEND,
  acceptanceClass,
  acceptanceStatusLabel,
  displayEdgeSource,
  displayEdgeTarget,
  edgeBundleOffsets,
  edgeConnectsNode,
  edgeKey,
  edgeRecencyOpacity,
  edgeSeenOnDate,
  edgeStrokeWidth,
  extractionMethodLabel,
  quadraticEdgePath,
  formatConfidence,
  isPendingReviewStatus,
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
  | "graphRangeDays"
  | "applyGraphRangeDays"
  | "playbackDate"
  | "applyPlaybackDate"
  | "playbackDates"
  | "pendingEdges"
  | "pendingReviewError"
  | "pendingReviewLoading"
  | "reviewing"
  | "reviewEdge"
>;

type CanvasView = { x: number; y: number; scale: number };

const FIT_VIEW: CanvasView = { x: 0, y: 0, scale: 1 };

function useCanvasViewport() {
  const [view, setView] = useState<CanvasView>(FIT_VIEW);
  const [panning, setPanning] = useState(false);
  const viewRef = useRef(view);
  viewRef.current = view;
  const dragRef = useRef<{ pointerId: number; x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);
  const pannedRef = useRef(false);
  const svgRef = useRef<SVGSVGElement | null>(null);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const current = viewRef.current;
      const rect = svg.getBoundingClientRect();
      const mx = ((event.clientX - rect.left) / rect.width) * GRAPH_CANVAS_WIDTH;
      const my = ((event.clientY - rect.top) / rect.height) * GRAPH_CANVAS_HEIGHT;
      const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
      const nextScale = Math.min(3.2, Math.max(0.45, current.scale * factor));
      const ratio = nextScale / current.scale;
      const next = {
        scale: nextScale,
        x: mx - (mx - current.x) * ratio,
        y: my - (my - current.y) * ratio,
      };
      viewRef.current = next;
      setView(next);
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, []);

  const resetView = () => {
    viewRef.current = FIT_VIEW;
    setView(FIT_VIEW);
  };

  const onPointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (event.button !== 0) return;
    pannedRef.current = false;
    const target = event.target as Element;
    if (target.closest("[data-graph-item]")) return;
    dragRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      vx: view.x,
      vy: view.y,
      moved: false,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    setPanning(true);
  };

  const onPointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const dx = ((event.clientX - drag.x) / rect.width) * GRAPH_CANVAS_WIDTH;
    const dy = ((event.clientY - drag.y) / rect.height) * GRAPH_CANVAS_HEIGHT;
    if (Math.abs(dx) + Math.abs(dy) > 3) {
      drag.moved = true;
      pannedRef.current = true;
    }
    const next = { ...view, x: drag.vx + dx, y: drag.vy + dy };
    viewRef.current = next;
    setView(next);
  };

  const onPointerUp = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    setPanning(false);
  };

  const didPan = () => pannedRef.current;

  return { view, panning, svgRef, resetView, onPointerDown, onPointerMove, onPointerUp, didPan };
}

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
    graphRangeDays,
    applyGraphRangeDays,
    playbackDate,
    applyPlaybackDate,
    playbackDates,
    pendingEdges,
    pendingReviewError,
    pendingReviewLoading,
    reviewing,
    reviewEdge,
  } = controller;
  const canvas = useCanvasViewport();
  const bundleOffsets = edgeBundleOffsets(visibleGraphEdges);

  return (
    <>
        <section className="panel relationship-canvas-panel">
          <div className="panel-header">
            <div>
              <p className="section-kicker">关系图</p>
              <h3>可点选关系视图</h3>
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
            <div className="relationship-view-tabs" role="group" aria-label="关系图时间范围">
              {GRAPH_RANGE_PRESETS.map((preset) => (
                <button
                  key={preset.days}
                  type="button"
                  className={`relationship-view-tab${graphRangeDays === preset.days ? " is-active" : ""}`}
                  onClick={() => void applyGraphRangeDays(preset.days)}
                  aria-pressed={graphRangeDays === preset.days}
                >
                  {preset.label}
                </button>
              ))}
            </div>
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
              <button className="button button-secondary button-compact" type="button" onClick={canvas.resetView}>
                适应画布
              </button>
            </div>
          </div>
          {playbackDates.length > 1 && (
            <div className="relationship-playback" aria-label="按日回放">
              <label>
                <span>按日回放</span>
                <input
                  type="range"
                  min={0}
                  max={playbackDates.length - 1}
                  value={Math.max(0, playbackDates.indexOf(playbackDate || playbackDates[playbackDates.length - 1] || ""))}
                  onChange={(event) => void applyPlaybackDate(playbackDates[Number(event.target.value)] || "")}
                />
              </label>
              <strong>{playbackDate || "区间概览"}</strong>
              <button
                className="button button-secondary button-compact"
                type="button"
                onClick={() => void applyPlaybackDate("")}
                disabled={!playbackDate}
              >
                退出回放
              </button>
            </div>
          )}
          {!!graphNodes.length && (
            <p className="relationship-graph-summary">
              {graphSummaryText} 可在画布上点选、拖动画布平移、滚轮缩放；待审关系用虚线，线宽表示证据数，透明度表示新旧。
            </p>
          )}
          {loading ? (
            <div className="relationship-empty">
              <strong>正在加载关系图</strong>
              <span>后端正在返回安全图谱摘要，不会展示原始聊天内容。</span>
            </div>
          ) : graphNodes.length ? (
            <svg
              ref={canvas.svgRef}
              className={`relationship-canvas${canvas.panning ? " is-panning" : ""}`}
              viewBox={`0 0 ${GRAPH_CANVAS_WIDTH} ${GRAPH_CANVAS_HEIGHT}`}
              role="img"
              aria-label="群聊关系图"
              onPointerDown={canvas.onPointerDown}
              onPointerMove={canvas.onPointerMove}
              onPointerUp={canvas.onPointerUp}
              onPointerCancel={canvas.onPointerUp}
            >
              <g transform={`translate(${canvas.view.x} ${canvas.view.y}) scale(${canvas.view.scale})`}>
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
                  const pending = isPendingReviewStatus(edge.acceptance_status);
                  const playbackHit = edgeSeenOnDate(edge, playbackDate);
                  const path = quadraticEdgePath(from, to, bundleOffsets.get(edgeKey(edge)) || 0);
                  return (
                    <g
                      key={edgeKey(edge)}
                      className={`relationship-edge${selected ? " is-selected" : ""}${connectedToSelectedNode ? " is-neighbor" : ""}${faded ? " is-faded" : ""}${pending ? " is-pending" : ""}${playbackHit ? " is-playback" : ""}`}
                      role="button"
                      tabIndex={0}
                      data-graph-item="edge"
                      aria-label={`关系 ${readableRelationType(edge.label || edge.type)}`}
                      aria-pressed={selected}
                      onClick={(event) => {
                        event.stopPropagation();
                        if (canvas.didPan()) return;
                        setSelection({ kind: "edge", item: edge });
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelection({ kind: "edge", item: edge });
                        }
                      }}
                    >
                      <path d={path} className="relationship-edge-hit" />
                      <path
                        d={path}
                        className="relationship-edge-line"
                        style={{
                          strokeWidth: selected || playbackHit ? Math.max(2.8, edgeStrokeWidth(edge)) : edgeStrokeWidth(edge),
                          opacity: faded && !playbackHit ? 0.12 : edgeRecencyOpacity(edge),
                          strokeDasharray: pending ? "6 4" : undefined,
                        }}
                      />
                      {(selected || connectedToSelectedNode) && (
                        <text x={(from.x + to.x) / 2} y={(from.y + to.y) / 2 - 6} className="relationship-edge-label">
                          {readableRelationType(edge.label || edge.type)} · {extractionMethodLabel(edge.extraction_method)}
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
                      role="button"
                      tabIndex={0}
                      data-graph-item="node"
                      aria-label={`节点 ${safeNodeDisplayLabel(node)}`}
                      aria-pressed={selected}
                      onClick={(event) => {
                        event.stopPropagation();
                        if (canvas.didPan()) return;
                        setSelection({ kind: "node", item: node });
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelection({ kind: "node", item: node });
                        }
                      }}
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
              </g>
            </svg>
          ) : (
            <div className="relationship-empty">
              <strong>{graph === null ? "尚未加载" : "没有可显示的节点"}</strong>
              <span>{graphStateMessage}</span>
            </div>
          )}
        </section>

        <div className="relationship-side-stack">
          <section className="panel relationship-list-panel" aria-labelledby="relationship-review-queue-title">
            <div className="panel-header">
              <div>
                <p className="section-kicker">审核</p>
                <h3 id="relationship-review-queue-title">待审核队列</h3>
              </div>
              <span className="relationship-queue-count">{pendingEdges.length}</span>
            </div>
            <div className="relationship-list">
              {pendingReviewError && (
                <p className="admin-notice admin-notice-danger" role="alert">
                  {pendingReviewError}
                </p>
              )}
              {pendingEdges.map((edge) => (
                <div
                  key={edge.id}
                  className={`relationship-queue-item${selectedEdge?.id === edge.id ? " is-selected" : ""}`}
                >
                  <button
                    type="button"
                    className="relationship-list-item"
                    onClick={() => setSelection({ kind: "edge", item: edge })}
                    aria-pressed={selectedEdge?.id === edge.id}
                  >
                    <strong>{relationLabel(edge, nodesById)}</strong>
                    <span className={acceptanceClass(edge.acceptance_status)}>{acceptanceStatusLabel(edge.acceptance_status)}</span>
                    <small>
                      {readableRelationType(edge.label || edge.type)} · {extractionMethodLabel(edge.extraction_method)} · 置信度 {formatConfidence(edge.confidence)} · {edge.evidence_count ?? 0} 条证据
                    </small>
                  </button>
                  <div className="relationship-queue-actions">
                    <DangerAction
                      label={reviewing ? "审核中" : "接受"}
                      title="确认接受该关系"
                      confirmLabel="确认接受"
                      pendingLabel="正在接受…"
                      disabled={reviewing}
                      impact={(
                        <dl>
                          <div><dt>关系</dt><dd>{readableRelationType(edge.label || edge.type)}</dd></div>
                          <div><dt>当前状态</dt><dd>{acceptanceStatusLabel(edge.acceptance_status)}</dd></div>
                          <div><dt>影响</dt><dd>写入记忆验收记录，并进入默认关系图。</dd></div>
                        </dl>
                      )}
                      onConfirm={() => reviewEdge("accept", edge)}
                    />
                    <DangerAction
                      label={reviewing ? "审核中" : "拒绝"}
                      title="确认拒绝该关系"
                      confirmLabel="确认拒绝"
                      pendingLabel="正在拒绝…"
                      disabled={reviewing}
                      impact={(
                        <dl>
                          <div><dt>关系</dt><dd>{readableRelationType(edge.label || edge.type)}</dd></div>
                          <div><dt>当前状态</dt><dd>{acceptanceStatusLabel(edge.acceptance_status)}</dd></div>
                          <div><dt>影响</dt><dd>标记为已拒绝；默认关系图不再显示该边。</dd></div>
                        </dl>
                      )}
                      onConfirm={() => reviewEdge("reject", edge)}
                    />
                  </div>
                </div>
              ))}
              {!pendingReviewError && !pendingEdges.length && (
                <p className="muted-copy">{pendingReviewLoading ? "正在加载待审核关系..." : "当前时间范围内没有待审核关系。"}</p>
              )}
            </div>
          </section>

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
                  <small>
                    {readableRelationType(edge.label || edge.type)} · {extractionMethodLabel(edge.extraction_method)} · 置信度 {formatConfidence(edge.confidence)} · {edge.evidence_count ?? 0} 条证据
                  </small>
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

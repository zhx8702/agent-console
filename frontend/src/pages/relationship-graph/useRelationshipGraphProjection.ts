import { useMemo } from "react";

import type {
  GroupGraphEdge,
  GroupGraphHistoryDateRow,
  GroupGraphNode,
  GroupGraphResponse,
  GroupGraphWindowStatsResponse,
  MemoryExtractionJobStatsResponse,
} from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import {
  DEFAULT_EDGE_TYPES,
  DEFAULT_NODE_TYPES,
  GRAPH_VIEW_BUDGETS,
  buildAnonymousGraphLabels,
  buildGraphLayout,
  buildVisibleLabels,
  dateStatusLabel,
  displayEdgeSource,
  displayEdgeTarget,
  filterNodesForMode,
  jobCount,
  jobCountsTotal,
  jobStatsSummary,
  matchesGraphText,
  nodeDisplayLabel,
  relationLabel,
  selectGraphNodes,
  shouldKeepEdgeForMode,
  sortEdgesByImportance,
  sortedUnique,
  type GraphViewMode,
} from "./graphModel";

type RelationshipGraphProjectionInput = {
  config: ConsoleConfig;
  graph: GroupGraphResponse | null;
  selectedNode: GroupGraphNode | null;
  selectedEdge: GroupGraphEdge | null;
  selectedGroupIsVerified: boolean;
  nodeSearch: string;
  edgeSearch: string;
  graphViewMode: GraphViewMode;
  dateRows: GroupGraphHistoryDateRow[];
  targetDate: string;
  jobStats: MemoryExtractionJobStatsResponse | null;
  extractionBatchLimit: string;
  extractionContinuous: boolean;
  extractionMaxJobs: string;
  windowExtractionSize: string;
  windowExtractionMaxWindows: string;
  windowCatchupMaxWindows: string;
  windowStats: GroupGraphWindowStatsResponse | null;
  loading: boolean;
  syncing: boolean;
};

export function useRelationshipGraphProjection({
  config,
  graph,
  selectedNode,
  selectedEdge,
  selectedGroupIsVerified,
  nodeSearch,
  edgeSearch,
  graphViewMode,
  dateRows,
  targetDate,
  jobStats,
  extractionBatchLimit,
  extractionContinuous,
  extractionMaxJobs,
  windowExtractionSize,
  windowExtractionMaxWindows,
  windowCatchupMaxWindows,
  windowStats,
  loading,
  syncing,
}: RelationshipGraphProjectionInput) {
  const nodeTypeOptions = useMemo(
    () => sortedUnique([...(graph?.schema?.node_types || []), ...DEFAULT_NODE_TYPES]),
    [graph?.schema?.node_types],
  );
  const edgeTypeOptions = useMemo(
    () => sortedUnique([...(graph?.schema?.edge_types || []), ...DEFAULT_EDGE_TYPES]),
    [graph?.schema?.edge_types],
  );

  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  const nodesById = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const filteredEdges = useMemo(
    () => edges.filter((edge) => matchesGraphText(relationLabel(edge, nodesById), edgeSearch)),
    [edgeSearch, edges, nodesById],
  );
  const edgeNodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const edge of filteredEdges) {
      ids.add(displayEdgeSource(edge));
      ids.add(displayEdgeTarget(edge));
    }
    return ids;
  }, [filteredEdges]);
  const filteredNodes = useMemo(
    () => nodes.filter((node) => (
      matchesGraphText(
        [nodeDisplayLabel(node), node.label, node.display_label, node.technical_label, node.id, node.type, ...(node.aliases || [])]
          .filter(Boolean)
          .join(" "),
        nodeSearch,
      )
      && (!edgeSearch.trim() || edgeNodeIds.has(node.id))
    )),
    [edgeNodeIds, edgeSearch, nodeSearch, nodes],
  );
  const forcedGraphNodeIds = useMemo(() => {
    const ids = new Set<string>();
    if (nodeSearch.trim()) {
      for (const node of filteredNodes) ids.add(node.id);
    }
    if (edgeSearch.trim()) {
      for (const edge of filteredEdges) {
        ids.add(displayEdgeSource(edge));
        ids.add(displayEdgeTarget(edge));
      }
    }
    if (selectedNode) ids.add(selectedNode.id);
    if (selectedEdge) {
      ids.add(displayEdgeSource(selectedEdge));
      ids.add(displayEdgeTarget(selectedEdge));
    }
    return ids;
  }, [edgeSearch, filteredEdges, filteredNodes, nodeSearch, selectedEdge, selectedNode]);
  const modeFilteredEdges = useMemo(
    () => filteredEdges.filter((edge) => shouldKeepEdgeForMode(edge, nodesById, graphViewMode, forcedGraphNodeIds)),
    [filteredEdges, forcedGraphNodeIds, graphViewMode, nodesById],
  );
  const modeFilteredNodes = useMemo(
    () => filterNodesForMode(filteredNodes, modeFilteredEdges, graphViewMode, forcedGraphNodeIds),
    [filteredNodes, forcedGraphNodeIds, graphViewMode, modeFilteredEdges],
  );
  const graphBudget = GRAPH_VIEW_BUDGETS[graphViewMode];
  const filteredNodeIds = useMemo(() => new Set(modeFilteredNodes.map((node) => node.id)), [modeFilteredNodes]);
  const visibleEdges = useMemo(
    () => filteredEdges.filter((edge) => filteredNodeIds.has(displayEdgeSource(edge)) && filteredNodeIds.has(displayEdgeTarget(edge))),
    [filteredEdges, filteredNodeIds],
  );
  const rankedGraphEdges = useMemo(
    () => sortEdgesByImportance(
      modeFilteredEdges.filter((edge) => filteredNodeIds.has(displayEdgeSource(edge)) && filteredNodeIds.has(displayEdgeTarget(edge))),
      nodesById,
      forcedGraphNodeIds,
    ),
    [filteredNodeIds, forcedGraphNodeIds, modeFilteredEdges, nodesById],
  );
  const graphNodes = useMemo(
    () => selectGraphNodes(modeFilteredNodes, rankedGraphEdges, graphBudget.nodes, forcedGraphNodeIds),
    [forcedGraphNodeIds, graphBudget.nodes, modeFilteredNodes, rankedGraphEdges],
  );
  const graphNodeIds = useMemo(() => new Set(graphNodes.map((node) => node.id)), [graphNodes]);
  const graphEdges = useMemo(
    () => rankedGraphEdges.filter((edge) => graphNodeIds.has(displayEdgeSource(edge)) && graphNodeIds.has(displayEdgeTarget(edge))),
    [graphNodeIds, rankedGraphEdges],
  );
  const anonymousGraphLabels = useMemo(
    () => buildAnonymousGraphLabels(graphNodes),
    [graphNodes],
  );
  const layout = useMemo(() => buildGraphLayout(graphNodes, graphEdges), [graphEdges, graphNodes]);
  const visibleGraphEdges = useMemo(
    () => graphEdges
      .filter((edge) => layout.has(displayEdgeSource(edge)) && layout.has(displayEdgeTarget(edge)))
      .slice(0, graphBudget.edges),
    [graphBudget.edges, graphEdges, layout],
  );
  const visibleLabels = useMemo(
    () => buildVisibleLabels(
      graphNodes,
      visibleGraphEdges,
      layout,
      anonymousGraphLabels,
      graphBudget.labels,
      selectedNode?.id,
      selectedEdge,
    ),
    [anonymousGraphLabels, graphBudget.labels, graphNodes, layout, selectedEdge, selectedNode?.id, visibleGraphEdges],
  );
  const hiddenGraphNodeCount = Math.max(0, modeFilteredNodes.length - graphNodes.length);
  const hiddenGraphEdgeCount = Math.max(0, rankedGraphEdges.length - visibleGraphEdges.length);
  const modeHiddenNodeCount = Math.max(0, filteredNodes.length - modeFilteredNodes.length);
  const modeHiddenEdgeCount = Math.max(0, visibleEdges.length - rankedGraphEdges.length);
  const graphSummaryText = graphViewMode === "all"
    ? `调试视图：显示 ${graphNodes.length} 个节点 / ${visibleGraphEdges.length} 条关系。`
    : `摘要视图：显示 ${graphNodes.length} 个核心节点 / ${visibleGraphEdges.length} 条高信号关系；另隐藏 ${modeHiddenNodeCount + hiddenGraphNodeCount} 节点 / ${modeHiddenEdgeCount + hiddenGraphEdgeCount} 关系。`;
  const selectedDateStatus = useMemo(
    () => dateRows.find((row) => row.date === targetDate),
    [dateRows, targetDate],
  );
  const missingHistorySyncFields = useMemo(
    () => [
      !config.tenantId.trim() ? "tenant_id" : "",
      !selectedGroupIsVerified ? "session_id" : "",
      !targetDate ? "target_date" : "",
    ].filter(Boolean),
    [config.tenantId, selectedGroupIsVerified, targetDate],
  );
  const missingHistorySyncLabels = missingHistorySyncFields.map((field) => ({
    tenant_id: "租户ID tenant_id",
    session_id: "群聊ID session_id",
    target_date: "同步日期 target_date",
  }[field] || field));
  const historySyncHint = syncing
    ? "正在提交历史同步请求，请等待后端导入和 AI 抽取排队。"
    : missingHistorySyncFields.length
      ? `还缺少 ${missingHistorySyncLabels.join("、")}，补齐后才会发送请求。`
      : "已准备好同步所选日期；建议保持自动 AI 抽取开启。";
  const optionalUserScopeLabel = "当前群全部获授权成员";
  const selectedDateStatusText = dateStatusLabel(selectedDateStatus?.status);
  const selectedDateRawCount = selectedDateStatus?.raw_message_count ?? 0;
  const selectedDateImportedCount = selectedDateStatus?.imported_count ?? 0;
  const scopeJobStats = jobStatsSummary(jobStats);
  const selectedDateJobCounts = selectedDateStatus?.job_counts;
  const selectedJobStats = selectedDateJobCounts
    ? {
        pending: jobCount(selectedDateJobCounts, "pending"),
        running: jobCount(selectedDateJobCounts, "running"),
        succeeded: jobCount(selectedDateJobCounts, "succeeded"),
        failed: jobCount(selectedDateJobCounts, "failed"),
        dead: jobCount(selectedDateJobCounts, "dead"),
        total: jobCountsTotal(selectedDateJobCounts),
        ready: scopeJobStats.ready,
        delayed: scopeJobStats.delayed,
      }
    : scopeJobStats;
  const extractionBatchSize = Number(extractionBatchLimit) || 50;
  const extractionMaxJobCount = extractionContinuous
    ? Math.max(1, Math.min(Number(extractionMaxJobs) || 200, 500))
    : extractionBatchSize;
  const windowExtractionSizeValue = Math.max(10, Math.min(Number(windowExtractionSize) || 50, 100));
  const windowExtractionMaxWindowsValue = Math.max(1, Math.min(Number(windowExtractionMaxWindows) || 1, 10));
  const windowCatchupMaxWindowsValue = Math.max(1, Math.min(Number(windowCatchupMaxWindows) || 20, 100));
  const windowStatsTotals = windowStats?.totals || {};
  const windowStatsAcceptance = windowStats?.acceptance_counts || {};
  const estimatedExtractionClicks = selectedJobStats.pending
    ? Math.ceil(selectedJobStats.pending / Math.max(1, extractionBatchSize))
    : 0;
  const historyNextStep = missingHistorySyncFields.length
    ? `先填写 ${missingHistorySyncLabels.join("、")}。`
    : syncing
      ? "保持页面打开，等待同步完成后再查看日期状态或加载关系图。"
      : selectedDateStatus?.status === "extracted"
        ? selectedJobStats.pending || selectedJobStats.running
          ? "历史已导入；AI 抽取任务仍在排队或运行，完成后再刷新关系图。"
          : "历史已导入；如 AI 抽取成功，可点击“刷新关系图”查看最新关系。"
        : selectedDateStatus?.status === "partial"
          ? "历史只部分导入；可再次同步所选日期，AI 抽取任务状态需单独查看。"
          : "点击“同步所选日期”先导入历史；AI 抽取完成后关系图才会更新。";
  const neighborNodeIds = useMemo(() => {
    const ids = new Set<string>();
    if (!selectedNode) return ids;
    for (const edge of visibleGraphEdges) {
      if (displayEdgeSource(edge) === selectedNode.id) ids.add(displayEdgeTarget(edge));
      if (displayEdgeTarget(edge) === selectedNode.id) ids.add(displayEdgeSource(edge));
    }
    return ids;
  }, [selectedNode, visibleGraphEdges]);
  const graphStateMessage = loading
    ? "正在加载关系图，请稍候。"
    : graph === null
      ? "尚未加载关系图：填写范围和过滤条件后点击“加载关系图”。"
      : nodes.length === 0
        ? "当前范围没有关系图数据；可放宽状态、类型或时间范围后重试。"
        : !filteredNodes.length && (nodeSearch.trim() || edgeSearch.trim())
          ? "当前搜索没有匹配节点或关系；请调整节点/关系关键词。"
          : !graphNodes.length
            ? "当前视图模式隐藏了所有可见节点；可切换到“全部”或调整搜索条件。"
      : "";


  return {
    nodeTypeOptions,
    edgeTypeOptions,
    nodes,
    edges,
    nodesById,
    modeFilteredNodes,
    graphNodes,
    graphEdges,
    layout,
    visibleGraphEdges,
    visibleLabels,
    hiddenGraphNodeCount,
    hiddenGraphEdgeCount,
    modeHiddenNodeCount,
    modeHiddenEdgeCount,
    graphSummaryText,
    selectedDateStatus,
    missingHistorySyncFields,
    historySyncHint,
    optionalUserScopeLabel,
    selectedDateStatusText,
    selectedDateRawCount,
    selectedDateImportedCount,
    scopeJobStats,
    selectedJobStats,
    extractionBatchSize,
    extractionMaxJobCount,
    windowExtractionSizeValue,
    windowExtractionMaxWindowsValue,
    windowCatchupMaxWindowsValue,
    windowStatsTotals,
    windowStatsAcceptance,
    estimatedExtractionClicks,
    historyNextStep,
    neighborNodeIds,
    graphStateMessage,
  };
}

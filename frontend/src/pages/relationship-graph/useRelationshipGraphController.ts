import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  apiRequest,
  formatJson,
  getGroupGraph,
  getGroupGraphEdgeEvidence,
  getGroupGraphHistoryDates,
  getGroupGraphWindowStats,
  getMemoryExtractionJobStats,
  runGroupGraphDailyExtraction,
  runGroupGraphWindowCatchup,
  runGroupGraphWindowExtraction,
  type GroupGraphEdge,
  type GroupGraphEdgeEvidenceResponse,
  type GroupGraphHistoryDateRow,
  type GroupGraphResponse,
  type GroupGraphWindowStatsResponse,
  type MemoryBackfillResponse,
  type MemoryExtractionJobStatsResponse,
} from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { requireSelectedGroup, useConsoleConfig } from "../../state/console-config";

import {
  HISTORY_RECENT_DAYS,
  type Selection,
  type GraphViewMode,
  sanitizeEdgeEvidence,
  localDateValue,
  safeBackfillDebug,
} from "./graphModel";
import { useRelationshipGraphProjection } from "./useRelationshipGraphProjection";

export function useRelationshipGraphController() {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const selectedGroupId = config.sessionId.trim();
  const selectedGroupIsVerified = Boolean(selectedGroupId && verifiedGroupIds.has(selectedGroupId));
  const [channel, setChannel] = useState("wechat");
  const [sourceKey, setSourceKey] = useState("wxbot");
  const [acceptanceStatus, setAcceptanceStatus] = useState("");
  const [nodeType, setNodeType] = useState("");
  const [edgeType, setEdgeType] = useState("");
  const [minConfidence, setMinConfidence] = useState("");
  const [limit, setLimit] = useState("100");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [nodeSearch, setNodeSearch] = useState("");
  const [edgeSearch, setEdgeSearch] = useState("");
  const [graphViewMode, setGraphViewMode] = useState<GraphViewMode>("readable");
  const [targetDate, setTargetDate] = useState(localDateValue());
  const [dateRows, setDateRows] = useState<GroupGraphHistoryDateRow[]>([]);
  const [dateLoading, setDateLoading] = useState(false);
  const [enqueueLlmJobs, setEnqueueLlmJobs] = useState(true);
  const [extractionBatchLimit, setExtractionBatchLimit] = useState("50");
  const [extractionContinuous, setExtractionContinuous] = useState(false);
  const [extractionMaxJobs, setExtractionMaxJobs] = useState("200");
  const [windowExtractionSize, setWindowExtractionSize] = useState("50");
  const [windowExtractionMaxWindows, setWindowExtractionMaxWindows] = useState("1");
  const [windowExtractionDryRun, setWindowExtractionDryRun] = useState(false);
  const [windowExtractionCursor, setWindowExtractionCursor] = useState(0);
  const [windowCatchupMaxWindows, setWindowCatchupMaxWindows] = useState("20");
  const [actionPanelOpen, setActionPanelOpen] = useState(false);
  const extractionTimeBudgetSeconds = 60;
  const currentGraphScopeKey = selectedGroupIsVerified
    ? [config.tenantId.trim(), channel.trim(), sourceKey.trim(), selectedGroupId].join("\u001f")
    : "";
  const autoLoadGroupScopeKey = selectedGroupIsVerified
    ? [config.tenantId.trim(), selectedGroupId].join("\u001f")
    : "";
  const activeGraphScopeKeyRef = useRef(currentGraphScopeKey);
  activeGraphScopeKeyRef.current = currentGraphScopeKey;
  const graphRequestIdRef = useRef(0);
  const autoLoadedGroupScopeKeyRef = useRef<string | null>(null);
  const [loadedGraph, setLoadedGraph] = useState<GroupGraphResponse | null>(null);
  const [loadedGraphScopeKey, setLoadedGraphScopeKey] = useState("");
  const graph = loadedGraphScopeKey === currentGraphScopeKey ? loadedGraph : null;
  const [selection, setSelection] = useState<Selection>(null);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [evidence, setEvidence] = useState<GroupGraphEdgeEvidenceResponse | null>(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceStatus, setEvidenceStatus] = useState("选择一条关系后会自动加载证据来源。");
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');
  const [syncOutput, setSyncOutput] = useState('{\n  "status": "waiting"\n}');
  const [jobStats, setJobStats] = useState<MemoryExtractionJobStatsResponse | null>(null);
  const [jobStatsLoading, setJobStatsLoading] = useState(false);
  const [jobStatsStatus, setJobStatsStatus] = useState("填写 tenant_id 和群聊ID后可查看 AI 抽取任务数量。");
  const [windowStats, setWindowStats] = useState<GroupGraphWindowStatsResponse | null>(null);
  const [windowStatsStatus, setWindowStatsStatus] = useState("选择已验证群聊后可查看窗口关系统计。");
  const selectedNode = selection?.kind === "node" ? selection.item : null;
  const selectedEdge = selection?.kind === "edge" ? selection.item : null;

  const projection = useRelationshipGraphProjection({
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
  });
  const {
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
  } = projection;

  const scopeQuery = useMemo(() => {
    const tenantId = config.tenantId.trim();
    return {
      ...(tenantId ? { tenant_id: tenantId } : {}),
      ...(channel.trim() ? { channel: channel.trim() } : {}),
      ...(sourceKey.trim() ? { source_key: sourceKey.trim() } : {}),
      ...(selectedGroupIsVerified ? { session_id: selectedGroupId } : {}),
    };
  }, [channel, config.tenantId, selectedGroupId, selectedGroupIsVerified, sourceKey]);

  const loadJobStats = useCallback(async () => {
    const tenantId = config.tenantId.trim();
    if (!tenantId || !selectedGroupIsVerified) {
      setJobStats(null);
      setJobStatsLoading(false);
      setJobStatsStatus("填写 tenant_id 和群聊ID后可查看 AI 抽取任务数量。");
      return;
    }
    const requestScopeKey = currentGraphScopeKey;
    setJobStatsLoading(true);
    setJobStatsStatus("正在加载 AI 抽取任务数量。");
    try {
      const query = {
        ...scopeQuery,
        ...(targetDate ? { created_after: `${targetDate}T00:00:00`, created_before: `${targetDate}T23:59:59` } : {}),
        limit: 10,
      };
      const result = await getMemoryExtractionJobStats(config, query);
      if (activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setJobStats(result);
      setJobStatsStatus("AI 抽取任务数量已加载；仅显示状态计数，不显示聊天内容。");
    } catch (err) {
      if (activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setJobStats(null);
      setJobStatsStatus(
        `AI 抽取任务数量加载失败：${
          err instanceof ApiError || err instanceof Error ? err.message : "job stats request failed"
        }`,
      );
    } finally {
      if (activeGraphScopeKeyRef.current === requestScopeKey) {
        setJobStatsLoading(false);
      }
    }
  }, [config, currentGraphScopeKey, scopeQuery, selectedGroupIsVerified, targetDate]);

  const loadWindowStats = useCallback(async () => {
    const tenantId = config.tenantId.trim();
    if (!tenantId || !selectedGroupIsVerified) {
      setWindowStats(null);
      setWindowStatsStatus("选择已验证群聊后可查看窗口关系统计。");
      return;
    }
    const requestScopeKey = currentGraphScopeKey;
    setWindowStatsStatus("正在加载窗口关系统计。");
    try {
      const result = await getGroupGraphWindowStats(config, {
        tenant_id: tenantId,
        channel: channel.trim(),
        source_key: sourceKey.trim(),
        session_id: selectedGroupId,
        date: targetDate || undefined,
      });
      if (activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setWindowStats(result);
      setWindowStatsStatus("窗口关系统计已加载；仅显示计数。");
    } catch (err) {
      if (activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setWindowStats(null);
      setWindowStatsStatus(
        `窗口关系统计加载失败：${
          err instanceof ApiError || err instanceof Error ? err.message : "window stats request failed"
        }`,
      );
    }
  }, [channel, config, currentGraphScopeKey, selectedGroupId, selectedGroupIsVerified, sourceKey, targetDate]);

  const loadGraph = useCallback(async (overrides: Partial<{
    acceptanceStatus: string;
    nodeType: string;
    edgeType: string;
    minConfidence: string;
    limit: string;
    fromDate: string;
    toDate: string;
  }> = {}) => {
    if (!config.tenantId.trim() || !selectedGroupIsVerified) {
      graphRequestIdRef.current += 1;
      setLoadedGraph(null);
      setLoadedGraphScopeKey("");
      setLoading(false);
      setOutput(formatJson({ error: "请先从已验证群聊列表选择目标群" }));
      return;
    }
    const requestId = graphRequestIdRef.current + 1;
    graphRequestIdRef.current = requestId;
    const requestScopeKey = currentGraphScopeKey;
    const queryEdgeType = overrides.edgeType ?? edgeType;
    setLoading(true);
    try {
      const result = await getGroupGraph(config, {
        tenant_id: config.tenantId,
        channel,
        source_key: sourceKey,
        session_id: selectedGroupId,
        acceptance_status: overrides.acceptanceStatus ?? acceptanceStatus,
        node_type: overrides.nodeType ?? nodeType,
        edge_type: queryEdgeType,
        relation_type: queryEdgeType,
        min_confidence: overrides.minConfidence ?? minConfidence,
        limit: overrides.limit ?? limit,
        from: (overrides.fromDate ?? fromDate) || undefined,
        to: (overrides.toDate ?? toDate) || undefined,
      });
      if (graphRequestIdRef.current !== requestId || activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setLoadedGraph(result);
      setLoadedGraphScopeKey(requestScopeKey);
      setSelection(null);
      setEvidence(null);
      setEvidenceStatus("选择一条关系后会自动加载证据来源。");
      setOutput(formatJson({
        schema: result.schema,
        scope: result.scope,
        filters: result.filters,
        counts: result.counts,
        visible_counts: {
          nodes: result.nodes?.length ?? 0,
          edges: result.edges?.length ?? 0,
        },
        generated_from: result.generated_from,
      }));
      void loadJobStats();
    } catch (err) {
      if (graphRequestIdRef.current !== requestId || activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setLoadedGraph(null);
      setLoadedGraphScopeKey("");
      setSelection(null);
      setEvidence(null);
      setEvidenceStatus("关系图加载失败，暂无证据来源。");
      setOutput(formatJson({
        error: err instanceof ApiError || err instanceof Error ? err.message : "group graph request failed",
      }));
    } finally {
      if (graphRequestIdRef.current === requestId && activeGraphScopeKeyRef.current === requestScopeKey) {
        setLoading(false);
      }
    }
  }, [acceptanceStatus, channel, config, currentGraphScopeKey, edgeType, fromDate, limit, loadJobStats, minConfidence, nodeType, selectedGroupId, selectedGroupIsVerified, sourceKey, toDate]);
  const loadGraphRef = useRef(loadGraph);
  loadGraphRef.current = loadGraph;

  const loadEdgeEvidence = useCallback(async (edge: GroupGraphEdge) => {
    const tenantId = config.tenantId.trim();
    if (!tenantId || !selectedGroupIsVerified) {
      setEvidence(null);
      setEvidenceStatus("无法加载证据来源：缺少 tenant_id。");
      return;
    }
    const requestScopeKey = currentGraphScopeKey;
    setEvidenceLoading(true);
    setEvidence(null);
    setEvidenceStatus("正在加载证据来源；不展示原始聊天内容。");
    try {
      const result = await getGroupGraphEdgeEvidence(config, edge.id, {
        tenant_id: tenantId,
        channel: channel.trim(),
        source_key: sourceKey.trim(),
        session_id: selectedGroupId,
      });
      if (activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setEvidence(sanitizeEdgeEvidence(result));
      setEvidenceStatus("证据来源已加载；不展示原始聊天内容。");
    } catch (err) {
      if (activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setEvidence(null);
      setEvidenceStatus(
        `证据来源加载失败，关系图已保留：${
          err instanceof ApiError || err instanceof Error ? err.message : "evidence request failed"
        }`,
      );
    } finally {
      if (activeGraphScopeKeyRef.current === requestScopeKey) {
        setEvidenceLoading(false);
      }
    }
  }, [channel, config, currentGraphScopeKey, selectedGroupId, selectedGroupIsVerified, sourceKey]);

  const loadDateStatuses = useCallback(async () => {
    const tenantId = config.tenantId.trim();
    if (!tenantId || !selectedGroupIsVerified) {
      setDateRows([]);
      setDateLoading(false);
      return;
    }
    const requestScopeKey = currentGraphScopeKey;
    setDateLoading(true);
    try {
      const result = await getGroupGraphHistoryDates(config, {
        tenant_id: tenantId,
        channel: channel.trim(),
        source_key: sourceKey.trim(),
        session_id: selectedGroupId,
        recent_days: HISTORY_RECENT_DAYS,
      });
      if (activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setDateRows(result.items || []);
      void loadJobStats();
    } catch (err) {
      if (activeGraphScopeKeyRef.current !== requestScopeKey) return;
      setDateRows([]);
      setSyncOutput(formatJson({
        error: err instanceof ApiError || err instanceof Error ? err.message : "history date status request failed",
      }));
    } finally {
      if (activeGraphScopeKeyRef.current === requestScopeKey) {
        setDateLoading(false);
      }
    }
  }, [channel, config, currentGraphScopeKey, loadJobStats, selectedGroupId, selectedGroupIsVerified, sourceKey]);

  const loadGraphAndStatus = useCallback(async (graphOverrides?: Parameters<typeof loadGraph>[0]) => {
    await Promise.all([loadGraph(graphOverrides), loadDateStatuses(), loadJobStats(), loadWindowStats()]);
  }, [loadDateStatuses, loadGraph, loadJobStats, loadWindowStats]);

  const showAllGraph = useCallback(async () => {
    setGraphViewMode("all");
    setAcceptanceStatus("accepted");
    setNodeType("");
    setEdgeType("");
    setMinConfidence("");
    setFromDate("");
    setToDate("");
    setNodeSearch("");
    setEdgeSearch("");
    setLimit("500");
    await loadGraphAndStatus({
      acceptanceStatus: "accepted",
      nodeType: "",
      edgeType: "",
      minConfidence: "",
      fromDate: "",
      toDate: "",
      limit: "500",
    });
  }, [loadGraphAndStatus]);

  const runHistorySync = async () => {
    const tenantId = config.tenantId.trim();
    const sessionId = requireSelectedGroup(config, verifiedGroupIds);
    const normalizedChannel = channel.trim();
    const normalizedSourceKey = sourceKey.trim();
    const connectionId = "legacy-wechat-default";
    const missingFields = [
      !tenantId ? "tenant_id" : "",
      !targetDate ? "target_date" : "",
    ].filter(Boolean);

    if (missingFields.length) {
      setSyncOutput(formatJson({
        status: "validation_failed",
        message: `校验失败：缺少 ${missingFields.join(", ")}，未发送 API 请求。`,
        no_api_request_sent: true,
        missing_fields: missingFields,
        error: `Missing ${missingFields.join(", ")}. No API request was sent.`,
      }));
      return;
    }

    setSyncing(true);
    setSyncOutput(formatJson({
      status: "submitting",
      message: "正在提交历史同步请求。",
      scope: {
        tenant_id: tenantId,
        channel: normalizedChannel,
        source_key: normalizedSourceKey,
        connection_id: connectionId,
        user_scope: optionalUserScopeLabel,
        session_id: sessionId,
        target_date: targetDate,
        enqueue_llm_jobs: enqueueLlmJobs,
      },
    }));
    try {
      const body = {
        tenant_id: tenantId,
        channel: normalizedChannel,
        source_key: normalizedSourceKey,
        connection_id: connectionId,
        session_ids: [sessionId],
        target_date: targetDate,
        enqueue_llm_jobs: enqueueLlmJobs,
      };
      const intent = `relationship:history:${tenantId}:${connectionId}:${sessionId}:${targetDate}:${enqueueLlmJobs}`;
      const result = await apiRequest<MemoryBackfillResponse>(config, "/plugins/memory/backfill", {
        auth: true,
        init: {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": keyFor(intent),
          },
          body: JSON.stringify(body),
        },
      });
      await loadGraphAndStatus();
      setSyncOutput(formatJson({
        status: result.ok === false ? "completed_with_warning" : "success",
        message: result.ok === false
          ? "同步请求已完成，但后端返回 ok=false，请查看调试字段。"
          : "同步完成：历史消息已导入或去重；AI 抽取任务已按设置入队，任务处理完成后关系图才会更新。",
        target_date: targetDate,
        debug: safeBackfillDebug(result),
      }));
      clear(intent);
    } catch (err) {
      setSyncOutput(formatJson({
        status: "failed",
        message: "同步失败：请检查接口错误、群聊ID和日期。",
        error: err instanceof ApiError || err instanceof Error ? err.message : "history sync failed",
      }));
      throw err;
    } finally {
      setSyncing(false);
    }
  };

  const runDailyExtraction = async () => {
    const tenantId = config.tenantId.trim();
    const sessionId = requireSelectedGroup(config, verifiedGroupIds);
    const normalizedChannel = channel.trim();
    const normalizedSourceKey = sourceKey.trim();
    const missingFields = [
      !tenantId ? "tenant_id" : "",
      !targetDate ? "target_date" : "",
    ].filter(Boolean);

    if (missingFields.length) {
      setSyncOutput(formatJson({
        status: "validation_failed",
        message: `校验失败：缺少 ${missingFields.join(", ")}，未发送 AI 抽取请求。`,
        no_api_request_sent: true,
        missing_fields: missingFields,
      }));
      return;
    }

    setExtracting(true);
    setSyncOutput(formatJson({
      status: "submitting",
      message: "正在运行所选日期 AI 抽取任务；仅处理当前范围和日期的小批量任务。",
      scope: {
        tenant_id: tenantId,
        channel: normalizedChannel,
        source_key: normalizedSourceKey,
        session_id: sessionId,
        user_scope: optionalUserScopeLabel,
        date: targetDate,
      },
    }));
    try {
      const body = {
        tenant_id: tenantId,
        channel: normalizedChannel,
        source_key: normalizedSourceKey,
        session_id: sessionId,
        date: targetDate,
        batch_limit: extractionBatchSize,
        max_jobs: extractionMaxJobCount,
        continuous: extractionContinuous,
        time_budget_seconds: extractionTimeBudgetSeconds,
      };
      const intent = `relationship:daily:${tenantId}:${sessionId}:${targetDate}:${extractionBatchSize}:${extractionMaxJobCount}:${extractionContinuous}`;
      const result = await apiRequest<Awaited<ReturnType<typeof runGroupGraphDailyExtraction>>>(
        config,
        "/plugins/memory/group-graph/extract-daily",
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify(body),
          },
        },
      );
      await loadGraphAndStatus();
      setSyncOutput(formatJson({
        status: result.ok === false ? "completed_with_warning" : "success",
        message: result.more_remain
          ? "所选日期 AI 抽取已处理一批，仍有该日期任务待处理。"
          : "所选日期 AI 抽取任务已处理完成；关系图已刷新。",
        target_date: targetDate,
        summary: {
          ok: result.ok,
          status: result.status,
          result_status: result.result_status,
          skipped_reason: result.skipped_reason,
          controls: result.controls || {
            batch_limit: extractionBatchSize,
            max_jobs: extractionMaxJobCount,
            continuous: extractionContinuous,
            time_budget_seconds: extractionTimeBudgetSeconds,
          },
          counts: result.counts || {},
          jobs: {
            claimed: result.jobs?.claimed ?? 0,
            succeeded: result.jobs?.succeeded ?? 0,
            failed: result.jobs?.failed ?? 0,
            dead: result.jobs?.dead ?? 0,
            batches: result.jobs?.batches ?? 0,
          },
          job_counts_before: result.job_counts_before || {},
          job_counts_after: result.job_counts_after || {},
          more_remain: result.more_remain,
        },
        note: "调试输出仅保留状态和计数，不显示原始聊天内容。",
      }));
      clear(intent);
    } catch (err) {
      setSyncOutput(formatJson({
        status: "failed",
        message: "AI 关系抽取请求失败：请确认登录会话/权限、群聊和日期。",
        error: err instanceof ApiError || err instanceof Error ? err.message : "daily extraction failed",
      }));
      throw err;
    } finally {
      setExtracting(false);
    }
  };

  const runWindowExtraction = async () => {
    const tenantId = config.tenantId.trim();
    const sessionId = requireSelectedGroup(config, verifiedGroupIds);
    const normalizedChannel = channel.trim();
    const normalizedSourceKey = sourceKey.trim();
    const missingFields = [
      !tenantId ? "tenant_id" : "",
      !targetDate ? "target_date" : "",
    ].filter(Boolean);

    if (missingFields.length) {
      setSyncOutput(formatJson({
        status: "validation_failed",
        message: `校验失败：缺少 ${missingFields.join(", ")}，未发送窗口关系抽取请求。`,
        no_api_request_sent: true,
        missing_fields: missingFields,
      }));
      return;
    }

    setExtracting(true);
    setSyncOutput(formatJson({
      status: "submitting",
      message: "正在运行窗口关系抽取；调试输出只显示窗口和候选关系计数。",
      scope: {
        tenant_id: tenantId,
        channel: normalizedChannel,
        source_key: normalizedSourceKey,
        session_id: sessionId,
        user_scope: optionalUserScopeLabel,
        date: targetDate,
      },
      controls: {
        window_size: windowExtractionSizeValue,
        max_windows: windowExtractionMaxWindowsValue,
        cursor_event_id: windowExtractionCursor,
        dry_run: windowExtractionDryRun,
      },
    }));
    try {
      const body = {
        tenant_id: tenantId,
        channel: normalizedChannel,
        source_key: normalizedSourceKey,
        session_id: sessionId,
        date: targetDate,
        window_size: windowExtractionSizeValue,
        max_windows: windowExtractionMaxWindowsValue,
        cursor_event_id: windowExtractionCursor,
        dry_run: windowExtractionDryRun,
      };
      const intent = `relationship:window:${tenantId}:${sessionId}:${targetDate}:${windowExtractionSizeValue}:${windowExtractionMaxWindowsValue}:${windowExtractionCursor}:${windowExtractionDryRun}`;
      const result = await apiRequest<Awaited<ReturnType<typeof runGroupGraphWindowExtraction>>>(
        config,
        "/plugins/memory/group-graph/extract-window",
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify(body),
          },
        },
      );
      if (typeof result.next_cursor_event_id === "number") {
        setWindowExtractionCursor(result.next_cursor_event_id);
      }
      await loadGraphAndStatus();
      setSyncOutput(formatJson({
        status: result.ok === false ? "completed_with_warning" : "success",
        message: result.more_remain
          ? "窗口关系抽取已处理一段消息，后续还有事件可继续处理。"
          : "窗口关系抽取已完成当前范围；关系图已刷新。",
        target_date: targetDate,
        summary: {
          ok: result.ok,
          status: result.status,
          skipped_reason: result.skipped_reason,
          controls: result.controls || {
            window_size: windowExtractionSizeValue,
            max_windows: windowExtractionMaxWindowsValue,
            cursor_event_id: windowExtractionCursor,
            dry_run: windowExtractionDryRun,
          },
          windows: result.windows || [],
          totals: result.totals || {},
          next_cursor_event_id: result.next_cursor_event_id ?? windowExtractionCursor,
          more_remain: result.more_remain,
          generated_from: result.generated_from || [],
        },
        note: "调试输出不包含原始聊天文本。",
      }));
      clear(intent);
    } catch (err) {
      setSyncOutput(formatJson({
        status: "failed",
        message: "窗口关系抽取请求失败：请确认登录会话/权限、群聊和日期。",
        error: err instanceof ApiError || err instanceof Error ? err.message : "window extraction failed",
      }));
      throw err;
    } finally {
      setExtracting(false);
    }
  };

  const runWindowCatchup = async () => {
    const tenantId = config.tenantId.trim();
    const sessionId = requireSelectedGroup(config, verifiedGroupIds);
    const normalizedChannel = channel.trim();
    const normalizedSourceKey = sourceKey.trim();
    const missingFields = [
      !tenantId ? "tenant_id" : "",
      !targetDate ? "target_date" : "",
    ].filter(Boolean);

    if (missingFields.length) {
      setSyncOutput(formatJson({
        status: "validation_failed",
        message: `校验失败：缺少 ${missingFields.join(", ")}，未发送连续窗口追平请求。`,
        no_api_request_sent: true,
        missing_fields: missingFields,
      }));
      return;
    }

    setExtracting(true);
    setSyncOutput(formatJson({
      status: "submitting",
      message: "正在连续追平窗口关系抽取；调试输出只显示游标和计数。",
      scope: {
        tenant_id: tenantId,
        channel: normalizedChannel,
        source_key: normalizedSourceKey,
        session_id: sessionId,
        user_scope: optionalUserScopeLabel,
        date: targetDate,
      },
      controls: {
        window_size: windowExtractionSizeValue,
        max_windows_per_run: windowCatchupMaxWindowsValue,
        cursor_event_id: windowExtractionCursor,
        dry_run: windowExtractionDryRun,
        time_budget_seconds: extractionTimeBudgetSeconds,
      },
    }));
    try {
      const body = {
        tenant_id: tenantId,
        channel: normalizedChannel,
        source_key: normalizedSourceKey,
        session_id: sessionId,
        date: targetDate,
        window_size: windowExtractionSizeValue,
        max_windows_per_run: windowCatchupMaxWindowsValue,
        cursor_event_id: windowExtractionCursor,
        dry_run: windowExtractionDryRun,
        time_budget_seconds: extractionTimeBudgetSeconds,
      };
      const intent = `relationship:catchup:${tenantId}:${sessionId}:${targetDate}:${windowExtractionSizeValue}:${windowCatchupMaxWindowsValue}:${windowExtractionCursor}:${windowExtractionDryRun}`;
      const result = await apiRequest<Awaited<ReturnType<typeof runGroupGraphWindowCatchup>>>(
        config,
        "/plugins/memory/group-graph/extract-window-catchup",
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify(body),
          },
        },
      );
      if (typeof result.next_cursor_event_id === "number") {
        setWindowExtractionCursor(result.next_cursor_event_id);
      }
      let refreshedWindowStats: GroupGraphWindowStatsResponse | null = null;
      try {
        refreshedWindowStats = await getGroupGraphWindowStats(config, {
          tenant_id: tenantId,
          channel: normalizedChannel,
          source_key: normalizedSourceKey,
          session_id: sessionId,
          date: targetDate,
        });
        setWindowStats(refreshedWindowStats);
        setWindowStatsStatus("窗口关系统计已加载；仅显示计数。");
      } catch {
        refreshedWindowStats = null;
      }
      await loadGraphAndStatus();
      setSyncOutput(formatJson({
        status: result.ok === false ? "completed_with_warning" : "success",
        message: result.more_remain
          ? "连续窗口追平已达到本次停止条件，后续还有事件可继续处理。"
          : "连续窗口追平已完成当前范围；关系图和统计已刷新。",
        target_date: targetDate,
        summary: {
          ok: result.ok,
          status: result.status,
          stop_reason: result.stop_reason,
          controls: result.controls || {},
          totals: result.totals || {},
          windows_processed: result.windows_processed ?? 0,
          next_cursor_event_id: result.next_cursor_event_id ?? windowExtractionCursor,
          more_remain: result.more_remain,
          window_stats: {
            totals: refreshedWindowStats?.totals || {},
            acceptance_counts: refreshedWindowStats?.acceptance_counts || {},
          },
        },
        note: "调试输出不包含原始聊天文本。",
      }));
      clear(intent);
    } catch (err) {
      setSyncOutput(formatJson({
        status: "failed",
        message: "连续窗口追平请求失败：请确认登录会话/权限、群聊和日期。",
        error: err instanceof ApiError || err instanceof Error ? err.message : "window catchup failed",
      }));
      throw err;
    } finally {
      setExtracting(false);
    }
  };

  useEffect(() => {
    if (!selectedEdge) {
      setEvidence(null);
      setEvidenceStatus(selection ? "当前选择是节点；请选择关系查看证据来源。" : "选择一条关系后会自动加载证据来源。");
      return;
    }
    void loadEdgeEvidence(selectedEdge);
  }, [loadEdgeEvidence, selectedEdge?.id, selection]);

  useEffect(() => {
    void Promise.all([loadDateStatuses(), loadJobStats()]);
  }, [loadDateStatuses, loadJobStats]);

  useEffect(() => {
    if (autoLoadedGroupScopeKeyRef.current === autoLoadGroupScopeKey) return;
    autoLoadedGroupScopeKeyRef.current = autoLoadGroupScopeKey;
    graphRequestIdRef.current += 1;
    setLoadedGraph(null);
    setLoadedGraphScopeKey("");
    setSelection(null);
    setEvidence(null);
    setDateRows([]);
    setJobStats(null);
    setWindowStats(null);
    setWindowExtractionCursor(0);
    if (!selectedGroupIsVerified) {
      setLoading(false);
      setOutput(formatJson({ message: "请先从页面上方的已验证群聊列表选择目标群" }));
      return;
    }
    setOutput(formatJson({
      status: "auto_loading",
      message: "已切换到当前已验证群聊，正在自动加载关系图。",
      session_id: selectedGroupId,
    }));
    void loadGraphRef.current();
  }, [autoLoadGroupScopeKey, selectedGroupId, selectedGroupIsVerified]);

  return {
    config,
    selectedGroupId,
    selectedGroupIsVerified,
    channel,
    setChannel,
    sourceKey,
    setSourceKey,
    acceptanceStatus,
    setAcceptanceStatus,
    nodeType,
    setNodeType,
    edgeType,
    setEdgeType,
    minConfidence,
    setMinConfidence,
    limit,
    setLimit,
    fromDate,
    setFromDate,
    toDate,
    setToDate,
    nodeSearch,
    setNodeSearch,
    edgeSearch,
    setEdgeSearch,
    graphViewMode,
    setGraphViewMode,
    targetDate,
    setTargetDate,
    dateRows,
    dateLoading,
    enqueueLlmJobs,
    setEnqueueLlmJobs,
    extractionBatchLimit,
    setExtractionBatchLimit,
    extractionContinuous,
    setExtractionContinuous,
    extractionMaxJobs,
    setExtractionMaxJobs,
    windowExtractionSize,
    setWindowExtractionSize,
    windowExtractionMaxWindows,
    setWindowExtractionMaxWindows,
    windowExtractionDryRun,
    setWindowExtractionDryRun,
    windowExtractionCursor,
    windowCatchupMaxWindows,
    setWindowCatchupMaxWindows,
    actionPanelOpen,
    setActionPanelOpen,
    graph,
    selection,
    setSelection,
    loading,
    syncing,
    extracting,
    evidence,
    evidenceLoading,
    evidenceStatus,
    output,
    syncOutput,
    jobStatsLoading,
    jobStatsStatus,
    windowStatsStatus,
    selectedNode,
    selectedEdge,
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
    extractionMaxJobCount,
    windowExtractionMaxWindowsValue,
    windowCatchupMaxWindowsValue,
    estimatedExtractionClicks,
    historyNextStep,
    windowStatsTotals,
    windowStatsAcceptance,
    neighborNodeIds,
    graphStateMessage,
    loadGraph,
    loadEdgeEvidence,
    loadGraphAndStatus,
    showAllGraph,
    runHistorySync,
    runDailyExtraction,
    runWindowExtraction,
    runWindowCatchup,
  };
}

export type RelationshipGraphController = ReturnType<typeof useRelationshipGraphController>;

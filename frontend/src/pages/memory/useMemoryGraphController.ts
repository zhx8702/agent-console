import { useCallback, useId, useMemo } from "react";

import { formatJson } from "../../lib/api";
import {
  type GraphQualityKey,
  type MemoryGraphEmptyContext,
  type MemoryGraphEntity,
  type MemoryGraphEpisode,
  type MemoryGraphFact,
  type MemoryGraphMode,
  type MemoryGraphReviewEntry,
  type MemoryGraphSelectionKind,
  GRAPH_ENTITY_TYPE_LABELS,
  GRAPH_PREDICATE_LABELS,
  allCounts,
  copyToClipboard,
  countDefinedIds,
  getGraphQualityKeys,
  graphEntityHasEvidence,
  graphEntityLabel,
  graphEpisodeHasEvidence,
  graphEpisodeLabel,
  graphFactHasEvidence,
  graphFactObject,
  graphFactSubject,
  graphHumanLabel,
  graphItemId,
  graphQualityReportItems,
  graphReviewItemLabel,
  graphTabForKind,
  hasDefinedId,
  matchesGraphSearch,
  safeMemoryGraphSelectionPayload,
  sortGraphEntities,
  sortGraphEpisodes,
  sortGraphFacts,
} from "./model";
import { useMemoryGraphData } from "./useMemoryGraphData";

interface UseMemoryGraphControllerOptions {
  sessionId: string;
  channel: string;
  sourceKey: string;
  userId: string;
  limit: number;
  selectedSessionIsGroup: boolean;
  selectedMemberIsVerified: boolean;
  onOutput: (value: string) => void;
}

export function useMemoryGraphController({
  sessionId,
  channel,
  sourceKey,
  userId,
  limit,
  selectedSessionIsGroup,
  selectedMemberIsVerified,
  onOutput: setMemoryGraphOutput,
}: UseMemoryGraphControllerOptions) {
  const memoryGraphModeTabsId = useId();
  const memoryGraphDataTabsId = useId();
  const data = useMemoryGraphData({
    sessionId,
    channel,
    sourceKey,
    userId,
    limit,
    selectedSessionIsGroup,
    selectedMemberIsVerified,
    onOutput: setMemoryGraphOutput,
  });
  const {
    tenantId,
    memoryGraphEntities,
    memoryGraphFacts,
    memoryGraphEpisodes,
    memoryGraphPreview,
    memoryGraphStatusFilter,
    memoryGraphSearch,
    memoryGraphEntityTypeFilter,
    memoryGraphPredicateFilter,
    memoryGraphConfidenceMin,
    memoryGraphEvidenceOnly,
    memoryGraphHideReviewed,
    memoryGraphReviewedIds,
    setMemoryGraphReviewedIds,
    memoryGraphSort,
    setMemoryGraphMode,
    memoryGraphTab,
    setMemoryGraphTab,
    memoryGraphSelection,
    setMemoryGraphSelection,
  } = data;

  const memoryGraphCounts = memoryGraphPreview?.counts || {
    entities: memoryGraphEntities.length,
    facts: memoryGraphFacts.length,
    episodes: memoryGraphEpisodes.length,
  };
  const memoryGraphLoadedCounts = {
    entities: memoryGraphEntities.length,
    facts: memoryGraphFacts.length,
    episodes: memoryGraphEpisodes.length,
  };
  const memoryGraphConfidenceThreshold = useMemo(() => {
    if (!memoryGraphConfidenceMin.trim()) {
      return null;
    }
    const parsed = Number(memoryGraphConfidenceMin);
    if (Number.isNaN(parsed)) {
      return null;
    }
    return Math.min(1, Math.max(0, parsed));
  }, [memoryGraphConfidenceMin]);
  const graphEntityHasEvidenceFields = useMemo(
    () => memoryGraphEntities.some((item) =>
      "memory_item_id" in item ||
      "source_event_id" in item ||
      "memory_item_ids" in item ||
      "source_event_ids" in item ||
      "event_ids" in item,
    ),
    [memoryGraphEntities],
  );
  const filteredMemoryGraphEntities = useMemo(
    () =>
      sortGraphEntities(
        memoryGraphEntities.filter((item) => {
          if (memoryGraphEntityTypeFilter && (item.entity_type || "unknown") !== memoryGraphEntityTypeFilter) {
            return false;
          }
          if (memoryGraphConfidenceThreshold !== null && Number(item.confidence ?? -1) < memoryGraphConfidenceThreshold) {
            return false;
          }
          if (memoryGraphEvidenceOnly && graphEntityHasEvidenceFields && !graphEntityHasEvidence(item)) {
            return false;
          }
          return matchesGraphSearch(
            [
              item.id,
              item.entity_type,
              graphHumanLabel(item.entity_type, GRAPH_ENTITY_TYPE_LABELS),
              item.name,
              item.normalized_name,
              ...(item.aliases || []),
              item.memory_item_id,
              item.source_event_id,
              ...(item.memory_item_ids || []),
              ...(item.source_event_ids || []),
              ...(item.event_ids || []),
              item.status,
            ],
            memoryGraphSearch,
          );
        }),
        memoryGraphSort,
      ),
    [
      graphEntityHasEvidenceFields,
      memoryGraphConfidenceThreshold,
      memoryGraphEntities,
      memoryGraphEntityTypeFilter,
      memoryGraphEvidenceOnly,
      memoryGraphSearch,
      memoryGraphSort,
    ],
  );
  const filteredMemoryGraphFacts = useMemo(
    () =>
      sortGraphFacts(
        memoryGraphFacts.filter((item) => {
          if (memoryGraphPredicateFilter && (item.predicate || "unknown") !== memoryGraphPredicateFilter) {
            return false;
          }
          if (memoryGraphConfidenceThreshold !== null && Number(item.confidence ?? -1) < memoryGraphConfidenceThreshold) {
            return false;
          }
          if (memoryGraphEvidenceOnly && !graphFactHasEvidence(item)) {
            return false;
          }
          return matchesGraphSearch(
            [
              item.id,
              item.subject_name,
              item.subject_entity_id,
              item.predicate,
              graphHumanLabel(item.predicate, GRAPH_PREDICATE_LABELS),
              item.object_name,
              item.object_entity_id,
              item.object_value,
              item.memory_item_id,
              item.source_event_id,
              item.status,
            ],
            memoryGraphSearch,
          );
        }),
        memoryGraphSort,
      ),
    [
      memoryGraphConfidenceThreshold,
      memoryGraphEvidenceOnly,
      memoryGraphFacts,
      memoryGraphPredicateFilter,
      memoryGraphSearch,
      memoryGraphSort,
    ],
  );
  const filteredMemoryGraphEpisodes = useMemo(
    () =>
      sortGraphEpisodes(
        memoryGraphEpisodes.filter((item) => {
          if (memoryGraphConfidenceThreshold !== null && Number(item.importance ?? -1) < memoryGraphConfidenceThreshold) {
            return false;
          }
          if (memoryGraphEvidenceOnly && !graphEpisodeHasEvidence(item)) {
            return false;
          }
          return matchesGraphSearch(
            [
              item.id,
              item.title,
              item.session_id,
              item.status,
              ...(item.event_ids || []),
              ...(item.memory_item_ids || []),
            ],
            memoryGraphSearch,
          );
        }),
        memoryGraphSort,
      ),
    [
      memoryGraphConfidenceThreshold,
      memoryGraphEpisodes,
      memoryGraphEvidenceOnly,
      memoryGraphSearch,
      memoryGraphSort,
    ],
  );
  const graphEntityTypeFilterCounts = useMemo(
    () => allCounts(memoryGraphEntities.map((item) => item.entity_type)),
    [memoryGraphEntities],
  );
  const graphPredicateFilterCounts = useMemo(
    () => allCounts(memoryGraphFacts.map((item) => item.predicate)),
    [memoryGraphFacts],
  );
  const graphEvidenceCounts = useMemo(
    () => {
      const factMemoryItems = filteredMemoryGraphFacts.filter((item) => item.memory_item_id !== null && item.memory_item_id !== undefined).length;
      const factSourceEvents = filteredMemoryGraphFacts.filter((item) => item.source_event_id !== null && item.source_event_id !== undefined).length;
      const episodeMemoryItems = filteredMemoryGraphEpisodes.reduce((sum, item) => sum + countDefinedIds(item.memory_item_ids), 0);
      const episodeSourceEvents = filteredMemoryGraphEpisodes.reduce((sum, item) => sum + countDefinedIds(item.event_ids), 0);
      return {
        memoryItems: factMemoryItems + episodeMemoryItems,
        sourceEvents: factSourceEvents + episodeSourceEvents,
      };
    },
    [filteredMemoryGraphEpisodes, filteredMemoryGraphFacts],
  );
  const memoryGraphVisibleCounts = {
    entities: filteredMemoryGraphEntities.length,
    facts: filteredMemoryGraphFacts.length,
    episodes: filteredMemoryGraphEpisodes.length,
  };
  const visibleMemoryGraphTotal = memoryGraphVisibleCounts.entities + memoryGraphVisibleCounts.facts + memoryGraphVisibleCounts.episodes;
  const loadedMemoryGraphTotal = memoryGraphLoadedCounts.entities + memoryGraphLoadedCounts.facts + memoryGraphLoadedCounts.episodes;
  const hiddenMemoryGraphTotal = Math.max(0, loadedMemoryGraphTotal - visibleMemoryGraphTotal);
  const memoryGraphScopeFields = [
    { key: "tenant_id", label: "租户", value: tenantId },
    { key: "channel", label: "渠道", value: channel.trim() },
    { key: "source_key", label: "来源键", value: sourceKey.trim() },
    { key: "user_id", label: "用户 ID", value: userId.trim() },
    { key: "session_id", label: "会话 ID", value: sessionId.trim() },
    { key: "status", label: "状态", value: memoryGraphStatusFilter || "全部" },
  ];
  const memoryGraphEntityById = useMemo(
    () => new Map(memoryGraphEntities.map((item) => [item.id, item])),
    [memoryGraphEntities],
  );
  const visibleGraphQualityItems = useMemo(
    () => graphQualityReportItems(filteredMemoryGraphEntities, filteredMemoryGraphFacts, filteredMemoryGraphEpisodes),
    [filteredMemoryGraphEntities, filteredMemoryGraphEpisodes, filteredMemoryGraphFacts],
  );
  const visibleGraphReviewGroups = useMemo(() => {
    const now = Date.now();
    const groups: Record<GraphQualityKey, MemoryGraphReviewEntry[]> = {
      low_confidence: [],
      no_evidence: [],
      stale: [],
      inactive_status: [],
    };
    for (const entry of visibleGraphQualityItems) {
      const key = graphItemId(entry.kind, entry.item.id);
      if (memoryGraphHideReviewed && memoryGraphReviewedIds.has(key)) {
        continue;
      }
      const label = graphReviewItemLabel(entry.kind, entry.item);
      for (const flag of getGraphQualityKeys(entry.kind, entry.item, now)) {
        groups[flag].push({
          key,
          kind: entry.kind,
          item: entry.item,
          label,
        });
      }
    }
    return groups;
  }, [memoryGraphHideReviewed, memoryGraphReviewedIds, visibleGraphQualityItems]);
  const visibleGraphQualitySummary = useMemo(() => {
    const now = Date.now();
    const ids: Record<GraphQualityKey, string[]> = {
      low_confidence: [],
      no_evidence: [],
      stale: [],
      inactive_status: [],
    };
    for (const entry of visibleGraphQualityItems) {
      const itemId = graphItemId(entry.kind, entry.item.id);
      for (const flag of getGraphQualityKeys(entry.kind, entry.item, now)) {
        ids[flag].push(itemId);
      }
    }
    return {
      lowConfidence: ids.low_confidence.length,
      noEvidence: ids.no_evidence.length,
      stale: ids.stale.length,
      inactiveStatus: ids.inactive_status.length,
      ids,
    };
  }, [visibleGraphQualityItems]);
  const visibleGraphReviewSummary = useMemo(() => ({
    lowConfidence: visibleGraphReviewGroups.low_confidence.length,
    noEvidence: visibleGraphReviewGroups.no_evidence.length,
    stale: visibleGraphReviewGroups.stale.length,
    inactiveStatus: visibleGraphReviewGroups.inactive_status.length,
    ids: {
      low_confidence: visibleGraphReviewGroups.low_confidence.map((item) => item.key),
      no_evidence: visibleGraphReviewGroups.no_evidence.map((item) => item.key),
      stale: visibleGraphReviewGroups.stale.map((item) => item.key),
      inactive_status: visibleGraphReviewGroups.inactive_status.map((item) => item.key),
    },
  }), [visibleGraphReviewGroups]);
  const selectMemoryGraphItem = useCallback((
    kind: MemoryGraphSelectionKind,
    item: MemoryGraphEntity | MemoryGraphFact | MemoryGraphEpisode,
    mode: MemoryGraphMode = "explore",
  ) => {
    setMemoryGraphTab(graphTabForKind(kind));
    setMemoryGraphMode(mode);
    setMemoryGraphSelection(
      kind === "entity"
        ? { kind, item: item as MemoryGraphEntity }
        : kind === "fact"
          ? { kind, item: item as MemoryGraphFact }
          : { kind, item: item as MemoryGraphEpisode },
    );
  }, []);
  const markMemoryGraphReviewed = useCallback((kind: MemoryGraphSelectionKind, id: number) => {
    const key = graphItemId(kind, id);
    setMemoryGraphReviewedIds((current) => {
      const next = new Set(current);
      next.add(key);
      return next;
    });
  }, []);
  const selectedMemoryGraphReviewKey = memoryGraphSelection
    ? graphItemId(memoryGraphSelection.kind, memoryGraphSelection.item.id)
    : "";
  const selectedMemoryGraphIsReviewed = selectedMemoryGraphReviewKey
    ? memoryGraphReviewedIds.has(selectedMemoryGraphReviewKey)
    : false;
  const visibleReviewedGraphTotal = useMemo(
    () => visibleGraphQualityItems.filter((entry) => memoryGraphReviewedIds.has(graphItemId(entry.kind, entry.item.id))).length,
    [memoryGraphReviewedIds, visibleGraphQualityItems],
  );
  const copyVisibleGraphQualityReport = useCallback(() => {
    copyToClipboard(formatJson({
      scope: Object.fromEntries(memoryGraphScopeFields.map((field) => [field.key, field.value || null])),
      filters: {
        search: memoryGraphSearch.trim() || null,
        entity_type: memoryGraphEntityTypeFilter || null,
        predicate: memoryGraphPredicateFilter || null,
        min_confidence: memoryGraphConfidenceThreshold,
        evidence_only: memoryGraphEvidenceOnly,
        sort: memoryGraphSort,
      },
      local_only_note: "本地复核 ID 只保存在当前浏览器会话中；此报告不会修改记忆数据。",
      hide_locally_reviewed: memoryGraphHideReviewed,
      locally_reviewed_visible_ids: visibleGraphQualityItems
        .map((entry) => graphItemId(entry.kind, entry.item.id))
        .filter((id) => memoryGraphReviewedIds.has(id)),
      visible_counts: memoryGraphVisibleCounts,
      loaded_counts: memoryGraphLoadedCounts,
      hidden_by_filters: hiddenMemoryGraphTotal,
      review_queue: {
        low_confidence: {
          count: visibleGraphReviewSummary.lowConfidence,
          ids: visibleGraphReviewSummary.ids.low_confidence,
        },
        no_evidence: {
          count: visibleGraphReviewSummary.noEvidence,
          ids: visibleGraphReviewSummary.ids.no_evidence,
        },
        stale: {
          count: visibleGraphReviewSummary.stale,
          ids: visibleGraphReviewSummary.ids.stale,
        },
        invalidated_deleted: {
          count: visibleGraphReviewSummary.inactiveStatus,
          ids: visibleGraphReviewSummary.ids.inactive_status,
        },
      },
    }));
  }, [
    hiddenMemoryGraphTotal,
    memoryGraphConfidenceThreshold,
    memoryGraphEntityTypeFilter,
    memoryGraphEvidenceOnly,
    memoryGraphHideReviewed,
    memoryGraphLoadedCounts,
    memoryGraphPredicateFilter,
    memoryGraphReviewedIds,
    memoryGraphScopeFields,
    memoryGraphSearch,
    memoryGraphSort,
    memoryGraphVisibleCounts,
    visibleGraphQualityItems,
    visibleGraphReviewSummary,
  ]);
  const missingMemoryGraphScope = [
    !channel.trim() ? "渠道" : "",
    !sourceKey.trim() ? "来源键" : "",
    !userId.trim() ? "用户 ID" : "",
  ].filter(Boolean);
  const isMemoryGraphScopeComplete = missingMemoryGraphScope.length === 0;
  const hasNoMemoryGraphData =
    isMemoryGraphScopeComplete &&
    (memoryGraphCounts.entities ?? 0) === 0 &&
    (memoryGraphCounts.facts ?? 0) === 0 &&
    (memoryGraphCounts.episodes ?? 0) === 0;
  const hasActiveMemoryGraphFilters = Boolean(
    memoryGraphSearch.trim() ||
    memoryGraphEntityTypeFilter ||
    memoryGraphPredicateFilter ||
    memoryGraphConfidenceThreshold !== null ||
    memoryGraphEvidenceOnly ||
    memoryGraphSort !== "updated_desc",
  );
  const hasVisibleGraphReviewItems = Object.values(visibleGraphReviewSummary.ids).some((ids) => ids.length > 0);
  const hasHiddenLocalReviewedGraphItems =
    memoryGraphHideReviewed &&
    Object.values(visibleGraphQualitySummary.ids).some((ids) =>
      ids.some((id) => memoryGraphReviewedIds.has(id)),
    );
  const memoryGraphStateLabel = (() => {
    if (!isMemoryGraphScopeComplete) {
      return "查询范围未完成";
    }
    if (!loadedMemoryGraphTotal) {
      return "尚未读到图谱数据";
    }
    if (!visibleMemoryGraphTotal) {
      return "数据已加载但当前不可见";
    }
    if (!memoryGraphSelection) {
      return "已加载，等待选择";
    }
    return "已选择，可查看检查面板";
  })();
  const memoryGraphNextAction = (() => {
    if (!isMemoryGraphScopeComplete) {
      return `先补全 ${missingMemoryGraphScope.join(" / ")}，通常是先选群成员。`;
    }
    if (!loadedMemoryGraphTotal) {
      return "选择群成员后，重置当前群筛选，再刷新图谱。";
    }
    if (!visibleMemoryGraphTotal) {
      if (hasActiveMemoryGraphFilters) {
        return "当前筛选后没有结果，请先清空筛选或搜索词。";
      }
      if (hasHiddenLocalReviewedGraphItems) {
        return "本地复核标记隐藏了待查看项，请先清空本地复核标记。";
      }
      return "数据已加载但没有可见项，请重置当前群筛选后再刷新图谱。";
    }
    if (!memoryGraphSelection) {
      return "打开概览，点击最近项目、表格行或复核队列条目。";
    }
    return "在检查面板查看邻域和证据，必要时复制路径。";
  })();
  const memoryGraphGuideSteps = [
    {
      label: "选群成员",
      detail: isMemoryGraphScopeComplete
        ? "查询范围已完整。"
        : `缺少 ${missingMemoryGraphScope.join(" / ")}。`,
      done: isMemoryGraphScopeComplete,
      active: !isMemoryGraphScopeComplete,
    },
    {
      label: "重置筛选",
      detail: "只清空当前已验证群成员范围内的状态、搜索和本地过滤，不改变群聊或成员。",
      done: isMemoryGraphScopeComplete && !memoryGraphStatusFilter && !hasActiveMemoryGraphFilters,
      active: isMemoryGraphScopeComplete && (!loadedMemoryGraphTotal || !visibleMemoryGraphTotal) && Boolean(memoryGraphStatusFilter || hasActiveMemoryGraphFilters),
    },
    {
      label: "刷新图谱",
      detail: loadedMemoryGraphTotal
        ? `已加载 ${loadedMemoryGraphTotal} 项，当前可见 ${visibleMemoryGraphTotal} 项。`
        : "读取当前租户、渠道、来源和用户范围内的图谱。",
      done: loadedMemoryGraphTotal > 0,
      active: isMemoryGraphScopeComplete && loadedMemoryGraphTotal === 0,
    },
    {
      label: "查看概览",
      detail: visibleMemoryGraphTotal
        ? "从最近实体、关系和事件片段中快速打开详情。"
        : "无可见项时先处理上方提示。",
      done: visibleMemoryGraphTotal > 0,
      active: loadedMemoryGraphTotal > 0 && visibleMemoryGraphTotal === 0,
    },
    {
      label: "选择项目",
      detail: memoryGraphSelection ? `已选择 ${memoryGraphSelection.kind} #${memoryGraphSelection.item.id}。` : "点击概览项目、表格行或复核队列条目。",
      done: Boolean(memoryGraphSelection),
      active: visibleMemoryGraphTotal > 0 && !memoryGraphSelection,
    },
    {
      label: "检查详情",
      detail: memoryGraphSelection ? "查看邻域和证据，必要时复制路径。" : "右侧面板会显示邻居、证据和复制操作。",
      done: Boolean(memoryGraphSelection),
      active: Boolean(memoryGraphSelection),
    },
  ];
  const memoryGraphSamples = {
    entities: filteredMemoryGraphEntities.slice(0, 3),
    facts: filteredMemoryGraphFacts.slice(0, 3),
    episodes: filteredMemoryGraphEpisodes.slice(0, 3),
  };
  const hasMemoryGraphSearch = Boolean(memoryGraphSearch.trim());
  const currentVisibleGraphIds = useMemo(() => {
    if (memoryGraphTab === "entities") {
      return filteredMemoryGraphEntities.map((item) => item.id);
    }
    if (memoryGraphTab === "facts") {
      return filteredMemoryGraphFacts.map((item) => item.id);
    }
    if (memoryGraphTab === "episodes") {
      return filteredMemoryGraphEpisodes.map((item) => item.id);
    }
    return [
      ...memoryGraphSamples.entities.map((item) => `entity:${item.id}`),
      ...memoryGraphSamples.facts.map((item) => `fact:${item.id}`),
      ...memoryGraphSamples.episodes.map((item) => `episode:${item.id}`),
    ];
  }, [
    filteredMemoryGraphEntities,
    filteredMemoryGraphEpisodes,
    filteredMemoryGraphFacts,
    memoryGraphSamples.entities,
    memoryGraphSamples.episodes,
    memoryGraphSamples.facts,
    memoryGraphTab,
  ]);
  const currentGraphTabVisibleCount =
    memoryGraphTab === "entities"
      ? filteredMemoryGraphEntities.length
      : memoryGraphTab === "facts"
        ? filteredMemoryGraphFacts.length
        : memoryGraphTab === "episodes"
          ? filteredMemoryGraphEpisodes.length
          : visibleMemoryGraphTotal;
  const currentGraphTabLoadedCount =
    memoryGraphTab === "entities"
      ? memoryGraphLoadedCounts.entities
      : memoryGraphTab === "facts"
        ? memoryGraphLoadedCounts.facts
        : memoryGraphTab === "episodes"
          ? memoryGraphLoadedCounts.episodes
          : loadedMemoryGraphTotal;
  const selectedGraphItemIsVisible = useMemo(() => {
    if (!memoryGraphSelection) {
      return true;
    }
    if (memoryGraphSelection.kind === "entity") {
      return filteredMemoryGraphEntities.some((item) => item.id === memoryGraphSelection.item.id);
    }
    if (memoryGraphSelection.kind === "fact") {
      return filteredMemoryGraphFacts.some((item) => item.id === memoryGraphSelection.item.id);
    }
    return filteredMemoryGraphEpisodes.some((item) => item.id === memoryGraphSelection.item.id);
  }, [filteredMemoryGraphEntities, filteredMemoryGraphEpisodes, filteredMemoryGraphFacts, memoryGraphSelection]);
  const selectedGraphEntityRelatedFacts = useMemo(() => {
    if (memoryGraphSelection?.kind !== "entity") {
      return [];
    }
    const entityId = memoryGraphSelection.item.id;
    const selectedEntityTerms = [
      memoryGraphSelection.item.name,
      memoryGraphSelection.item.normalized_name,
      ...(memoryGraphSelection.item.aliases || []),
    ]
      .map((value) => String(value || "").trim().toLowerCase())
      .filter((value) => value.length >= 2);
    const mentionsSelectedEntity = (value?: string) => {
      const normalized = String(value || "").trim().toLowerCase();
      return Boolean(normalized && selectedEntityTerms.some((term) => normalized.includes(term)));
    };
    return memoryGraphFacts.filter((item) =>
      item.subject_entity_id === entityId ||
      item.object_entity_id === entityId ||
      mentionsSelectedEntity(item.object_value) ||
      mentionsSelectedEntity(item.object_name) ||
      mentionsSelectedEntity(item.subject_name),
    );
  }, [memoryGraphFacts, memoryGraphSelection]);
  const selectedGraphEntityNeighborhood = useMemo(() => {
    if (memoryGraphSelection?.kind !== "entity") {
      return {
        subject: [] as MemoryGraphFact[],
        object: [] as MemoryGraphFact[],
        valueMention: [] as MemoryGraphFact[],
        relatedEntities: [] as MemoryGraphEntity[],
      };
    }
    const entityId = memoryGraphSelection.item.id;
    const subject = selectedGraphEntityRelatedFacts.filter((item) => item.subject_entity_id === entityId);
    const object = selectedGraphEntityRelatedFacts.filter((item) => item.object_entity_id === entityId);
    const directIds = new Set([...subject, ...object].map((item) => item.id));
    const valueMention = selectedGraphEntityRelatedFacts.filter((item) => !directIds.has(item.id));
    const relatedIds = new Set<number>();
    for (const fact of selectedGraphEntityRelatedFacts) {
      if (fact.subject_entity_id && fact.subject_entity_id !== entityId) {
        relatedIds.add(fact.subject_entity_id);
      }
      if (fact.object_entity_id && fact.object_entity_id !== entityId) {
        relatedIds.add(fact.object_entity_id);
      }
    }
    return {
      subject,
      object,
      valueMention,
      relatedEntities: Array.from(relatedIds)
        .map((id) => memoryGraphEntityById.get(id))
        .filter((item): item is MemoryGraphEntity => Boolean(item)),
    };
  }, [memoryGraphEntityById, memoryGraphSelection, selectedGraphEntityRelatedFacts]);
  const selectedGraphFactSubjectEntity =
    memoryGraphSelection?.kind === "fact" && memoryGraphSelection.item.subject_entity_id
      ? memoryGraphEntityById.get(memoryGraphSelection.item.subject_entity_id) || null
      : null;
  const selectedGraphFactObjectEntity =
    memoryGraphSelection?.kind === "fact" && memoryGraphSelection.item.object_entity_id
      ? memoryGraphEntityById.get(memoryGraphSelection.item.object_entity_id) || null
      : null;
  const copySelectedMemoryGraphPath = useCallback(() => {
    if (!memoryGraphSelection) {
      return;
    }
    if (memoryGraphSelection.kind === "fact") {
      const item = memoryGraphSelection.item;
      copyToClipboard([
        `Fact #${item.id}`,
        `${graphFactSubject(item)}${item.subject_entity_id ? ` (entity:${item.subject_entity_id})` : ""}`,
        `-> ${graphHumanLabel(item.predicate, GRAPH_PREDICATE_LABELS)} (${item.predicate || "-"})`,
        `-> ${graphFactObject(item)}${item.object_entity_id ? ` (entity:${item.object_entity_id})` : ""}`,
        `memory_item_id: ${item.memory_item_id ?? "-"}`,
        `source_event_id: ${item.source_event_id ?? "-"}`,
      ].join("\n"));
      return;
    }
    if (memoryGraphSelection.kind === "entity") {
      const item = memoryGraphSelection.item;
      copyToClipboard([
        `Entity #${item.id}: ${graphEntityLabel(item)}`,
        `type: ${item.entity_type || "-"}`,
        `related_fact_ids: ${selectedGraphEntityRelatedFacts.map((fact) => fact.id).join(", ") || "-"}`,
        `related_entity_ids: ${selectedGraphEntityNeighborhood.relatedEntities.map((entity) => entity.id).join(", ") || "-"}`,
        `memory_item_ids: ${[item.memory_item_id, ...(item.memory_item_ids || [])].filter(hasDefinedId).join(", ") || "-"}`,
        `event_ids: ${[item.source_event_id, ...(item.source_event_ids || []), ...(item.event_ids || [])].filter(hasDefinedId).join(", ") || "-"}`,
      ].join("\n"));
      return;
    }
    const item = memoryGraphSelection.item;
    copyToClipboard([
      `Episode #${item.id}: ${graphEpisodeLabel(item)}`,
      `session_id: ${item.session_id || "-"}`,
      `event_ids: ${(item.event_ids || []).filter(hasDefinedId).join(", ") || "-"}`,
      `memory_item_ids: ${(item.memory_item_ids || []).filter(hasDefinedId).join(", ") || "-"}`,
    ].join("\n"));
  }, [memoryGraphSelection, selectedGraphEntityNeighborhood.relatedEntities, selectedGraphEntityRelatedFacts]);
  const copySelectedMemoryGraphJson = useCallback(() => {
    if (!memoryGraphSelection) {
      return;
    }
    copyToClipboard(formatJson(safeMemoryGraphSelectionPayload(memoryGraphSelection)));
  }, [memoryGraphSelection]);
  const memoryGraphEmptyHint = (() => {
    if (!isMemoryGraphScopeComplete) {
      return `图谱查询范围缺少 ${missingMemoryGraphScope.join(" / ")}，补全后刷新。`;
    }
    if (currentGraphTabLoadedCount > 0 && currentGraphTabVisibleCount === 0 && hasActiveMemoryGraphFilters) {
      return "当前搜索或筛选条件没有命中；数据仍在当前范围内，可清空筛选后查看。";
    }
    if (hasNoMemoryGraphData) {
      if (memoryGraphStatusFilter) {
        return `当前范围在 status=${memoryGraphStatusFilter} 下没有图谱数据。可切换为“全部”检查是否为状态筛选不匹配。`;
      }
      return "当前已验证群成员范围没有图谱数据。请确认成员与来源键后刷新。";
    }
    if (hasMemoryGraphSearch) {
      return "当前搜索没有命中。可放宽关键词，或清空搜索后查看当前范围的全部图谱数据。";
    }
    if (memoryGraphTab === "episodes" && Boolean(sessionId.trim()) && filteredMemoryGraphEpisodes.length === 0) {
      return "当前已验证群聊下没有事件片段。可重置状态和搜索筛选后刷新。";
    }
    return "当前表格没有可显示项。可调整状态、搜索词或刷新当前范围。";
  })();
  const getMemoryGraphEmptyState = (context: MemoryGraphEmptyContext) => {
    const labels: Record<MemoryGraphEmptyContext, string> = {
      preview: "概览",
      entities: "实体",
      facts: "关系",
      episodes: "事件片段",
      review: "复核队列",
    };
    const contextLoadedCount =
      context === "entities"
        ? memoryGraphLoadedCounts.entities
        : context === "facts"
          ? memoryGraphLoadedCounts.facts
          : context === "episodes"
            ? memoryGraphLoadedCounts.episodes
            : loadedMemoryGraphTotal;
    const contextVisibleCount =
      context === "entities"
        ? filteredMemoryGraphEntities.length
        : context === "facts"
          ? filteredMemoryGraphFacts.length
          : context === "episodes"
            ? filteredMemoryGraphEpisodes.length
            : context === "review"
              ? visibleGraphReviewGroups.low_confidence.length +
                visibleGraphReviewGroups.no_evidence.length +
                visibleGraphReviewGroups.stale.length +
                visibleGraphReviewGroups.inactive_status.length
              : visibleMemoryGraphTotal;
    const state = {
      title: `${labels[context]} 暂无可显示项`,
      body: memoryGraphEmptyHint,
      actions: [] as Array<"clearFilters" | "broadScope" | "refresh" | "clearReviewed">,
      tone: "neutral" as "neutral" | "warning",
    };

    if (!isMemoryGraphScopeComplete) {
      state.title = "先完成图谱查询范围";
      state.body = `缺少 ${missingMemoryGraphScope.join(" / ")}。请先选择群成员或补全字段，再刷新图谱。`;
      state.actions = ["refresh"];
      state.tone = "warning";
      return state;
    }

    if (!loadedMemoryGraphTotal) {
      state.title = "还没有读到图谱数据";
      state.body = "请确认当前群成员和状态是否正确；可重置当前群筛选后刷新图谱。";
      state.actions = ["broadScope", "refresh"];
      return state;
    }

    if (contextLoadedCount > 0 && contextVisibleCount === 0 && hasActiveMemoryGraphFilters) {
      state.title = `${labels[context]} 被过滤条件隐藏`;
      state.body = "当前范围有数据，但按搜索词、类型、关系、置信度或“只看有证据项”筛选后没有命中。";
      state.actions = ["clearFilters"];
      state.tone = "warning";
      return state;
    }

    if (context === "review" && hasHiddenLocalReviewedGraphItems) {
      state.title = "复核队列已被本地复核标记隐藏";
      state.body = "本地复核标记只影响当前浏览器视图，不会修改记忆数据；需要重新检查时请清空本地标记。";
      state.actions = ["clearReviewed"];
      return state;
    }

    if (context === "review" && visibleMemoryGraphTotal > 0 && !hasVisibleGraphReviewItems) {
      state.title = "当前可见项没有复核告警";
      state.body = "可从概览或表格点击条目进入检查面板；如需重新检查隐藏项，请清空筛选或本地复核标记。";
      state.actions = hasActiveMemoryGraphFilters ? ["clearFilters"] : [];
      return state;
    }

    if (context === "episodes" && Boolean(sessionId.trim())) {
      state.title = "当前会话 ID 下没有事件片段";
      state.body = "当前已验证群聊没有事件片段；可重置状态、搜索和本地筛选后刷新。";
      state.actions = ["broadScope", "refresh"];
      return state;
    }

    if (contextLoadedCount === 0 && context !== "preview" && context !== "review") {
      state.title = `${labels[context]} 尚无数据`;
      state.body = context === "facts"
        ? "当前群成员范围没有关系数据。请先确认成员与状态，再重置筛选后刷新。"
        : `当前群成员范围没有${labels[context]}。请先确认成员与状态，再重置筛选后刷新。`;
      state.actions = ["broadScope", "refresh"];
      return state;
    }

    if (visibleMemoryGraphTotal === 0) {
      state.title = "数据已加载但当前不可见";
      state.body = "请清空筛选、搜索词和本地复核标记；如果仍为 0，请刷新当前已验证群成员范围。";
      state.actions = ["clearFilters", "clearReviewed", "broadScope", "refresh"];
      return state;
    }

    state.body = "已有可见数据。点击概览项目、表格行或复核队列条目进入检查面板。";
    return state;
  };
  return {
    ...data,
    memoryGraphModeTabsId,
    memoryGraphDataTabsId,
    memoryGraphCounts,
    memoryGraphLoadedCounts,
    memoryGraphConfidenceThreshold,
    graphEntityHasEvidenceFields,
    filteredMemoryGraphEntities,
    filteredMemoryGraphFacts,
    filteredMemoryGraphEpisodes,
    graphEntityTypeFilterCounts,
    graphPredicateFilterCounts,
    graphEvidenceCounts,
    memoryGraphVisibleCounts,
    visibleMemoryGraphTotal,
    loadedMemoryGraphTotal,
    hiddenMemoryGraphTotal,
    memoryGraphScopeFields,
    memoryGraphEntityById,
    visibleGraphQualityItems,
    visibleGraphReviewGroups,
    visibleGraphQualitySummary,
    visibleGraphReviewSummary,
    selectMemoryGraphItem,
    markMemoryGraphReviewed,
    selectedMemoryGraphReviewKey,
    selectedMemoryGraphIsReviewed,
    visibleReviewedGraphTotal,
    copyVisibleGraphQualityReport,
    missingMemoryGraphScope,
    isMemoryGraphScopeComplete,
    hasNoMemoryGraphData,
    hasActiveMemoryGraphFilters,
    hasVisibleGraphReviewItems,
    hasHiddenLocalReviewedGraphItems,
    memoryGraphStateLabel,
    memoryGraphNextAction,
    memoryGraphGuideSteps,
    memoryGraphSamples,
    hasMemoryGraphSearch,
    currentVisibleGraphIds,
    currentGraphTabVisibleCount,
    currentGraphTabLoadedCount,
    selectedGraphItemIsVisible,
    selectedGraphEntityRelatedFacts,
    selectedGraphEntityNeighborhood,
    selectedGraphFactSubjectEntity,
    selectedGraphFactObjectEntity,
    copySelectedMemoryGraphPath,
    copySelectedMemoryGraphJson,
    memoryGraphEmptyHint,
    getMemoryGraphEmptyState,
  };
}

export type MemoryGraphController = ReturnType<typeof useMemoryGraphController>;

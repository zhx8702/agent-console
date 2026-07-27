import { useCallback, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";
import {
  type MemoryGraphEntity,
  type MemoryGraphEpisode,
  type MemoryGraphFact,
  type MemoryGraphMode,
  type MemoryGraphPreview,
  type MemoryGraphSelection,
  type MemoryGraphSort,
  type MemoryGraphTab,
} from "./model";

interface UseMemoryGraphDataOptions {
  sessionId: string;
  channel: string;
  sourceKey: string;
  userId: string;
  limit: number;
  selectedSessionIsGroup: boolean;
  selectedMemberIsVerified: boolean;
  onOutput: (value: string) => void;
}

export function useMemoryGraphData({
  sessionId,
  channel,
  sourceKey,
  userId,
  limit,
  selectedSessionIsGroup,
  selectedMemberIsVerified,
  onOutput: setMemoryGraphOutput,
}: UseMemoryGraphDataOptions) {
  const { config } = useConsoleConfig();
  const tenantId = config.tenantId;
  const [memoryGraphEntities, setMemoryGraphEntities] = useState<MemoryGraphEntity[]>([]);
  const [memoryGraphFacts, setMemoryGraphFacts] = useState<MemoryGraphFact[]>([]);
  const [memoryGraphEpisodes, setMemoryGraphEpisodes] = useState<MemoryGraphEpisode[]>([]);
  const [memoryGraphPreview, setMemoryGraphPreview] = useState<MemoryGraphPreview | null>(null);
  const [memoryGraphStatusFilter, setMemoryGraphStatusFilter] = useState("active");
  const [memoryGraphSearch, setMemoryGraphSearch] = useState("");
  const [memoryGraphEntityTypeFilter, setMemoryGraphEntityTypeFilter] = useState("");
  const [memoryGraphPredicateFilter, setMemoryGraphPredicateFilter] = useState("");
  const [memoryGraphConfidenceMin, setMemoryGraphConfidenceMin] = useState("");
  const [memoryGraphEvidenceOnly, setMemoryGraphEvidenceOnly] = useState(false);
  const [memoryGraphHideReviewed, setMemoryGraphHideReviewed] = useState(false);
  const [memoryGraphReviewedIds, setMemoryGraphReviewedIds] = useState<Set<string>>(() => new Set());
  const [memoryGraphSort, setMemoryGraphSort] = useState<MemoryGraphSort>("updated_desc");
  const [memoryGraphMode, setMemoryGraphMode] = useState<MemoryGraphMode>("overview");
  const [memoryGraphTab, setMemoryGraphTab] = useState<MemoryGraphTab>("preview");
  const [memoryGraphSelection, setMemoryGraphSelection] = useState<MemoryGraphSelection | null>(null);

  const loadMemoryGraph = useCallback(async () => {
    const scopedUserId = userId.trim();
    const scopedSourceKey = sourceKey.trim();
    if (!selectedSessionIsGroup || !selectedMemberIsVerified || !scopedSourceKey || !channel.trim()) {
      setMemoryGraphEntities([]);
      setMemoryGraphFacts([]);
      setMemoryGraphEpisodes([]);
      setMemoryGraphPreview(null);
      setMemoryGraphSelection(null);
      setMemoryGraphOutput(formatJson({ error: "请先选择已验证群聊和群成员后再读取关系图" }));
      return;
    }
    const query = {
      tenant_id: config.tenantId,
      channel: channel.trim(),
      source_key: scopedSourceKey,
      user_id: scopedUserId,
      session_id: sessionId.trim(),
      status: memoryGraphStatusFilter || undefined,
      limit,
    };
    const episodeQuery = {
      ...query,
      session_id: sessionId.trim(),
    };
    try {
      const [entitiesResult, factsResult, episodesResult, previewResult] = await Promise.all([
        apiRequest<{ items?: MemoryGraphEntity[] }>(config, "/plugins/memory/graph/entities", { auth: true, query }),
        apiRequest<{ items?: MemoryGraphFact[] }>(config, "/plugins/memory/graph/facts", { auth: true, query }),
        apiRequest<{ items?: MemoryGraphEpisode[] }>(config, "/plugins/memory/graph/episodes", { auth: true, query: episodeQuery }),
        apiRequest<MemoryGraphPreview>(config, "/plugins/memory/graph/preview", { auth: true, query: episodeQuery }),
      ]);
      const entities = entitiesResult.items || [];
      const facts = factsResult.items || [];
      const episodes = episodesResult.items || [];
      setMemoryGraphEntities(entities);
      setMemoryGraphFacts(facts);
      setMemoryGraphEpisodes(episodes);
      setMemoryGraphPreview(previewResult);
      setMemoryGraphSelection((current) => {
        if (!current) {
          return null;
        }
        if (current.kind === "entity") {
          const item = entities.find((candidate) => candidate.id === current.item.id)
            || previewResult.entities?.find((candidate) => candidate.id === current.item.id);
          return item ? { kind: "entity", item } : null;
        }
        if (current.kind === "fact") {
          const item = facts.find((candidate) => candidate.id === current.item.id)
            || previewResult.facts?.find((candidate) => candidate.id === current.item.id);
          return item ? { kind: "fact", item } : null;
        }
        const item = episodes.find((candidate) => candidate.id === current.item.id)
          || previewResult.episodes?.find((candidate) => candidate.id === current.item.id);
        return item ? { kind: "episode", item } : null;
      });
      setMemoryGraphOutput(formatJson({
        scope: {
          tenant_id: config.tenantId,
          channel: channel.trim(),
          source_key: scopedSourceKey,
          user_id: scopedUserId,
          session_id: sessionId.trim(),
          status: memoryGraphStatusFilter || undefined,
          limit,
        },
        counts: previewResult.counts || {
          entities: entities.length,
          facts: facts.length,
          episodes: episodes.length,
        },
      }));
    } catch (err) {
      setMemoryGraphEntities([]);
      setMemoryGraphFacts([]);
      setMemoryGraphEpisodes([]);
      setMemoryGraphPreview(null);
      setMemoryGraphSelection(null);
      setMemoryGraphOutput(formatJson({ error: err instanceof Error ? err.message : "记忆图谱读取失败" }));
    }
  }, [channel, config, limit, memoryGraphStatusFilter, selectedMemberIsVerified, selectedSessionIsGroup, sessionId, sourceKey, userId]);

  const clearMemoryGraphFilters = useCallback(() => {
    setMemoryGraphSearch("");
    setMemoryGraphEntityTypeFilter("");
    setMemoryGraphPredicateFilter("");
    setMemoryGraphConfidenceMin("");
    setMemoryGraphEvidenceOnly(false);
    setMemoryGraphSort("updated_desc");
  }, []);

  const resetMemoryGraphBroadScope = useCallback(() => {
    clearMemoryGraphFilters();
    setMemoryGraphStatusFilter("");
    setMemoryGraphHideReviewed(false);
    setMemoryGraphSelection(null);
    setMemoryGraphTab("preview");
  }, [clearMemoryGraphFilters]);


  return {
    tenantId,
    memoryGraphEntities,
    setMemoryGraphEntities,
    memoryGraphFacts,
    setMemoryGraphFacts,
    memoryGraphEpisodes,
    setMemoryGraphEpisodes,
    memoryGraphPreview,
    setMemoryGraphPreview,
    memoryGraphStatusFilter,
    setMemoryGraphStatusFilter,
    memoryGraphSearch,
    setMemoryGraphSearch,
    memoryGraphEntityTypeFilter,
    setMemoryGraphEntityTypeFilter,
    memoryGraphPredicateFilter,
    setMemoryGraphPredicateFilter,
    memoryGraphConfidenceMin,
    setMemoryGraphConfidenceMin,
    memoryGraphEvidenceOnly,
    setMemoryGraphEvidenceOnly,
    memoryGraphHideReviewed,
    setMemoryGraphHideReviewed,
    memoryGraphReviewedIds,
    setMemoryGraphReviewedIds,
    memoryGraphSort,
    setMemoryGraphSort,
    memoryGraphMode,
    setMemoryGraphMode,
    memoryGraphTab,
    setMemoryGraphTab,
    memoryGraphSelection,
    setMemoryGraphSelection,
    loadMemoryGraph,
    clearMemoryGraphFilters,
    resetMemoryGraphBroadScope,
  };
}

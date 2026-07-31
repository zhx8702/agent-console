import { useCallback, useEffect, useMemo, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { requireSelectedGroup, useConsoleConfig } from "../../state/console-config";
import { AcceptanceAuditPanel } from "./AcceptanceAuditPanel";
import { MemoryItemEditor } from "./MemoryItemEditor";
import { MemoryItemList } from "./MemoryItemList";
import {
  type AcceptanceLegacyAudit,
  type AcceptanceLegacyBackfillResult,
  type AcceptanceQueueFilter,
  type AcceptanceReviewAction,
  type AcceptanceStats,
  type GroupRosterCandidate,
  type MemoryItem,
  acceptanceHistoryOf,
  acceptanceSignalEntries,
  acceptanceStatusLabel,
  friendlyApiError,
  memoryItemStatusLabel,
  memoryScopeTypeLabel,
  memorySourceTypeLabel,
  optionalText,
  safeMemoryItemMutationDebug,
  safeMemoryItemPayload,
  safeMemoryItemsDebug,
  supersededByItemIdOf,
  supersedesItemIdOf,
} from "./model";

interface MemoryItemsWorkbenchProps {
  members: GroupRosterCandidate[];
  sessionId: string;
  channel: string;
  sourceKey: string;
  userId: string;
  selectedSessionIsGroup: boolean;
  selectedMemberIsVerified: boolean;
  refreshSignal: number;
  onDirtyChange: (dirty: boolean) => void;
  onOutput: (value: string) => void;
}

export function MemoryItemsWorkbench({
  members,
  sessionId,
  channel,
  sourceKey,
  userId,
  selectedSessionIsGroup,
  selectedMemberIsVerified,
  refreshSignal,
  onDirtyChange,
  onOutput: setMemoryItemsOutput,
}: MemoryItemsWorkbenchProps) {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [memoryItems, setMemoryItems] = useState<MemoryItem[]>([]);
  const [selectedMemoryItemId, setSelectedMemoryItemId] = useState<number | null>(null);
  const [memoryItemLimit, setMemoryItemLimit] = useState(100);
  const [memoryItemStatusFilter, setMemoryItemStatusFilter] = useState("active");
  const [memoryItemSourceTypeFilter, setMemoryItemSourceTypeFilter] = useState("");
  const [memoryAcceptanceQueueFilter, setMemoryAcceptanceQueueFilter] = useState<AcceptanceQueueFilter>("");
  const [acceptanceReviewReason, setAcceptanceReviewReason] = useState("");
  const [acceptanceReviewBusy, setAcceptanceReviewBusy] = useState<AcceptanceReviewAction | null>(null);
  const [supersededByItemIdInput, setSupersededByItemIdInput] = useState("");
  const [supersedesItemIdInput, setSupersedesItemIdInput] = useState("");
  const [acceptanceStats, setAcceptanceStats] = useState<AcceptanceStats | null>(null);
  const [acceptanceLegacyAudit, setAcceptanceLegacyAudit] = useState<AcceptanceLegacyAudit | null>(null);
  const [acceptanceBackfillResult, setAcceptanceBackfillResult] = useState<AcceptanceLegacyBackfillResult | null>(null);
  const [acceptanceStatsLoadedAt, setAcceptanceStatsLoadedAt] = useState<string | null>(null);
  const [acceptanceBackfillLimit, setAcceptanceBackfillLimit] = useState(25);
  const [acceptanceBackfillStatus, setAcceptanceBackfillStatus] = useState<"needs_review" | "candidate">("needs_review");
  const [acceptanceBackfillDryRun, setAcceptanceBackfillDryRun] = useState(true);
  const [acceptanceBackfillConfirm, setAcceptanceBackfillConfirm] = useState(false);
  const [newMemoryContent, setNewMemoryContent] = useState("");
  const newMemoryScopeType = "session";
  const [newMemoryType, setNewMemoryType] = useState("note");
  const [newMemoryPinned, setNewMemoryPinned] = useState(true);
  const [newMemoryPriority, setNewMemoryPriority] = useState(100);
  const [newMemoryRetentionDays, setNewMemoryRetentionDays] = useState("180");
  const [editMemoryContent, setEditMemoryContent] = useState("");
  const [editMemoryStatus, setEditMemoryStatus] = useState("active");
  const [editMemoryPinned, setEditMemoryPinned] = useState(true);
  const [editMemoryPriority, setEditMemoryPriority] = useState(100);
  const [editMemoryType, setEditMemoryType] = useState("note");
  const [editMemorySensitivity, setEditMemorySensitivity] = useState("normal");
  const [editMemoryConfidence, setEditMemoryConfidence] = useState("1");
  const [memoryItemBaseline, setMemoryItemBaseline] = useState<string | null>(null);

  const selectedMemoryItem = useMemo(
    () => memoryItems.find((item) => item.id === selectedMemoryItemId) || null,
    [memoryItems, selectedMemoryItemId],
  );
  const memoryItemFingerprint = JSON.stringify([
    editMemoryContent,
    editMemoryStatus,
    editMemoryPinned,
    editMemoryPriority,
    editMemoryType,
    editMemorySensitivity,
    editMemoryConfidence,
  ]);
  const memoryItemDirty = memoryItemBaseline !== null && memoryItemBaseline !== memoryItemFingerprint;
  const requireMemoryMemberScope = useCallback(() => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const memberId = userId.trim();
    if (!memberId || !members.some((item) => item.wxid === memberId)) {
      throw new Error("请先从当前群的已验证成员名册选择记忆对象");
    }
    return { groupId, memberId };
  }, [config, members, userId, verifiedGroupIds]);
  const acceptanceSignalRows = useMemo(
    () => selectedMemoryItem ? acceptanceSignalEntries(selectedMemoryItem) : [],
    [selectedMemoryItem],
  );
  const acceptanceHistoryRows = useMemo(
    () => selectedMemoryItem ? acceptanceHistoryOf(selectedMemoryItem) : [],
    [selectedMemoryItem],
  );

  const hydrateMemoryItemEditor = useCallback((item: MemoryItem) => {
    setSelectedMemoryItemId(item.id);
    setEditMemoryContent(item.content || "");
    setEditMemoryStatus(item.status || "active");
    setEditMemoryPinned(Boolean(item.pinned));
    setEditMemoryPriority(Number(item.priority ?? 0));
    setEditMemoryType(item.memory_type || "note");
    setEditMemorySensitivity(item.sensitivity || "normal");
    setEditMemoryConfidence(String(item.confidence ?? 1));
    setSupersededByItemIdInput(supersededByItemIdOf(item) ? String(supersededByItemIdOf(item)) : "");
    setSupersedesItemIdInput(supersedesItemIdOf(item) ? String(supersedesItemIdOf(item)) : "");
    setMemoryItemBaseline(JSON.stringify([
      item.content || "",
      item.status || "active",
      Boolean(item.pinned),
      Number(item.priority ?? 0),
      item.memory_type || "note",
      item.sensitivity || "normal",
      String(item.confidence ?? 1),
    ]));
  }, []);


  const loadMemoryItems = useCallback(async () => {
    const scopedUserId = userId.trim();
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setMemoryItems([]);
      setSelectedMemoryItemId(null);
      setMemoryItemsOutput(formatJson({ error: "请先选择已验证群聊和群成员" }));
      return;
    }
    try {
      const result = await apiRequest<{ items?: MemoryItem[] }>(config, "/plugins/memory/items", {
        auth: true,
        query: {
          tenant_id: config.tenantId,
          channel,
          source_key: sourceKey,
          user_id: scopedUserId,
          session_id: sessionId.trim(),
          scope_type: "session",
          source_type: memoryItemSourceTypeFilter || undefined,
          status: memoryItemStatusFilter || undefined,
          include_deleted: !memoryItemStatusFilter || memoryItemStatusFilter === "deleted",
          limit: memoryItemLimit,
        },
      });
      const items = result.items || [];
      setMemoryItems(items);
      setSelectedMemoryItemId((current) => (
        current && !items.some((item) => item.id === current) ? null : current
      ));
      setMemoryItemsOutput(formatJson(safeMemoryItemsDebug(result)));
    } catch (err) {
      setMemoryItems([]);
      setMemoryItemsOutput(formatJson({ error: friendlyApiError(err, "单条记忆读取失败") }));
    }
  }, [
    channel,
    config,
    memoryItemLimit,
    memoryItemSourceTypeFilter,
    memoryItemStatusFilter,
    selectedMemberIsVerified,
    selectedSessionIsGroup,
    sessionId,
    sourceKey,
    userId,
  ]);

  const acceptanceApiLimit = Math.min(10000, Math.max(1, memoryItemLimit || 100));
  const acceptanceFilters = useMemo(
    () => ({
      tenant_id: config.tenantId,
      channel: optionalText(channel),
      source_key: optionalText(sourceKey),
      user_id: optionalText(userId),
      session_id: optionalText(sessionId),
      scope_type: "session",
      source_type: optionalText(memoryItemSourceTypeFilter),
      status: optionalText(memoryItemStatusFilter),
    }),
    [
      channel,
      config.tenantId,
      memoryItemSourceTypeFilter,
      memoryItemStatusFilter,
      sessionId,
      sourceKey,
      userId,
    ],
  );

  const loadAcceptanceAudit = useCallback(async () => {
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setAcceptanceStats(null);
      setAcceptanceLegacyAudit(null);
      return;
    }
    try {
      const [statsResult, auditResult] = await Promise.all([
        apiRequest<AcceptanceStats>(config, "/plugins/memory/items/acceptance-stats", {
          auth: true,
          query: {
            ...acceptanceFilters,
            limit: acceptanceApiLimit,
          },
        }),
        apiRequest<AcceptanceLegacyAudit>(config, "/plugins/memory/items/acceptance-legacy-audit", {
          auth: true,
          query: {
            ...acceptanceFilters,
            limit: acceptanceApiLimit,
          },
        }),
      ]);
      setAcceptanceStats(statsResult);
      setAcceptanceLegacyAudit(auditResult);
      const loadedAt = new Date().toISOString();
      setAcceptanceStatsLoadedAt(loadedAt);
      setMemoryItemsOutput(formatJson({
        status: "acceptance_audit_loaded",
        loaded_at: loadedAt,
        filters: acceptanceFilters,
        counts: statsResult.counts || {},
        sensitivity_counts: statsResult.sensitivity_counts || {},
        missing_acceptance: auditResult.missing_acceptance || 0,
        ids_preview: auditResult.ids_preview || [],
        ids_truncated: auditResult.ids_truncated || 0,
      }));
    } catch (err) {
      setAcceptanceStats(null);
      setAcceptanceLegacyAudit(null);
      setMemoryItemsOutput(formatJson({ error: friendlyApiError(err, "采纳状态审计读取失败") }));
    }
  }, [acceptanceApiLimit, acceptanceFilters, config, selectedMemberIsVerified, selectedSessionIsGroup]);

  const visibleMemoryItems = useMemo(
    () => memoryItems.filter((item) => (
      memoryAcceptanceQueueFilter ? item.acceptance_status === memoryAcceptanceQueueFilter : true
    )),
    [memoryAcceptanceQueueFilter, memoryItems],
  );

  const memoryAcceptanceCounts = useMemo(() => ({
    candidate: memoryItems.filter((item) => item.acceptance_status === "candidate").length,
    accepted: memoryItems.filter((item) => item.acceptance_status === "accepted").length,
    needs_review: memoryItems.filter((item) => item.acceptance_status === "needs_review").length,
    rejected: memoryItems.filter((item) => item.acceptance_status === "rejected").length,
    superseded: memoryItems.filter((item) => item.acceptance_status === "superseded").length,
    expired: memoryItems.filter((item) => item.acceptance_status === "expired").length,
  }), [memoryItems]);
  const acceptanceCountMap = acceptanceStats?.counts || {};
  const acceptanceSensitivityMap = acceptanceStats?.sensitivity_counts || {};
  const acceptanceAuditGroups = acceptanceLegacyAudit?.groups || [];
  const memoryItemsEmptyText = userId.trim()
    ? `当前筛选条件下没有单条记忆；当前列表已限定已验证群成员 ${userId.trim()}。`
    : "请先从当前群名册选择成员。";


  const createManualMemoryItem = async () => {
    const { groupId, memberId } = requireMemoryMemberScope();
    if (!newMemoryContent.trim()) {
      setMemoryItemsOutput(formatJson({ error: "请先填写记忆内容" }));
      return;
    }
    const intent = `memory:item:create:${config.tenantId}:${groupId}:${memberId}:${newMemoryContent.trim()}:${newMemoryType}:${newMemoryRetentionDays || "no-expiry"}`;
    try {
      const result = await apiRequest<MemoryItem>(config, "/plugins/memory/items", {
        auth: true,
        init: {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": keyFor(intent),
          },
          body: JSON.stringify({
            tenant_id: config.tenantId,
            channel,
            source_key: sourceKey,
            user_id: memberId,
            session_id: groupId,
            scope_type: "session",
            source_type: "manual",
            source_kind: "manual",
            origin_session_kind: "group",
            audience_scope: "session",
            allowed_session_ids: [groupId],
            memory_type: newMemoryType.trim() || "note",
            content: newMemoryContent.trim(),
            confidence: 1,
            status: "active",
            pinned: newMemoryPinned,
            priority: newMemoryPriority,
            sensitivity: "normal",
            retention_days: newMemoryRetentionDays
              ? Math.max(1, Number(newMemoryRetentionDays) || 180)
              : undefined,
          }),
        },
      });
      setNewMemoryContent("");
      hydrateMemoryItemEditor(result);
      setMemoryItemsOutput(formatJson(safeMemoryItemMutationDebug("memory_item_created", result)));
      await loadMemoryItems();
      setMemoryItemsOutput(formatJson(safeMemoryItemMutationDebug("memory_item_created", result)));
      clear(intent);
    } catch (err) {
      setMemoryItemsOutput(formatJson({ error: err instanceof Error ? err.message : "单条记忆创建失败" }));
      throw err;
    }
  };

  const saveSelectedMemoryItem = async () => {
    const { groupId, memberId } = requireMemoryMemberScope();
    if (!selectedMemoryItem) {
      setMemoryItemsOutput(formatJson({ error: "请先选择一条记忆" }));
      return;
    }
    if (!editMemoryContent.trim()) {
      setMemoryItemsOutput(formatJson({ error: "记忆内容不能为空" }));
      return;
    }
    if (
      selectedMemoryItem.user_id !== memberId
      || selectedMemoryItem.scope_type !== "session"
      || selectedMemoryItem.session_id !== groupId
    ) {
      const error = new Error("只能编辑当前已验证群成员的群内记忆");
      setMemoryItemsOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `memory:item:save:${config.tenantId}:${groupId}:${memberId}:${selectedMemoryItem.id}:${editMemoryContent.trim()}`;
    try {
      const result = await apiRequest<MemoryItem>(
        config,
        `/plugins/memory/items/${encodeURIComponent(String(selectedMemoryItem.id))}`,
        {
          auth: true,
          query: { tenant_id: config.tenantId },
          init: {
            method: "PATCH",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
              ...(selectedMemoryItem.updated_at
                ? { "If-Match": selectedMemoryItem.updated_at }
                : {}),
            },
            body: JSON.stringify({
              content: editMemoryContent.trim(),
              status: editMemoryStatus,
              pinned: editMemoryPinned,
              priority: editMemoryPriority,
              memory_type: editMemoryType.trim() || "note",
              sensitivity: editMemorySensitivity.trim() || "normal",
              confidence: Number(editMemoryConfidence) || 0,
            }),
          },
        },
      );
      hydrateMemoryItemEditor(result);
      setMemoryItemsOutput(formatJson(safeMemoryItemMutationDebug("memory_item_saved", result)));
      await loadMemoryItems();
      setMemoryItemsOutput(formatJson(safeMemoryItemMutationDebug("memory_item_saved", result)));
      clear(intent);
    } catch (err) {
      setMemoryItemsOutput(formatJson({ error: err instanceof Error ? err.message : "单条记忆保存失败" }));
      throw err;
    }
  };

  const reviewSelectedMemoryAcceptance = async (
    action: AcceptanceReviewAction,
    options: { supersededByItemId?: number; supersedesItemId?: number } = {},
  ) => {
    const { groupId, memberId } = requireMemoryMemberScope();
    if (!selectedMemoryItem) {
      const error = new Error("请先选择一条记忆");
      setMemoryItemsOutput(formatJson({ error: error.message }));
      throw error;
    }
    if (
      selectedMemoryItem.user_id !== memberId
      || selectedMemoryItem.scope_type !== "session"
      || selectedMemoryItem.session_id !== groupId
    ) {
      const error = new Error("只能复核当前已验证群成员的群内记忆");
      setMemoryItemsOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `memory:acceptance:${selectedMemoryItem.id}:${action}:${options.supersededByItemId || ""}:${options.supersedesItemId || ""}:${acceptanceReviewReason.trim()}`;
    setAcceptanceReviewBusy(action);
    try {
      const result = await apiRequest<{ ok?: boolean; item?: MemoryItem }>(
        config,
        `/plugins/memory/items/${encodeURIComponent(String(selectedMemoryItem.id))}/acceptance-review`,
        {
          auth: true,
          query: { tenant_id: config.tenantId },
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({
              action,
              review_reason: acceptanceReviewReason.trim() || undefined,
              superseded_by_item_id: options.supersededByItemId || undefined,
              supersedes_item_id: options.supersedesItemId || undefined,
            }),
          },
        },
      );
      if (result.item) {
        hydrateMemoryItemEditor(result.item);
      }
      setMemoryItemsOutput(formatJson({
        persistent_review: true,
        action,
        ...options,
        ok: result.ok,
        item: safeMemoryItemPayload(result.item),
        omitted_fields: ["content", "original_text", "value_json"],
        note: "技术详情仅保留摘要；不包含记忆正文。",
      }));
      await loadMemoryItems();
      setMemoryItemsOutput(formatJson({
        persistent_review: true,
        action,
        ...options,
        ok: result.ok,
        item: safeMemoryItemPayload(result.item),
        omitted_fields: ["content", "original_text", "value_json"],
        note: "技术详情仅保留摘要；不包含记忆正文。",
      }));
      clear(intent);
    } catch (err) {
      setMemoryItemsOutput(formatJson({ error: friendlyApiError(err, "采纳状态复核失败") }));
      throw err;
    } finally {
      setAcceptanceReviewBusy(null);
    }
  };

  const reviewSelectedMemorySupersededBy = async () => {
    const targetId = Number(supersededByItemIdInput.trim());
    if (!Number.isInteger(targetId) || targetId <= 0) {
      const error = new Error("请输入有效的替代记忆 ID");
      setMemoryItemsOutput(formatJson({ error: error.message }));
      throw error;
    }
    await reviewSelectedMemoryAcceptance("supersede", { supersededByItemId: targetId });
  };

  const reviewSelectedMemorySupersedes = async () => {
    const targetId = Number(supersedesItemIdInput.trim());
    if (!Number.isInteger(targetId) || targetId <= 0) {
      const error = new Error("请输入有效的旧记忆 ID");
      setMemoryItemsOutput(formatJson({ error: error.message }));
      throw error;
    }
    await reviewSelectedMemoryAcceptance("supersede", { supersedesItemId: targetId });
  };

  const runAcceptanceLegacyBackfill = async (surfaceErrors = false) => {
    const fail = (message: string) => {
      setMemoryItemsOutput(formatJson({ error: message }));
      if (surfaceErrors) {
        throw new Error(message);
      }
      return false;
    };
    try {
      requireMemoryMemberScope();
    } catch (error) {
      fail(error instanceof Error ? error.message : "请先选择已验证群聊和群成员");
      return;
    }
    const maxItems = Math.max(1, Math.min(10000, acceptanceBackfillLimit || 1));
    if (!acceptanceBackfillDryRun && !acceptanceBackfillConfirm) {
      fail("非 dry-run 写操作需要先勾选确认框");
      return;
    }
    const intent = `memory:acceptance-backfill:${JSON.stringify(acceptanceFilters)}:${maxItems}:${acceptanceBackfillStatus}`;
    try {
      const result = await apiRequest<AcceptanceLegacyBackfillResult>(
        config,
        "/plugins/memory/items/acceptance-legacy-backfill",
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({
              ...acceptanceFilters,
              dry_run: acceptanceBackfillDryRun,
              max_items: maxItems,
              mark_missing_as: acceptanceBackfillStatus,
            }),
          },
        },
      );
      setAcceptanceBackfillResult(result);
      setMemoryItemsOutput(formatJson({
        status: "acceptance_legacy_backfill_completed",
        ...result,
        note: "技术详情仅保留 ID 和计数；不提供聊天正文。",
      }));
      await loadAcceptanceAudit();
      if (!result.dry_run) {
        await loadMemoryItems();
      }
      clear(intent);
    } catch (err) {
      setAcceptanceBackfillResult(null);
      setMemoryItemsOutput(formatJson({ error: friendlyApiError(err, "旧版采纳元数据补全执行失败") }));
      if (surfaceErrors) {
        throw err;
      }
    }
  };

  const deleteSelectedMemoryItem = async () => {
    const { groupId, memberId } = requireMemoryMemberScope();
    if (!selectedMemoryItem) {
      const error = new Error("请先选择一条记忆");
      setMemoryItemsOutput(formatJson({ error: error.message }));
      throw error;
    }
    if (
      selectedMemoryItem.user_id !== memberId
      || selectedMemoryItem.scope_type !== "session"
      || selectedMemoryItem.session_id !== groupId
    ) {
      const error = new Error("只能删除当前已验证群成员的群内记忆");
      setMemoryItemsOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `memory:delete:${config.tenantId}:${groupId}:${memberId}:${selectedMemoryItem.id}`;
    const itemPath = `/plugins/memory/items/${encodeURIComponent(String(selectedMemoryItem.id))}`;
    try {
      const result = await apiRequest(config, itemPath, {
        auth: true,
        query: { tenant_id: config.tenantId, allow_pinned: true },
        init: {
          method: "DELETE",
          headers: { "Idempotency-Key": keyFor(intent) },
        },
      });
      setSelectedMemoryItemId(null);
      setMemoryItemsOutput(formatJson(safeMemoryItemMutationDebug("memory_item_deleted", null, { allow_pinned: true, result })));
      await loadMemoryItems();
      setMemoryItemsOutput(formatJson(safeMemoryItemMutationDebug("memory_item_deleted", null, { allow_pinned: true, result })));
      clear(intent);
    } catch (err) {
      setMemoryItemsOutput(formatJson({ error: err instanceof Error ? err.message : "单条记忆删除失败" }));
      throw err;
    }
  };


  useEffect(() => {
    onDirtyChange(Boolean(memoryItemDirty || newMemoryContent.trim()));
  }, [memoryItemDirty, newMemoryContent, onDirtyChange]);

  useEffect(() => {
    setMemoryItemBaseline(null);
    setSelectedMemoryItemId(null);
  }, [config.sessionId, sessionId, userId]);

  useEffect(() => {
    void loadMemoryItems();
  }, [loadMemoryItems, refreshSignal]);

  useEffect(() => {
    void loadAcceptanceAudit();
  }, [loadAcceptanceAudit, refreshSignal]);

  return (
      <section className="panel span-3 memory-items-panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">记忆条目</p>
            <h3>单条记忆</h3>
          </div>
          <span className="pill pill-feature">{memoryItems.length} 条</span>
        </div>
        <p className="muted-copy">
          单条记忆固定读取当前已验证群聊与名册成员的会话范围，不能切换到其他用户或跨群范围。
        </p>
        <div className="form-grid memory-item-filters">
          <div className="field"><span>用户范围</span><strong>{userId || "尚未选择成员"}</strong><small>来自当前群名册</small></div>
          <label className="field">
            <span>状态</span>
            <select value={memoryItemStatusFilter} onChange={(event) => setMemoryItemStatusFilter(event.target.value)}>
              <option value="">全部（含已删除）</option>
              {(["active", "pending", "archived", "invalidated", "deleted"] as const).map((value) => <option value={value} key={value}>{memoryItemStatusLabel(value)}</option>)}
            </select>
          </label>
          <label className="field">
            <span>来源类型</span>
            <select value={memoryItemSourceTypeFilter} onChange={(event) => setMemoryItemSourceTypeFilter(event.target.value)}>
              <option value="">全部</option>
              {(["manual", "explicit_user", "auto", "backfill"] as const).map((value) => <option value={value} key={value}>{memorySourceTypeLabel(value)}</option>)}
            </select>
          </label>
          <div className="field"><span>记忆范围</span><strong>{memoryScopeTypeLabel("session")}</strong><small>{sessionId || "尚未选择群聊"}</small></div>
          <label className="field">
            <span>最多读取条数</span>
            <input
              type="number"
              min={1}
              max={500}
              value={memoryItemLimit}
              onChange={(event) => setMemoryItemLimit(Number(event.target.value) || 100)}
            />
          </label>
        </div>
        <p className="muted-copy">
          当前单条记忆列表范围：{sessionId || "未选择群聊"} / {userId || "未选择成员"}。读取和写入都复用同一已验证范围。
        </p>
        <div className="action-row">
          <button className="button button-secondary" onClick={() => void loadMemoryItems()}>
            刷新单条记忆
          </button>
          <button className="button button-secondary" onClick={() => void loadAcceptanceAudit()}>
            刷新采纳状态审计
          </button>
        </div>
        <div className="memory-acceptance-shortcuts" aria-label="采纳状态复核队列筛选">
          {[
            { value: "" as AcceptanceQueueFilter, label: "全部", count: memoryItems.length },
            { value: "candidate" as AcceptanceQueueFilter, label: acceptanceStatusLabel("candidate"), count: memoryAcceptanceCounts.candidate },
            { value: "accepted" as AcceptanceQueueFilter, label: acceptanceStatusLabel("accepted"), count: memoryAcceptanceCounts.accepted },
            { value: "needs_review" as AcceptanceQueueFilter, label: acceptanceStatusLabel("needs_review"), count: memoryAcceptanceCounts.needs_review },
            { value: "rejected" as AcceptanceQueueFilter, label: acceptanceStatusLabel("rejected"), count: memoryAcceptanceCounts.rejected },
            { value: "superseded" as AcceptanceQueueFilter, label: acceptanceStatusLabel("superseded"), count: memoryAcceptanceCounts.superseded },
            { value: "expired" as AcceptanceQueueFilter, label: acceptanceStatusLabel("expired"), count: memoryAcceptanceCounts.expired },
          ].map((item) => (
            <button
              key={item.value || "all"}
              className={`button button-compact ${memoryAcceptanceQueueFilter === item.value ? "button-primary" : "button-secondary"}`}
              type="button"
              onClick={() => {
                setMemoryAcceptanceQueueFilter(item.value);
                if (item.value === "candidate" || item.value === "needs_review" || item.value === "rejected") {
                  setMemoryItemStatusFilter("pending");
                } else if (item.value === "accepted") {
                  setMemoryItemStatusFilter("active");
                } else if (item.value === "superseded") {
                  setMemoryItemStatusFilter("invalidated");
                } else if (item.value === "expired") {
                  setMemoryItemStatusFilter("archived");
                }
              }}
            >
              {item.label} <span className="tab-count">{item.count}</span>
            </button>
          ))}
        </div>
        <div className="admin-notice">
          提示词、召回和向量检索只使用已采纳、有效且普通敏感级别的记忆。缺少采纳元数据的旧版记录暂按原有规则兼容；收紧规则前请先完成审计与补全。已取代、已过期、已拒绝、待处理、私密和敏感记忆不会进入这些路径。
        </div>

        <AcceptanceAuditPanel
          countMap={acceptanceCountMap}
          sensitivityMap={acceptanceSensitivityMap}
          audit={acceptanceLegacyAudit}
          auditGroups={acceptanceAuditGroups}
          statsLoadedAt={acceptanceStatsLoadedAt}
          backfillStatus={acceptanceBackfillStatus}
          backfillLimit={acceptanceBackfillLimit}
          backfillDryRun={acceptanceBackfillDryRun}
          backfillConfirm={acceptanceBackfillConfirm}
          backfillResult={acceptanceBackfillResult}
          filters={acceptanceFilters}
          onBackfillStatusChange={setAcceptanceBackfillStatus}
          onBackfillLimitChange={setAcceptanceBackfillLimit}
          onBackfillDryRunChange={setAcceptanceBackfillDryRun}
          onBackfillConfirmChange={setAcceptanceBackfillConfirm}
          onRunBackfill={runAcceptanceLegacyBackfill}
        />

        <div className="memory-items-workbench">
          <MemoryItemList
            items={visibleMemoryItems}
            selectedItemId={selectedMemoryItemId}
            emptyText={memoryItemsEmptyText}
            onSelect={hydrateMemoryItemEditor}
          />

          <MemoryItemEditor
            newDraft={{
              content: newMemoryContent,
              scopeType: newMemoryScopeType,
              memoryType: newMemoryType,
              pinned: newMemoryPinned,
              priority: newMemoryPriority,
              retentionDays: newMemoryRetentionDays,
            }}
            editDraft={{
              content: editMemoryContent,
              status: editMemoryStatus,
              memoryType: editMemoryType,
              sensitivity: editMemorySensitivity,
              confidence: editMemoryConfidence,
              pinned: editMemoryPinned,
              priority: editMemoryPriority,
            }}
            selectedItem={selectedMemoryItem}
            acceptanceSignalRows={acceptanceSignalRows}
            acceptanceHistoryRows={acceptanceHistoryRows}
            acceptanceReviewReason={acceptanceReviewReason}
            acceptanceReviewBusy={acceptanceReviewBusy}
            supersededByItemIdInput={supersededByItemIdInput}
            supersedesItemIdInput={supersedesItemIdInput}
            onNewContentChange={setNewMemoryContent}
            onNewMemoryTypeChange={setNewMemoryType}
            onNewPinnedChange={setNewMemoryPinned}
            onNewPriorityChange={setNewMemoryPriority}
            onNewRetentionDaysChange={setNewMemoryRetentionDays}
            onEditContentChange={setEditMemoryContent}
            onEditStatusChange={setEditMemoryStatus}
            onEditMemoryTypeChange={setEditMemoryType}
            onEditSensitivityChange={setEditMemorySensitivity}
            onEditConfidenceChange={setEditMemoryConfidence}
            onEditPinnedChange={setEditMemoryPinned}
            onEditPriorityChange={setEditMemoryPriority}
            onAcceptanceReviewReasonChange={setAcceptanceReviewReason}
            onSupersededByItemIdInputChange={setSupersededByItemIdInput}
            onSupersedesItemIdInputChange={setSupersedesItemIdInput}
            onCreate={createManualMemoryItem}
            onSave={saveSelectedMemoryItem}
            onDelete={deleteSelectedMemoryItem}
            onReview={reviewSelectedMemoryAcceptance}
            onReviewSupersededBy={reviewSelectedMemorySupersededBy}
            onReviewSupersedes={reviewSelectedMemorySupersedes}
          />
        </div>
      </section>
  );
}

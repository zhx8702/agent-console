import { useCallback, useEffect, useMemo, useState } from "react";

import { PageHeader } from "../../components/PageHeader";
import { SearchableSelect } from "../../components/SearchableSelect";
import { TabList } from "../../components/Tabs";
import { UnsavedChangesGuard } from "../../components/UnsavedChangesGuard";
import { apiRequest, formatJson, type MemoryBackfillRequest } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import {
  requireSelectedGroup,
  useConsoleConfig,
} from "../../state/console-config";
import { BackfillPanel } from "./BackfillPanel";
import { DebugOutputs } from "./DebugOutputs";
import { ExtractionJobsWorkbench } from "./ExtractionJobsWorkbench";
import { IdentityProfilePanel } from "./IdentityProfilePanel";
import { MemoryGraphWorkbench } from "./MemoryGraphWorkbench";
import { MemoryItemsWorkbench } from "./MemoryItemsWorkbench";
import { MemoryRuntimeStatusPanel } from "./MemoryRuntimeStatusPanel";
import { ProfileEnrichmentWorkbench } from "./ProfileEnrichmentWorkbench";
import {
  type WxbotSession,
  type GroupRosterCandidate,
  type IdentityProfile,
  type SessionProfile,
  type RuntimeProfile,
  type MemoryEvent,
  type BackfillResult,
  PLACEHOLDER_USER_IDS,
  isPlaceholderUserId,
  hasIdentityProfileContent,
  isGroupSession,
  getMemberDisplayName,
  parseSessionIds,
  safeRuntimeProfileDebug,
  safeEventListDebug,
  safeBackfillResultDebug,
} from "./model";
import { SessionProfilePanel } from "./SessionProfilePanel";

const LEGACY_WXBOT_HISTORY_CONNECTION_ID = "legacy-wechat-default";

const MEMORY_WORKSPACE_TABS = [
  { id: "profiles", label: "成员与档案" },
  { id: "items", label: "单条记忆" },
  { id: "enrichment", label: "画像复核" },
  { id: "maintenance", label: "任务维护" },
  { id: "backfill", label: "历史回填" },
  { id: "graph", label: "技术图谱" },
] as const;

type MemoryWorkspaceTab = (typeof MEMORY_WORKSPACE_TABS)[number]["id"];

export function MemoryWorkspace() {
  const {
    config,
    verifiedGroupIds,
    selectVerifiedGroup,
  } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [sessions, setSessions] = useState<WxbotSession[]>([]);
  const [members, setMembers] = useState<GroupRosterCandidate[]>([]);
  const [identityProfiles, setIdentityProfiles] = useState<IdentityProfile[]>([]);
  const [sessionProfiles, setSessionProfiles] = useState<SessionProfile[]>([]);
  const [events, setEvents] = useState<MemoryEvent[]>([]);
  const [sessionId, setSessionId] = useState(config.sessionId);
  const [selectedMemberWxid, setSelectedMemberWxid] = useState("");
  const [channel, setChannel] = useState("wechat");
  const [sourceKey, setSourceKey] = useState("wxbot");
  const [userId, setUserId] = useState(config.userId);
  const [identityLongTermMemory, setIdentityLongTermMemory] = useState("");
  const [identityManualNotes, setIdentityManualNotes] = useState("");
  const [identityProfileBaseline, setIdentityProfileBaseline] = useState<string | null>(null);
  const [identityProfileVersion, setIdentityProfileVersion] = useState<string | null>(null);
  const [sessionShortTermMemory, setSessionShortTermMemory] = useState("");
  const [sessionManualNotes, setSessionManualNotes] = useState("");
  const [sessionProfileBaseline, setSessionProfileBaseline] = useState<string | null>(null);
  const [sessionProfileVersion, setSessionProfileVersion] = useState<string | null>(null);
  const [runtimeProfile, setRuntimeProfile] = useState<RuntimeProfile | null>(null);
  const [daysLimit, setDaysLimit] = useState(180);
  const [maxMessagesPerSession, setMaxMessagesPerSession] = useState(200);
  const [backfillPickerSessionId, setBackfillPickerSessionId] = useState("");
  const [backfillSessionIdsText, setBackfillSessionIdsText] = useState("");
  const [limit, setLimit] = useState(50);
  const [memoryItemsDirty, setMemoryItemsDirty] = useState(false);
  const [memoryItemsRefreshSignal, setMemoryItemsRefreshSignal] = useState(0);
  const [extractionJobOutput, setExtractionJobOutput] = useState('{\n  "status": "waiting"\n}');
  const [metaOutput, setMetaOutput] = useState('{\n  "status": "waiting"\n}');
  const [identityOutput, setIdentityOutput] = useState('{\n  "status": "waiting"\n}');
  const [sessionOutput, setSessionOutput] = useState('{\n  "status": "waiting"\n}');
  const [runtimeOutput, setRuntimeOutput] = useState('{\n  "status": "waiting"\n}');
  const [memoryItemsOutput, setMemoryItemsOutput] = useState('{\n  "status": "waiting"\n}');
  const [memoryGraphOutput, setMemoryGraphOutput] = useState('{\n  "status": "waiting"\n}');
  const [profileEnrichmentOutput, setProfileEnrichmentOutput] = useState('{\n  "status": "waiting"\n}');
  const [activeWorkspaceTab, setActiveWorkspaceTab] = useState<MemoryWorkspaceTab>("profiles");
  const [visitedWorkspaceTabs, setVisitedWorkspaceTabs] = useState<Set<MemoryWorkspaceTab>>(
    () => new Set(["profiles"]),
  );

  const selectedSession = useMemo(
    () => sessions.find((item) => item.session_id === sessionId) || null,
    [sessionId, sessions],
  );
  const selectedSessionIsGroup = Boolean(
    sessionId.trim()
    && verifiedGroupIds.has(sessionId.trim())
    && selectedSession
    && isGroupSession(selectedSession),
  );
  const selectedMemberIsVerified = Boolean(
    userId.trim() && members.some((item) => item.wxid === userId.trim()),
  );
  const identityProfileFingerprint = JSON.stringify([identityLongTermMemory, identityManualNotes]);
  const sessionProfileFingerprint = JSON.stringify([sessionShortTermMemory, sessionManualNotes]);
  const identityProfileDirty = identityProfileBaseline !== null && identityProfileBaseline !== identityProfileFingerprint;
  const sessionProfileDirty = sessionProfileBaseline !== null && sessionProfileBaseline !== sessionProfileFingerprint;
  const hasUnsavedMemoryChanges = Boolean(
    identityProfileDirty
    || sessionProfileDirty
    || memoryItemsDirty,
  );
  const selectedMember = members.find((item) => item.wxid === selectedMemberWxid) || null;
  const requireMemoryMemberScope = useCallback(() => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const memberId = userId.trim();
    if (!memberId || !members.some((item) => item.wxid === memberId)) {
      throw new Error("请先从当前群的已验证成员名册选择记忆对象");
    }
    return { groupId, memberId };
  }, [config, members, userId, verifiedGroupIds]);
  const parsedBackfillSessionIds = useMemo(
    () => parseSessionIds(backfillSessionIdsText),
    [backfillSessionIdsText],
  );
  const backfillSessionSet = useMemo(
    () => new Set(parsedBackfillSessionIds),
    [parsedBackfillSessionIds],
  );
  const sessionOptions = useMemo(
    () =>
      sessions.map((item) => ({
        value: item.session_id,
        label: `${isGroupSession(item) ? "[群]" : "[私聊]"} ${item.session_name || item.session_id}`,
        keywords: [item.session_id, item.session_name || "", item.kind || ""],
      })),
    [sessions],
  );
  const availableBackfillSessionOptions = useMemo(
    () =>
      sessionOptions.filter((item) => item.value === sessionId && !backfillSessionSet.has(item.value)),
    [backfillSessionSet, sessionId, sessionOptions],
  );
  const selectedBackfillSessions = useMemo(
    () =>
      parsedBackfillSessionIds.map((value) => {
        const matched = sessions.find((item) => item.session_id === value);
        return {
          session_id: value,
          session_name: matched?.session_name || value,
          kind: matched?.kind || (value.endsWith("@chatroom") ? "group" : "private"),
        };
      }),
    [parsedBackfillSessionIds, sessions],
  );
  const groupBackfillCandidates = useMemo(
    () =>
      sessions.filter((item) => item.session_id === sessionId && isGroupSession(item) && !backfillSessionSet.has(item.session_id)),
    [backfillSessionSet, sessionId, sessions],
  );
  const memberOptions = useMemo(
    () =>
      members.map((item) => ({
        value: item.wxid,
        label: `${getMemberDisplayName(item)} (${item.wxid})`,
        keywords: [item.wxid, item.name || "", item.alias || "", item.remark || "", item.nick_name || ""],
      })),
    [members],
  );
  const applyIdentityProfile = useCallback((profile: IdentityProfile) => {
    if (!members.some((item) => item.wxid === profile.user_id)) {
      setIdentityOutput(formatJson({ error: "该档案不属于当前群的已验证成员，未切换记忆对象" }));
      return false;
    }
    setChannel(profile.channel || "wechat");
    setSourceKey(profile.source_key || "wxbot");
    setUserId(profile.user_id || "");
    setIdentityLongTermMemory(profile.long_term_memory || "");
    setIdentityManualNotes(profile.manual_notes || "");
    setIdentityProfileBaseline(JSON.stringify([profile.long_term_memory || "", profile.manual_notes || ""]));
    setIdentityProfileVersion(profile.updated_at || "");
    return true;
  }, [members]);

  const selectIdentityProfile = useCallback((profile: IdentityProfile) => {
    if (!applyIdentityProfile(profile)) {
      return;
    }
    setIdentityOutput(formatJson({
      status: "profile_selected",
      message: "已应用该全局档案。现在可以读取、编辑或保存该 wxid 的全局记忆。",
      profile,
    }));
  }, [applyIdentityProfile]);

  const applySessionProfile = useCallback((profile: SessionProfile) => {
    if (
      profile.session_id !== sessionId
      || !verifiedGroupIds.has(profile.session_id || "")
      || !members.some((item) => item.wxid === profile.user_id)
    ) {
      setSessionOutput(formatJson({ error: "该会话档案不属于当前已验证群成员，未切换范围" }));
      return;
    }
    setChannel(profile.channel || "wechat");
    setSourceKey(profile.source_key || "wxbot");
    setUserId(profile.user_id || "");
    setSessionShortTermMemory(profile.short_term_memory || "");
    setSessionManualNotes(profile.manual_notes || "");
    setSessionProfileBaseline(JSON.stringify([profile.short_term_memory || "", profile.manual_notes || ""]));
    setSessionProfileVersion(profile.updated_at || "");
  }, [members, sessionId, verifiedGroupIds]);

  const applyRuntimeProfile = useCallback((profile: RuntimeProfile) => {
    if (
      profile.session_id !== sessionId
      || !verifiedGroupIds.has(profile.session_id || "")
      || !members.some((item) => item.wxid === profile.user_id)
    ) {
      setRuntimeOutput(formatJson({ error: "运行时档案不属于当前已验证群成员，未切换范围" }));
      return;
    }
    setRuntimeProfile(profile);
    setChannel(profile.channel || "wechat");
    setSourceKey(profile.source_key || "wxbot");
    setUserId(profile.user_id || "");
    setIdentityLongTermMemory(profile.long_term_memory || "");
    setIdentityManualNotes(profile.identity_manual_notes || "");
    setSessionShortTermMemory(profile.short_term_memory || "");
    setSessionManualNotes(profile.session_manual_notes || "");
    setIdentityProfileBaseline(JSON.stringify([profile.long_term_memory || "", profile.identity_manual_notes || ""]));
    setSessionProfileBaseline(JSON.stringify([profile.short_term_memory || "", profile.session_manual_notes || ""]));
    if (profile.identity_profile) {
      setIdentityProfileVersion(profile.identity_profile.updated_at || "");
    }
    if (profile.session_profile) {
      setSessionProfileVersion(profile.session_profile.updated_at || "");
    }
  }, [members, sessionId, verifiedGroupIds]);

  const updateBackfillSessionIds = useCallback((items: string[]) => {
    const next = Array.from(new Set(
      items
        .map((item) => item.trim())
        .filter((item) => Boolean(item) && item === sessionId && verifiedGroupIds.has(item)),
    ));
    setBackfillSessionIdsText(next.join("\n"));
    return next;
  }, [sessionId, verifiedGroupIds]);

  const addBackfillSessions = useCallback((items: string[]) => {
    const next = updateBackfillSessionIds([...parsedBackfillSessionIds, ...items]);
    setRuntimeOutput(formatJson({ added: true, session_ids: next }));
  }, [parsedBackfillSessionIds, updateBackfillSessionIds]);

  const removeBackfillSession = useCallback((sessionIdToRemove: string) => {
    const next = parsedBackfillSessionIds.filter((item) => item !== sessionIdToRemove);
    updateBackfillSessionIds(next);
    setRuntimeOutput(formatJson({ removed: true, session_ids: next }));
  }, [parsedBackfillSessionIds, updateBackfillSessionIds]);

  const loadGroups = useCallback(async () => {
    try {
      const rosterGroups = await apiRequest<{ sessions?: WxbotSession[] }>(
        config,
        "/plugins/wxbot/admin/roster/groups",
        { auth: true },
      );
      const nextSessions = (rosterGroups.sessions || []).filter(isGroupSession);
      setSessions(nextSessions);
      setMetaOutput(
        formatJson({
          sessions: nextSessions.slice(0, 50),
          count: nextSessions.length,
          group_count: nextSessions.filter(isGroupSession).length,
          source: "authenticated_roster",
        }),
      );
    } catch (err) {
      setMetaOutput(formatJson({ error: err instanceof Error ? err.message : "会话列表加载失败" }));
    }
  }, [config]);

  const loadMembers = useCallback(async (targetSessionId = sessionId) => {
    const nextSessionId = targetSessionId.trim();
    if (!nextSessionId || !verifiedGroupIds.has(nextSessionId)) {
      setMembers([]);
      setSelectedMemberWxid("");
      setMetaOutput(formatJson({ error: "请先从已验证群聊列表选择目标群" }));
      return;
    }
    try {
      const result = await apiRequest<{ candidates?: GroupRosterCandidate[] }>(
        config,
        `/plugins/wxbot/admin/roster/groups/${encodeURIComponent(nextSessionId)}/members`,
        { auth: true },
      );
      const nextMembers = result.candidates || [];
      setMembers(nextMembers);
      setSelectedMemberWxid((current) => (
        nextMembers.some((item) => item.wxid === current) ? current : nextMembers[0]?.wxid || ""
      ));
      setMetaOutput(formatJson(result));
    } catch (err) {
      setMetaOutput(formatJson({ error: err instanceof Error ? err.message : "群成员加载失败" }));
    }
  }, [config, sessionId, verifiedGroupIds]);

  const loadIdentityProfile = useCallback(async () => {
    const scopedUserId = userId.trim();
    if (!selectedSessionIsGroup || !selectedMemberIsVerified || isPlaceholderUserId(scopedUserId)) {
      setIdentityOutput(formatJson({
        status: "needs_real_wxid",
        message: "读取全局记忆需要先选择当前已验证群的成员。",
        current_user_id: scopedUserId || null,
        placeholder_user_ids: Array.from(PLACEHOLDER_USER_IDS),
      }));
      return;
    }
    try {
      const result = await apiRequest<IdentityProfile>(
        config,
        `/plugins/memory/profiles/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(channel)}/${encodeURIComponent(sourceKey)}/${encodeURIComponent(scopedUserId)}`,
        { auth: true },
      );
      applyIdentityProfile(result);
      if (!hasIdentityProfileContent(result)) {
        setIdentityOutput(formatJson({
          status: "empty_profile",
          message: "该用户暂无全局记忆。",
          profile: result,
        }));
        return;
      }
      setIdentityOutput(formatJson(result));
    } catch (err) {
      setIdentityOutput(formatJson({ error: err instanceof Error ? err.message : "全局记忆读取失败" }));
    }
  }, [applyIdentityProfile, channel, config, selectedMemberIsVerified, selectedSessionIsGroup, sourceKey, userId]);

  const loadSessionProfile = useCallback(async () => {
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setSessionOutput(formatJson({ error: "请先选择已验证群聊和群成员" }));
      return;
    }
    try {
      const result = await apiRequest<SessionProfile>(
        config,
        `/plugins/memory/session-profiles/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(channel)}/${encodeURIComponent(sourceKey)}/${encodeURIComponent(sessionId.trim())}`,
        {
          auth: true,
          query: { user_id: userId.trim() },
        },
      );
      applySessionProfile(result);
      setSessionOutput(formatJson(result));
    } catch (err) {
      setSessionOutput(formatJson({ error: err instanceof Error ? err.message : "会话记忆读取失败" }));
    }
  }, [applySessionProfile, channel, config, selectedMemberIsVerified, selectedSessionIsGroup, sessionId, sourceKey, userId]);

  const loadRuntimeProfile = useCallback(async () => {
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setRuntimeOutput(formatJson({ error: "请先选择已验证群聊和群成员" }));
      return;
    }
    try {
      const result = await apiRequest<RuntimeProfile>(
        config,
        `/plugins/memory/runtime-profile/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(channel)}/${encodeURIComponent(sourceKey)}/${encodeURIComponent(sessionId.trim())}`,
        {
          auth: true,
          query: { user_id: userId.trim() },
        },
      );
      applyRuntimeProfile(result);
      setRuntimeOutput(formatJson(safeRuntimeProfileDebug(result)));
    } catch (err) {
      setRuntimeOutput(formatJson({ error: err instanceof Error ? err.message : "运行时记忆读取失败" }));
    }
  }, [applyRuntimeProfile, channel, config, selectedMemberIsVerified, selectedSessionIsGroup, sessionId, sourceKey, userId]);

  const loadIdentityProfiles = useCallback(async () => {
    const scopedUserId = userId.trim();
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setIdentityProfiles([]);
      setIdentityOutput(formatJson({ error: "请先选择已验证群聊和群成员" }));
      return;
    }
    try {
      const result = await apiRequest<{ items?: IdentityProfile[] }>(config, "/plugins/memory/profiles", {
        auth: true,
        query: {
          tenant_id: config.tenantId,
          channel,
          source_key: sourceKey,
          user_id: scopedUserId,
          limit,
        },
      });
      const items = result.items || [];
      setIdentityProfiles(items);
      setIdentityOutput(formatJson(result));
    } catch (err) {
      setIdentityProfiles([]);
      setIdentityOutput(formatJson({ error: err instanceof Error ? err.message : "全局记忆列表读取失败" }));
    }
  }, [channel, config, limit, selectedMemberIsVerified, selectedSessionIsGroup, sourceKey, userId]);

  const loadSessionProfiles = useCallback(async () => {
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setSessionProfiles([]);
      return;
    }
    try {
      const result = await apiRequest<{ items?: SessionProfile[] }>(config, "/plugins/memory/session-profiles", {
        auth: true,
        query: {
          tenant_id: config.tenantId,
          channel,
          source_key: sourceKey,
          session_id: sessionId.trim(),
          user_id: userId.trim(),
          limit,
        },
      });
      const items = result.items || [];
      setSessionProfiles(items);
      setSessionOutput(formatJson(result));
    } catch (err) {
      setSessionProfiles([]);
      setSessionOutput(formatJson({ error: err instanceof Error ? err.message : "会话记忆列表读取失败" }));
    }
  }, [channel, config, limit, selectedMemberIsVerified, selectedSessionIsGroup, sessionId, sourceKey, userId]);

  const loadEvents = useCallback(async () => {
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setEvents([]);
      return;
    }
    try {
      const result = await apiRequest<{ items?: MemoryEvent[] }>(config, "/plugins/memory/events", {
        auth: true,
        query: {
          tenant_id: config.tenantId,
          channel,
          source_key: sourceKey,
          user_id: userId.trim(),
          session_id: sessionId.trim(),
          limit,
        },
      });
      setEvents(result.items || []);
      setRuntimeOutput(formatJson(safeEventListDebug(result)));
    } catch (err) {
      setEvents([]);
      setRuntimeOutput(formatJson({ error: err instanceof Error ? err.message : "记忆事件读取失败" }));
    }
  }, [channel, config, limit, selectedMemberIsVerified, selectedSessionIsGroup, sessionId, sourceKey, userId]);

  const identityProfilesEmptyText = userId.trim()
    ? `暂无全局身份记忆；当前列表已限定已验证成员 ${userId.trim()}。`
    : "请先从当前群名册选择成员。";
  const saveIdentityProfile = async () => {
    const { groupId, memberId } = requireMemoryMemberScope();
    const intent = `memory:identity-profile:${config.tenantId}:${groupId}:${memberId}`;
    try {
      const result = await apiRequest<IdentityProfile>(config, "/plugins/memory/profiles", {
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
            long_term_memory: identityLongTermMemory,
            manual_notes: identityManualNotes,
            expected_version: identityProfileVersion ?? "",
          }),
        },
      });
      applyIdentityProfile(result);
      await loadIdentityProfiles();
      setIdentityOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setIdentityOutput(formatJson({ error: err instanceof Error ? err.message : "全局记忆保存失败" }));
    }
  };

  const saveSessionProfile = async () => {
    const { groupId, memberId } = requireMemoryMemberScope();
    const intent = `memory:session-profile:${config.tenantId}:${groupId}:${memberId}`;
    try {
      const result = await apiRequest<SessionProfile>(config, "/plugins/memory/session-profiles", {
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
            session_id: groupId,
            user_id: memberId,
            short_term_memory: sessionShortTermMemory,
            manual_notes: sessionManualNotes,
            expected_version: sessionProfileVersion ?? "",
          }),
        },
      });
      applySessionProfile(result);
      await loadSessionProfiles();
      setSessionOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setSessionOutput(formatJson({ error: err instanceof Error ? err.message : "会话记忆保存失败" }));
    }
  };

  const runBackfill = async () => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    if (!userId.trim() || !members.some((item) => item.wxid === userId.trim())) {
      const error = new Error("请先从当前群的已验证成员名册选择记忆对象");
      setRuntimeOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `memory:backfill:${config.tenantId}:${LEGACY_WXBOT_HISTORY_CONNECTION_ID}:${groupId}:${userId.trim()}:${daysLimit}:${maxMessagesPerSession}`;
    try {
      const body: MemoryBackfillRequest = {
        tenant_id: config.tenantId,
        connection_id: LEGACY_WXBOT_HISTORY_CONNECTION_ID,
        channel,
        source_key: sourceKey,
        user_id: userId.trim(),
        session_ids: [groupId],
        days_limit: daysLimit,
        max_messages_per_session: maxMessagesPerSession,
      };
      const result = await apiRequest<BackfillResult>(config, "/plugins/memory/backfill", {
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
      setRuntimeOutput(formatJson(safeBackfillResultDebug(result)));
      await Promise.all([
        loadIdentityProfile(),
        loadSessionProfile(),
        loadRuntimeProfile(),
        loadIdentityProfiles(),
        loadSessionProfiles(),
      ]);
      setMemoryItemsRefreshSignal((current) => current + 1);
      clear(intent);
    } catch (err) {
      setRuntimeOutput(formatJson({ error: err instanceof Error ? err.message : "历史回填失败" }));
      throw err;
    }
  };

  useEffect(() => {
    if (sessionId !== config.sessionId) {
      setSessionId(config.sessionId);
    }
    setUserId("");
    setSelectedMemberWxid("");
    setIdentityProfileBaseline(null);
    setSessionProfileBaseline(null);
    setIdentityProfileVersion(null);
    setSessionProfileVersion(null);
  }, [config.sessionId, sessionId]);

  useEffect(() => {
    void loadGroups();
  }, [loadGroups]);

  useEffect(() => {
    if (sessionId) {
      void loadMembers();
    }
  }, [loadMembers, sessionId]);

  useEffect(() => {
    if (!selectedSessionIsGroup) {
      setMembers([]);
      setSelectedMemberWxid("");
    }
  }, [selectedSessionIsGroup]);

  const memoryWorkspaceTabTriggers = MEMORY_WORKSPACE_TABS.map((tab) => ({
    ...tab,
    disabled: memoryItemsDirty && activeWorkspaceTab === "items" && tab.id !== "items",
  }));

  const selectMemoryWorkspaceTab = useCallback((id: string) => {
    const nextId = id as MemoryWorkspaceTab;
    setActiveWorkspaceTab(nextId);
    setVisitedWorkspaceTabs((current) => {
      if (current.has(nextId)) return current;
      const next = new Set(current);
      next.add(nextId);
      return next;
    });
  }, []);

  return (
    <div className="page-grid memory-page">
      <UnsavedChangesGuard when={hasUnsavedMemoryChanges} />
      <section className="panel span-3">
        <PageHeader
          eyebrow="记忆管理"
          title="用户记忆管理"
          description="当前记忆模型分为两层：全局身份记忆按用户 ID 聚合，会话记忆按会话 ID 与用户 ID 覆盖。实时对话继续通过流水线沉淀，历史记忆则通过 SDK 的 /ext/query/read 从微信解密库回填。"
        />
        <div className="summary-grid">
          <div className="summary-card" data-status="ok">
            <span>身份档案</span>
            <strong>{identityProfiles.length}</strong>
          </div>
          <div className="summary-card">
            <span>会话档案</span>
            <strong>{sessionProfiles.length}</strong>
          </div>
          <div className="summary-card">
            <span>互动事件</span>
            <strong>{events.length}</strong>
          </div>
          <div className="summary-card" data-status={runtimeProfile?.imported_message_count ? "warning" : undefined}>
            <span>已导入消息</span>
            <strong>{runtimeProfile?.imported_message_count ?? 0}</strong>
          </div>
        </div>
        <div className="data-flow-note">
          <strong>当前链路</strong>
          <span>实时消息进入控制台后，会同步更新当前会话短期记忆，并把稳定事实沉淀到全局身份记忆。</span>
          <span>历史回填不再要求 SDK 新增专用接口，而是由平台调 SDK 的 `/ext/query/read` 自己掌握查询模板和合并逻辑。</span>
        </div>
        <div className="form-grid">
          <label className="field">
            <span>微信会话</span>
            <SearchableSelect
              value={sessionId}
              onChange={(value) => {
                selectVerifiedGroup(value);
              }}
              options={sessionOptions}
              placeholder="请选择已验证群聊"
              searchPlaceholder="搜索群名或群 ID"
              emptyText="暂无已验证群聊"
              noResultsText="没有匹配的会话"
            />
          </label>
          <label className="field">
            <span>群成员</span>
            <SearchableSelect
              value={selectedMemberWxid}
              onChange={setSelectedMemberWxid}
              options={memberOptions}
              placeholder="请选择群成员"
              searchPlaceholder="搜索成员名或 WXID"
              emptyText="暂无群成员"
              noResultsText="没有匹配的群成员"
              disabled={!selectedSessionIsGroup}
            />
          </label>
          <label className="field">
            <span>消息渠道</span>
            <select value={channel} onChange={(event) => setChannel(event.target.value)}>
              <option value="wechat">微信</option>
              <option value="web">网页</option>
              <option value="whatsapp">WhatsApp</option>
              <option value="telegram">Telegram</option>
              <option value="email">电子邮件</option>
              <option value="sms">短信</option>
              <option value="voice">语音</option>
              <option value="custom">自定义</option>
            </select>
          </label>
          <label className="field">
            <span>来源键</span>
            <input value={sourceKey} onChange={(event) => setSourceKey(event.target.value)} />
          </label>
          <div className="field span-2">
            <span>当前记忆成员</span>
            <strong>{selectedMember ? getMemberDisplayName(selectedMember) : "尚未选择"}</strong>
            <small className="mono">{userId || "仅可从当前群成员名册选择"}</small>
          </div>
        </div>
        <p className="muted-copy">
          当前会话类型：
          <span> {selectedSessionIsGroup ? "群聊" : sessionId ? "私聊" : "-"}</span>
           。当前页只处理已验证群聊及其成员；跨群或私聊记忆不会在这里被手工并入。
        </p>
        <div className="action-row">
          <button className="button button-secondary" onClick={() => void loadGroups()}>
            刷新会话列表
          </button>
          <button
            className="button button-secondary"
            onClick={() => void loadMembers()}
            disabled={!selectedSessionIsGroup}
          >
            加载群成员
          </button>
          <button
            className="button button-primary"
            onClick={() => {
              if (!selectedSessionIsGroup) {
                setMetaOutput(formatJson({ error: "请先选择已验证群聊" }));
                return;
              }
              if (!selectedMember) {
                setMetaOutput(formatJson({ error: "请先选择群成员" }));
                return;
              }
              setUserId(selectedMember.wxid || "");
              setMetaOutput(formatJson({ applied: true, mode: "verified_group_member", member: selectedMember }));
            }}
          >
            应用当前选择到记忆对象
          </button>
          <button
            className="button button-secondary"
            onClick={() => {
              if (!sessionId.trim()) {
                setRuntimeOutput(formatJson({ error: "请先选择微信会话" }));
                return;
              }
              addBackfillSessions([sessionId.trim()]);
            }}
          >
            加入回填范围
          </button>
          <button
            className="button button-secondary"
            onClick={() => {
              if (!groupBackfillCandidates.length) {
                setRuntimeOutput(formatJson({ message: "当前没有可追加的群聊会话" }));
                return;
              }
              addBackfillSessions(groupBackfillCandidates.map((item) => item.session_id));
            }}
          >
            批量加入全部群聊
          </button>
        </div>
        <div className="table-scroll member-table-scroll">
          <table>
            <caption className="sr-only">当前已验证群聊成员名册</caption>
            <thead>
              <tr>
                <th scope="col">成员</th>
                <th scope="col">WXID</th>
                <th scope="col">发言数</th>
              </tr>
            </thead>
            <tbody>
              {members.map((item) => (
                <tr key={item.wxid}>
                  <th scope="row">
                    <button
                      type="button"
                      className="memory-graph-row-action"
                      onClick={() => setSelectedMemberWxid(item.wxid || "")}
                    >
                      {getMemberDisplayName(item)}
                    </button>
                  </th>
                  <td className="mono">{item.wxid}</td>
                  <td>{item.msg_count ?? 0}</td>
                </tr>
              ))}
              {!members.length && (
                <tr>
                  <td colSpan={3}>当前群还没有加载到成员列表</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="span-3 memory-workspace" aria-labelledby="memory-workspace-title">
        <div className="memory-workspace-heading">
          <div>
            <p className="section-kicker">任务工作区</p>
            <h2 id="memory-workspace-title">按任务管理成员记忆</h2>
            <p>一次只呈现一个工作区；群与成员范围统一继承页面上方的已验证选择。</p>
          </div>
          <div className="memory-workspace-scope" aria-label="当前继承范围">
            <span>当前范围</span>
            <strong>{selectedSession?.session_name || sessionId || "未选择群聊"}</strong>
            <small>{selectedMember ? `${getMemberDisplayName(selectedMember)} · ${userId}` : "未应用群成员"}</small>
          </div>
        </div>
        {memoryItemsDirty && activeWorkspaceTab === "items" && (
          <div className="admin-notice admin-notice-warning" role="status">
            单条记忆有未保存内容。保存或清空后才能切换工作区，避免丢失编辑。
          </div>
        )}
        <TabList
          tabs={memoryWorkspaceTabTriggers}
          activeId={activeWorkspaceTab}
          onChange={selectMemoryWorkspaceTab}
          ariaLabel="成员记忆任务"
          idPrefix="memory-workspace"
          className="tabs-list memory-workspace-tabs"
        />

        <div
          id="memory-workspace-panel-profiles"
          className="memory-workspace-panel"
          role="tabpanel"
          aria-labelledby="memory-workspace-tab-profiles"
          tabIndex={0}
          hidden={activeWorkspaceTab !== "profiles"}
        >
          {visitedWorkspaceTabs.has("profiles") && (
            <div className="memory-profile-grid">
              <IdentityProfilePanel
                longTermMemory={identityLongTermMemory}
                manualNotes={identityManualNotes}
                profiles={identityProfiles}
                canSave={selectedSessionIsGroup && selectedMemberIsVerified && identityProfileDirty}
                emptyText={identityProfilesEmptyText}
                onLongTermMemoryChange={setIdentityLongTermMemory}
                onManualNotesChange={setIdentityManualNotes}
                onSelectProfile={selectIdentityProfile}
                onLoad={loadIdentityProfile}
                onSave={saveIdentityProfile}
                onList={loadIdentityProfiles}
              />
              <SessionProfilePanel
                shortTermMemory={sessionShortTermMemory}
                manualNotes={sessionManualNotes}
                profiles={sessionProfiles}
                canSave={selectedSessionIsGroup && selectedMemberIsVerified && sessionProfileDirty}
                onShortTermMemoryChange={setSessionShortTermMemory}
                onManualNotesChange={setSessionManualNotes}
                onSelectProfile={applySessionProfile}
                onLoad={loadSessionProfile}
                onSave={saveSessionProfile}
                onList={loadSessionProfiles}
              />
            </div>
          )}
        </div>

        <div
          id="memory-workspace-panel-items"
          className="memory-workspace-panel"
          role="tabpanel"
          aria-labelledby="memory-workspace-tab-items"
          tabIndex={0}
          hidden={activeWorkspaceTab !== "items"}
        >
          {visitedWorkspaceTabs.has("items") && (
            <MemoryItemsWorkbench
              members={members}
              sessionId={sessionId}
              channel={channel}
              sourceKey={sourceKey}
              userId={userId}
              selectedSessionIsGroup={selectedSessionIsGroup}
              selectedMemberIsVerified={selectedMemberIsVerified}
              refreshSignal={memoryItemsRefreshSignal}
              onDirtyChange={setMemoryItemsDirty}
              onOutput={setMemoryItemsOutput}
            />
          )}
        </div>

        <div
          id="memory-workspace-panel-enrichment"
          className="memory-workspace-panel"
          role="tabpanel"
          aria-labelledby="memory-workspace-tab-enrichment"
          tabIndex={0}
          hidden={activeWorkspaceTab !== "enrichment"}
        >
          {visitedWorkspaceTabs.has("enrichment") && (
            <ProfileEnrichmentWorkbench
              sessions={sessions}
              members={members}
              sessionId={sessionId}
              userId={userId}
              selectedSessionIsGroup={selectedSessionIsGroup}
              selectedMemberIsVerified={selectedMemberIsVerified}
              onOutput={setProfileEnrichmentOutput}
            />
          )}
        </div>

        <div
          id="memory-workspace-panel-maintenance"
          className="memory-workspace-panel"
          role="tabpanel"
          aria-labelledby="memory-workspace-tab-maintenance"
          tabIndex={0}
          hidden={activeWorkspaceTab !== "maintenance"}
        >
          {visitedWorkspaceTabs.has("maintenance") && (
            <div className="memory-maintenance-grid">
              <MemoryRuntimeStatusPanel
                sessionId={sessionId}
                channel={channel}
                sourceKey={sourceKey}
                userId={userId}
                selectedSessionIsGroup={selectedSessionIsGroup}
                selectedMemberIsVerified={selectedMemberIsVerified}
                onOutput={setExtractionJobOutput}
              />
              <ExtractionJobsWorkbench
                members={members}
                sessionId={sessionId}
                channel={channel}
                sourceKey={sourceKey}
                userId={userId}
                selectedSessionIsGroup={selectedSessionIsGroup}
                selectedMemberIsVerified={selectedMemberIsVerified}
                onOutput={setExtractionJobOutput}
              />
              <DebugOutputs
                meta={metaOutput}
                identity={identityOutput}
                session={sessionOutput}
                memoryItems={memoryItemsOutput}
                profileEnrichment={profileEnrichmentOutput}
                extractionJobs={extractionJobOutput}
                memoryGraph={memoryGraphOutput}
                runtime={runtimeOutput}
              />
            </div>
          )}
        </div>

        <div
          id="memory-workspace-panel-backfill"
          className="memory-workspace-panel"
          role="tabpanel"
          aria-labelledby="memory-workspace-tab-backfill"
          tabIndex={0}
          hidden={activeWorkspaceTab !== "backfill"}
        >
          {visitedWorkspaceTabs.has("backfill") && (
            <BackfillPanel
              connectionId={LEGACY_WXBOT_HISTORY_CONNECTION_ID}
              pickerSessionId={backfillPickerSessionId}
              pickerOptions={availableBackfillSessionOptions}
              daysLimit={daysLimit}
              maxMessagesPerSession={maxMessagesPerSession}
              limit={limit}
              runtimeProfile={runtimeProfile}
              userId={userId}
              selectedSessions={selectedBackfillSessions}
              events={events}
              onPickerSessionIdChange={setBackfillPickerSessionId}
              onSessionIdsTextChange={setBackfillSessionIdsText}
              onDaysLimitChange={setDaysLimit}
              onMaxMessagesPerSessionChange={setMaxMessagesPerSession}
              onLimitChange={setLimit}
              onRuntimeOutputChange={setRuntimeOutput}
              onAddSessions={addBackfillSessions}
              onRemoveSession={removeBackfillSession}
              onRunBackfill={runBackfill}
              onLoadRuntimeProfile={loadRuntimeProfile}
              onLoadEvents={loadEvents}
            />
          )}
        </div>

        <div
          id="memory-workspace-panel-graph"
          className="memory-workspace-panel"
          role="tabpanel"
          aria-labelledby="memory-workspace-tab-graph"
          tabIndex={0}
          hidden={activeWorkspaceTab !== "graph"}
        >
          {visitedWorkspaceTabs.has("graph") && (
            <MemoryGraphWorkbench
              sessionId={sessionId}
              channel={channel}
              sourceKey={sourceKey}
              userId={userId}
              limit={limit}
              onLimitChange={setLimit}
              selectedSessionIsGroup={selectedSessionIsGroup}
              selectedMemberIsVerified={selectedMemberIsVerified}
              onOutput={setMemoryGraphOutput}
            />
          )}
        </div>
      </section>
    </div>
  );
}

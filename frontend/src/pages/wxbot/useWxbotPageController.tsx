import {
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  formatJson,
  type GroupParticipationPolicyDocument,
  type GroupParticipationPolicyUpdate,
} from "../../lib/api";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useConsoleConfig } from "../../state/console-config";
import { useLocation, useNavigate } from "react-router-dom";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { useWxbotAgentAdmin } from "./useWxbotAgentAdmin";
import { useWxbotEventAdmin } from "./useWxbotEventAdmin";
import { useWxbotQueueAdmin } from "./useWxbotQueueAdmin";

import { BridgeStatus, GlobalReplyPolicy, GroupActivityConfig, GroupActivityDecision, GroupActivityEvent, GroupParticipationPolicy, QueueStats, ReplyPolicy, ReplyPolicyAggregate, ReportMessagesPayload, ReportPreview, ReportSubscription, ReportType, SdkTriggerDebugConfig, SelfReviewJob, SelfReviewPreview, SelfReviewPublishResult, SelfReviewSubscription, SessionStateSnapshot, WxbotSession, WxbotTab, createDefaultGroupActivityConfig, groupActivityValidationError, isGroupSession, normalizeGroupActivityConfig, normalizeGroupParticipationPolicy, normalizeGroupReplyMode, normalizeSessionReplyMode, readWxbotTabFromLocation, resolveExplicitVerifiedGroupSessionId, sessionDisplayName } from "./model";

export function useWxbotPageController() {
const { config, registerVerifiedGroups, selectVerifiedGroup, updateConfig } = useConsoleConfig();

const { keyFor, clear: clearIdempotencyKey } = useStableIdempotencyKeys();

const location = useLocation();

const navigate = useNavigate();

const wxbotFocus = useMemo(() => {
    const searchParams = new URLSearchParams(location.search);
    const hashParams = new URLSearchParams(location.hash.startsWith("#") ? location.hash.slice(1) : location.hash);
    return searchParams.get("focus") || hashParams.get("focus") || "";
  }, [location.hash, location.search]);

const weeklyReportFocused = wxbotFocus === "weekly";

const [bridgeStatus, setBridgeStatus] = useState<BridgeStatus | null>(null);

const [queueStats, setQueueStats] = useState<QueueStats>({});

const [sdkQueueStats, setSdkQueueStats] = useState<Record<string, unknown>>({});

const [sdkTriggerDebug, setSdkTriggerDebug] = useState<SdkTriggerDebugConfig | null>(null);

const [sdkGroupRequireAtMe, setSdkGroupRequireAtMe] = useState("true");

const [sessions, setSessions] = useState<WxbotSession[]>([]);

const [rosterGroups, setRosterGroups] = useState<WxbotSession[]>([]);

const [sessionId, setSessionId] = useState("");

const [sessionName, setSessionName] = useState("");

const [groupSessionId, setGroupSessionId] = useState("");

const [replyMode, setReplyMode] = useState("inherit");

const [mentionSenderMode, setMentionSenderMode] = useState("inherit");

const [triggerKeywordsText, setTriggerKeywordsText] = useState("");

const [globalPrivateReplyMode, setGlobalPrivateReplyMode] = useState("all");

const [globalGroupReplyMode, setGlobalGroupReplyMode] = useState("off");

const [globalGroupReplyMentionSender, setGlobalGroupReplyMentionSender] = useState("false");

const [globalTriggerKeywordsText, setGlobalTriggerKeywordsText] = useState("");

const [policySnapshot, setPolicySnapshot] = useState<ReplyPolicy | null>(null);

const [policyEtag, setPolicyEtag] = useState<string | null>(null);

const [globalPolicySnapshot, setGlobalPolicySnapshot] = useState<GlobalReplyPolicy | null>(null);

const [globalPolicyEtag, setGlobalPolicyEtag] = useState<string | null>(null);

const [policyConflict, setPolicyConflict] = useState<"" | "global" | "session" | "aggregate">("");

const [participationPolicy, setParticipationPolicy] = useState<GroupParticipationPolicy>(() => (
    normalizeGroupParticipationPolicy()
  ));

const [groupParticipationPolicy, setGroupParticipationPolicy] = useState<GroupParticipationPolicyDocument | null>(null);

const [groupParticipationSnapshot, setGroupParticipationSnapshot] = useState<GroupParticipationPolicyDocument | null>(null);

const [groupParticipationEtag, setGroupParticipationEtag] = useState<string | null>(null);

const [groupParticipationStatus, setGroupParticipationStatus] = useState<"idle" | "loading" | "loaded" | "saving" | "error" | "conflict">("idle");

const [groupParticipationError, setGroupParticipationError] = useState("");

const [groupActivityConfig, setGroupActivityConfig] = useState<GroupActivityConfig>(() => (
    createDefaultGroupActivityConfig(config.tenantId, "")
  ));

const [groupActivityDecision, setGroupActivityDecision] = useState<GroupActivityDecision | null>(null);

const [groupActivityEvents, setGroupActivityEvents] = useState<GroupActivityEvent[]>([]);

const [groupActivityFeedback, setGroupActivityFeedback] = useState("");

const [groupActivityBusy, setGroupActivityBusy] = useState<"" | "load" | "save" | "dry-run">("");

const [loadedGroupActivityConfig, setLoadedGroupActivityConfig] = useState<GroupActivityConfig | null>(null);

const [groupActivityEtag, setGroupActivityEtag] = useState<string | null>(null);

const [groupActivityServerEtag, setGroupActivityServerEtag] = useState<string | null>(null);

const [groupActivityStatus, setGroupActivityStatus] = useState<"idle" | "loading" | "loaded" | "saving" | "error" | "conflict">("idle");

const [sessionStateSnapshot, setSessionStateSnapshot] = useState<SessionStateSnapshot | null>(null);

const [sessionStateEtag, setSessionStateEtag] = useState<string | null>(null);

const [sessionStateStatus, setSessionStateStatus] = useState<"idle" | "loading" | "loaded" | "saving" | "error" | "conflict">("idle");

const [reportSubscriptions, setReportSubscriptions] = useState<ReportSubscription[]>([]);

const [reportSubscriptionsEtag, setReportSubscriptionsEtag] = useState<string | null>(null);

const [reportSubscriptionsStatus, setReportSubscriptionsStatus] = useState<"idle" | "loading" | "loaded" | "saving" | "error" | "conflict">("idle");

const [reportDailyEnabled, setReportDailyEnabled] = useState("false");

const [reportWeeklyEnabled, setReportWeeklyEnabled] = useState("true");

const [reportMonthlyEnabled, setReportMonthlyEnabled] = useState("false");

const [reportDailyHour, setReportDailyHour] = useState(9);

const [reportWeeklyDay, setReportWeeklyDay] = useState(1);

const [reportWeeklyHour, setReportWeeklyHour] = useState(9);

const [reportMonthlyDay, setReportMonthlyDay] = useState(1);

const [reportTz, setReportTz] = useState("Asia/Shanghai");

const [reportPreviewType, setReportPreviewType] = useState<ReportType>(weeklyReportFocused ? "weekly" : "daily");

const [reportPreview, setReportPreview] = useState<ReportPreview | null>(null);

const [reportDate, setReportDate] = useState("");

const [reportYearMonth, setReportYearMonth] = useState("");

const [reportMessages, setReportMessages] = useState<ReportMessagesPayload | null>(null);

const [selfReviewSubscriptions, setSelfReviewSubscriptions] = useState<SelfReviewSubscription[]>([]);

const [selfReviewSubscriptionsEtag, setSelfReviewSubscriptionsEtag] = useState<string | null>(null);

const [selfReviewSubscriptionsStatus, setSelfReviewSubscriptionsStatus] = useState<"idle" | "loading" | "loaded" | "saving" | "error" | "conflict">("idle");

const [selfReviewEnabled, setSelfReviewEnabled] = useState("false");

const [selfReviewDailyHour, setSelfReviewDailyHour] = useState(23);

const [selfReviewTz, setSelfReviewTz] = useState("Asia/Shanghai");

const [selfReviewDate, setSelfReviewDate] = useState("");

const [selfReviewPreview, setSelfReviewPreview] = useState<SelfReviewPreview | null>(null);

const [selfReviewJobs, setSelfReviewJobs] = useState<SelfReviewJob[]>([]);

const [selfReviewPublishingJobId, setSelfReviewPublishingJobId] = useState<number | null>(null);

const [activeTab, setActiveTab] = useState<WxbotTab>(() => readWxbotTabFromLocation(location.search, location.hash));

const [loading, setLoading] = useState(false);

const [error, setError] = useState("");

const [actionOutput, setActionOutput] = useState('{\n  "status": "waiting"\n}');

const [policyOutput, setPolicyOutput] = useState('{\n  "status": "waiting"\n}');

const [reportOutput, setReportOutput] = useState('{\n  "status": "waiting"\n}');

const [selfReviewOutput, setSelfReviewOutput] = useState('{\n  "status": "waiting"\n}');

const groupSessions = useMemo(() => {
    return rosterGroups.filter(isGroupSession);
  }, [rosterGroups]);

const effectiveSessionId = sessionId || config.sessionId;

const effectiveSession = useMemo(
    () => sessions.find((item) => item.session_id === effectiveSessionId) || null,
    [effectiveSessionId, sessions],
  );

const effectiveSessionIsGroup = effectiveSession
    ? isGroupSession(effectiveSession)
    : isGroupSession({ session_id: effectiveSessionId, kind: "" });

const effectiveActivitySessionId = effectiveSessionIsGroup ? effectiveSessionId : "";

const effectiveActivitySessionName = effectiveSessionIsGroup
    ? (effectiveSession ? sessionDisplayName(effectiveSession) : effectiveSessionId)
    : "";

const groupActivityLoadedForScope = Boolean(
    loadedGroupActivityConfig
      && loadedGroupActivityConfig.tenant_id === config.tenantId
      && loadedGroupActivityConfig.session_id === effectiveActivitySessionId,
  );

const groupActivityDirty = Boolean(
    groupActivityLoadedForScope
      && loadedGroupActivityConfig
      && groupActivityEditableSnapshot(groupActivityConfig)
        !== groupActivityEditableSnapshot(loadedGroupActivityConfig),
);

const globalPolicyDirty = Boolean(
    globalPolicySnapshot
      && (
        globalPrivateReplyMode !== (globalPolicySnapshot.private_reply_mode || "all")
        || globalGroupReplyMode !== normalizeGroupReplyMode(globalPolicySnapshot.group_reply_mode)
        || globalGroupReplyMentionSender !== String(globalPolicySnapshot.group_reply_mention_sender ?? false)
        || globalTriggerKeywordsText !== (globalPolicySnapshot.trigger_keywords_text || "")
      ),
  );

const sessionPolicyDirty = Boolean(
    policySnapshot
      && (
        replyMode !== normalizeSessionReplyMode(policySnapshot.reply_mode, effectiveSessionIsGroup)
        || mentionSenderMode !== (policySnapshot.mention_sender_mode || "inherit")
        || triggerKeywordsText !== (policySnapshot.trigger_keywords_text || "")
        || (
          effectiveSessionIsGroup
          && JSON.stringify(participationPolicy)
            !== JSON.stringify(normalizeGroupParticipationPolicy(policySnapshot.participation_policy))
        )
      ),
  );

const groupParticipationDirty = Boolean(
    groupParticipationPolicy
      && groupParticipationSnapshot
      && groupParticipationPolicy.kill_switches.group_enabled
        !== groupParticipationSnapshot.kill_switches.group_enabled,
  );

const sdkGateDirty = Boolean(
    sdkTriggerDebug
      && sdkGroupRequireAtMe !== String(sdkTriggerDebug.group_require_at_me ?? true),
  );

const replyPolicyDirty = globalPolicyDirty || sessionPolicyDirty || sdkGateDirty;
const groupActivityDirtyRef = useRef(groupActivityDirty);
groupActivityDirtyRef.current = groupActivityDirty;

const groupActivityFormDisabled = !groupActivityLoadedForScope
    || groupActivityStatus === "loading"
    || groupActivityStatus === "saving";

const effectiveGroupSessionId = resolveExplicitVerifiedGroupSessionId(
    groupSessionId,
    config.sessionId,
    groupSessions,
  );

const effectiveGroupSession = useMemo(
    () => groupSessions.find((item) => item.session_id === effectiveGroupSessionId) || null,
    [effectiveGroupSessionId, groupSessions],
  );

const effectiveGroupSessionName = sessionDisplayName(effectiveGroupSession)
    || effectiveGroupSessionId
    || "未选择群会话";

const queueAdmin = useWxbotQueueAdmin({
    clearIdempotencyKey,
    config,
    effectiveGroupSessionId,
    keyFor,
    setActionOutput,
  });

const agentAdmin = useWxbotAgentAdmin({
    activeTab,
    clearIdempotencyKey,
    config,
    effectiveGroupSessionId,
    keyFor,
  });

const eventAdmin = useWxbotEventAdmin({
    activeTab,
    clearIdempotencyKey,
    config,
    effectiveGroupSessionId,
    groupSessions,
    keyFor,
  });

const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [bridge, queue, sdkQueue, sdkSessions, roster] = await Promise.all([
        apiRequest<BridgeStatus>(config, "/plugins/wxbot/bridge/status"),
        apiRequest<QueueStats>(config, "/plugins/wxbot/admin/reply-queue/stats", {
          auth: true,
          query: { tenant_id: config.tenantId },
        }),
        apiRequest<Record<string, unknown>>(config, "/plugins/wxbot/admin/sdk/queue/stats", {
          auth: true,
        }).catch(() => ({})),
        apiRequest<{ sessions?: WxbotSession[] }>(config, "/plugins/wxbot/admin/sessions", {
          auth: true,
        }).catch(() => ({ sessions: [] })),
        apiRequest<{ sessions?: WxbotSession[] }>(config, "/plugins/wxbot/admin/roster/groups", {
          auth: true,
        }).catch(() => ({ sessions: [] })),
      ]);
      const nextSessions = sdkSessions.sessions || [];
      const nextGroups = roster.sessions || [];
      setBridgeStatus(bridge);
      setQueueStats(queue);
      setSdkQueueStats(sdkQueue);
      setSessions(nextSessions);
      setRosterGroups(nextGroups);
      registerVerifiedGroups(nextGroups.filter(isGroupSession).map((item) => item.session_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取失败");
    } finally {
      setLoading(false);
    }
  }, [config, registerVerifiedGroups]);

useEffect(() => {
    if (config.adminToken) {
      void refresh();
    }
  }, [config.adminToken, refresh]);

useEffect(() => {
    const nextTab = readWxbotTabFromLocation(location.search, location.hash);
    setActiveTab((currentTab) => (currentTab === nextTab ? currentTab : nextTab));
  }, [location.hash, location.search]);

useEffect(() => {
    if (weeklyReportFocused) {
      setReportPreviewType("weekly");
    }
  }, [weeklyReportFocused]);

useEffect(() => {
    const matched = sessions.find((item) => item.session_id === sessionId);
    if (matched) {
      setSessionName(matched.session_name || "");
    }
  }, [sessions, sessionId]);

useEffect(() => {
    if (!effectiveGroupSessionId) {
      return;
    }
    const matched = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    if (matched && !sessionName && matched.session_name) {
      setSessionName(matched.session_name);
    }
  }, [effectiveGroupSessionId, groupSessions, sessionName]);

useEffect(() => {
    const current = reportSubscriptions.find((item) => item.session_id === effectiveGroupSessionId);
    if (current) {
      setReportDailyEnabled(String(Boolean(current.daily_enabled)));
      setReportWeeklyEnabled(String(Boolean(current.weekly_enabled ?? true)));
      setReportMonthlyEnabled(String(Boolean(current.monthly_enabled)));
      setReportDailyHour(Number(current.daily_hour ?? 9));
      setReportWeeklyDay(Number(current.weekly_day ?? 1));
      setReportWeeklyHour(Number(current.weekly_hour ?? 9));
      setReportMonthlyDay(Number(current.monthly_day ?? 1));
      setReportTz(current.tz || "Asia/Shanghai");
      return;
    }
    setReportDailyEnabled("false");
    setReportWeeklyEnabled("true");
    setReportMonthlyEnabled("false");
    setReportDailyHour(9);
    setReportWeeklyDay(1);
    setReportWeeklyHour(9);
    setReportMonthlyDay(1);
    setReportTz("Asia/Shanghai");
  }, [effectiveGroupSessionId, reportSubscriptions]);

useEffect(() => {
    const current = selfReviewSubscriptions.find((item) => item.session_id === effectiveGroupSessionId);
    if (current) {
      setSelfReviewEnabled(String(Boolean(current.enabled)));
      setSelfReviewDailyHour(Number(current.daily_hour ?? 23));
      setSelfReviewTz(current.tz || "Asia/Shanghai");
      return;
    }
    setSelfReviewEnabled("false");
    setSelfReviewDailyHour(23);
    setSelfReviewTz("Asia/Shanghai");
  }, [effectiveGroupSessionId, selfReviewSubscriptions]);

useEffect(() => {
    if (!config.sessionId) {
      return;
    }
    if (sessionId !== config.sessionId) {
      setSessionId(config.sessionId);
    }
  }, [config.sessionId, sessionId]);

useEffect(() => {
    const nextGroupSessionId = groupSessions.some((item) => item.session_id === config.sessionId)
      ? config.sessionId
      : "";
    if (groupSessionId !== nextGroupSessionId) {
      setGroupSessionId(nextGroupSessionId);
    }
  }, [config.sessionId, groupSessionId, groupSessions]);

const chooseVerifiedGroup = useCallback((groupId: string) => {
    const normalized = groupId.trim();
    selectVerifiedGroup(normalized);
    setGroupSessionId(normalized);
    if (normalized) {
      setSessionId(normalized);
    }
  }, [selectVerifiedGroup]);

const loadGlobalReplyPolicy = useCallback(async () => {
    try {
      const resource = await apiVersionedResource<GlobalReplyPolicy>(
        config,
        `/plugins/wxbot/admin/reply-policy/global/${encodeURIComponent(config.tenantId)}`,
        { auth: true },
      );
      if (!resource.etag) {
        throw new Error("服务器未返回全局策略版本，已禁止覆盖保存");
      }
      const result = resource.value;
      setGlobalPrivateReplyMode(result.private_reply_mode || "all");
      setGlobalGroupReplyMode(normalizeGroupReplyMode(result.group_reply_mode));
      setGlobalGroupReplyMentionSender(String(result.group_reply_mention_sender ?? false));
      setGlobalTriggerKeywordsText(result.trigger_keywords_text || "");
      setGlobalPolicySnapshot(result);
      setGlobalPolicyEtag(resource.etag);
      setPolicyConflict("");
      setPolicyOutput(formatJson(result));
    } catch (err) {
      setGlobalPolicyEtag(null);
      setPolicyOutput(formatJson({ error: err instanceof Error ? err.message : "读取全局回复策略失败" }));
    }
  }, [config]);

const saveGlobalReplyPolicy = async () => {
    if (!globalPolicyEtag) {
      setPolicyOutput(formatJson({ error: "请先读取带版本的全局策略，再保存本地草稿" }));
      return;
    }
    const intent = `wxbot-global-policy:${config.tenantId}:${globalPolicyEtag}`;
    try {
      const resource = await apiVersionedResource<GlobalReplyPolicy, {
        private_reply_mode: string;
        group_reply_mode: string;
        group_reply_mention_sender: boolean;
        trigger_keywords_text: string;
      }>(
        config,
        `/plugins/wxbot/admin/reply-policy/global/${encodeURIComponent(config.tenantId)}`,
        {
          auth: true,
          method: "POST",
          ifMatch: globalPolicyEtag,
          idempotencyKey: keyFor(intent),
          body: {
            private_reply_mode: globalPrivateReplyMode,
            group_reply_mode: globalGroupReplyMode,
            group_reply_mention_sender: globalGroupReplyMentionSender === "true",
            trigger_keywords_text: globalTriggerKeywordsText,
          },
        },
      );
      if (!resource.etag) {
        throw new Error("保存成功但服务器未返回新版本，请重新读取");
      }
      const result = resource.value;
      setGlobalPrivateReplyMode(result.private_reply_mode || "all");
      setGlobalGroupReplyMode(normalizeGroupReplyMode(result.group_reply_mode));
      setGlobalGroupReplyMentionSender(String(result.group_reply_mention_sender ?? false));
      setGlobalTriggerKeywordsText(result.trigger_keywords_text || "");
      setGlobalPolicySnapshot(result);
      setGlobalPolicyEtag(resource.etag);
      setPolicyConflict("");
      clearIdempotencyKey(intent);
      setPolicyOutput(formatJson(result));
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setPolicyConflict("global");
        setPolicyOutput(formatJson({
          error: "全局策略已被其他操作者更新，本地草稿已保留",
          recovery: "重新读取服务端版本，比较后再次保存",
          current_etag: err.serverEtag,
        }));
      } else {
        setPolicyOutput(formatJson({ error: err instanceof Error ? err.message : "保存全局回复策略失败" }));
      }
    }
  };

const loadReplyPolicy = useCallback(async () => {
    if (!effectiveSessionId) {
      setPolicyOutput(formatJson({ error: "请先选择会话" }));
      return;
    }
    try {
      const resource = await apiVersionedResource<ReplyPolicy>(
        config,
        `/plugins/wxbot/admin/reply-policy/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}`,
        { auth: true },
      );
      if (!resource.etag) {
        throw new Error("服务器未返回会话策略版本，已禁止覆盖保存");
      }
      const result = resource.value;
      setReplyMode(normalizeSessionReplyMode(result.reply_mode, effectiveSessionIsGroup));
      setMentionSenderMode(result.mention_sender_mode || "inherit");
      setTriggerKeywordsText(result.trigger_keywords_text || "");
      setParticipationPolicy(normalizeGroupParticipationPolicy(result.participation_policy));
      setPolicySnapshot(result);
      setPolicyEtag(resource.etag);
      setPolicyConflict("");
      setPolicyOutput(formatJson(result));
    } catch (err) {
      setPolicyEtag(null);
      setPolicyOutput(formatJson({ error: err instanceof Error ? err.message : "读取回复策略失败" }));
    }
  }, [config, effectiveSessionId, effectiveSessionIsGroup]);

const loadGroupParticipationPolicy = useCallback(async () => {
    if (!effectiveActivitySessionId) {
      setGroupParticipationPolicy(null);
      setGroupParticipationSnapshot(null);
      setGroupParticipationEtag(null);
      setGroupParticipationStatus("idle");
      setGroupParticipationError("");
      return;
    }
    setGroupParticipationStatus("loading");
    setGroupParticipationError("");
    try {
      const resource = await apiVersionedResource<GroupParticipationPolicyDocument>(
        config,
        `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(effectiveActivitySessionId)}/participation-policy`,
        { auth: true },
      );
      if (!resource.etag) {
        throw new Error("服务器未返回群参与开关版本，已禁止覆盖保存");
      }
      setGroupParticipationPolicy(resource.value);
      setGroupParticipationSnapshot(resource.value);
      setGroupParticipationEtag(resource.etag);
      setGroupParticipationStatus("loaded");
    } catch (err) {
      setGroupParticipationPolicy(null);
      setGroupParticipationSnapshot(null);
      setGroupParticipationEtag(null);
      setGroupParticipationStatus("error");
      setGroupParticipationError(err instanceof Error ? err.message : "读取群参与总开关失败");
    }
  }, [config, effectiveActivitySessionId]);

const loadSessionState = useCallback(async () => {
    if (!effectiveSessionId) {
      setSessionStateSnapshot(null);
      setSessionStateEtag(null);
      setSessionStateStatus("idle");
      return;
    }
    setSessionStateStatus("loading");
    try {
      const resource = await apiVersionedResource<SessionStateSnapshot>(
        config,
        `/plugins/wxbot/admin/session-state/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}`,
        { auth: true },
      );
      if (!resource.etag) {
        throw new Error("服务器未返回会话状态版本，已禁止覆盖操作");
      }
      const result = resource.value;
      setSessionStateSnapshot(result);
      setSessionStateEtag(resource.etag);
      setSessionStateStatus("loaded");
    } catch (err) {
      setSessionStateSnapshot(null);
      setSessionStateEtag(null);
      setSessionStateStatus("error");
      setPolicyOutput(formatJson({ error: err instanceof Error ? err.message : "读取会话状态失败" }));
    }
  }, [config, effectiveSessionId]);

const setSessionAutoReplyEnabled = async (enabled: boolean) => {
    if (!effectiveSessionId) {
      setPolicyOutput(formatJson({ error: "请先选择会话" }));
      return;
    }
    if (!sessionStateEtag || !sessionStateSnapshot) {
      setPolicyOutput(formatJson({ error: "请先读取带版本的会话状态，再执行切换" }));
      return;
    }
    const body = { auto_reply_enabled: enabled };
    const intent = `wxbot:session-state:${config.tenantId}:${effectiveSessionId}:${sessionStateEtag}:${String(enabled)}`;
    setSessionStateStatus("saving");
    try {
      const resource = await apiVersionedResource<SessionStateSnapshot, typeof body>(
        config,
        `/plugins/wxbot/admin/session-state/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}`,
        {
          auth: true,
          method: "POST",
          ifMatch: sessionStateEtag,
          idempotencyKey: keyFor(intent),
          body,
        },
      );
      if (!resource.etag) {
        throw new Error("切换成功但服务器未返回新版本，请重新读取");
      }
      const result = resource.value;
      setSessionStateSnapshot(result);
      setSessionStateEtag(resource.etag);
      setSessionStateStatus("loaded");
      clearIdempotencyKey(intent);
      setPolicyOutput(formatJson(result));
    } catch (err) {
      setSessionStateStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setPolicyOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "会话状态已被其他操作者更新，本次操作未覆盖；请重新读取"
          : err instanceof Error ? err.message : "切换会话自动回复状态失败",
      }));
    }
  };

const saveReplyPolicy = async () => {
    if (!effectiveSessionId) {
      setPolicyOutput(formatJson({ error: "请先选择会话" }));
      return;
    }
    if (!policyEtag) {
      setPolicyOutput(formatJson({ error: "请先读取带版本的会话策略，再保存本地草稿" }));
      return;
    }
    const intent = `wxbot-session-policy:${config.tenantId}:${effectiveSessionId}:${policyEtag}`;
    try {
      const resource = await apiVersionedResource<ReplyPolicy, {
        reply_mode: string;
        mention_sender_mode: string;
        trigger_keywords_text: string;
        participation_policy?: GroupParticipationPolicy;
      }>(
        config,
        `/plugins/wxbot/admin/reply-policy/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}`,
        {
          auth: true,
          method: "POST",
          ifMatch: policyEtag,
          idempotencyKey: keyFor(intent),
          body: {
            reply_mode: replyMode,
            mention_sender_mode: mentionSenderMode,
            trigger_keywords_text: triggerKeywordsText,
            ...(effectiveSessionIsGroup ? { participation_policy: participationPolicy } : {}),
          },
        },
      );
      if (!resource.etag) {
        throw new Error("保存成功但服务器未返回新版本，请重新读取");
      }
      const result = resource.value;
      setReplyMode(normalizeSessionReplyMode(result.reply_mode, effectiveSessionIsGroup));
      setMentionSenderMode(result.mention_sender_mode || "inherit");
      setTriggerKeywordsText(result.trigger_keywords_text || "");
      setParticipationPolicy(normalizeGroupParticipationPolicy(result.participation_policy));
      setPolicySnapshot(result);
      setPolicyEtag(resource.etag);
      setPolicyConflict("");
      clearIdempotencyKey(intent);
      setPolicyOutput(formatJson(result));
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setPolicyConflict("session");
        setPolicyOutput(formatJson({
          error: "会话策略已被其他操作者更新，本地草稿已保留",
          recovery: "重新读取服务端版本，比较后再次保存",
          current_etag: err.serverEtag,
        }));
      } else {
        setPolicyOutput(formatJson({ error: err instanceof Error ? err.message : "保存回复策略失败" }));
      }
    }
  };

const saveGroupParticipationPolicy = async () => {
    if (!effectiveActivitySessionId || !groupParticipationPolicy || !groupParticipationEtag) {
      setGroupParticipationError("请先读取当前群参与开关及其版本");
      return;
    }
    const payload: GroupParticipationPolicyUpdate = {
      kill_switches: groupParticipationPolicy.kill_switches,
      policy: groupParticipationPolicy.policy,
      voice_profile: groupParticipationPolicy.voice_profile,
      change_reason: "updated from WeChat reply policy page",
    };
    const intent = `wxbot-group-participation:${config.tenantId}:${effectiveActivitySessionId}:${groupParticipationEtag}:${String(groupParticipationPolicy.kill_switches.group_enabled)}`;
    setGroupParticipationStatus("saving");
    setGroupParticipationError("");
    try {
      const resource = await apiVersionedResource<
        GroupParticipationPolicyDocument,
        GroupParticipationPolicyUpdate
      >(
        config,
        `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(effectiveActivitySessionId)}/participation-policy`,
        {
          auth: true,
          method: "PUT",
          ifMatch: groupParticipationEtag,
          idempotencyKey: keyFor(intent),
          body: payload,
        },
      );
      if (!resource.etag) {
        throw new Error("保存成功但服务器未返回群参与开关版本，请重新读取");
      }
      clearIdempotencyKey(intent);
      setGroupParticipationPolicy(resource.value);
      setGroupParticipationSnapshot(resource.value);
      setGroupParticipationEtag(resource.etag);
      setGroupParticipationStatus("loaded");
      setPolicyOutput(formatJson({
        ok: true,
        summary: resource.value.effective_enabled
          ? "当前群已允许参与；回复模式仍按本页配置执行"
          : "当前群仍被全局、租户或群开关阻断",
        group_participation: resource.value,
      }));
    } catch (err) {
      setGroupParticipationStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setGroupParticipationError(
        err instanceof VersionConflictError
          ? "群参与开关已被其他操作者更新，请重新读取后再保存"
          : err instanceof Error ? err.message : "保存群参与总开关失败",
      );
    }
  };

const setGroupParticipationEnabled = (enabled: boolean) => {
    setGroupParticipationPolicy((current) => (
      current
        ? {
            ...current,
            kill_switches: { ...current.kill_switches, group_enabled: enabled },
            effective_enabled: Boolean(
              current.kill_switches.global_enabled
                && current.kill_switches.tenant_enabled
                && enabled,
            ),
          }
        : current
    ));
  };

const loadGroupActivity = useCallback(async () => {
    if (!effectiveActivitySessionId) {
      setGroupActivityConfig(createDefaultGroupActivityConfig(config.tenantId, ""));
      setLoadedGroupActivityConfig(null);
      setGroupActivityEtag(null);
      setGroupActivityServerEtag(null);
      setGroupActivityStatus("idle");
      setGroupActivityDecision(null);
      setGroupActivityEvents([]);
      setGroupActivityFeedback("");
      return;
    }
    setGroupActivityBusy("load");
    setGroupActivityStatus("loading");
    setGroupActivityFeedback("");
    try {
      const [activityResource, eventResult] = await Promise.all([
        apiVersionedResource<Partial<GroupActivityConfig>>(
          config,
          `/plugins/group_activity/config/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveActivitySessionId)}`,
          { auth: true },
        ),
        apiRequest<{ items?: GroupActivityEvent[] }>(
          config,
          `/plugins/group_activity/events/${encodeURIComponent(config.tenantId)}`,
          {
            auth: true,
            query: { session_id: effectiveActivitySessionId, limit: 8 },
          },
        ),
      ]);
      const activityConfig = normalizeGroupActivityConfig(
        activityResource.value,
        config.tenantId,
        effectiveActivitySessionId,
      );
      setGroupActivityConfig(activityConfig);
      setLoadedGroupActivityConfig(activityConfig);
      setGroupActivityEtag(activityResource.etag);
      setGroupActivityServerEtag(null);
      setGroupActivityStatus("loaded");
      setGroupActivityEvents(eventResult.items || []);
    } catch (err) {
      setGroupActivityStatus("error");
      setGroupActivityFeedback(err instanceof Error ? err.message : "读取自动暖场配置失败");
    } finally {
      setGroupActivityBusy("");
    }
  }, [config, effectiveActivitySessionId]);

const saveGroupActivity = async () => {
    if (!effectiveActivitySessionId) {
      setGroupActivityFeedback("请先选择群会话");
      return;
    }
    if (!groupActivityLoadedForScope || !groupActivityEtag) {
      setGroupActivityStatus("error");
      setGroupActivityFeedback("暖场配置尚未成功读取，已阻止覆盖服务器数据");
      return;
    }
    const validationError = groupActivityValidationError(groupActivityConfig);
    if (validationError) {
      setGroupActivityFeedback(validationError);
      return;
    }
    setGroupActivityBusy("save");
    setGroupActivityStatus("saving");
    setGroupActivityFeedback("");
    try {
      const result = await apiVersionedResource<Partial<GroupActivityConfig>, {
        session_name: string;
        enabled: boolean;
        active_start: string;
        active_end: string;
        quiet_start: string;
        quiet_end: string;
        timezone: string;
        idle_minutes: number;
        lookback_minutes: number;
        min_send_interval_minutes: number;
        max_per_day: number;
        topic_repeat_window_minutes: number;
        llm_model_tier: string;
        temperature: number;
        agent_tool_scope: string;
      }>(
        config,
        `/plugins/group_activity/config/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveActivitySessionId)}`,
        {
          auth: true,
          method: "POST",
          ifMatch: groupActivityEtag,
          body: {
            session_name: effectiveActivitySessionName || effectiveActivitySessionId,
            enabled: groupActivityConfig.enabled,
            active_start: groupActivityConfig.active_start,
            active_end: groupActivityConfig.active_end,
            quiet_start: groupActivityConfig.quiet_start,
            quiet_end: groupActivityConfig.quiet_end,
            timezone: groupActivityConfig.timezone,
            idle_minutes: groupActivityConfig.idle_minutes,
            lookback_minutes: groupActivityConfig.lookback_minutes,
            min_send_interval_minutes: groupActivityConfig.min_send_interval_minutes,
            max_per_day: groupActivityConfig.max_per_day,
            topic_repeat_window_minutes: groupActivityConfig.topic_repeat_window_minutes,
            llm_model_tier: groupActivityConfig.llm_model_tier,
            temperature: groupActivityConfig.temperature,
            agent_tool_scope: groupActivityConfig.agent_tool_scope,
          },
        },
      );
      const savedConfig = normalizeGroupActivityConfig(
        result.value,
        config.tenantId,
        effectiveActivitySessionId,
      );
      setGroupActivityConfig(savedConfig);
      setLoadedGroupActivityConfig(savedConfig);
      setGroupActivityEtag(result.etag);
      setGroupActivityServerEtag(null);
      setGroupActivityStatus("loaded");
      setGroupActivityFeedback("自动暖场配置已保存；dry-run 会按这份配置检查，但不会发消息。");
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setGroupActivityServerEtag(err.serverEtag);
        setGroupActivityStatus("conflict");
      } else {
        setGroupActivityStatus("error");
      }
      setGroupActivityFeedback(err instanceof Error ? err.message : "保存自动暖场配置失败");
    } finally {
      setGroupActivityBusy("");
    }
  };

const discardGroupActivityDraft = () => {
    if (!loadedGroupActivityConfig || !groupActivityLoadedForScope) {
      return;
    }
    setGroupActivityConfig(loadedGroupActivityConfig);
    setGroupActivityServerEtag(null);
    setGroupActivityStatus("loaded");
    setGroupActivityFeedback("已放弃未保存的暖场修改。");
  };

const runGroupActivityDryRun = async () => {
    if (!effectiveActivitySessionId) {
      setGroupActivityFeedback("请先选择群会话");
      return;
    }
    setGroupActivityBusy("dry-run");
    setGroupActivityFeedback("");
    try {
      const result = await apiRequest<GroupActivityDecision>(
        config,
        `/plugins/group_activity/trigger/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveActivitySessionId)}`,
        {
          auth: true,
          init: {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ dry_run: true, force: false }),
          },
        },
      );
      setGroupActivityDecision(result);
      setGroupActivityFeedback("安全检查完成：没有向群里发送消息。");
    } catch (err) {
      setGroupActivityDecision(null);
      setGroupActivityFeedback(err instanceof Error ? err.message : "执行暖场 dry-run 失败");
    } finally {
      setGroupActivityBusy("");
    }
  };

useEffect(() => {
    setParticipationPolicy(normalizeGroupParticipationPolicy());
    setGroupParticipationPolicy(null);
    setGroupParticipationSnapshot(null);
    setGroupParticipationEtag(null);
    setGroupParticipationStatus("idle");
    setGroupParticipationError("");
    setGroupActivityConfig(createDefaultGroupActivityConfig(config.tenantId, effectiveActivitySessionId));
    setLoadedGroupActivityConfig(null);
    setGroupActivityEtag(null);
    setGroupActivityServerEtag(null);
    setGroupActivityStatus("idle");
    setGroupActivityDecision(null);
    setGroupActivityEvents([]);
    setGroupActivityFeedback("");
  }, [config.tenantId, effectiveActivitySessionId]);

useEffect(() => {
    if (config.adminToken) {
      void loadGlobalReplyPolicy();
    }
  }, [config.adminToken, loadGlobalReplyPolicy]);

useEffect(() => {
    if (config.adminToken && effectiveSessionId) {
      void loadReplyPolicy();
      void loadSessionState();
    }
  }, [config.adminToken, effectiveSessionId, loadReplyPolicy, loadSessionState]);

useEffect(() => {
    if (config.adminToken && effectiveActivitySessionId) {
      void loadGroupParticipationPolicy();
    }
  }, [config.adminToken, effectiveActivitySessionId, loadGroupParticipationPolicy]);

useEffect(() => {
    if (
      !config.adminToken
      || activeTab !== "policy"
      || !effectiveActivitySessionId
      || groupActivityDirtyRef.current
    ) {
      return;
    }
    void loadGroupActivity();
  }, [
    activeTab,
    config.adminToken,
    effectiveActivitySessionId,
    loadGroupActivity,
  ]);

const loadSdkTriggerDebug = useCallback(async () => {
    try {
      const result = await apiRequest<SdkTriggerDebugConfig>(
        config,
        "/plugins/wxbot/admin/sdk/debug/trigger-config",
        { auth: true },
      );
      setSdkTriggerDebug(result);
      setSdkGroupRequireAtMe(String(result.group_require_at_me ?? true));
    } catch (err) {
      setPolicyOutput(formatJson({ error: err instanceof Error ? err.message : "读取 SDK 触发调试配置失败" }));
    }
  }, [config]);

const applyAggregateSnapshot = (aggregate: ReplyPolicyAggregate) => {
    const globalResult = aggregate.global_policy;
    const sessionResult = aggregate.session_policy;
    const sdkResult = aggregate.sdk_gate;
    setGlobalPrivateReplyMode(globalResult.private_reply_mode || "all");
    setGlobalGroupReplyMode(normalizeGroupReplyMode(globalResult.group_reply_mode));
    setGlobalGroupReplyMentionSender(String(globalResult.group_reply_mention_sender ?? false));
    setGlobalTriggerKeywordsText(globalResult.trigger_keywords_text || "");
    setGlobalPolicySnapshot(globalResult);
    setGlobalPolicyEtag(`"${aggregate.versions.global}"`);
    setReplyMode(normalizeSessionReplyMode(sessionResult.reply_mode, true));
    setMentionSenderMode(sessionResult.mention_sender_mode || "inherit");
    setTriggerKeywordsText(sessionResult.trigger_keywords_text || "");
    setParticipationPolicy(normalizeGroupParticipationPolicy(sessionResult.participation_policy));
    setPolicySnapshot(sessionResult);
    setPolicyEtag(`"${aggregate.versions.session}"`);
    setSdkTriggerDebug(sdkResult);
    setSdkGroupRequireAtMe(String(sdkResult.group_require_at_me ?? true));
    setPolicyConflict("");
  };

const mutateReplyPolicyAggregate = async (
    mode: "preset" | "sdk",
    sdkRequireAtMe: boolean,
  ) => {
    if (!effectiveGroupSessionId) {
      setPolicyOutput(formatJson({ error: "聚合策略仅适用于已验证的群会话，请先选择群聊" }));
      return;
    }
    const path = "/plugins/wxbot/admin/reply-policy/aggregate";
    let intent = "";
    try {
      const current = await apiVersionedResource<ReplyPolicyAggregate>(config, path, {
        auth: true,
        query: {
          tenant_id: config.tenantId,
          session_id: effectiveGroupSessionId,
        },
      });
      if (!current.etag) {
        throw new Error("服务器未返回聚合策略版本，已禁止覆盖保存");
      }
      const currentValue = current.value;
      const globalPolicy = currentValue.global_policy;
      const sessionPolicy = currentValue.session_policy;
      const repeaterConfig = currentValue.repeater_config;
      const body = {
        tenant_id: config.tenantId,
        session_id: effectiveGroupSessionId,
        private_reply_mode: mode === "preset" ? "all" : globalPolicy.private_reply_mode,
        group_reply_mode: mode === "preset" ? "contains" : normalizeGroupReplyMode(globalPolicy.group_reply_mode),
        group_reply_mention_sender: mode === "preset" ? false : Boolean(globalPolicy.group_reply_mention_sender),
        trigger_keywords_text: mode === "preset" ? "" : (globalPolicy.trigger_keywords_text || ""),
        session_reply_mode: normalizeSessionReplyMode(sessionPolicy.reply_mode, true),
        session_mention_sender_mode: sessionPolicy.mention_sender_mode || "inherit",
        session_trigger_keywords_text: sessionPolicy.trigger_keywords_text || "",
        participation_policy: sessionPolicy.participation_policy
          ? normalizeGroupParticipationPolicy(sessionPolicy.participation_policy)
          : null,
        repeater_enabled: Boolean(repeaterConfig.enabled),
        repeater_cooldown_seconds: Math.max(1, Number(repeaterConfig.cooldown_seconds || 300)),
        sdk_group_require_at_me: sdkRequireAtMe,
      };
      intent = `wxbot-reply-policy-aggregate:${mode}:${config.tenantId}:${effectiveGroupSessionId}:${current.etag}:${JSON.stringify(body)}`;
      const resource = await apiVersionedResource<ReplyPolicyAggregate, typeof body>(
        config,
        path,
        {
          auth: true,
          method: "POST",
          ifMatch: current.etag,
          idempotencyKey: keyFor(intent),
          body,
        },
      );
      if (!resource.etag) {
        throw new Error("保存成功但服务器未返回新聚合版本，请重新读取");
      }
      applyAggregateSnapshot(resource.value);
      clearIdempotencyKey(intent);
      setPolicyOutput(formatJson({
        ok: true,
        preset: mode === "preset" ? "private_all_group_at_me" : undefined,
        summary: mode === "preset"
          ? "全局、会话、复读与 SDK 门禁已在一个事务中校验并保存"
          : "SDK 门禁已通过聚合策略事务保存",
        etag: resource.etag,
        aggregate: resource.value,
      }));
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setPolicyConflict("aggregate");
        setPolicyOutput(formatJson({
          error: "聚合策略已被其他操作者更新，本地草稿已保留",
          recovery: "重新读取服务端策略，比较后再次提交",
          current_etag: err.serverEtag,
        }));
      } else {
        setPolicyOutput(formatJson({
          error: err instanceof Error ? err.message : "保存聚合回复策略失败",
        }));
      }
    }
  };

const saveSdkTriggerDebug = async () => {
    await mutateReplyPolicyAggregate("sdk", sdkGroupRequireAtMe === "true");
  };

const applySimpleReplyPreset = async () => {
    await mutateReplyPolicyAggregate("preset", true);
  };

useEffect(() => {
    if (config.adminToken) {
      void loadSdkTriggerDebug();
    }
  }, [config.adminToken, loadSdkTriggerDebug]);

const loadReportSubscriptions = async () => {
    setReportSubscriptionsStatus("loading");
    try {
      const resource = await apiVersionedResource<{ subscriptions?: ReportSubscription[] }>(
        config,
        "/plugins/wxbot/admin/reports/subscriptions",
        { auth: true },
      );
      if (!resource.etag) {
        throw new Error("服务器未返回报表订阅版本，已禁止覆盖保存");
      }
      const result = resource.value;
      const items = result.subscriptions || [];
      setReportSubscriptions(items);
      setReportSubscriptionsEtag(resource.etag);
      setReportSubscriptionsStatus("loaded");
      const current = items.find((item) => item.session_id === effectiveGroupSessionId);
      if (current) {
        setReportDailyEnabled(String(Boolean(current.daily_enabled)));
        setReportMonthlyEnabled(String(Boolean(current.monthly_enabled)));
        setReportDailyHour(Number(current.daily_hour ?? 9));
        setReportMonthlyDay(Number(current.monthly_day ?? 1));
        setReportTz(current.tz || "Asia/Shanghai");
      }
      setReportOutput(formatJson(result));
    } catch (err) {
      setReportSubscriptionsEtag(null);
      setReportSubscriptionsStatus("error");
      setReportOutput(formatJson({ error: err instanceof Error ? err.message : "读取日报周报月报订阅失败" }));
    }
  };

const loadSelfReviewSubscriptions = async () => {
    setSelfReviewSubscriptionsStatus("loading");
    try {
      const resource = await apiVersionedResource<{ subscriptions?: SelfReviewSubscription[] }>(
        config,
        "/plugins/wxbot/admin/self-review/subscriptions",
        { auth: true },
      );
      if (!resource.etag) {
        throw new Error("服务器未返回复盘订阅版本，已禁止覆盖保存");
      }
      const result = resource.value;
      const items = result.subscriptions || [];
      setSelfReviewSubscriptions(items);
      setSelfReviewSubscriptionsEtag(resource.etag);
      setSelfReviewSubscriptionsStatus("loaded");
      const current = items.find((item) => item.session_id === effectiveGroupSessionId);
      if (current) {
        setSelfReviewEnabled(String(Boolean(current.enabled)));
        setSelfReviewDailyHour(Number(current.daily_hour ?? 23));
        setSelfReviewTz(current.tz || "Asia/Shanghai");
      }
      setSelfReviewOutput(formatJson(result));
    } catch (err) {
      setSelfReviewSubscriptionsEtag(null);
      setSelfReviewSubscriptionsStatus("error");
      setSelfReviewOutput(formatJson({ error: err instanceof Error ? err.message : "读取自我复盘订阅失败" }));
    }
  };

const saveSelfReviewSubscription = async () => {
    if (!effectiveGroupSessionId) {
      setSelfReviewOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    if (!selfReviewSubscriptionsEtag) {
      setSelfReviewOutput(formatJson({ error: "请先读取带版本的复盘订阅，再保存草稿" }));
      return;
    }
    const group = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    const body = {
      session_id: effectiveGroupSessionId,
      session_name: group?.session_name || effectiveGroupSessionId,
      enabled: selfReviewEnabled === "true",
      daily_hour: selfReviewDailyHour,
      tz: selfReviewTz,
      focus_mode: "bot_interactions",
      auto_create_kb_doc: false,
    };
    const intent = `wxbot:self-review-subscription:save:${selfReviewSubscriptionsEtag}:${JSON.stringify(body)}`;
    setSelfReviewSubscriptionsStatus("saving");
    try {
      const resource = await apiVersionedResource<unknown, typeof body>(config, "/plugins/wxbot/admin/self-review/subscriptions", {
        auth: true,
        method: "POST",
        ifMatch: selfReviewSubscriptionsEtag,
        idempotencyKey: keyFor(intent),
        body,
      });
      setSelfReviewSubscriptionsEtag(resource.etag);
      setSelfReviewSubscriptionsStatus("loaded");
      setSelfReviewOutput(formatJson(resource.value));
      await loadSelfReviewSubscriptions();
      await loadSelfReviewJobs();
      clearIdempotencyKey(intent);
    } catch (err) {
      setSelfReviewSubscriptionsStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setSelfReviewOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "复盘订阅已被其他操作者更新，本地草稿已保留"
          : err instanceof Error ? err.message : "保存自我复盘订阅失败",
      }));
    }
  };

const deleteSelfReviewSubscription = async () => {
    if (!effectiveGroupSessionId) {
      const error = new Error("请先选择群会话");
      setSelfReviewOutput(formatJson({ error: error.message }));
      throw error;
    }
    if (!selfReviewSubscriptionsEtag) {
      const error = new Error("请先读取带版本的复盘订阅，再删除");
      setSelfReviewOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `wxbot:self-review-subscription:delete:${effectiveGroupSessionId}:${selfReviewSubscriptionsEtag}`;
    setSelfReviewSubscriptionsStatus("saving");
    try {
      const resource = await apiVersionedResource<unknown>(
        config,
        `/plugins/wxbot/admin/self-review/subscriptions/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          method: "DELETE",
          ifMatch: selfReviewSubscriptionsEtag,
          idempotencyKey: keyFor(intent),
        },
      );
      setSelfReviewPreview(null);
      setSelfReviewSubscriptionsEtag(resource.etag);
      setSelfReviewSubscriptionsStatus("loaded");
      setSelfReviewOutput(formatJson(resource.value));
      await loadSelfReviewSubscriptions();
      await loadSelfReviewJobs();
      clearIdempotencyKey(intent);
    } catch (err) {
      setSelfReviewSubscriptionsStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setSelfReviewOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "复盘订阅已被其他操作者更新，删除未执行"
          : err instanceof Error ? err.message : "删除自我复盘订阅失败",
      }));
      throw err;
    }
  };

const loadSelfReviewJobs = async () => {
    try {
      const result = await apiRequest<{ items?: SelfReviewJob[] }>(
        config,
        "/plugins/wxbot/admin/self-review/jobs",
        {
          auth: true,
          query: {
            session_id: effectiveGroupSessionId,
            limit: 20,
          },
        },
      );
      setSelfReviewJobs(result.items || []);
      setSelfReviewOutput(formatJson(result));
    } catch (err) {
      setSelfReviewOutput(formatJson({ error: err instanceof Error ? err.message : "读取自我复盘任务失败" }));
    }
  };

const previewSelfReview = async () => {
    if (!effectiveGroupSessionId) {
      setSelfReviewOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    const group = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    try {
      const result = await apiRequest<SelfReviewPreview>(
        config,
        `/plugins/wxbot/admin/self-review/preview/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          query: {
            session_name: group?.session_name || effectiveGroupSessionId,
            date: selfReviewDate,
            auto_create_kb_doc: false,
          },
        },
      );
      setSelfReviewPreview(result);
      setSelfReviewOutput(formatJson(result));
      await loadSelfReviewJobs();
    } catch (err) {
      setSelfReviewOutput(formatJson({ error: err instanceof Error ? err.message : "生成自我复盘失败" }));
    }
  };

const publishSelfReviewJob = async (jobId: number) => {
    if (!jobId || selfReviewPublishingJobId !== null) {
      return;
    }
    const intent = `wxbot:self-review:publish:${jobId}`;
    setSelfReviewPublishingJobId(jobId);
    try {
      const result = await apiRequest<SelfReviewPublishResult>(
        config,
        `/plugins/wxbot/admin/self-review/jobs/${jobId}/publish`,
        {
          auth: true,
          init: {
            method: "POST",
            headers: { "Idempotency-Key": keyFor(intent) },
          },
        },
      );
      setSelfReviewPreview((current) => (
        current?.job_id === jobId
          ? {
              ...current,
              kb_doc_id: result.kb_doc_id,
              kb_doc_title: result.kb_doc_title,
              kb_publish_status: result.kb_publish_status,
            }
          : current
      ));
      setSelfReviewJobs((current) => current.map((item) => (
        item.id === jobId
          ? {
              ...item,
              kb_doc_id: result.kb_doc_id,
              kb_doc_title: result.kb_doc_title,
              kb_publish_status: result.kb_publish_status,
              review_payload: {
                ...item.review_payload,
                kb_doc_id: result.kb_doc_id,
                kb_doc_title: result.kb_doc_title,
                kb_publish_status: result.kb_publish_status,
              },
            }
          : item
      )));
      setSelfReviewOutput(formatJson(result));
      clearIdempotencyKey(intent);
    } catch (err) {
      setSelfReviewOutput(formatJson({ error: err instanceof Error ? err.message : "发布复盘草稿失败" }));
      throw err;
    } finally {
      setSelfReviewPublishingJobId(null);
    }
  };

const saveReportSubscription = async () => {
    if (!effectiveGroupSessionId) {
      setReportOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    if (!reportSubscriptionsEtag) {
      setReportOutput(formatJson({ error: "请先读取带版本的报表订阅，再保存草稿" }));
      return;
    }
    const group = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    const body = {
      session_id: effectiveGroupSessionId,
      session_name: group?.session_name || effectiveGroupSessionId,
      daily_enabled: reportDailyEnabled === "true",
      weekly_enabled: reportWeeklyEnabled === "true",
      monthly_enabled: reportMonthlyEnabled === "true",
      daily_hour: reportDailyHour,
      weekly_day: reportWeeklyDay,
      weekly_hour: reportWeeklyHour,
      monthly_day: reportMonthlyDay,
      tz: reportTz,
    };
    const intent = `wxbot:report-subscription:save:${reportSubscriptionsEtag}:${JSON.stringify(body)}`;
    setReportSubscriptionsStatus("saving");
    try {
      const resource = await apiVersionedResource<unknown, typeof body>(config, "/plugins/wxbot/admin/reports/subscriptions", {
        auth: true,
        method: "POST",
        ifMatch: reportSubscriptionsEtag,
        idempotencyKey: keyFor(intent),
        body,
      });
      setReportSubscriptionsEtag(resource.etag);
      setReportSubscriptionsStatus("loaded");
      setReportOutput(formatJson(resource.value));
      await loadReportSubscriptions();
      clearIdempotencyKey(intent);
    } catch (err) {
      setReportSubscriptionsStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setReportOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "报表订阅已被其他操作者更新，本地草稿已保留"
          : err instanceof Error ? err.message : "保存日报周报月报订阅失败",
      }));
    }
  };

const deleteReportSubscription = async () => {
    if (!effectiveGroupSessionId) {
      const error = new Error("请先选择群会话");
      setReportOutput(formatJson({ error: error.message }));
      throw error;
    }
    if (!reportSubscriptionsEtag) {
      const error = new Error("请先读取带版本的报表订阅，再删除");
      setReportOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `wxbot:report-subscription:delete:${effectiveGroupSessionId}:${reportSubscriptionsEtag}`;
    setReportSubscriptionsStatus("saving");
    try {
      const resource = await apiVersionedResource<unknown>(
        config,
        `/plugins/wxbot/admin/reports/subscriptions/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          method: "DELETE",
          ifMatch: reportSubscriptionsEtag,
          idempotencyKey: keyFor(intent),
        },
      );
      setReportPreview(null);
      setReportSubscriptionsEtag(resource.etag);
      setReportSubscriptionsStatus("loaded");
      setReportOutput(formatJson(resource.value));
      await loadReportSubscriptions();
      clearIdempotencyKey(intent);
    } catch (err) {
      setReportSubscriptionsStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setReportOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "报表订阅已被其他操作者更新，删除未执行"
          : err instanceof Error ? err.message : "删除日报周报月报订阅失败",
      }));
      throw err;
    }
  };

const previewReport = async (reportType: "daily" | "weekly" | "monthly") => {
    if (!effectiveGroupSessionId) {
      setReportOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    const group = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    try {
      const result = await apiRequest<ReportPreview>(
        config,
        `/plugins/wxbot/admin/reports/preview/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          query: {
            report_type: reportType,
            session_name: group?.session_name || effectiveGroupSessionId,
            date: reportType === "daily" || reportType === "weekly" ? reportDate : "",
            year_month: reportType === "monthly" ? reportYearMonth : "",
          },
        },
      );
      setReportPreviewType(reportType);
      setReportPreview(result);
      setReportOutput(formatJson(result));
    } catch (err) {
      setReportOutput(formatJson({ error: err instanceof Error ? err.message : "预览报告失败" }));
    }
  };

const sendReport = async () => {
    if (!effectiveGroupSessionId) {
      const error = new Error("请先选择群会话");
      setReportOutput(formatJson({ error: error.message }));
      throw error;
    }
    const group = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    const intent = `wxbot:report:send:${effectiveGroupSessionId}:${reportPreviewType}:${reportDate}:${reportYearMonth}`;
    try {
      const result = await apiRequest(config, "/plugins/wxbot/admin/reports/send", {
        auth: true,
        init: {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": keyFor(intent),
          },
          body: JSON.stringify({
            session_id: effectiveGroupSessionId,
            session_name: group?.session_name || effectiveGroupSessionId,
            report_type: reportPreviewType,
            date: reportPreviewType === "daily" || reportPreviewType === "weekly" ? reportDate : "",
            year_month: reportPreviewType === "monthly" ? reportYearMonth : "",
          }),
        },
      });
      setReportOutput(formatJson(result));
      clearIdempotencyKey(intent);
    } catch (err) {
      setReportOutput(formatJson({ error: err instanceof Error ? err.message : "发送报告失败" }));
      throw err;
    }
  };

const loadReportMessages = async (reportType: "daily") => {
    if (!effectiveGroupSessionId) {
      setReportOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    const group = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    try {
      const result = await apiRequest<ReportMessagesPayload>(
        config,
        `/plugins/wxbot/admin/reports/messages/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          query: {
            report_type: reportType,
            session_name: group?.session_name || effectiveGroupSessionId,
            date: reportDate,
            year_month: "",
          },
        },
      );
      setReportPreviewType(reportType);
      setReportMessages(result);
      setReportOutput(formatJson(result));
    } catch (err) {
      setReportOutput(formatJson({ error: err instanceof Error ? err.message : "读取原始聊天记录失败" }));
    }
  };

useEffect(() => {
    if (!config.adminToken || !reportPreview || !effectiveGroupSessionId) {
      return undefined;
    }
    if (!["pending", "running"].includes(String(reportPreview.status || ""))) {
      return undefined;
    }
    const group = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    const timer = window.setInterval(() => {
      void apiRequest<ReportPreview>(
        config,
        `/plugins/wxbot/admin/reports/preview/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          query: {
            report_type: reportPreviewType,
            session_name: group?.session_name || effectiveGroupSessionId,
            date: reportPreviewType === "daily" || reportPreviewType === "weekly" ? reportDate : "",
            year_month: reportPreviewType === "monthly" ? reportYearMonth : "",
          },
        },
      )
        .then((result) => {
          setReportPreview(result);
          setReportOutput(formatJson(result));
        })
        .catch((err) => {
          setReportOutput(formatJson({ error: err instanceof Error ? err.message : "读取报告状态失败" }));
        });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [config, effectiveGroupSessionId, groupSessions, reportDate, reportPreview, reportPreviewType, reportYearMonth]);

useEffect(() => {
    if (!config.adminToken || activeTab !== "reports") {
      return;
    }
    void loadReportSubscriptions();
    void loadSelfReviewSubscriptions();
    void loadSelfReviewJobs();
  }, [activeTab, config.adminToken, effectiveGroupSessionId]);

useEffect(() => {
    if (!config.adminToken || !selfReviewPreview || !effectiveGroupSessionId) {
      return undefined;
    }
    if (!["pending", "running"].includes(String(selfReviewPreview.status || ""))) {
      return undefined;
    }
    const group = groupSessions.find((item) => item.session_id === effectiveGroupSessionId);
    const timer = window.setInterval(() => {
      void apiRequest<SelfReviewPreview>(
        config,
        `/plugins/wxbot/admin/self-review/preview/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          query: {
            session_name: group?.session_name || effectiveGroupSessionId,
            date: selfReviewDate,
            auto_create_kb_doc: false,
          },
        },
      )
        .then((result) => {
          setSelfReviewPreview(result);
          setSelfReviewOutput(formatJson(result));
          void loadSelfReviewJobs();
        })
        .catch((err) => {
          setSelfReviewOutput(formatJson({ error: err instanceof Error ? err.message : "读取自我复盘状态失败" }));
        });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [config, effectiveGroupSessionId, groupSessions, selfReviewDate, selfReviewPreview]);

const selectedGroupSubscription = reportSubscriptions.find((item) => item.session_id === effectiveGroupSessionId) || null;

const selectedSelfReviewSubscription = selfReviewSubscriptions.find((item) => item.session_id === effectiveGroupSessionId) || null;

const reportSubscriptionDirty = Boolean(
    reportSubscriptionsEtag
      && effectiveGroupSessionId
      && (
        reportDailyEnabled !== String(Boolean(selectedGroupSubscription?.daily_enabled ?? false))
        || reportWeeklyEnabled !== String(Boolean(selectedGroupSubscription?.weekly_enabled ?? true))
        || reportMonthlyEnabled !== String(Boolean(selectedGroupSubscription?.monthly_enabled ?? false))
        || reportDailyHour !== Number(selectedGroupSubscription?.daily_hour ?? 9)
        || reportWeeklyDay !== Number(selectedGroupSubscription?.weekly_day ?? 1)
        || reportWeeklyHour !== Number(selectedGroupSubscription?.weekly_hour ?? 9)
        || reportMonthlyDay !== Number(selectedGroupSubscription?.monthly_day ?? 1)
        || reportTz !== (selectedGroupSubscription?.tz || "Asia/Shanghai")
      ),
  );

const selfReviewSubscriptionDirty = Boolean(
    selfReviewSubscriptionsEtag
      && effectiveGroupSessionId
      && (
        selfReviewEnabled !== String(Boolean(selectedSelfReviewSubscription?.enabled ?? false))
        || selfReviewDailyHour !== Number(selectedSelfReviewSubscription?.daily_hour ?? 23)
        || selfReviewTz !== (selectedSelfReviewSubscription?.tz || "Asia/Shanghai")
      ),
  );

const adminConfigDirty = agentAdmin.agentToolPolicyDirty
    || eventAdmin.eventSubscriptionDirty
    || eventAdmin.groupSettingsDirty
    || reportSubscriptionDirty
    || selfReviewSubscriptionDirty;

const output = error
    ? formatJson({ error })
    : formatJson({ bridgeStatus, queueStats, sdkQueueStats, sessions, rosterGroups });

const bridgeSummaryText = bridgeStatus
    ? bridgeStatus.running
      ? "运行中"
      : "已停止"
    : error
      ? "无法连接"
      : "-";

const sdkSummaryText = bridgeStatus
    ? bridgeStatus.sdk_online
      ? "在线"
      : "离线"
    : error
      ? "未知"
      : "-";

const selectTab = (tab: WxbotTab) => {
    setActiveTab(tab);
    const params = new URLSearchParams(location.search);
    params.set("tab", tab);
    if (tab !== "reports") {
      params.delete("focus");
    }
    const nextSearch = params.toString();
    navigate({ pathname: location.pathname, search: nextSearch ? `?${nextSearch}` : "" });
  };

  return {
    ...agentAdmin,
    ...eventAdmin,
    ...queueAdmin,
    actionOutput,
    activeTab,
    adminConfigDirty,
    applySimpleReplyPreset,
    bridgeStatus,
    bridgeSummaryText,
    clearIdempotencyKey,
    chooseVerifiedGroup,
    config,
    deleteReportSubscription,
    deleteSelfReviewSubscription,
    discardGroupActivityDraft,
    effectiveActivitySessionId,
    effectiveActivitySessionName,
    effectiveGroupSession,
    effectiveGroupSessionId,
    effectiveGroupSessionName,
    effectiveSession,
    effectiveSessionId,
    effectiveSessionIsGroup,
    error,
    globalGroupReplyMentionSender,
    globalGroupReplyMode,
    globalPolicyDirty,
    globalPolicyEtag,
    globalPolicySnapshot,
    globalPrivateReplyMode,
    globalTriggerKeywordsText,
    groupActivityBusy,
    groupActivityConfig,
    groupActivityDecision,
    groupActivityDirty,
    groupActivityEtag,
    groupActivityEvents,
    groupActivityFeedback,
    groupActivityFormDisabled,
    groupActivityLoadedForScope,
    groupActivityServerEtag,
    groupActivityStatus,
    groupParticipationDirty,
    groupParticipationError,
    groupParticipationEtag,
    groupParticipationPolicy,
    groupParticipationStatus,
    groupSessionId,
    groupSessions,
    keyFor,
    loadGlobalReplyPolicy,
    loadGroupActivity,
    loadGroupParticipationPolicy,
    loadReplyPolicy,
    loadReportMessages,
    loadReportSubscriptions,
    loadSdkTriggerDebug,
    loadSelfReviewJobs,
    loadSelfReviewSubscriptions,
    loadSessionState,
    loading,
    location,
    mentionSenderMode,
    navigate,
    output,
    participationPolicy,
    policyConflict,
    policyEtag,
    policyOutput,
    policySnapshot,
    previewReport,
    previewSelfReview,
    publishSelfReviewJob,
    queueStats,
    refresh,
    replyMode,
    replyPolicyDirty,
    reportDailyEnabled,
    reportDailyHour,
    reportDate,
    reportMessages,
    reportMonthlyDay,
    reportMonthlyEnabled,
    reportOutput,
    reportPreview,
    reportPreviewType,
    reportSubscriptions,
    reportSubscriptionDirty,
    reportSubscriptionsEtag,
    reportSubscriptionsStatus,
    reportTz,
    reportWeeklyDay,
    reportWeeklyEnabled,
    reportWeeklyHour,
    reportYearMonth,
    rosterGroups,
    runGroupActivityDryRun,
    saveGlobalReplyPolicy,
    saveGroupActivity,
    saveGroupParticipationPolicy,
    saveReplyPolicy,
    saveReportSubscription,
    saveSdkTriggerDebug,
    saveSelfReviewSubscription,
    sdkGroupRequireAtMe,
    sdkGateDirty,
    sdkQueueStats,
    sdkSummaryText,
    sdkTriggerDebug,
    sessionPolicyDirty,
    selectVerifiedGroup,
    selectTab,
    selectedGroupSubscription,
    selectedSelfReviewSubscription,
    selfReviewDailyHour,
    selfReviewDate,
    selfReviewEnabled,
    selfReviewJobs,
    selfReviewOutput,
    selfReviewPreview,
    selfReviewPublishingJobId,
    selfReviewSubscriptions,
    selfReviewSubscriptionDirty,
    selfReviewSubscriptionsEtag,
    selfReviewSubscriptionsStatus,
    selfReviewTz,
    sendReport,
    sessionId,
    sessionName,
    sessionStateSnapshot,
    sessionStateEtag,
    sessionStateStatus,
    sessions,
    setActionOutput,
    setActiveTab,
    setBridgeStatus,
    setError,
    setGlobalGroupReplyMentionSender,
    setGlobalGroupReplyMode,
    setGlobalPolicySnapshot,
    setGlobalPrivateReplyMode,
    setGlobalTriggerKeywordsText,
    setGroupActivityBusy,
    setGroupActivityConfig,
    setGroupActivityDecision,
    setGroupActivityEvents,
    setGroupActivityFeedback,
    setGroupParticipationEnabled,
    setGroupSessionId,
    setLoading,
    setMentionSenderMode,
    setParticipationPolicy,
    setPolicyOutput,
    setPolicySnapshot,
    setQueueStats,
    setReplyMode,
    setReportDailyEnabled,
    setReportDailyHour,
    setReportDate,
    setReportMessages,
    setReportMonthlyDay,
    setReportMonthlyEnabled,
    setReportOutput,
    setReportPreview,
    setReportPreviewType,
    setReportSubscriptions,
    setReportTz,
    setReportWeeklyDay,
    setReportWeeklyEnabled,
    setReportWeeklyHour,
    setReportYearMonth,
    setRosterGroups,
    setSdkGroupRequireAtMe,
    setSdkQueueStats,
    setSdkTriggerDebug,
    setSelfReviewDailyHour,
    setSelfReviewDate,
    setSelfReviewEnabled,
    setSelfReviewJobs,
    setSelfReviewOutput,
    setSelfReviewPreview,
    setSelfReviewPublishingJobId,
    setSelfReviewSubscriptions,
    setSelfReviewTz,
    setSessionAutoReplyEnabled,
    setSessionId,
    setSessionName,
    setSessionStateSnapshot,
    setSessions,
    setTriggerKeywordsText,
    triggerKeywordsText,
    updateConfig,
    weeklyReportFocused,
    wxbotFocus,
  } as const;
}

export type WxbotPageController = ReturnType<typeof useWxbotPageController>;

function groupActivityEditableSnapshot(config: GroupActivityConfig) {
  return JSON.stringify({
    enabled: config.enabled,
    active_start: config.active_start,
    active_end: config.active_end,
    quiet_start: config.quiet_start,
    quiet_end: config.quiet_end,
    timezone: config.timezone,
    idle_minutes: config.idle_minutes,
    lookback_minutes: config.lookback_minutes,
    min_send_interval_minutes: config.min_send_interval_minutes,
    max_per_day: config.max_per_day,
    topic_repeat_window_minutes: config.topic_repeat_window_minutes,
    llm_model_tier: config.llm_model_tier,
    temperature: config.temperature,
    agent_tool_scope: config.agent_tool_scope,
  });
}

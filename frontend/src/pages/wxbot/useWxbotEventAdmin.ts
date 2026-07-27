import { useCallback, useEffect, useState } from "react";

import {
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  formatJson,
} from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import type {
  EventSubscription,
  GroupMemberSettings,
  MemberEvent,
  WxbotSession,
  WxbotTab,
} from "./model";

type EventAdminOptions = {
  activeTab: WxbotTab;
  clearIdempotencyKey: (intent: string) => void;
  config: ConsoleConfig;
  effectiveGroupSessionId: string;
  groupSessions: readonly WxbotSession[];
  keyFor: (intent: string) => string;
};

export function useWxbotEventAdmin({
  activeTab,
  clearIdempotencyKey,
  config,
  effectiveGroupSessionId,
  groupSessions,
  keyFor,
}: EventAdminOptions) {
  const [memberEvents, setMemberEvents] = useState<MemberEvent[]>([]);
  const [eventLimit, setEventLimit] = useState(20);
  const [eventTypeFilter, setEventTypeFilter] = useState("");
  const [eventSessionFilter, setEventSessionFilter] = useState("");
  const [subscriptionId, setSubscriptionId] = useState("");
  const [subscriptionEventType, setSubscriptionEventType] = useState("group.member.joined");
  const [subscriptionSessionId, setSubscriptionSessionId] = useState("");
  const [subscriptionTargetUrl, setSubscriptionTargetUrl] = useState("");
  const [subscriptionEnabled, setSubscriptionEnabled] = useState("true");
  const [subscriptions, setSubscriptions] = useState<EventSubscription[]>([]);
  const [subscriptionsEtag, setSubscriptionsEtag] = useState<string | null>(null);
  const [subscriptionsStatus, setSubscriptionsStatus] = useState<
    "idle" | "loading" | "loaded" | "saving" | "error" | "conflict"
  >("idle");
  const [welcomeEnabled, setWelcomeEnabled] = useState("false");
  const [welcomeTemplate, setWelcomeTemplate] = useState("欢迎 {{member_name}}");
  const [welcomeMention, setWelcomeMention] = useState("false");
  const [groupSettingsSnapshot, setGroupSettingsSnapshot] = useState<GroupMemberSettings | null>(null);
  const [groupSettingsEtag, setGroupSettingsEtag] = useState<string | null>(null);
  const [groupSettingsStatus, setGroupSettingsStatus] = useState<
    "idle" | "loading" | "loaded" | "saving" | "error" | "conflict"
  >("idle");
  const [eventOutput, setEventOutput] = useState('{\n  "status": "waiting"\n}');
  const [groupOutput, setGroupOutput] = useState('{\n  "status": "waiting"\n}');

  const loadMemberEvents = useCallback(async () => {
    try {
      const result = await apiRequest<{ events?: MemberEvent[]; count?: number }>(
        config,
        "/plugins/wxbot/admin/member-events",
        { auth: true, query: { tenant_id: config.tenantId, limit: eventLimit } },
      );
      setMemberEvents(result.events || []);
      setEventOutput(formatJson(result));
    } catch (err) {
      setEventOutput(formatJson({ error: err instanceof Error ? err.message : "读取成员事件失败" }));
    }
  }, [config, eventLimit]);

  useEffect(() => {
    if (config.adminToken) void loadMemberEvents();
  }, [config.adminToken, loadMemberEvents]);

  useEffect(() => {
    if (!config.adminToken || activeTab !== "events") return;
    void loadMemberEvents();
    const timer = window.setInterval(() => void loadMemberEvents(), 15000);
    return () => window.clearInterval(timer);
  }, [activeTab, config.adminToken, loadMemberEvents]);

  const loadSubscriptions = async () => {
    setSubscriptionsStatus("loading");
    try {
      const resource = await apiVersionedResource<{ items?: EventSubscription[]; count?: number }>(
        config,
        "/plugins/wxbot/admin/event-subscriptions",
        { auth: true },
      );
      if (!resource.etag) throw new Error("服务器未返回订阅版本，已禁止覆盖保存");
      const result = resource.value;
      setSubscriptions(result.items || []);
      setSubscriptionsEtag(resource.etag);
      setSubscriptionsStatus("loaded");
      setEventOutput(formatJson(result));
    } catch (err) {
      setSubscriptionsEtag(null);
      setSubscriptionsStatus("error");
      setEventOutput(formatJson({ error: err instanceof Error ? err.message : "读取 webhook 订阅失败" }));
    }
  };

  const saveSubscription = async () => {
    if (!subscriptionTargetUrl.trim()) {
      setEventOutput(formatJson({ error: "请填写 webhook URL" }));
      return;
    }
    if (!subscriptionSessionId.trim() || !groupSessions.some((item) => item.session_id === subscriptionSessionId.trim())) {
      setEventOutput(formatJson({ error: "请从已验证群列表明确选择订阅群" }));
      return;
    }
    if (!subscriptionsEtag) {
      setEventOutput(formatJson({ error: "请先读取带版本的订阅列表，再保存草稿" }));
      return;
    }
    const body = {
      event_type: subscriptionEventType,
      target_url: subscriptionTargetUrl.trim(),
      session_id: subscriptionSessionId.trim(),
      enabled: subscriptionEnabled === "true",
    };
    const intent = `wxbot:webhook-subscription:save:${subscriptionsEtag}:${JSON.stringify(body)}`;
    setSubscriptionsStatus("saving");
    try {
      const resource = await apiVersionedResource<unknown, typeof body>(
        config,
        "/plugins/wxbot/admin/event-subscriptions",
        {
          auth: true,
          method: "POST",
          ifMatch: subscriptionsEtag,
          idempotencyKey: keyFor(intent),
          body,
        },
      );
      setSubscriptionsEtag(resource.etag);
      setSubscriptionsStatus("loaded");
      setEventOutput(formatJson(resource.value));
      await loadSubscriptions();
      clearIdempotencyKey(intent);
    } catch (err) {
      setSubscriptionsStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setEventOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "订阅已被其他操作者更新，本地草稿已保留"
          : err instanceof Error ? err.message : "保存 webhook 订阅失败",
      }));
    }
  };

  const deleteSubscription = async () => {
    if (!subscriptionId.trim() || !subscriptionsEtag) {
      const error = new Error(
        !subscriptionId.trim() ? "请先选择要删除的 webhook 订阅" : "请先读取带版本的订阅列表，再删除",
      );
      setEventOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `wxbot:webhook-subscription:delete:${subscriptionId.trim()}:${subscriptionsEtag}`;
    setSubscriptionsStatus("saving");
    try {
      const resource = await apiVersionedResource<unknown>(
        config,
        `/plugins/wxbot/admin/event-subscriptions/${encodeURIComponent(subscriptionId.trim())}`,
        {
          auth: true,
          method: "DELETE",
          ifMatch: subscriptionsEtag,
          idempotencyKey: keyFor(intent),
        },
      );
      setSubscriptionsEtag(resource.etag);
      setSubscriptionsStatus("loaded");
      setEventOutput(formatJson(resource.value));
      await loadSubscriptions();
      clearIdempotencyKey(intent);
    } catch (err) {
      setSubscriptionsStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setEventOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "订阅已被其他操作者更新，删除未执行；本地选择已保留"
          : err instanceof Error ? err.message : "删除 webhook 订阅失败",
      }));
      throw err;
    }
  };

  const loadGroupSettings = async () => {
    if (!effectiveGroupSessionId) {
      setGroupOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    setGroupSettingsStatus("loading");
    try {
      const resource = await apiVersionedResource<GroupMemberSettings>(
        config,
        `/plugins/wxbot/admin/group-members/settings/${encodeURIComponent(effectiveGroupSessionId)}`,
        { auth: true },
      );
      if (!resource.etag) throw new Error("服务器未返回欢迎语版本，已禁止覆盖保存");
      const result = resource.value;
      setWelcomeEnabled(String(Boolean(result.welcome_enabled)));
      setWelcomeTemplate(result.welcome_template || "");
      setWelcomeMention(String(Boolean(result.welcome_mention)));
      setGroupSettingsSnapshot(result);
      setGroupSettingsEtag(resource.etag);
      setGroupSettingsStatus("loaded");
      setGroupOutput(formatJson(result));
    } catch (err) {
      setGroupSettingsEtag(null);
      setGroupSettingsStatus("error");
      setGroupOutput(formatJson({ error: err instanceof Error ? err.message : "读取欢迎语设置失败" }));
    }
  };

  const saveGroupSettings = async () => {
    if (!effectiveGroupSessionId || !groupSettingsEtag || !groupSettingsSnapshot) {
      setGroupOutput(formatJson({
        error: !effectiveGroupSessionId
          ? "请先选择群会话"
          : "请先读取带版本的欢迎语设置，再保存草稿",
      }));
      return;
    }
    const body = {
      welcome_enabled: welcomeEnabled === "true",
      welcome_template: welcomeTemplate,
      welcome_mention: welcomeMention === "true",
    };
    const intent = `wxbot:group-settings:${effectiveGroupSessionId}:${groupSettingsEtag}:${JSON.stringify(body)}`;
    setGroupSettingsStatus("saving");
    try {
      const resource = await apiVersionedResource<GroupMemberSettings, typeof body>(
        config,
        `/plugins/wxbot/admin/group-members/settings/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          method: "POST",
          ifMatch: groupSettingsEtag,
          idempotencyKey: keyFor(intent),
          body,
        },
      );
      if (!resource.etag) throw new Error("保存成功但服务器未返回新版本，请重新读取");
      setGroupSettingsSnapshot(resource.value);
      setGroupSettingsEtag(resource.etag);
      setGroupSettingsStatus("loaded");
      clearIdempotencyKey(intent);
      setGroupOutput(formatJson(resource.value));
    } catch (err) {
      setGroupSettingsStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setGroupOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "欢迎语设置已被其他操作者更新，本地草稿已保留"
          : err instanceof Error ? err.message : "保存欢迎语设置失败",
      }));
    }
  };

  const filteredEvents = memberEvents.filter((item) => {
    if (eventTypeFilter && item.event_type !== eventTypeFilter) return false;
    if (eventSessionFilter && item.session_id !== eventSessionFilter) return false;
    return true;
  });
  const selectedEventSubscription = subscriptions.find((item) => String(item.id) === subscriptionId) || null;
  const eventSubscriptionDirty = Boolean(
    subscriptionsEtag
      && (
        subscriptionEventType !== (selectedEventSubscription?.event_type || "group.member.joined")
        || subscriptionSessionId !== (selectedEventSubscription?.session_id || "")
        || subscriptionTargetUrl !== (selectedEventSubscription?.target_url || "")
        || subscriptionEnabled !== String(selectedEventSubscription?.enabled ?? true)
      ),
  );
  const groupSettingsDirty = Boolean(
    groupSettingsSnapshot
      && (
        welcomeEnabled !== String(Boolean(groupSettingsSnapshot.welcome_enabled))
        || welcomeTemplate !== (groupSettingsSnapshot.welcome_template || "")
        || welcomeMention !== String(Boolean(groupSettingsSnapshot.welcome_mention))
      ),
  );

  return {
    deleteSubscription,
    eventLimit,
    eventOutput,
    eventSessionFilter,
    eventSubscriptionDirty,
    eventTypeFilter,
    filteredEvents,
    groupOutput,
    groupSettingsDirty,
    groupSettingsEtag,
    groupSettingsSnapshot,
    groupSettingsStatus,
    loadGroupSettings,
    loadMemberEvents,
    loadSubscriptions,
    memberEvents,
    saveGroupSettings,
    saveSubscription,
    setEventLimit,
    setEventOutput,
    setEventSessionFilter,
    setEventTypeFilter,
    setGroupOutput,
    setMemberEvents,
    setSubscriptionEnabled,
    setSubscriptionEventType,
    setSubscriptionId,
    setSubscriptionSessionId,
    setSubscriptionTargetUrl,
    setSubscriptions,
    setWelcomeEnabled,
    setWelcomeMention,
    setWelcomeTemplate,
    subscriptionEnabled,
    subscriptionEventType,
    subscriptionId,
    subscriptionSessionId,
    subscriptionTargetUrl,
    subscriptions,
    subscriptionsEtag,
    subscriptionsStatus,
    welcomeEnabled,
    welcomeMention,
    welcomeTemplate,
  } as const;
}

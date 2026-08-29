import { useCallback, useEffect, useMemo, useState } from "react";

import { DangerAction } from "../components/DangerAction";
import { GroupScopeEmpty } from "../components/GroupScopeEmpty";
import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { UnsavedChangesGuard } from "../components/UnsavedChangesGuard";
import { TechnicalDetails } from "../components/TechnicalDetails";
import {
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  formatJson,
} from "../lib/api";
import { useStableIdempotencyKeys } from "../lib/idempotency";
import { useConsoleConfig } from "../state/console-config";

type ModerationConfig = {
  tenant_id: string;
  session_id: string;
  enabled: boolean;
  reminder_mode: string;
  reminder_text: string;
  webhook_url: string;
  webhook_enabled: boolean;
  version: number;
  updated_at?: string | null;
};

type ModerationConfigDraft = {
  enabled: string;
  reminderMode: string;
  reminderText: string;
  webhookUrl: string;
  webhookEnabled: string;
};

type KeywordResponse = {
  items?: ModerationKeyword[];
  count?: number;
  version?: number;
};

type ResourceStatus = "idle" | "loading" | "loaded" | "saving" | "error" | "conflict";

type ModerationKeyword = {
  id: number;
  keyword: string;
  enabled: boolean;
  created_at?: string | null;
};

type ModerationEvent = {
  id: number;
  tenant_id: string;
  session_id: string;
  session_name?: string;
  user_id: string;
  sender_name?: string;
  message_text: string;
  message_preview?: string;
  matched_keywords: string;
  matched_keyword_list?: string[];
  action: string;
  webhook_status?: string;
  trace_id?: string;
  created_at?: string | null;
};

type ModerationSessionSummary = {
  session_id: string;
  session_name: string;
  kind?: string;
  enabled: boolean;
  keyword_count: number;
  event_count: number;
  last_event_at?: string | null;
  updated_at?: string | null;
};

type WxbotSession = {
  session_id: string;
  session_name: string;
  kind?: string;
};

type GroupSessionPayload = {
  sessions?: WxbotSession[];
};

function isGroupSession(sessionId: string, kind?: string) {
  return sessionId.endsWith("@chatroom") || kind === "group" || kind === "chatroom";
}

function parseKeywordLines(value: string) {
  return Array.from(
    new Set(
      value
        .split(/\r?\n/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function formatTimestamp(value?: string | null) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

const RESOURCE_STATUS_LABELS: Record<ResourceStatus, string> = {
  idle: "等待读取",
  loading: "正在读取",
  loaded: "已同步",
  saving: "正在保存",
  error: "操作失败",
  conflict: "版本冲突",
};

function eventActionLabel(action?: string) {
  return ({
    flagged: "仅记录",
    reminder_append: "追加提醒",
    reminder_replace: "直接拦截",
  } as Record<string, string>)[action || ""] || "其他动作";
}

function webhookStatusLabel(status?: string) {
  return ({
    sent: "已发送",
    pending: "等待发送",
    "skipped:no_url": "未配置地址，已跳过",
  } as Record<string, string>)[status || ""] || (status ? "其他状态" : "-");
}

export function mergeSessions(
  moderationSessions: ModerationSessionSummary[],
  wxbotSessions: WxbotSession[],
) {
  const merged = new Map<string, ModerationSessionSummary>();
  for (const item of wxbotSessions) {
    if (!isGroupSession(item.session_id, item.kind)) {
      continue;
    }
    const moderation = moderationSessions.find((candidate) => candidate.session_id === item.session_id);
    const current = merged.get(item.session_id);
    merged.set(item.session_id, {
      session_id: item.session_id,
      session_name: item.session_name || moderation?.session_name || current?.session_name || item.session_id,
      kind: item.kind || moderation?.kind || current?.kind || "group",
      enabled: moderation?.enabled ?? current?.enabled ?? false,
      keyword_count: moderation?.keyword_count ?? current?.keyword_count ?? 0,
      event_count: moderation?.event_count ?? current?.event_count ?? 0,
      last_event_at: moderation?.last_event_at ?? current?.last_event_at ?? null,
      updated_at: moderation?.updated_at ?? current?.updated_at ?? null,
    });
  }
  return Array.from(merged.values())
    .sort((left, right) => {
      const leftTs = new Date(left.last_event_at || left.updated_at || 0).getTime();
      const rightTs = new Date(right.last_event_at || right.updated_at || 0).getTime();
      if (left.enabled !== right.enabled) {
        return left.enabled ? -1 : 1;
      }
      if (leftTs !== rightTs) {
        return rightTs - leftTs;
      }
      return (left.session_name || left.session_id).localeCompare(right.session_name || right.session_id, "zh-CN");
    });
}

export function ModerationPage() {
  const {
    config,
    verifiedGroupIds,
    registerVerifiedGroups,
  } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const basePath = "/plugins/moderation";
  const [sessionId, setSessionId] = useState(config.sessionId);
  const [sessions, setSessions] = useState<ModerationSessionSummary[]>([]);
  const [enabled, setEnabled] = useState("false");
  const [reminderMode, setReminderMode] = useState("off");
  const [reminderText, setReminderText] = useState("检测到命中审核关键词，请谨慎表述。");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [webhookEnabled, setWebhookEnabled] = useState("false");
  const [keywords, setKeywords] = useState<ModerationKeyword[]>([]);
  const [keywordDraft, setKeywordDraft] = useState("");
  const [quickKeyword, setQuickKeyword] = useState("");
  const [events, setEvents] = useState<ModerationEvent[]>([]);
  const [selectedEventId, setSelectedEventId] = useState<number | null>(null);
  const [eventAction, setEventAction] = useState("");
  const [eventWebhookStatus, setEventWebhookStatus] = useState("");
  const [eventKeyword, setEventKeyword] = useState("");
  const [eventLimit, setEventLimit] = useState(50);
  const [sessionOutput, setSessionOutput] = useState('{\n  "status": "waiting"\n}');
  const [configOutput, setConfigOutput] = useState('{\n  "status": "waiting"\n}');
  const [opsOutput, setOpsOutput] = useState('{\n  "status": "waiting"\n}');
  const [loadedConfig, setLoadedConfig] = useState<ModerationConfig | null>(null);
  const [loadedKeywordDraft, setLoadedKeywordDraft] = useState<string | null>(null);
  const [loadedKeywordScope, setLoadedKeywordScope] = useState("");
  const [configEtag, setConfigEtag] = useState<string | null>(null);
  const [keywordEtag, setKeywordEtag] = useState<string | null>(null);
  const [keywordVersion, setKeywordVersion] = useState<number | null>(null);
  const [configServerEtag, setConfigServerEtag] = useState<string | null>(null);
  const [keywordServerEtag, setKeywordServerEtag] = useState<string | null>(null);
  const [configStatus, setConfigStatus] = useState<ResourceStatus>("idle");
  const [keywordStatus, setKeywordStatus] = useState<ResourceStatus>("idle");

  const effectiveSessionId = sessionId.trim();
  const scopeReady = Boolean(effectiveSessionId && verifiedGroupIds.has(effectiveSessionId));
  const selectedEvent = useMemo(
    () => events.find((item) => item.id === selectedEventId) || null,
    [events, selectedEventId],
  );
  const resourceScope = `${config.tenantId}\u0000${effectiveSessionId}`;
  const configLoadedForScope = Boolean(
    loadedConfig
      && loadedConfig.tenant_id === config.tenantId
      && loadedConfig.session_id === effectiveSessionId,
  );
  const keywordsLoadedForScope = Boolean(
    loadedKeywordDraft !== null && loadedKeywordScope === resourceScope,
  );
  const scopedKeywords = keywordsLoadedForScope ? keywords : [];
  const enabledKeywordCount = scopedKeywords.filter((item) => item.enabled !== false).length;
  const currentConfigDraft: ModerationConfigDraft = {
    enabled,
    reminderMode,
    reminderText,
    webhookUrl,
    webhookEnabled,
  };
  const loadedConfigDraft: ModerationConfigDraft | null = configLoadedForScope && loadedConfig
    ? {
        enabled: String(Boolean(loadedConfig.enabled)),
        reminderMode: String(loadedConfig.reminder_mode || "off"),
        reminderText: String(loadedConfig.reminder_text || ""),
        webhookUrl: String(loadedConfig.webhook_url || ""),
        webhookEnabled: String(Boolean(loadedConfig.webhook_enabled)),
      }
    : null;
  const configDirty = Boolean(
    loadedConfigDraft
      && JSON.stringify(currentConfigDraft) !== JSON.stringify(loadedConfigDraft),
  );
  const keywordDirty = Boolean(
    keywordsLoadedForScope && loadedKeywordDraft !== null
      && JSON.stringify(parseKeywordLines(keywordDraft))
        !== JSON.stringify(parseKeywordLines(loadedKeywordDraft)),
  );
  const hasUnsavedChanges = configDirty || keywordDirty;
  const configFormDisabled = !scopeReady
    || !configLoadedForScope
    || configStatus === "loading"
    || configStatus === "saving";
  const keywordFormDisabled = !scopeReady
    || !keywordsLoadedForScope
    || keywordStatus === "loading"
    || keywordStatus === "saving";
  const loadSessions = useCallback(async () => {
    try {
      const roster = await apiRequest<GroupSessionPayload>(config, "/plugins/wxbot/admin/roster/groups", {
        auth: true,
      });
      const verifiedGroups = (roster.sessions || []).filter((item) =>
        isGroupSession(item.session_id, item.kind),
      );
      registerVerifiedGroups(verifiedGroups.map((item) => item.session_id));
      const nextSessions = mergeSessions(sessions, verifiedGroups);
      setSessions(nextSessions);
      if (
        effectiveSessionId
        && !hasUnsavedChanges
        && !nextSessions.some((item) => item.session_id === effectiveSessionId)
      ) {
        setSessionId("");
      }
      setSessionOutput(
        formatJson({
          source: "verified_roster",
          count: nextSessions.length,
        }),
      );
    } catch (err) {
      setSessionOutput(formatJson({ error: err instanceof Error ? err.message : "群列表读取失败" }));
    }
  }, [config, effectiveSessionId, hasUnsavedChanges, registerVerifiedGroups, sessions]);

  const loadConfig = useCallback(async () => {
    if (!scopeReady) {
      setConfigOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    setConfigStatus("loading");
    try {
      const result = await apiVersionedResource<ModerationConfig>(
        config,
        `${basePath}/config/${config.tenantId}/${encodeURIComponent(effectiveSessionId)}`,
      );
      setEnabled(String(Boolean(result.value.enabled)));
      setReminderMode(String(result.value.reminder_mode || "off"));
      setReminderText(String(result.value.reminder_text || "检测到命中审核关键词，请谨慎表述。"));
      setWebhookUrl(String(result.value.webhook_url || ""));
      setWebhookEnabled(String(Boolean(result.value.webhook_enabled)));
      setLoadedConfig(result.value);
      setConfigEtag(result.etag);
      setConfigServerEtag(null);
      setConfigStatus("loaded");
      setConfigOutput(formatJson(result.value));
    } catch (err) {
      setConfigStatus("error");
      setConfigOutput(formatJson({ error: err instanceof Error ? err.message : "读取配置失败" }));
    }
  }, [basePath, config, effectiveSessionId, scopeReady]);

  const loadKeywords = useCallback(async () => {
    if (!scopeReady) {
      setOpsOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    setKeywordStatus("loading");
    try {
      const result = await apiVersionedResource<KeywordResponse>(
        config,
        `${basePath}/keywords/${config.tenantId}/${encodeURIComponent(effectiveSessionId)}`,
      );
      const items = result.value.items || [];
      const nextDraft = items.map((item) => item.keyword).join("\n");
      setKeywords(items);
      setKeywordDraft(nextDraft);
      setLoadedKeywordDraft(nextDraft);
      setLoadedKeywordScope(resourceScope);
      setKeywordEtag(result.etag);
      setKeywordVersion(result.value.version ?? null);
      setKeywordServerEtag(null);
      setKeywordStatus("loaded");
      setOpsOutput(formatJson(result.value));
    } catch (err) {
      setKeywordStatus("error");
      setOpsOutput(formatJson({ error: err instanceof Error ? err.message : "关键词读取失败" }));
    }
  }, [basePath, config, effectiveSessionId, resourceScope, scopeReady]);

  const loadEvents = useCallback(async () => {
    const scopeSessionId = effectiveSessionId;
    if (!scopeReady) {
      setEvents([]);
      setSelectedEventId(null);
      setOpsOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    try {
      const result = await apiRequest<{ items?: ModerationEvent[] }>(
        config,
        `${basePath}/events/${config.tenantId}`,
        {
          query: {
            session_id: scopeSessionId,
            action: eventAction,
            webhook_status: eventWebhookStatus,
            keyword: eventKeyword,
            limit: eventLimit,
          },
        },
      );
      const items = result.items || [];
      setEvents(items);
      setSelectedEventId((current) => {
        if (current && items.some((item) => item.id === current)) {
          return current;
        }
        return items[0]?.id ?? null;
      });
      setOpsOutput(formatJson(result));
    } catch (err) {
      setEvents([]);
      setSelectedEventId(null);
      setOpsOutput(formatJson({ error: err instanceof Error ? err.message : "事件读取失败" }));
    }
  }, [basePath, config, effectiveSessionId, eventAction, eventKeyword, eventLimit, eventWebhookStatus, scopeReady]);

  const refreshSelected = useCallback(async () => {
    await Promise.all([loadConfig(), loadKeywords(), loadEvents(), loadSessions()]);
  }, [loadConfig, loadEvents, loadKeywords, loadSessions]);

  const saveConfig = async () => {
    if (!scopeReady) {
      setConfigOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    if (!configEtag || !loadedConfig || !configLoadedForScope) {
      setConfigStatus("error");
      setConfigOutput(formatJson({ error: "配置尚未成功读取，已阻止覆盖服务器数据" }));
      return;
    }
    const intent = `moderation:config:${config.tenantId}:${effectiveSessionId}:${configEtag}:${enabled}:${reminderMode}:${reminderText}:${webhookUrl}:${webhookEnabled}`;
    const siblingWasSynchronized = keywordEtag !== null && keywordEtag === configEtag;
    setConfigStatus("saving");
    try {
      const result = await apiVersionedResource<ModerationConfig, {
        enabled: boolean;
        reminder_mode: string;
        reminder_text: string;
        webhook_url: string;
        webhook_enabled: boolean;
      }>(
        config,
        `${basePath}/config/${config.tenantId}/${encodeURIComponent(effectiveSessionId)}`,
        {
          method: "POST",
          ifMatch: configEtag,
          idempotencyKey: keyFor(intent),
          body: {
            enabled: enabled === "true",
            reminder_mode: reminderMode,
            reminder_text: reminderText.trim(),
            webhook_url: webhookUrl.trim(),
            webhook_enabled: webhookEnabled === "true",
          },
        },
      );
      setEnabled(String(Boolean(result.value.enabled)));
      setReminderMode(String(result.value.reminder_mode || "off"));
      setReminderText(String(result.value.reminder_text || ""));
      setWebhookUrl(String(result.value.webhook_url || ""));
      setWebhookEnabled(String(Boolean(result.value.webhook_enabled)));
      setLoadedConfig(result.value);
      setConfigEtag(result.etag);
      setConfigServerEtag(null);
      setConfigStatus("loaded");
      if (siblingWasSynchronized) {
        setKeywordEtag(result.etag);
        setKeywordVersion(result.value.version);
      }
      setConfigOutput(formatJson(result.value));
      await loadSessions();
      clear(intent);
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setConfigServerEtag(err.serverEtag);
        setConfigStatus("conflict");
      } else {
        setConfigStatus("error");
      }
      setConfigOutput(formatJson({ error: err instanceof Error ? err.message : "保存配置失败" }));
    }
  };

  const replaceKeywords = async () => {
    if (!scopeReady) {
      setOpsOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    if (!keywordEtag || loadedKeywordDraft === null || !keywordsLoadedForScope) {
      setKeywordStatus("error");
      setOpsOutput(formatJson({ error: "关键词尚未成功读取，已阻止覆盖服务器数据" }));
      return;
    }
    const normalizedKeywords = parseKeywordLines(keywordDraft);
    const intent = `moderation:keywords:replace:${config.tenantId}:${effectiveSessionId}:${keywordEtag}:${normalizedKeywords.join("\u0000")}`;
    const siblingWasSynchronized = configEtag !== null && configEtag === keywordEtag;
    setKeywordStatus("saving");
    try {
      const payload = {
        keywords: normalizedKeywords,
        replace: true,
      };
      const result = await apiVersionedResource<KeywordResponse, typeof payload>(
        config,
        `${basePath}/keywords/${config.tenantId}/${encodeURIComponent(effectiveSessionId)}`,
        {
          method: "POST",
          ifMatch: keywordEtag,
          idempotencyKey: keyFor(intent),
          body: payload,
        },
      );
      const items = result.value.items || [];
      const nextDraft = items.map((item) => item.keyword).join("\n");
      setKeywords(items);
      setKeywordDraft(nextDraft);
      setLoadedKeywordDraft(nextDraft);
      setLoadedKeywordScope(resourceScope);
      setKeywordEtag(result.etag);
      setKeywordVersion(result.value.version ?? keywordVersion);
      setKeywordServerEtag(null);
      setKeywordStatus("loaded");
      if (siblingWasSynchronized && result.value.version !== undefined) {
        setConfigEtag(result.etag);
        setLoadedConfig((current) => current
          ? { ...current, version: result.value.version as number }
          : current);
      }
      setOpsOutput(formatJson(result.value));
      await loadSessions();
      clear(intent);
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setKeywordServerEtag(err.serverEtag);
        setKeywordStatus("conflict");
      } else {
        setKeywordStatus("error");
      }
      setOpsOutput(formatJson({ error: err instanceof Error ? err.message : "关键词保存失败" }));
    }
  };

  const appendQuickKeyword = async () => {
    if (!scopeReady) {
      setOpsOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    if (!quickKeyword.trim()) {
      setOpsOutput(formatJson({ error: "请先输入要追加的关键词" }));
      return;
    }
    if (!keywordEtag || !keywordsLoadedForScope || keywordDirty) {
      setOpsOutput(formatJson({ error: keywordDirty
        ? "请先保存或放弃批量关键词草稿"
        : "关键词尚未成功读取，已阻止覆盖服务器数据" }));
      return;
    }
    const normalizedKeyword = quickKeyword.trim();
    const intent = `moderation:keywords:add:${config.tenantId}:${effectiveSessionId}:${keywordEtag}:${normalizedKeyword}`;
    const siblingWasSynchronized = configEtag !== null && configEtag === keywordEtag;
    setKeywordStatus("saving");
    try {
      const result = await apiVersionedResource<KeywordResponse, { keyword: string }>(
        config,
        `${basePath}/keywords/${config.tenantId}/${encodeURIComponent(effectiveSessionId)}`,
        {
          method: "POST",
          ifMatch: keywordEtag,
          idempotencyKey: keyFor(intent),
          body: { keyword: normalizedKeyword },
        },
      );
      const items = result.value.items || [];
      const nextDraft = items.map((item) => item.keyword).join("\n");
      setQuickKeyword("");
      setKeywords(items);
      setKeywordDraft(nextDraft);
      setLoadedKeywordDraft(nextDraft);
      setLoadedKeywordScope(resourceScope);
      setKeywordEtag(result.etag);
      setKeywordVersion(result.value.version ?? keywordVersion);
      setKeywordServerEtag(null);
      setKeywordStatus("loaded");
      if (siblingWasSynchronized && result.value.version !== undefined) {
        setConfigEtag(result.etag);
        setLoadedConfig((current) => current
          ? { ...current, version: result.value.version as number }
          : current);
      }
      setOpsOutput(formatJson(result.value));
      await loadSessions();
      clear(intent);
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setKeywordServerEtag(err.serverEtag);
        setKeywordStatus("conflict");
      } else {
        setKeywordStatus("error");
      }
      setOpsOutput(formatJson({ error: err instanceof Error ? err.message : "关键词追加失败" }));
    }
  };

  const clearKeywords = async () => {
    if (!scopeReady) {
      setOpsOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    if (!keywordEtag || !keywordsLoadedForScope || keywordDirty) {
      const error = new Error(keywordDirty
        ? "请先保存或放弃批量关键词草稿"
        : "关键词尚未成功读取，已阻止覆盖服务器数据");
      setOpsOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `moderation:keywords:clear:${config.tenantId}:${effectiveSessionId}:${keywordEtag}`;
    const siblingWasSynchronized = configEtag !== null && configEtag === keywordEtag;
    setKeywordStatus("saving");
    try {
      const result = await apiVersionedResource<KeywordResponse, { clear_all: boolean }>(
        config,
        `${basePath}/keywords/${config.tenantId}/${encodeURIComponent(effectiveSessionId)}`,
        {
          method: "DELETE",
          ifMatch: keywordEtag,
          idempotencyKey: keyFor(intent),
          body: { clear_all: true },
        },
      );
      const items = result.value.items || [];
      const nextDraft = items.map((item) => item.keyword).join("\n");
      setKeywords(items);
      setKeywordDraft(nextDraft);
      setLoadedKeywordDraft(nextDraft);
      setLoadedKeywordScope(resourceScope);
      setKeywordEtag(result.etag);
      setKeywordVersion(result.value.version ?? keywordVersion);
      setKeywordServerEtag(null);
      setKeywordStatus("loaded");
      if (siblingWasSynchronized && result.value.version !== undefined) {
        setConfigEtag(result.etag);
        setLoadedConfig((current) => current
          ? { ...current, version: result.value.version as number }
          : current);
      }
      setOpsOutput(formatJson(result.value));
      await loadSessions();
      clear(intent);
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setKeywordServerEtag(err.serverEtag);
        setKeywordStatus("conflict");
      } else {
        setKeywordStatus("error");
      }
      setOpsOutput(formatJson({ error: err instanceof Error ? err.message : "清空关键词失败" }));
      throw err;
    }
  };

  const removeKeyword = async (value: string) => {
    if (!scopeReady) {
      setOpsOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    if (!keywordEtag || !keywordsLoadedForScope || keywordDirty) {
      const error = new Error(keywordDirty
        ? "请先保存或放弃批量关键词草稿"
        : "关键词尚未成功读取，已阻止覆盖服务器数据");
      setOpsOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `moderation:keywords:remove:${config.tenantId}:${effectiveSessionId}:${keywordEtag}:${value}`;
    const siblingWasSynchronized = configEtag !== null && configEtag === keywordEtag;
    setKeywordStatus("saving");
    try {
      const result = await apiVersionedResource<KeywordResponse>(
        config,
        `${basePath}/keywords/${config.tenantId}/${encodeURIComponent(effectiveSessionId)}`,
        {
          method: "DELETE",
          ifMatch: keywordEtag,
          idempotencyKey: keyFor(intent),
          query: { keyword: value },
        },
      );
      const items = result.value.items || [];
      const nextDraft = items.map((item) => item.keyword).join("\n");
      setKeywords(items);
      setKeywordDraft(nextDraft);
      setLoadedKeywordDraft(nextDraft);
      setLoadedKeywordScope(resourceScope);
      setKeywordEtag(result.etag);
      setKeywordVersion(result.value.version ?? keywordVersion);
      setKeywordServerEtag(null);
      setKeywordStatus("loaded");
      if (siblingWasSynchronized && result.value.version !== undefined) {
        setConfigEtag(result.etag);
        setLoadedConfig((current) => current
          ? { ...current, version: result.value.version as number }
          : current);
      }
      setOpsOutput(formatJson(result.value));
      await loadSessions();
      clear(intent);
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setKeywordServerEtag(err.serverEtag);
        setKeywordStatus("conflict");
      } else {
        setKeywordStatus("error");
      }
      setOpsOutput(formatJson({ error: err instanceof Error ? err.message : "删除关键词失败" }));
      throw err;
    }
  };

  const discardConfigDraft = useCallback(() => {
    if (!loadedConfig || !configLoadedForScope) {
      return;
    }
    setEnabled(String(Boolean(loadedConfig.enabled)));
    setReminderMode(String(loadedConfig.reminder_mode || "off"));
    setReminderText(String(loadedConfig.reminder_text || ""));
    setWebhookUrl(String(loadedConfig.webhook_url || ""));
    setWebhookEnabled(String(Boolean(loadedConfig.webhook_enabled)));
    setConfigServerEtag(null);
    setConfigStatus("loaded");
  }, [configLoadedForScope, loadedConfig]);

  const discardKeywordDraft = useCallback(() => {
    if (loadedKeywordDraft === null || !keywordsLoadedForScope) {
      return;
    }
    setKeywordDraft(loadedKeywordDraft);
    setKeywordServerEtag(null);
    setKeywordStatus("loaded");
  }, [keywordsLoadedForScope, loadedKeywordDraft]);

  useEffect(() => {
    const selected = config.sessionId.trim();
    if (
      !hasUnsavedChanges
      && sessionId !== selected
      && (!selected || verifiedGroupIds.has(selected))
    ) {
      setSessionId(selected);
    }
  }, [config.sessionId, hasUnsavedChanges, sessionId, verifiedGroupIds]);

  useEffect(() => {
    void loadSessions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.adminToken, config.apiBaseUrl, config.tenantId]);

  useEffect(() => {
    if (!scopeReady || hasUnsavedChanges) {
      return;
    }
    void Promise.all([loadConfig(), loadKeywords()]);
  }, [effectiveSessionId, hasUnsavedChanges, loadConfig, loadKeywords, scopeReady]);

  useEffect(() => {
    void loadEvents();
  }, [loadEvents]);

  if (!scopeReady) {
    return (
      <GroupScopeEmpty
        eyebrow="内容审核"
        title="审核配置与事件运营"
        description="按当前已验证群聊配置审核开关、提醒和关键词。群聊请在顶部会话条切换。"
      />
    );
  }

  return (
    <div className="page-grid moderation-page">
      <UnsavedChangesGuard when={hasUnsavedChanges} />
      <section className="panel span-3 moderation-overview-panel">
        <PageHeader
          eyebrow="内容审核"
          title="审核配置与事件运营"
          description="按当前群配置审核开关、提醒和关键词。命中后可只留痕、追加提醒或拦截回复。"
          actions={
            <div className="action-row">
              <button className="button button-secondary button-compact" onClick={() => void loadSessions()}>
                刷新群列表
              </button>
              <button
                className="button button-secondary button-compact"
                onClick={() => void refreshSelected()}
                disabled={!scopeReady || hasUnsavedChanges}
              >
                刷新当前群
              </button>
            </div>
          }
        />

        <div className="summary-grid page-hero-metrics">
          <div className="summary-card" data-status={sessions.length ? "ok" : "warning"}>
            <span>可管理群</span>
            <strong>{sessions.length}</strong>
          </div>
          <div className="summary-card" data-status={enabled === "true" ? "ok" : "warning"}>
            <span>当前群审核</span>
            <strong>{enabled === "true" ? "已开启" : "未开启"}</strong>
          </div>
          <div className="summary-card">
            <span>当前群关键词</span>
            <strong>{enabledKeywordCount}</strong>
          </div>
          <div className="summary-card">
            <span>当前事件视图</span>
            <strong>{events.length}</strong>
          </div>
        </div>
      </section>

      <section className="panel span-2 moderation-config-panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">审核配置</p>
            <h3>群级审核配置</h3>
          </div>
          <span className={`pill ${enabled === "true" ? "pill-ok" : "pill-muted"}`}>
            {enabled === "true" ? "已生效" : "未启用"}
          </span>
        </div>

        <div className="moderation-config-grid">
          <label className="field">
            <span>是否启用审核</span>
            <select
              value={enabled}
              onChange={(event) => setEnabled(event.target.value)}
              disabled={configFormDisabled}
            >
              <option value="true">开启</option>
              <option value="false">关闭</option>
            </select>
          </label>
          <label className="field">
            <span>命中后动作</span>
            <select
              value={reminderMode}
              onChange={(event) => setReminderMode(event.target.value)}
              disabled={configFormDisabled}
            >
              <option value="off">只记事件，不改回复</option>
              <option value="append">在回复后追加提醒</option>
              <option value="replace">直接拦截并返回提醒</option>
            </select>
          </label>
          <label className="field span-2">
            <span>提醒文案</span>
            <textarea
              rows={3}
              value={reminderText}
              onChange={(event) => setReminderText(event.target.value)}
              disabled={configFormDisabled}
              placeholder="检测到命中审核关键词，请谨慎表述。"
            />
          </label>
          <label className="field">
            <span>外部回调开关</span>
            <select
              value={webhookEnabled}
              onChange={(event) => setWebhookEnabled(event.target.value)}
              disabled={configFormDisabled}
            >
              <option value="false">关闭</option>
              <option value="true">开启</option>
            </select>
          </label>
          <label className="field">
            <span>外部回调地址</span>
            <input
              value={webhookUrl}
              onChange={(event) => setWebhookUrl(event.target.value)}
              disabled={configFormDisabled}
              placeholder="https://example.com/moderation/webhook"
            />
          </label>
        </div>
        <TechnicalDetails
          summary="查看外部回调字段"
          value={["session_id", "user_id", "message_text", "matched_keywords", "trace_id"]}
        />

        <div className="action-row">
          <button
            className="button button-secondary"
            onClick={() => void loadConfig()}
            disabled={!scopeReady || configDirty || configStatus === "loading" || configStatus === "saving"}
          >
            {configStatus === "loading" ? "读取中…" : "重新读取配置"}
          </button>
          {configDirty ? (
            <button className="button button-secondary" onClick={discardConfigDraft}>
              放弃配置草稿
            </button>
          ) : null}
          <button
            className="button button-primary"
            onClick={() => void saveConfig()}
            disabled={
              !scopeReady
              || !configLoadedForScope
              || !configDirty
              || configStatus === "loading"
              || configStatus === "saving"
            }
          >
            {configStatus === "saving" ? "保存中…" : "保存当前群配置"}
          </button>
        </div>
        <div className="route-list" aria-live="polite">
          <div>
            加载状态：<strong>{RESOURCE_STATUS_LABELS[configStatus]}</strong>
            {" · "}草稿：<strong>{configDirty ? "有未保存修改" : "已同步"}</strong>
          </div>
          <div>
            配置版本：<span>{configLoadedForScope ? loadedConfig?.version : "-"}</span>
          </div>
          <TechnicalDetails summary="查看配置版本令牌" value={configLoadedForScope ? configEtag || "-" : "尚未读取"} />
        </div>
        {configStatus === "conflict" ? (
          <div className="alert alert-warning" role="alert">
            <span className="alert-icon" aria-hidden="true">!</span>
            <div className="alert-content">
              <strong>审核配置已被其他操作者更新</strong>
              <div>
                本地草稿仍保留。重新加载只会覆盖审核配置表单，不会覆盖关键词草稿。
              </div>
              <TechnicalDetails summary="查看服务器版本令牌" value={configServerEtag || "未知"} />
              <button className="button button-secondary" onClick={() => void loadConfig()}>
                加载配置服务器版本（覆盖草稿）
              </button>
            </div>
          </div>
        ) : null}
      </section>

      <section className="panel moderation-keyword-panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">关键词</p>
            <h3>关键词管理</h3>
          </div>
          <span className="pill pill-feature">{enabledKeywordCount} 个启用词</span>
        </div>

        <label className="field">
          <span>批量维护</span>
          <textarea
            rows={10}
            value={keywordDraft}
            onChange={(event) => setKeywordDraft(event.target.value)}
            disabled={keywordFormDisabled}
            placeholder={"每行一个关键词\n保存时会整体替换当前群的词表"}
          />
        </label>

        <div className="action-row">
          <button
            className="button button-secondary"
            onClick={() => void loadKeywords()}
            disabled={!scopeReady || keywordDirty || keywordStatus === "loading" || keywordStatus === "saving"}
          >
            {keywordStatus === "loading" ? "读取中…" : "从服务端回填"}
          </button>
          {keywordDirty ? (
            <button className="button button-secondary" onClick={discardKeywordDraft}>
              放弃关键词草稿
            </button>
          ) : null}
          <button
            className="button button-primary"
            onClick={() => void replaceKeywords()}
            disabled={
              !scopeReady
              || !keywordsLoadedForScope
              || !keywordDirty
              || keywordStatus === "loading"
              || keywordStatus === "saving"
            }
          >
            {keywordStatus === "saving" ? "保存中…" : "整体替换保存"}
          </button>
          <DangerAction
            label="清空当前群关键词"
            title="确认清空全部审核关键词"
            impact={<p>将删除当前群的 {scopedKeywords.length} 个关键词；审核配置本身不会关闭。</p>}
            confirmLabel="确认清空"
            pendingLabel="正在清空…"
            disabled={
              !scopeReady
              || !keywordsLoadedForScope
              || !scopedKeywords.length
              || keywordDirty
              || keywordStatus === "loading"
              || keywordStatus === "saving"
            }
            onConfirm={clearKeywords}
          />
        </div>

        <div className="moderation-quick-add">
          <label className="field">
            <span>快速追加单个关键词</span>
            <input
              value={quickKeyword}
              onChange={(event) => setQuickKeyword(event.target.value)}
              disabled={keywordFormDisabled || keywordDirty}
              placeholder="例如：退款返现"
            />
          </label>
          <button
            className="button button-secondary"
            onClick={() => void appendQuickKeyword()}
            disabled={keywordFormDisabled || keywordDirty}
          >
            追加
          </button>
        </div>

        <div className="route-list" aria-live="polite">
          <div>
            加载状态：<strong>{RESOURCE_STATUS_LABELS[keywordStatus]}</strong>
            {" · "}草稿：<strong>{keywordDirty ? "有未保存修改" : "已同步"}</strong>
          </div>
          <div>
            关键词版本：<span>{keywordsLoadedForScope ? keywordVersion ?? "-" : "-"}</span>
          </div>
          <TechnicalDetails summary="查看关键词版本令牌" value={keywordsLoadedForScope ? keywordEtag || "-" : "尚未读取"} />
        </div>
        {keywordStatus === "conflict" ? (
          <div className="alert alert-warning" role="alert">
            <span className="alert-icon" aria-hidden="true">!</span>
            <div className="alert-content">
              <strong>关键词已被其他操作者更新</strong>
              <div>
                本地草稿仍保留。重新加载只会覆盖关键词表单，不会覆盖审核配置草稿。
              </div>
              <TechnicalDetails summary="查看服务器版本令牌" value={keywordServerEtag || "未知"} />
              <button className="button button-secondary" onClick={() => void loadKeywords()}>
                加载关键词服务器版本（覆盖草稿）
              </button>
            </div>
          </div>
        ) : null}

        <div className="table-scroll compact-table-scroll">
          <table>
            <caption className="sr-only">当前群审核关键词</caption>
            <thead>
              <tr>
                <th scope="col">关键词</th>
                <th scope="col">状态</th>
                <th scope="col">创建时间</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {scopedKeywords.map((item) => (
                <tr key={item.id}>
                  <td>{item.keyword}</td>
                  <td>
                    <span className={`pill ${item.enabled ? "pill-ok" : "pill-muted"}`}>
                      {item.enabled ? "已启用" : "已停用"}
                    </span>
                  </td>
                  <td>{formatTimestamp(item.created_at)}</td>
                  <td>
                    <DangerAction
                      label="删除"
                      title={`删除关键词“${item.keyword}”`}
                      impact={<p>删除后，该词将不再触发当前群的审核规则。</p>}
                      confirmLabel="确认删除"
                      pendingLabel="正在删除…"
                      disabled={keywordFormDisabled || keywordDirty}
                      className="button-danger-sm"
                      onConfirm={() => removeKeyword(item.keyword)}
                    />
                  </td>
                </tr>
              ))}
              {!scopedKeywords.length && (
                <tr>
                  <td colSpan={4} className="empty-cell">当前群还没有审核关键词</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel span-3 panel-scroll moderation-events-panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">审核事件</p>
            <h3>最近审核事件</h3>
          </div>
          <span className="pill pill-feature">当前 {events.length} 条</span>
        </div>

        <div className="moderation-event-filters">
          <label className="field">
            <span>动作</span>
            <select value={eventAction} onChange={(event) => setEventAction(event.target.value)}>
              <option value="">全部</option>
               <option value="flagged">仅记录</option>
               <option value="reminder_append">追加提醒</option>
               <option value="reminder_replace">直接拦截</option>
            </select>
          </label>
          <label className="field">
             <span>外部回调状态</span>
            <select value={eventWebhookStatus} onChange={(event) => setEventWebhookStatus(event.target.value)}>
              <option value="">全部</option>
               <option value="sent">已发送</option>
               <option value="pending">等待发送</option>
               <option value="skipped:no_url">未配置地址，已跳过</option>
            </select>
          </label>
          <label className="field">
            <span>关键词筛选</span>
            <input
              value={eventKeyword}
              onChange={(event) => setEventKeyword(event.target.value)}
              placeholder="输入关键词片段"
            />
          </label>
          <label className="field">
            <span>条数</span>
            <input
              type="number"
              min={1}
              max={200}
              value={eventLimit}
              onChange={(event) => setEventLimit(Number(event.target.value) || 50)}
            />
          </label>
          <div className="moderation-toolbar-actions">
            <button className="button button-secondary" onClick={() => void loadEvents()} disabled={!scopeReady}>
              刷新事件
            </button>
          </div>
        </div>

        <div
          className="table-scroll moderation-event-scroll"
          role="region"
          aria-label="当前群最近审核事件表格"
          tabIndex={0}
        >
          <table>
            <caption className="sr-only">当前群最近审核事件</caption>
            <thead>
              <tr>
                <th scope="col">时间</th>
                <th scope="col">群</th>
                <th scope="col">触发人</th>
                <th scope="col">动作</th>
                <th scope="col">关键词</th>
                 <th scope="col">外部回调</th>
                <th scope="col">消息摘要</th>
                 <th scope="col">追踪</th>
              </tr>
            </thead>
            <tbody>
              {events.map((item) => (
                <tr
                  key={item.id}
                  className={item.id === selectedEventId ? "table-row-active" : ""}
                >
                  <td>
                    <button
                      type="button"
                      className="data-table-row-action"
                      aria-pressed={item.id === selectedEventId}
                      onClick={() => setSelectedEventId(item.id)}
                    >
                      {formatTimestamp(item.created_at)}
                    </button>
                  </td>
                   <td>{item.session_name || "当前群聊"}</td>
                   <td>{item.sender_name || "未知成员"}</td>
                   <td>{eventActionLabel(item.action)}</td>
                  <td>{(item.matched_keyword_list || []).join("、") || item.matched_keywords || "-"}</td>
                   <td>{webhookStatusLabel(item.webhook_status)}</td>
                  <td>{item.message_preview || "内容已在列表中隐藏"}</td>
                   <td>{item.trace_id ? "可追踪" : "-"}</td>
                </tr>
              ))}
              {!events.length && (
                <tr>
                  <td colSpan={8} className="empty-cell">当前筛选条件下还没有审核事件</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel span-3">
        <OutputPanel flush title="群列表与索引响应" value={sessionOutput} />
        <OutputPanel flush title="审核配置响应" value={configOutput} />
        <OutputPanel flush title={selectedEvent ? `审核事件 #${selectedEvent.id}` : "审核操作响应"} value={selectedEvent ? formatJson(selectedEvent) : opsOutput} />
      </section>
    </div>
  );
}

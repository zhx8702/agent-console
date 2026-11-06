import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  ApiError,
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  formatJson,
} from "../../lib/api";
import type { useStableIdempotencyKeys } from "../../lib/idempotency";
import { useConsoleConfig } from "../../state/console-config";
import { useRequestEpoch } from "./useRequestEpoch";
import { queryPluginRuntime } from "./runtimeQuery";
import type { DrawRuntimeConfig } from "./DrawConfigSection";
import type {
  EffectAuditFilters,
  FlowEffectLogResponse,
  FlowEffectSummaryResponse,
  FlowEffectSummaryRow,
  FlowTraceSnapshotResponse,
  GroupPluginState,
  InstalledPlugin,
  InstalledPluginsResponse,
  MessageFlowRuntimeStatus,
  PluginEvent,
  PluginRuntime,
  PluginScopeState,
  PluginSummary,
  ReadyzFlowChecks,
  TraceAggregate,
  TraceReplyQueueItem,
  TraceStreamMessage,
  WxbotSession,
} from "./models";
import {
  emptyEffectSummary,
  summarizeEffectLogRows,
} from "./models";

export function usePluginsPageController(idempotencyKeys: ReturnType<typeof useStableIdempotencyKeys>) {
  const {
    config,
    registerVerifiedGroups,
    selectVerifiedGroup,
  } = useConsoleConfig();
  const beginRequest = useRequestEpoch();
  const [runtimeErrors, setRuntimeErrors] = useState<Record<string, string>>({});
  const [installedStatus, setInstalledStatus] = useState<"loading" | "ready" | "error">("loading");
  const [scopeReady, setScopeReady] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const [data, setData] = useState<PluginSummary | null>(null);
  const [installed, setInstalled] = useState<InstalledPlugin[]>([]);
  const [runtime, setRuntime] = useState<PluginRuntime>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [groups, setGroups] = useState<WxbotSession[]>([]);
  const [managedGroupSessionId, setManagedGroupSessionId] = useState(
    config.sessionId.endsWith("@chatroom") ? config.sessionId : "",
  );
  const [groupPluginState, setGroupPluginState] = useState<GroupPluginState>({});
  const [groupPluginVersions, setGroupPluginVersions] = useState<Record<string, number>>({});
  const [pluginEvents, setPluginEvents] = useState<PluginEvent[]>([]);
  const [groupOutput, setGroupOutput] = useState('{\n  "status": "waiting"\n}');
  const [flowStatus, setFlowStatus] = useState<MessageFlowRuntimeStatus | null>(null);
  const [readyzFlow, setReadyzFlow] = useState<ReadyzFlowChecks | null>(null);
  const [effectLog, setEffectLog] = useState<FlowEffectLogResponse | null>(null);
  const [effectSummary, setEffectSummary] = useState<FlowEffectSummaryResponse | null>(null);
  const [effectTraceFilter, setEffectTraceFilter] = useState(() => searchParams.get("trace_id")?.trim() || "");
  const [effectAuditFilters, setEffectAuditFilters] = useState<EffectAuditFilters>({});
  const [traceAggregate, setTraceAggregate] = useState<TraceAggregate | null>(null);
  const [traceAggregateLoading, setTraceAggregateLoading] = useState(false);
  const [traceAggregateError, setTraceAggregateError] = useState("");
  const [flowLoading, setFlowLoading] = useState(false);
  const [flowError, setFlowError] = useState("");
  const [restartRequired, setRestartRequired] = useState(false);
  const [drawConfig, setDrawConfig] = useState<DrawRuntimeConfig | null>(null);
  const [drawEtag, setDrawEtag] = useState("");
  const [drawApiUrl, setDrawApiUrl] = useState("");
  const [drawApiKey, setDrawApiKey] = useState("");
  const [drawApiModel, setDrawApiModel] = useState("");
  const [drawApiEditUrl, setDrawApiEditUrl] = useState("");
  const [drawLoading, setDrawLoading] = useState(false);
  const [drawSaving, setDrawSaving] = useState(false);
  const [drawError, setDrawError] = useState("");
  const [drawNotice, setDrawNotice] = useState("");
  const selectedPluginName = searchParams.get("plugin")?.trim() || "";

  const pluginCards: InstalledPlugin[] = installedStatus === "ready" ? installed : (data?.plugins || []).map((plugin) => ({
    ...plugin,
    enabled: null,
    system: false,
    status: "unknown",
    restart_required: false,
  }));
  const loadedPluginNames = new Set(pluginCards.map((plugin) => plugin.name));
  const groupScopedPlugins = pluginCards.filter(
    (plugin) => plugin.admin_ui?.scope === "group",
  );

  const loadSummary = async () => {
    const current = beginRequest("summary");
    beginRequest("runtime");
    beginRequest("scope");
    setLoading(true);
    setError("");
    setInstalledStatus("loading");
    setRuntime({});
    setRuntimeErrors({});
    setScopeReady(false);
    try {
      const [summaryResult, installedResult] = await Promise.allSettled([
        apiRequest<PluginSummary>(config, "/v1/admin/plugins/summary", { auth: true }),
        apiRequest<InstalledPluginsResponse>(config, "/v1/admin/plugins/installed", { auth: true }),
      ]);
      if (!current()) return;
      setData(summaryResult.status === "fulfilled" ? summaryResult.value : null);
      if (installedResult.status === "rejected" || !Array.isArray(installedResult.value?.plugins)) {
        setInstalled([]);
        setInstalledStatus("error");
        setError("插件状态查询失败，当前启用状态未知，请刷新重试。");
        return;
      }
      const plugins = installedResult.value.plugins;
      setInstalled(plugins);
      setInstalledStatus("ready");
      if (summaryResult.status === "rejected") setError("插件摘要查询失败");
      // Runtime diagnostics settle independently; one slow plugin cannot hold
      // the installed list, group controls or other diagnostic panels hostage.
      void loadRuntime(plugins);
      if (plugins.some((plugin) => plugin.name === "wxbot")) void loadGroups();
      void loadPluginEvents();
      void loadRestartInstructions(plugins);
      void loadFlowRuntimeStatus();
    } finally {
      if (current()) setLoading(false);
    }
  };

  const loadRuntime = async (plugins: Array<{ name: string }>) => {
    const current = beginRequest("runtime");
    setRuntime({});
    setRuntimeErrors({});
    await Promise.all(plugins.map(async ({ name }) => {
      try {
        const facts = await queryPluginRuntime(config, name);
        if (current()) setRuntime((previous) => ({ ...previous, [name]: facts }));
      } catch (err) {
        if (current()) setRuntimeErrors((previous) => ({
          ...previous, [name]: err instanceof Error ? err.message : "运行状态查询失败",
        }));
      }
    }));
  };

  const loadGroups = async () => {
    const current = beginRequest("groups");
    if (!config.adminToken) {
      return;
    }
    try {
      const result = await apiRequest<{ sessions?: WxbotSession[] }>(
        config,
        "/plugins/wxbot/admin/roster/groups",
        {
          auth: true,
        },
      );
      if (!current()) return;
      const nextGroups = result.sessions || [];
      setGroups(nextGroups);
      registerVerifiedGroups(nextGroups.map((item) => item.session_id));
    } catch (err) {
      if (!current()) return;
      setGroupOutput(formatJson({ error: err instanceof Error ? err.message : "群列表加载失败" }));
    }
  };

  const loadGroupPluginState = async (sessionId: string) => {
    const current = beginRequest("scope");
    setScopeReady(false);
    setGroupPluginState({});
    setGroupPluginVersions({});
    if (!sessionId) {
      return;
    }
    try {
      const result = await apiRequest<{ items?: PluginScopeState[] }>(
        config,
        "/v1/admin/plugins/scopes",
        {
          auth: true,
          query: { tenant_id: config.tenantId, session_id: sessionId },
        },
      );
      if (!current()) return;
      if (!Array.isArray(result.items)) throw new Error("群插件状态响应无效");
      const entries = result.items
        .filter((item) => groupScopedPlugins.some((plugin) => plugin.name === item.plugin_name))
        .map((item) => [item.plugin_name, item.enabled] as [string, boolean]);
      const nextState = {
        ...Object.fromEntries(
          groupScopedPlugins.map((plugin) => [plugin.name, Boolean(plugin.enabled)]),
        ),
        ...Object.fromEntries(entries),
      } as GroupPluginState;
      const versions = (result.items || [])
        .filter((item) => groupScopedPlugins.some((plugin) => plugin.name === item.plugin_name))
        .map((item) => [item.plugin_name, Math.max(0, Number(item.version || 0))]);
      setScopeReady(true);
      setGroupPluginState(nextState);
      setGroupPluginVersions({
        ...Object.fromEntries(groupScopedPlugins.map((plugin) => [plugin.name, 0])),
        ...Object.fromEntries(versions),
      });
      setGroupOutput(formatJson({ session_id: sessionId, plugins: nextState }));
    } catch (err) {
      if (!current()) return;
      setGroupOutput(formatJson({ error: err instanceof Error ? err.message : "群插件状态加载失败" }));
    }
  };

  const loadPluginEvents = async () => {
    const current = beginRequest("events");
    if (!config.adminToken) {
      return;
    }
    try {
      const result = await apiRequest<{ events?: PluginEvent[] }>(config, "/v1/admin/plugins/events", {
        auth: true,
        query: { limit: 20 },
      });
      if (!current()) return;
      setPluginEvents(result.events || []);
    } catch {
      if (!current()) return;
      setPluginEvents([]);
    }
  };

  const loadRestartInstructions = async (plugins: Array<{ restart_required?: boolean }>) => {
    const current = beginRequest("restart");
    if (plugins.some((plugin) => plugin.restart_required)) {
      setRestartRequired(true);
      return;
    }
    if (!config.adminToken) {
      setRestartRequired(false);
      return;
    }
    try {
      const result = await apiRequest<{ restart_required?: boolean }>(
        config,
        "/v1/admin/runtime/restart-instructions",
        { auth: true, init: { method: "POST" } },
      );
      if (!current()) return;
      setRestartRequired(Boolean(result.restart_required));
    } catch {
      if (!current()) return;
      setRestartRequired(false);
    }
  };

  const loadTraceAggregate = async (traceId: string) => {
    const current = beginRequest("trace");
    const normalizedTraceId = traceId.trim();
    if (!normalizedTraceId) {
      setTraceAggregate(null);
      setTraceAggregateError("");
      return;
    }
    setTraceAggregateLoading(true);
    setTraceAggregateError("");
    const errors: string[] = [];
    try {
      const [inboundResult, outboundResult, effectsResult, replyQueueResult, traceSnapshotResult] = await Promise.all([
        apiRequest<{ items?: TraceStreamMessage[] }>(config, "/v1/admin/streams/recent-messages", {
          auth: true,
          query: {
            stream: "inbound",
            limit: 20,
            trace_id: normalizedTraceId,
            include_media_events: false,
          },
        }).catch((err: unknown) => {
          errors.push(`入站消息加载失败：${err instanceof Error ? err.message : String(err)}`);
          return { items: [] };
        }),
        apiRequest<{ items?: TraceStreamMessage[] }>(config, "/v1/admin/streams/recent-messages", {
          auth: true,
          query: {
            stream: "outbound",
            limit: 20,
            trace_id: normalizedTraceId,
            include_media_events: false,
          },
        }).catch((err: unknown) => {
          errors.push(`出站消息加载失败：${err instanceof Error ? err.message : String(err)}`);
          return { items: [] };
        }),
        config.adminToken
          ? apiRequest<FlowEffectLogResponse>(config, "/v1/admin/message-flows/effects", {
              auth: true,
              query: { limit: 100, trace_id: normalizedTraceId },
            }).catch((err: unknown) => {
              errors.push(`Effect audit 加载失败：${err instanceof Error ? err.message : String(err)}`);
              return { items: [] };
            })
          : Promise.resolve<FlowEffectLogResponse>({ items: [] }),
        config.adminToken
          ? apiRequest<{ items?: TraceReplyQueueItem[] }>(config, "/plugins/wxbot/admin/reply-queue/messages", {
              auth: true,
              query: {
                tenant_id: config.tenantId,
                trace_id: normalizedTraceId,
                limit: 100,
              },
            }).catch((err: unknown) => {
              errors.push(`微信回复队列加载失败：${err instanceof Error ? err.message : String(err)}`);
              return { items: [] };
            })
          : Promise.resolve<{ items?: TraceReplyQueueItem[] }>({ items: [] }),
        config.adminToken
          ? apiRequest<FlowTraceSnapshotResponse>(
              config,
              `/v1/admin/message-flows/traces/${encodeURIComponent(normalizedTraceId)}`,
              { auth: true },
            ).catch((err: unknown) => {
              errors.push(`Flow trace snapshot 加载失败：${err instanceof Error ? err.message : String(err)}`);
              return {} as FlowTraceSnapshotResponse;
            })
          : Promise.resolve<FlowTraceSnapshotResponse>({}),
      ]);
      if (!current()) return;
      if (traceSnapshotResult.error) {
        errors.push(`Flow trace snapshot 加载失败：${traceSnapshotResult.error}`);
      }
      setTraceAggregate({
        traceId: normalizedTraceId,
        inbound: inboundResult.items || [],
        outbound: outboundResult.items || [],
        effects: effectsResult.items || [],
        replyQueue: replyQueueResult.items || [],
        runtimeResult: traceSnapshotResult.runtime || null,
        shadowResult: traceSnapshotResult.shadow || null,
        errors,
      });
      setTraceAggregateError(errors.length ? errors.join("；") : "");
    } catch (err) {
      if (!current()) return;
      setTraceAggregateError(err instanceof Error ? err.message : "trace 聚合加载失败");
    } finally {
      if (!current()) return;
      setTraceAggregateLoading(false);
    }
  };

  const loadFlowRuntimeStatus = async (
    traceFilter = effectTraceFilter,
    auditFilters = effectAuditFilters,
  ) => {
    const current = beginRequest("flow");
    beginRequest("trace");
    setTraceAggregate(null);
    setTraceAggregateLoading(false);
    setTraceAggregateError("");
    setFlowLoading(true);
    setFlowError("");
    try {
      const [readyzResult, runtimeResult] = await Promise.all([
        apiRequest<ReadyzFlowChecks>(config, "/readyz").catch((err: unknown) => {
          throw new Error(err instanceof Error ? `readyz: ${err.message}` : "readyz 加载失败");
        }),
        config.adminToken
          ? apiRequest<MessageFlowRuntimeStatus>(config, "/v1/admin/message-flows/runtime", {
              auth: true,
            })
          : Promise.resolve<MessageFlowRuntimeStatus | null>(null),
      ]);
      if (!current()) return;
      const [effectLogResult, effectSummaryResult] = config.adminToken
        ? await Promise.all([
            apiRequest<FlowEffectLogResponse>(config, "/v1/admin/message-flows/effects", {
              auth: true,
              query: { limit: 20, trace_id: traceFilter, ...auditFilters },
            }).catch((err: unknown) => ({
              enabled: false,
              backend: "error",
              items: [],
              error: err instanceof Error ? err.message : "effect log 加载失败",
            })),
            apiRequest<FlowEffectSummaryResponse>(config, "/v1/admin/message-flows/effects/summary", {
              auth: true,
              query: { trace_id: traceFilter, ...auditFilters },
            }).catch((err: unknown) => ({
              enabled: err instanceof ApiError && err.status === 404,
              backend: err instanceof ApiError && err.status === 404 ? "derived" : "error",
              summary: emptyEffectSummary(),
              error: err instanceof ApiError && err.status === 404
                ? ""
                : err instanceof Error ? err.message : "effect summary 加载失败",
            })),
          ])
        : [null, null];
      if (!current()) return;
      const resolvedEffectSummary = effectSummaryResult?.backend === "derived"
        ? {
            ...effectSummaryResult,
            summary: summarizeEffectLogRows(effectLogResult?.items || []),
          }
        : effectSummaryResult;
      setReadyzFlow(readyzResult);
      setFlowStatus(runtimeResult);
      setEffectLog(effectLogResult);
      setEffectSummary(resolvedEffectSummary);
      if (traceFilter.trim()) {
        void loadTraceAggregate(traceFilter.trim());
      }
      if (!config.adminToken) {
        setFlowError("请先填写 Admin Token 以查看 admin runtime 详情");
      }
    } catch (err) {
      if (!current()) return;
      setFlowError(err instanceof Error ? err.message : "message flow runtime 加载失败");
    } finally {
      if (!current()) return;
      setFlowLoading(false);
    }
  };

  const setPluginEnabled = async (pluginName: string, enabled: boolean) => {
    const current = beginRequest("lifecycle");
    const intent = `plugin-lifecycle:${config.apiBaseUrl}:${config.tenantId}:${pluginName}:${enabled ? "enable" : "disable"}`;
    try {
      const result = await apiVersionedResource<{ plugin?: InstalledPlugin }>(
        config,
        `/v1/admin/plugins/${pluginName}/${enabled ? "enable" : "disable"}`,
        {
          auth: true,
          method: "POST",
          idempotencyKey: idempotencyKeys.keyFor(intent),
        },
      );
      if (!current()) return;
      await loadSummary();
      if (!current()) return;
      await loadPluginEvents();
      if (!current()) return;
      idempotencyKeys.clear(intent);
      setGroupOutput(formatJson(result.value));
    } catch (err) {
      if (!current()) return;
      setGroupOutput(formatJson({ error: err instanceof Error ? err.message : "插件开关保存失败" }));
      throw err;
    }
  };

  const setGroupPluginEnabled = async (
    pluginName: string,
    enabled: boolean,
  ) => {
    const current = beginRequest("scopeMutation");
    if (!managedGroupSessionId || !scopeReady) {
      setGroupOutput(formatJson({ error: "请先选择群" }));
      throw new Error("请先选择群");
    }
    const intent = `plugin-scope:${config.apiBaseUrl}:${config.tenantId}:${managedGroupSessionId}:${pluginName}:${enabled ? "enable" : "disable"}`;
    try {
      const selectedGroup = groups.find((item) => item.session_id === managedGroupSessionId);
      const plugin = groupScopedPlugins.find((item) => item.name === pluginName);
      const acceptsSessionName = Boolean(
        plugin?.config_schema?.properties?.session_name,
      );
      const result = await apiVersionedResource<
        { scope_state?: PluginScopeState },
        {
          tenant_id: string;
          session_id: string;
          enabled: boolean;
          config: Record<string, unknown>;
        }
      >(
        config,
        `/v1/admin/plugins/${pluginName}/scopes`,
        {
          auth: true,
          method: "POST",
          ifMatch: `"plugin-scope-${groupPluginVersions[pluginName] ?? 0}"`,
          idempotencyKey: idempotencyKeys.keyFor(intent),
          body: {
            tenant_id: config.tenantId,
            session_id: managedGroupSessionId,
            enabled,
            config: acceptsSessionName
              ? { session_name: selectedGroup?.session_name || managedGroupSessionId }
              : {},
          },
        },
      );
      if (!current()) return;
      setGroupPluginState((prev) => ({ ...prev, [pluginName]: enabled }));
      setGroupPluginVersions((prev) => ({
        ...prev,
        [pluginName]: Math.max(
          0,
          Number(result.value.scope_state?.version ?? prev[pluginName] + 1),
        ),
      }));
      await loadPluginEvents();
      if (!current()) return;
      idempotencyKeys.clear(intent);
      setGroupOutput(formatJson(result.value));
    } catch (err) {
      if (!current()) return;
      if (err instanceof VersionConflictError) {
        await loadGroupPluginState(managedGroupSessionId);
        if (!current()) return;
      }
      setGroupOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "群插件配置已被其他管理员修改，已重新读取，请核对后重试。"
          : err instanceof Error
            ? err.message
            : "群插件开关保存失败",
      }));
      throw err;
    }
  };

  const applyDrawConfig = (document: DrawRuntimeConfig, etag?: string | null) => {
    setDrawConfig(document);
    setDrawEtag(etag || `"${document.version}"`);
    setDrawApiUrl(document.api_url || "");
    setDrawApiModel(document.api_model || "");
    setDrawApiEditUrl(document.api_edit_url || "");
    setDrawApiKey("");
  };

  const parseDrawConfig = (value: unknown): DrawRuntimeConfig | null => {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return null;
    }
    const row = value as Record<string, unknown>;
    if (!("version" in row) && !("api_url" in row) && !("enabled" in row)) {
      return null;
    }
    return {
      version: Number(row.version ?? 0) || 0,
      enabled: Boolean(row.enabled),
      api_url: String(row.api_url || ""),
      api_edit_url: String(row.api_edit_url || ""),
      api_model: String(row.api_model || ""),
      api_provider: String(row.api_provider || ""),
      api_key_configured: Boolean(row.api_key_configured),
      api_key_hint: String(row.api_key_hint || ""),
      api_host: String(row.api_host || ""),
    };
  };

  const loadDrawConfig = async () => {
    const current = beginRequest("drawRead");
    if (!config.adminToken) {
      return;
    }
    setDrawLoading(true);
    setDrawError("");
    try {
      const result = await apiVersionedResource<unknown>(config, "/plugins/draw/admin/config", {
        auth: true,
      });
      if (!current()) return;
      const parsed = parseDrawConfig(result.value);
      if (!parsed) {
        setDrawConfig(null);
        return;
      }
      applyDrawConfig(parsed, result.etag);
    } catch (err) {
      if (!current()) return;
      setDrawError(err instanceof Error ? err.message : "画图接口配置读取失败");
      setDrawConfig(null);
    } finally {
      if (!current()) return;
      setDrawLoading(false);
    }
  };

  const saveDrawConfig = async () => {
    const current = beginRequest("drawMutation");
    beginRequest("drawRead");
    setDrawSaving(true);
    setDrawError("");
    setDrawNotice("");
    try {
      const body: Record<string, string> = {
        api_url: drawApiUrl.trim(),
        api_model: drawApiModel.trim(),
        api_edit_url: drawApiEditUrl.trim(),
      };
      if (drawApiKey.trim()) {
        body.api_key = drawApiKey.trim();
      }
      const result = await apiVersionedResource<unknown, Record<string, string>>(
        config,
        "/plugins/draw/admin/config",
        {
          method: "POST",
          auth: true,
          ifMatch: drawEtag || `"${drawConfig?.version ?? 0}"`,
          body,
        },
      );
      if (!current()) return;
      const parsed = parseDrawConfig(result.value);
      if (parsed) {
        applyDrawConfig(parsed, result.etag);
      }
      setDrawNotice(body.api_url ? "已保存，当前请求立即使用新地址。" : "已停用画图接口。");
    } catch (err) {
      if (!current()) return;
      if (err instanceof VersionConflictError) {
        await loadDrawConfig();
        if (!current()) return;
        setDrawError("配置已被其他管理员修改，已重新读取，请核对后重试。");
      } else {
        setDrawError(err instanceof Error ? err.message : "画图接口配置保存失败");
      }
    } finally {
      if (!current()) return;
      setDrawSaving(false);
    }
  };

  const disableDrawConfig = async () => {
    const current = beginRequest("drawMutation");
    beginRequest("drawRead");
    setDrawSaving(true);
    setDrawError("");
    setDrawNotice("");
    try {
      const result = await apiVersionedResource<unknown, { api_url: string }>(
        config,
        "/plugins/draw/admin/config",
        {
          method: "POST",
          auth: true,
          ifMatch: drawEtag || `"${drawConfig?.version ?? 0}"`,
          body: { api_url: "" },
        },
      );
      if (!current()) return;
      const parsed = parseDrawConfig(result.value);
      if (parsed) {
        applyDrawConfig(parsed, result.etag);
      } else {
        setDrawApiUrl("");
      }
      setDrawNotice("已停用画图接口。");
    } catch (err) {
      if (!current()) return;
      if (err instanceof VersionConflictError) {
        await loadDrawConfig();
        if (!current()) return;
        setDrawError("配置已被其他管理员修改，已重新读取，请核对后重试。");
      } else {
        setDrawError(err instanceof Error ? err.message : "画图接口停用失败");
      }
    } finally {
      if (!current()) return;
      setDrawSaving(false);
    }
  };

  useEffect(() => {
    if (config.adminToken) {
      void loadSummary();
      void loadDrawConfig();
    }
    void loadFlowRuntimeStatus();
  }, []);

  useEffect(() => {
    if (managedGroupSessionId && installedStatus === "ready") {
      void loadGroupPluginState(managedGroupSessionId);
    }
  }, [managedGroupSessionId, installed, installedStatus]);

  useEffect(() => {
    const nextGroupSessionId = config.sessionId.endsWith("@chatroom") ? config.sessionId : "";
    if (nextGroupSessionId !== managedGroupSessionId) {
      setManagedGroupSessionId(nextGroupSessionId);
    }
  }, [config.sessionId, managedGroupSessionId]);

  useEffect(() => {
    if (!selectedPluginName) {
      return;
    }
    const card = document.getElementById(`plugin-card-${selectedPluginName}`);
    if (card && typeof card.scrollIntoView === "function") {
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [selectedPluginName, pluginCards.length]);

  useEffect(() => {
    const nextTraceId = searchParams.get("trace_id")?.trim() || "";
    if (nextTraceId === effectTraceFilter) {
      return;
    }
    setEffectTraceFilter(nextTraceId);
    if (nextTraceId) {
      void loadFlowRuntimeStatus(nextTraceId, effectAuditFilters);
    } else {
      setTraceAggregate(null);
      void loadFlowRuntimeStatus("", effectAuditFilters);
    }
  }, [searchParams]);

  const output = error
    ? formatJson({ error })
    : formatJson(
        data || installed.length || Object.keys(runtime).length
          ? { summary: data, installed, runtime, message_flow_runtime: flowStatus, readyz_flow: readyzFlow }
          : { message: config.adminToken ? "等待加载" : "请先填写 Admin Token" },
      );

  const setTraceQueryParam = (traceId: string) => {
    const nextParams = new URLSearchParams(searchParams);
    if (traceId) {
      nextParams.set("trace_id", traceId);
    } else {
      nextParams.delete("trace_id");
    }
    setSearchParams(nextParams, { replace: true });
  };
  const selectEffectTrace = (traceId: string | undefined) => {
    const nextTraceId = String(traceId || "").trim();
    if (!nextTraceId) {
      return;
    }
    setEffectTraceFilter(nextTraceId);
    setTraceQueryParam(nextTraceId);
    void loadFlowRuntimeStatus(nextTraceId, effectAuditFilters);
  };
  const clearEffectTraceFilter = () => {
    setEffectTraceFilter("");
    setTraceQueryParam("");
    setTraceAggregate(null);
    void loadFlowRuntimeStatus("", effectAuditFilters);
  };
  const selectEffectAuditFilters = (item: FlowEffectSummaryRow) => {
    const nextFilters: EffectAuditFilters = {
      owner: item.owner || undefined,
      type: item.type || undefined,
      status: item.status || undefined,
      dry_run: item.dry_run,
    };
    setEffectAuditFilters(nextFilters);
    void loadFlowRuntimeStatus(effectTraceFilter, nextFilters);
  };
  const clearEffectAuditFilters = () => {
    const nextFilters: EffectAuditFilters = {};
    setEffectAuditFilters(nextFilters);
    void loadFlowRuntimeStatus(effectTraceFilter, nextFilters);
  };
  const clearAllEffectFilters = () => {
    const nextFilters: EffectAuditFilters = {};
    setEffectTraceFilter("");
    setEffectAuditFilters(nextFilters);
    setTraceQueryParam("");
    setTraceAggregate(null);
    void loadFlowRuntimeStatus("", nextFilters);
  };



  const selectGroup = (sessionId: string) => {
    const normalized = sessionId.trim();
    try {
      selectVerifiedGroup(normalized);
      setManagedGroupSessionId(normalized);
    } catch (err) {
      setGroupOutput(formatJson({
        error: err instanceof Error ? err.message : "只能选择已同步并验证的群聊",
      }));
    }
  };

  return {
    data,
    pluginCards,
    runtime,
    runtimeErrors,
    error,
    scopeReady,
    loading,
    canManage: Boolean(config.adminToken),
    groups,
    loadedPluginNames,
    groupScopedPlugins,
    managedGroupSessionId,
    groupPluginState,
    pluginEvents,
    output,
    groupOutput,
    flowStatus,
    readyzFlow,
    effectLog,
    effectSummary,
    effectTraceFilter,
    effectAuditFilters,
    traceAggregate,
    traceAggregateLoading,
    traceAggregateError,
    flowLoading,
    flowError,
    selectedPluginName,
    restartRequired,
    drawConfig,
    drawApiUrl,
    drawApiKey,
    drawApiModel,
    drawApiEditUrl,
    drawLoading,
    drawSaving,
    drawError,
    drawNotice,
    setDrawApiUrl,
    setDrawApiKey,
    setDrawApiModel,
    setDrawApiEditUrl,
    saveDrawConfig,
    disableDrawConfig,
    refreshSummary: loadSummary,
    refreshFlowRuntime: loadFlowRuntimeStatus,
    selectEffectTrace,
    clearEffectTraceFilter,
    selectEffectAuditFilters,
    clearEffectAuditFilters,
    clearAllEffectFilters,
    setPluginEnabled,
    selectGroup,
    setGroupPluginEnabled,
  };
}

import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  ApiError,
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  formatJson,
} from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { useConsoleConfig } from "../../state/console-config";
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
  PluginRuntimeEnvelope,
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

export function usePluginsPageController() {
  const {
    config,
    registerVerifiedGroups,
    selectVerifiedGroup,
  } = useConsoleConfig();
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
  const idempotencyKeys = useStableIdempotencyKeys();
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

  const pluginCards: InstalledPlugin[] = installed.length ? installed : (data?.plugins || []).map((plugin) => ({
    ...plugin,
    enabled: true,
    system: false,
    status: "active",
    restart_required: false,
  }));
  const loadedPluginNames = new Set(pluginCards.map((plugin) => plugin.name));
  const groupScopedPlugins = pluginCards.filter(
    (plugin) => plugin.admin_ui?.scope === "group",
  );

  const loadSummary = async () => {
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest<PluginSummary>(config, "/v1/admin/plugins/summary", {
        auth: true,
      });
      setData(result);
      const installedResult = await apiRequest<InstalledPluginsResponse>(config, "/v1/admin/plugins/installed", {
        auth: true,
      }).catch(() => ({ plugins: [] }));
      setInstalled(installedResult.plugins || []);
      await loadRuntime(installedResult.plugins?.length ? installedResult.plugins : result.plugins);
      if ((installedResult.plugins?.length ? installedResult.plugins : result.plugins).some((plugin) => plugin.name === "wxbot")) {
        await loadGroups();
      }
      await loadPluginEvents();
      await loadFlowRuntimeStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  };

  const loadRuntime = async (plugins: Array<{ name: string }>) => {
    const loaded = new Set(plugins.map((plugin) => plugin.name));
    const unifiedRequests = plugins.map((plugin) =>
      apiRequest<PluginRuntimeEnvelope>(config, `/v1/admin/plugins/${plugin.name}/runtime`, {
        auth: true,
      })
        .then((result) => [plugin.name, result.runtime_status || result.runtime || {}] as [string, Record<string, unknown>])
        .catch(() => null),
    );
    const unifiedEntries = (await Promise.all(unifiedRequests)).filter(Boolean) as Array<[
      string,
      Record<string, unknown>,
    ]>;
    if (unifiedEntries.length) {
      setRuntime(Object.fromEntries(unifiedEntries) as PluginRuntime);
      return;
    }

    const requests: Array<Promise<[keyof PluginRuntime, PluginRuntime[keyof PluginRuntime]]>> = [];

    if (loaded.has("credits")) {
      requests.push(
        apiRequest<Record<string, unknown>>(
          config,
          `/plugins/credits/config/${config.tenantId}/${config.sessionId}`,
        ).then((result) => [
          "credits",
          {
            enabled: Boolean(result.enabled),
            credit_name: String(result.credit_name || "积分"),
            cost_per_chat: Number(result.cost_per_chat ?? 0),
          },
        ]),
      );
    }

    if (loaded.has("amap")) {
      requests.push(
        apiRequest<Record<string, unknown>>(config, "/plugins/amap/admin/config", {
          auth: true,
        }).then((result) => [
          "amap",
          {
            api_key_configured: Boolean(result.api_key_configured),
            timeout_seconds: Number(result.timeout_seconds ?? 15),
            storage_dir: String(result.storage_dir || ""),
            storage_dir_exists: Boolean(result.storage_dir_exists),
            storage_dir_writable: Boolean(result.storage_dir_writable),
            agent_scope: String(result.agent_scope || "group_personal_map"),
            tools: Array.isArray(result.tools) ? result.tools.map(String) : [],
          },
        ]),
      );
    }

    if (loaded.has("commands")) {
      requests.push(
        apiRequest<Record<string, unknown>>(
          config,
          `/plugins/commands/config/${config.tenantId}`,
        ).then((result) => [
          "commands",
          {
            admins: Array.isArray(result.admin_user_ids) ? result.admin_user_ids.length : 0,
            user_commands: Array.isArray(result.user_commands) ? result.user_commands.length : 0,
            admin_commands: Array.isArray(result.admin_commands) ? result.admin_commands.length : 0,
          },
        ]),
      );
    }

    if (loaded.has("moderation")) {
      requests.push(
        apiRequest<Record<string, unknown>>(
          config,
          `/plugins/moderation/config/${config.tenantId}/${config.sessionId}`,
        ).then((result) => [
          "moderation",
          {
            enabled: Boolean(result.enabled),
            reminder_mode: String(result.reminder_mode || "off"),
            webhook_enabled: Boolean(result.webhook_enabled),
          },
        ]),
      );
    }

    if (loaded.has("persona_extract")) {
      requests.push(
        Promise.all([
          apiRequest<{ items?: unknown[] }>(config, "/plugins/persona_extract/profiles", {
            query: { tenant_id: config.tenantId, session_id: config.sessionId },
          }),
          apiRequest<{ items?: unknown[] }>(config, "/plugins/persona_extract/jobs", {
            query: { tenant_id: config.tenantId, session_id: config.sessionId },
          }),
        ]).then(([profiles, jobs]) => [
          "persona_extract",
          {
            profiles: profiles.items?.length ?? 0,
            jobs: jobs.items?.length ?? 0,
          },
        ]),
      );
    }

    if (loaded.has("memory")) {
      requests.push(
        Promise.all([
          apiRequest<{ items?: unknown[] }>(config, "/plugins/memory/profiles", {
            query: { tenant_id: config.tenantId, limit: 200 },
          }),
          apiRequest<{ items?: unknown[] }>(config, "/plugins/memory/events", {
            query: { tenant_id: config.tenantId, limit: 200 },
          }),
        ]).then(([profiles, events]) => [
          "memory",
          {
            profiles: profiles.items?.length ?? 0,
            events: events.items?.length ?? 0,
          },
        ]),
      );
    }

    if (loaded.has("repeater")) {
      requests.push(
        apiRequest<Record<string, unknown>>(
          config,
          `/plugins/repeater/config/${config.tenantId}/${config.sessionId}`,
        ).then((result) => [
          "repeater",
          {
            enabled: Boolean(result.enabled),
            cooldown_seconds: Number(result.cooldown_seconds ?? 300),
          },
        ]),
      );
    }

    if (loaded.has("wxbot")) {
      requests.push(
        Promise.all([
          apiRequest<Record<string, unknown>>(config, "/plugins/wxbot/bridge/status"),
          apiRequest<Record<string, number>>(config, "/plugins/wxbot/admin/reply-queue/stats", {
            auth: true,
            query: { tenant_id: config.tenantId },
          }).catch((): Record<string, number> => ({})),
          apiRequest<{ sessions?: unknown[] }>(config, "/plugins/wxbot/admin/sessions", {
            auth: true,
          }).catch(() => ({ sessions: [] })),
        ]).then(([bridge, queue, sessionResult]) => [
          "wxbot",
          {
            running: Boolean(bridge.running),
            sdk_online: Boolean(bridge.sdk_online),
            ingest_mode: String(bridge.ingest_mode || "-"),
            pending: Number(queue.pending ?? 0),
            sessions: sessionResult.sessions?.length ?? 0,
          },
        ]),
      );
    }

    const entries = await Promise.all(requests);
    setRuntime(Object.fromEntries(entries) as PluginRuntime);
  };

  const loadGroups = async () => {
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
      const nextGroups = result.sessions || [];
      setGroups(nextGroups);
      registerVerifiedGroups(nextGroups.map((item) => item.session_id));
    } catch (err) {
      setGroupOutput(formatJson({ error: err instanceof Error ? err.message : "群列表加载失败" }));
    }
  };

  const loadGroupPluginState = async (sessionId: string) => {
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
      const entries = (result.items || [])
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
      setGroupPluginState(nextState);
      setGroupPluginVersions({
        ...Object.fromEntries(groupScopedPlugins.map((plugin) => [plugin.name, 0])),
        ...Object.fromEntries(versions),
      });
      setGroupOutput(formatJson({ session_id: sessionId, plugins: nextState }));
    } catch (err) {
      setGroupOutput(formatJson({ error: err instanceof Error ? err.message : "群插件状态加载失败" }));
    }
  };

  const loadPluginEvents = async () => {
    if (!config.adminToken) {
      return;
    }
    try {
      const result = await apiRequest<{ events?: PluginEvent[] }>(config, "/v1/admin/plugins/events", {
        auth: true,
        query: { limit: 20 },
      });
      setPluginEvents(result.events || []);
    } catch {
      setPluginEvents([]);
    }
  };

  const loadTraceAggregate = async (traceId: string) => {
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
      setTraceAggregateError(err instanceof Error ? err.message : "trace 聚合加载失败");
    } finally {
      setTraceAggregateLoading(false);
    }
  };

  const loadFlowRuntimeStatus = async (
    traceFilter = effectTraceFilter,
    auditFilters = effectAuditFilters,
  ) => {
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
      setFlowError(err instanceof Error ? err.message : "message flow runtime 加载失败");
    } finally {
      setFlowLoading(false);
    }
  };

  const setPluginEnabled = async (pluginName: string, enabled: boolean) => {
    const intent = `plugin-lifecycle:${pluginName}:${enabled ? "enable" : "disable"}`;
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
      await loadSummary();
      await loadPluginEvents();
      idempotencyKeys.clear(intent);
      setGroupOutput(formatJson(result.value));
    } catch (err) {
      setGroupOutput(formatJson({ error: err instanceof Error ? err.message : "插件开关保存失败" }));
      throw err;
    }
  };

  const setGroupPluginEnabled = async (
    pluginName: string,
    enabled: boolean,
  ) => {
    if (!managedGroupSessionId) {
      setGroupOutput(formatJson({ error: "请先选择群" }));
      throw new Error("请先选择群");
    }
    const intent = `plugin-scope:${managedGroupSessionId}:${pluginName}:${enabled ? "enable" : "disable"}`;
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
      setGroupPluginState((prev) => ({ ...prev, [pluginName]: enabled }));
      setGroupPluginVersions((prev) => ({
        ...prev,
        [pluginName]: Math.max(
          0,
          Number(result.value.scope_state?.version ?? prev[pluginName] + 1),
        ),
      }));
      await loadPluginEvents();
      idempotencyKeys.clear(intent);
      setGroupOutput(formatJson(result.value));
    } catch (err) {
      if (err instanceof VersionConflictError) {
        await loadGroupPluginState(managedGroupSessionId);
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

  useEffect(() => {
    if (config.adminToken) {
      void (async () => {
        const result = await apiRequest<PluginSummary>(config, "/v1/admin/plugins/summary", {
          auth: true,
        });
        setData(result);
        const installedResult = await apiRequest<InstalledPluginsResponse>(config, "/v1/admin/plugins/installed", {
          auth: true,
        }).catch(() => ({ plugins: [] }));
        setInstalled(installedResult.plugins || []);
        const plugins = installedResult.plugins?.length ? installedResult.plugins : result.plugins;
        await loadRuntime(plugins);
        if (plugins.some((plugin) => plugin.name === "wxbot")) {
          await loadGroups();
        }
        await loadPluginEvents();
        await loadFlowRuntimeStatus();
      })().catch((err: unknown) => {
        setError(err instanceof Error ? err.message : "加载失败");
      });
    } else {
      void loadFlowRuntimeStatus();
    }
  }, [config.adminToken, config.sessionId, config.tenantId]);

  useEffect(() => {
    if (managedGroupSessionId && data?.plugins?.length) {
      void loadGroupPluginState(managedGroupSessionId);
    }
  }, [managedGroupSessionId, data?.plugins?.length, config.sessionId, config.tenantId]);

  useEffect(() => {
    const nextGroupSessionId = config.sessionId.endsWith("@chatroom") ? config.sessionId : "";
    if (nextGroupSessionId !== managedGroupSessionId) {
      setManagedGroupSessionId(nextGroupSessionId);
    }
  }, [config.sessionId, managedGroupSessionId]);

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

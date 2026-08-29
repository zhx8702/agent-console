export type PluginSummary = {
  plugins: Array<{ name: string; version: string; description: string }>;
  plugin_routes: string[];
  hooks: Record<string, string[]>;
  channels: string[];
  channel_labels: Record<string, string>;
  channel_adapters?: Array<{
    adapter_id: string;
    display_name: string;
    channel: string;
    version?: string;
    capabilities?: string[];
    runtime_modes?: string[];
  }>;
};

export type InstalledPlugin = {
  name: string;
  plugin_name?: string;
  version: string;
  description?: string;
  enabled: boolean;
  system: boolean;
  status: string;
  restart_required: boolean;
  last_error?: string;
  permissions?: string[];
  has_router?: boolean;
  has_capability_engine?: boolean;
  config_schema?: {
    type?: string;
    properties?: Record<string, Record<string, unknown>>;
    [key: string]: unknown;
  };
  admin_ui?: {
    scope?: "global" | "tenant" | "session" | "group";
    label?: string;
    summary?: string;
  };
};

export type InstalledPluginsResponse = {
  plugins: InstalledPlugin[];
};

export type PluginRuntimeEnvelope = {
  plugin_name: string;
  runtime_status?: Record<string, unknown>;
  runtime?: Record<string, unknown>;
};

export type PluginRuntime = {
  amap?: {
    api_key_configured: boolean;
    timeout_seconds?: number;
    storage_dir?: string;
    storage_dir_exists?: boolean;
    storage_dir_writable?: boolean;
    agent_scope?: string;
    tools?: string[];
  };
  commands?: {
    admins?: number;
    user_commands?: number;
    admin_commands?: number;
  };
  credits?: {
    enabled: boolean;
    credit_name?: string;
    cost_per_chat?: number;
  };
  moderation?: {
    enabled: boolean;
    reminder_mode?: string;
    webhook_enabled?: boolean;
  };
  memory?: {
    profiles: number;
    events: number;
  };
  persona_extract?: {
    profiles: number;
    jobs: number;
  };
  repeater?: {
    enabled: boolean;
    cooldown_seconds?: number;
  };
  tibo_reset?: {
    running?: boolean;
    scheduler_enabled?: boolean;
    configured_enabled?: boolean;
    api_url_configured?: boolean;
    poll_interval_seconds?: number;
    enabled_groups?: number;
    latest_tweet_id?: string;
    last_success_at?: string;
    last_error?: string;
    stats?: {
      history_count?: number;
      week_count?: number;
      week_everyone_count?: number;
      today_count?: number;
      today_has_reset?: boolean;
      timezone?: string;
      latest_reset_at?: string;
    };
  };
  wxbot?: {
    running?: boolean;
    sdk_online?: boolean;
    ingest_mode?: string;
    pending?: number;
    sessions?: number;
  };
};

export type WxbotSession = {
  session_id: string;
  session_name: string;
  kind?: string;
};

export type GroupPluginState = Record<string, boolean>;

export type PluginScopeState = {
  plugin_name: string;
  enabled: boolean;
  version: number;
};

export type PluginEvent = {
  id: number;
  plugin_name: string;
  event_type: string;
  status: string;
  message?: string;
  created_at: string;
};

export type FlowRuntimeConfig = {
  enabled?: boolean;
  name?: string;
  mode?: string;
  backend?: string;
  allowed?: boolean;
  reason?: string;
  allowed_names?: string[];
  allow_target_flows?: boolean;
  allow_compatible_fallback?: boolean;
  core_preview_enabled?: boolean;
  plugin_dry_run_enabled?: boolean;
  effect_dry_run_enabled?: boolean;
  ttl_seconds?: number;
  key_prefix?: string;
  stream?: string;
  handlers_enabled?: boolean;
  handler_allowlist?: string[];
  handler_mode?: string;
  handlers_commit_backend_safe?: boolean;
  log_backend?: string;
  log_failure_policy?: string;
};

export type FlowRunResult = {
  flow_name?: string;
  trace_id?: string;
  tenant_id?: string;
  session_id?: string;
  status?: string;
  stop_reason?: string | null;
  error?: string | null;
  ok?: boolean;
  steps?: FlowRunStepTrace[];
  effect_commits?: FlowEffectTraceRecord[];
  effect_dispatches?: FlowEffectTraceRecord[];
};

export type FlowRunStepTrace = {
  id?: string;
  kind?: string;
  owner?: string;
  status?: string;
  action?: string;
  reason?: string;
  error?: string;
  elapsed_ms?: number;
  attempts?: number;
};

export type FlowEffectTraceRecord = {
  type?: string;
  owner?: string;
  idempotency_key?: string;
  status?: string;
  commit_status?: string;
  error?: string;
  dry_run?: boolean;
};

export type FlowEffectHandlerItem = {
  type?: string;
  owner?: string;
  handler?: string;
};

export type FlowEffectHandlerFallback = {
  type?: string;
  owner?: string;
  fallback_for?: string;
};

export type FlowEffectHandlers = {
  count?: number;
  owners?: string[];
  types?: string[];
  fallbacks?: FlowEffectHandlerFallback[];
  items?: FlowEffectHandlerItem[];
};

export type FlowEffectLogItem = {
  id?: number;
  idempotency_key?: string;
  tenant_id?: string;
  session_id?: string;
  trace_id?: string;
  owner?: string;
  type?: string;
  status?: string;
  dry_run?: boolean;
  payload_keys?: string[];
  payload_size?: number;
  created_at?: string;
};

export type FlowEffectLogResponse = {
  enabled?: boolean;
  backend?: string;
  items?: FlowEffectLogItem[];
  error?: string;
};

export type FlowEffectSummaryRow = {
  owner?: string;
  type?: string;
  status?: string;
  dry_run?: boolean;
  count?: number;
};

export type FlowEffectSummary = {
  total?: number;
  by_status?: FlowEffectSummaryRow[];
  by_owner?: FlowEffectSummaryRow[];
  by_type?: FlowEffectSummaryRow[];
  by_dry_run?: FlowEffectSummaryRow[];
  matrix?: FlowEffectSummaryRow[];
};

export type FlowEffectSummaryResponse = {
  enabled?: boolean;
  backend?: string;
  summary?: FlowEffectSummary;
  error?: string;
};

export const emptyEffectSummary = (): FlowEffectSummary => ({
  total: 0,
  by_status: [],
  by_owner: [],
  by_type: [],
  by_dry_run: [],
  matrix: [],
});

export const summarizeEffectLogRows = (rows: FlowEffectLogItem[]): FlowEffectSummary => {
  const statusCounts = new Map<string, number>();
  const ownerCounts = new Map<string, number>();
  const typeCounts = new Map<string, number>();
  const dryRunCounts = new Map<string, number>();
  const matrixCounts = new Map<string, FlowEffectSummaryRow>();

  rows.forEach((row) => {
    const status = row.status || "";
    const owner = row.owner || "";
    const type = row.type || "";
    const dryRun = Boolean(row.dry_run);
    statusCounts.set(status, (statusCounts.get(status) || 0) + 1);
    ownerCounts.set(owner, (ownerCounts.get(owner) || 0) + 1);
    typeCounts.set(type, (typeCounts.get(type) || 0) + 1);
    dryRunCounts.set(String(dryRun), (dryRunCounts.get(String(dryRun)) || 0) + 1);
    const matrixKey = `${owner}\u0000${type}\u0000${status}\u0000${String(dryRun)}`;
    const current = matrixCounts.get(matrixKey);
    matrixCounts.set(matrixKey, {
      owner,
      type,
      status,
      dry_run: dryRun,
      count: (current?.count || 0) + 1,
    });
  });

  const countRows = (
    counts: Map<string, number>,
    key: keyof FlowEffectSummaryRow,
  ): FlowEffectSummaryRow[] => (
    Array.from(counts.entries())
      .map(([value, count]) => ({ [key]: key === "dry_run" ? value === "true" : value, count }))
      .sort((a, b) => (b.count || 0) - (a.count || 0))
  );

  return {
    total: rows.length,
    by_status: countRows(statusCounts, "status"),
    by_owner: countRows(ownerCounts, "owner"),
    by_type: countRows(typeCounts, "type"),
    by_dry_run: countRows(dryRunCounts, "dry_run"),
    matrix: Array.from(matrixCounts.values()).sort((a, b) => (b.count || 0) - (a.count || 0)),
  };
};

export type EffectAuditFilters = {
  owner?: string;
  type?: string;
  status?: string;
  dry_run?: boolean;
};

export type TraceStreamMessage = {
  id: string;
  source?: string | null;
  stream_key?: string;
  stream?: string;
  tenant_id?: string | null;
  session_id?: string | null;
  user_id?: string | null;
  trace_id?: string | null;
  channel?: string | null;
  attempts?: number;
  reason?: string | null;
  created_ts_ms?: number | null;
  payload?: Record<string, unknown>;
  headers?: Record<string, unknown>;
};

export type TraceReplyQueueItem = {
  id?: number;
  tenant_id?: string;
  session_id?: string;
  session_name?: string;
  sender_name?: string;
  reply_text?: string;
  trace_id?: string;
  status?: string;
  attempt_count?: number;
  error?: string;
  command_id?: string;
  sdk_outbound_id?: string;
  created_at?: string;
  queued_at?: string;
  sent_at?: string;
};

export type TraceAggregate = {
  traceId: string;
  inbound: TraceStreamMessage[];
  outbound: TraceStreamMessage[];
  effects: FlowEffectLogItem[];
  replyQueue: TraceReplyQueueItem[];
  runtimeResult?: FlowRunResult | null;
  shadowResult?: FlowRunResult | null;
  errors: string[];
};

export type TraceEventCard = {
  key: string;
  eyebrow: string;
  title: string;
  status: string;
  state: "hit" | "miss" | "error" | "dry";
  detail: string;
  meta: string[];
  chips: string[];
};

export type FlowTraceSnapshotResponse = {
  trace_id?: string;
  enabled?: boolean;
  backend?: string;
  ttl_seconds?: number;
  runtime?: FlowRunResult | null;
  shadow?: FlowRunResult | null;
  error?: string;
};

export const asObject = (value: unknown): Record<string, unknown> | null => {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
};

export const textLength = (value: unknown) => typeof value === "string" ? value.trim().length : 0;

export const compactList = (items: string[], limit = 8) => {
  if (!items.length) {
    return "-";
  }
  const head = items.slice(0, limit).join(", ");
  return items.length > limit ? `${head} +${items.length - limit}` : head;
};

export const payloadKeys = (payload?: Record<string, unknown>) => Object.keys(payload || {}).sort();

export const traceMessageSummary = (payload?: Record<string, unknown>) => {
  const record = payload || {};
  const message = asObject(record.message);
  const metadata = asObject(record.metadata);
  const media = asObject(metadata?.media) || asObject(record.media);
  const quote = asObject(metadata?.quote) || asObject(message?.quote) || asObject(record.quote);
  const contentLength = textLength(message?.content) || textLength(record.content) || textLength(record.text);
  const segments = Array.isArray(record.segments) ? record.segments.length : 0;
  const attachments = Array.isArray(message?.attachments) ? message.attachments.length : 0;
  const parts = [
    typeof message?.type === "string" ? `type=${message.type}` : "",
    typeof record.route === "string" ? `route=${record.route}` : "",
    contentLength ? `文本 ${contentLength} 字` : "",
    media ? "含媒体" : "",
    quote ? "含引用" : "",
    attachments ? `附件 ${attachments}` : "",
    segments ? `segments ${segments}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "无可展示摘要";
};

export const replyQueueSummary = (item: TraceReplyQueueItem) => {
  const length = textLength(item.reply_text);
  const parts = [
    length ? `回复 ${length} 字` : "无回复文本",
    item.command_id ? `command=${item.command_id}` : "",
    item.sdk_outbound_id ? `sdk=${item.sdk_outbound_id}` : "",
  ].filter(Boolean);
  return parts.join(" · ");
};

export const formatTraceTime = (value?: number | string | null) => {
  if (!value) {
    return "-";
  }
  if (typeof value === "number") {
    try {
      return new Date(value).toLocaleString("zh-CN", { hour12: false });
    } catch {
      return String(value);
    }
  }
  return value;
};

export type MessageFlowRuntimeStatus = {
  runtime?: FlowRuntimeConfig;
  shadow?: FlowRuntimeConfig;
  effect_commit?: FlowRuntimeConfig;
  effect_handlers?: FlowEffectHandlers;
  last_runtime_result?: FlowRunResult | null;
  last_shadow_result?: FlowRunResult | null;
};

export type ReadyzFlowChecks = {
  status?: string;
  checks?: {
    flow_runtime?: FlowRuntimeConfig;
    flow_shadow?: FlowRuntimeConfig;
    flow_effect_commit?: FlowRuntimeConfig;
    flow_effect_handlers?: FlowEffectHandlers;
  };
};

export const PLUGIN_LINKS: Record<string, string> = {
  amap: "/amap",
  commands: "/commands",
  credits: "/credits",
  memory: "/memory",
  moderation: "/moderation",
  persona_extract: "/persona",
  repeater: "/repeater",
  wxbot: "/wxbot",
};

export const PLUGIN_DISPLAY_NAMES: Record<string, string> = {
  amap: "高德地图",
  commands: "命令中心",
  credits: "积分运营",
  memory: "成员记忆",
  moderation: "内容审核",
  wxbot: "微信适配器",
  persona_extract: "回复风格",
  repeater: "复读策略",
  tibo_reset: "Tibo 重置",
};

const PLUGIN_RUNTIME_FIELD_LABELS: Record<string, string> = {
  api_url_configured: "接口地址",
  api_key_configured: "API 密钥",
  configured: "接口配置",
  fallback_configured: "备用接口",
  scheduler_enabled: "调度器",
  running: "运行中",
  poll_interval_seconds: "轮询间隔（秒）",
  last_error: "最近错误",
  last_success_at: "最近成功",
  latest_tweet_id: "最新条目",
  enabled_groups: "已开群数",
};

// configured_enabled duplicates api_url_configured on tibo_reset and would
// render two identical rows on the card.
const HIDDEN_RUNTIME_KEYS = new Set(["stats", "tools", "configured_enabled"]);

function pluginRuntimeUnconfigured(
  pluginName: string,
  runtime?: PluginRuntime,
): boolean {
  const facts = runtime?.[pluginName as keyof PluginRuntime] as
    | Record<string, unknown>
    | undefined;
  if (!facts) return false;
  if (facts.api_url_configured === false) return true;
  if (facts.api_key_configured === false) return true;
  // A plugin with a working fallback endpoint (e.g. draw) is still usable
  // when only the primary endpoint is missing.
  return facts.configured === false && facts.fallback_configured !== true;
}

export function pluginEnablementLabel(
  plugin: InstalledPlugin | undefined,
  runtime?: PluginRuntime,
): string {
  if (!plugin) return "未加载";
  if (plugin.restart_required) return "待重启";
  if (!plugin.enabled) return "已停用";
  if (pluginRuntimeUnconfigured(plugin.name, runtime)) {
    return "未配置";
  }
  return "已启用";
}

export function pluginRuntimeFacts(
  pluginName: string,
  runtime: PluginRuntime,
): Array<{ label: string; value: string }> {
  const raw = runtime[pluginName as keyof PluginRuntime];
  if (!raw || typeof raw !== "object") {
    return [];
  }
  return Object.entries(raw)
    .filter(([key, value]) => !HIDDEN_RUNTIME_KEYS.has(key) && value != null && typeof value !== "object")
    .map(([key, value]) => ({
      label: PLUGIN_RUNTIME_FIELD_LABELS[key] || key,
      value: formatRuntimeFact(key, value),
    }));
}

function formatRuntimeFact(key: string, value: unknown): string {
  if (typeof value === "boolean") {
    if (key === "configured" || key.endsWith("_configured")) {
      return value ? "已配置" : "未配置";
    }
    return value ? "是" : "否";
  }
  if (typeof value === "number" || typeof value === "string") {
    const text = String(value).trim();
    return text || "-";
  }
  return "-";
}

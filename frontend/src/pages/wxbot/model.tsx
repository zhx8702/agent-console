import { formatJson } from "../../lib/api";

export type BridgeStatus = {
  running: boolean;
  sdk_url: string;
  sdk_online: boolean;
  sdk_auth_state?: string;
  sdk_auth_reason?: string;
  tenant_id: string;
  cursor: number;
  event_cursor?: number;
  ingest_mode: string;
  event_mode?: string;
  poll_interval: number;
  send_interval: number;
  member_event_stats?: Record<string, number>;
  error?: string;
};

export type WxbotSession = {
  session_id: string;
  session_name: string;
  kind?: string;
};

export type QueueStats = Record<string, number>;

export type ReplyQueueMessage = {
  id: number;
  command_id?: string;
  tenant_id?: string;
  session_id: string;
  session_name?: string;
  sender_name?: string;
  sender_wxid?: string;
  mention_sender?: boolean;
  reply_to_msg_svr_id?: string;
  session_kind?: string;
  reply_text?: string;
  msg_type?: string;
  media_id?: string;
  source_message?: Record<string, unknown>;
  delivery?: Record<string, unknown>;
  participation_status?: string;
  not_before?: string | null;
  expires_at?: string | null;
  trace_id?: string;
  status?: string;
  attempt_count?: number;
  error?: string;
  created_at?: string | null;
  sent_at?: string | null;
  created_ts?: number;
  claimed_ts?: number | null;
  sent_ts?: number | null;
};

export type SdkTriggerDebugConfig = {
  group_require_at_me: boolean;
  group_capture_mode?: string;
  my_names?: string[];
  config_path?: string;
  saved?: boolean;
};

export type MemberEvent = {
  sdk_event_id: number;
  event_type: string;
  session_id: string;
  session_name?: string;
  entity_wxid?: string;
  entity_name?: string;
  payload?: Record<string, unknown>;
  created_ts?: number;
};

export type EventSubscription = {
  id: number;
  event_type: string;
  target_url: string;
  session_id?: string;
  enabled: boolean;
};

export type GroupMemberSettings = {
  session_id: string;
  welcome_enabled?: boolean;
  welcome_template?: string;
  welcome_mention?: boolean;
  saved?: boolean;
  version?: number;
};

export type GroupParticipationPolicy = {
  threshold: number;
  quiet_start_hour: number;
  quiet_end_hour: number;
  timezone: string;
  max_soft_replies_10m: number;
  max_soft_replies_hour: number;
  max_bot_ratio_last_40: number;
  max_consecutive_bot_messages: number;
};

export type ReplyPolicy = {
  tenant_id: string;
  session_id: string;
  reply_mode: string;
  mention_sender_mode: string;
  trigger_keywords_text: string;
  default_mode?: string;
  effective_mode?: string;
  default_mention_sender?: boolean;
  effective_mention_sender?: boolean;
  effective_trigger_keywords_text?: string;
  trigger_keywords?: string[];
  inherits_global_keywords?: boolean;
  global_policy?: GlobalReplyPolicy;
  participation_policy?: Partial<GroupParticipationPolicy>;
  version?: number;
  updated_at?: string | null;
};

export type GlobalReplyPolicy = {
  tenant_id: string;
  private_reply_mode: string;
  group_reply_mode: string;
  group_reply_mention_sender: boolean;
  trigger_keywords_text: string;
  trigger_keywords?: string[];
  version?: number;
  updated_at?: string | null;
};

export type ReplyPolicyAggregate = {
  tenant_id: string;
  session_id: string;
  global_policy: GlobalReplyPolicy;
  session_policy: ReplyPolicy;
  repeater_config: {
    tenant_id: string;
    session_id: string;
    enabled: boolean;
    cooldown_seconds: number;
    version?: number;
  };
  sdk_gate: SdkTriggerDebugConfig & {
    status?: string;
    idempotency_key?: string;
  };
  versions: {
    global: number;
    session: number;
    repeater: number;
    aggregate: number;
  };
  etag?: string;
};

export type SessionStateSnapshot = {
  tenant_id: string;
  session_id: string;
  session_name?: string;
  user_id?: string;
  channel?: string;
  state: string;
  auto_reply_enabled: boolean;
  suppress_ai_reply: boolean;
  handoff_hint_keywords?: string[];
  latest_handoff_turn?: {
    content?: string;
    created_at?: string | null;
    trace_id?: string | null;
  } | null;
  explanation?: string;
  last_active_at?: string | null;
  updated_at?: string | null;
  previous_state?: string;
  version?: number;
};

export type ReportSubscription = {
  session_id: string;
  session_name: string;
  daily_enabled: boolean | number;
  weekly_enabled: boolean | number;
  monthly_enabled: boolean | number;
  daily_hour: number;
  weekly_day: number;
  weekly_hour: number;
  monthly_day: number;
  tz: string;
};

export type ReportPreview = {
  job_id?: number;
  session_id: string;
  session_name: string;
  report_type: string;
  period?: string;
  status?: string;
  current_stage?: string;
  cached?: boolean;
  report?: string;
};

export type ReportMessagesPayload = {
  session_id: string;
  session_name: string;
  report_type: string;
  period?: string;
  count: number;
  messages: Array<{
    ts: number;
    timestamp: string;
    sender_wxid: string;
    sender_name: string;
    msg_type: string;
    text: string;
    is_self_sent?: boolean;
  }>;
};

export type SelfReviewSubscription = {
  session_id: string;
  session_name: string;
  enabled: boolean | number;
  daily_hour: number;
  tz: string;
  focus_mode?: string;
  auto_create_kb_doc?: boolean | number;
};

export type SelfReviewPreview = {
  job_id?: number;
  session_id: string;
  session_name: string;
  period?: string;
  status?: string;
  current_stage?: string;
  cached?: boolean;
  report?: string;
  count?: number;
  focused_message_count?: number;
  focused_thread_count?: number;
  bot_message_count?: number;
  trigger_message_count?: number;
  kb_doc_id?: number | null;
  kb_doc_title?: string;
  kb_doc_error?: string;
  kb_publish_status?: string;
};

export type SelfReviewJob = {
  id: number;
  session_id: string;
  session_name?: string;
  period_key?: string;
  period_label?: string;
  status?: string;
  current_stage?: string;
  msg_count?: number;
  kb_doc_id?: number | null;
  kb_doc_title?: string;
  kb_publish_status?: string;
  error?: string;
  created_at?: string | null;
  completed_at?: string | null;
  review_payload?: Record<string, unknown> & {
    kb_publish_status?: string;
    kb_doc_id?: number | null;
    kb_doc_title?: string;
  };
};

export type SelfReviewPublishResult = {
  ok?: boolean;
  job_id: number;
  kb_doc_id: number;
  kb_doc_title?: string;
  kb_publish_status: string;
  idempotent?: boolean;
  request_id?: string;
};

export type GroupActivityConfig = {
  tenant_id: string;
  session_id: string;
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
  version: number;
  updated_at?: string | null;
};

export type GroupActivityDecision = {
  status: string;
  reason?: string;
  reason_code?: string;
  message_count?: number;
  generated_text?: string;
  event_id?: number | null;
  reply_queue_id?: number | null;
  command_id?: string;
};

export type GroupActivityEvent = {
  id: number;
  session_id: string;
  session_name?: string;
  status?: string;
  message_count?: number;
  generated_text?: string;
  reply_queue_id?: number | null;
  trace_id?: string;
  reason_code?: string;
  error?: string;
  created_at?: string | null;
  completed_at?: string | null;
};

export type AgentToolCatalogItem = {
  scope?: string;
  name: string;
  description?: string;
  owner?: string;
  channels?: string[];
  session_kinds?: string[];
};

export type AgentToolPolicy = {
  tenant_id: string;
  session_id: string;
  enabled: boolean;
  allowed_tools: string[];
  available_tools: string[];
  effective_tools: string[];
  inherits_default_tools: boolean;
  policy_configured?: boolean;
  denial_reason?: string;
  updated_at?: string | null;
  scope?: string;
  unsupported?: boolean;
  version?: number;
};

export type AgentToolAuditItem = {
  id: number;
  tenant_id: string;
  session_id: string;
  user_id?: string;
  channel?: string;
  scope?: string;
  tool_name: string;
  tool_args?: Record<string, unknown>;
  tool_result?: unknown;
  tool_error?: string;
  latency_ms?: number;
  trace_id?: string;
  final_reply_text?: string;
  created_at?: string | null;
};

export type WxbotTab = "overview" | "policy" | "agent" | "events" | "reports" | "send";

export type ReportType = "daily" | "weekly" | "monthly";

export const WXBOT_TABS: WxbotTab[] = ["overview", "policy", "agent", "events", "reports", "send"];

export const DEFAULT_GROUP_PARTICIPATION_POLICY: GroupParticipationPolicy = {
  threshold: 60,
  quiet_start_hour: 23,
  quiet_end_hour: 8,
  timezone: "Asia/Shanghai",
  max_soft_replies_10m: 2,
  max_soft_replies_hour: 6,
  max_bot_ratio_last_40: 0.15,
  max_consecutive_bot_messages: 2,
};

export const DEFAULT_GROUP_ACTIVITY_FORM: Omit<GroupActivityConfig, "tenant_id" | "session_id"> = {
  session_name: "",
  enabled: false,
  active_start: "08:00",
  active_end: "17:00",
  quiet_start: "23:00",
  quiet_end: "08:00",
  timezone: "Asia/Shanghai",
  idle_minutes: 180,
  lookback_minutes: 120,
  min_send_interval_minutes: 180,
  max_per_day: 1,
  topic_repeat_window_minutes: 1440,
  llm_model_tier: "tier-2",
  temperature: 0.9,
  agent_tool_scope: "group_info",
  version: 0,
  updated_at: null,
};

export const GROUP_ACTIVITY_REASON_LABELS: Record<string, string> = {
  awaiting_human_response: "上一条暖场后还没人回应，先保持安静",
  bot_identity_unavailable: "无法确认机器人身份，已安全跳过",
  cooldown_active: "仍在最短发送间隔内",
  daily_limit_reached: "已达到今日暖场上限",
  disabled: "当前群未开启自动暖场",
  duplicate_topic: "与近期话题过于相似",
  event_already_running: "同一暖场任务正在处理中",
  generation_empty: "模型没有生成可用内容",
  generation_identity_deception: "内容未通过 AI 身份透明检查",
  generation_prompt_leak: "内容疑似泄露内部提示",
  generation_too_long: "内容过长，已安全跳过",
  group_not_idle: "群聊仍活跃，暂不打断",
  internal_error: "内部检查失败，未发送任何消息",
  missing_scope: "缺少租户或群会话范围",
  new_message_before_send: "发送前出现新消息，已取消暖场",
  not_enough_context: "近期有效聊天内容不足",
  outside_active_window: "当前不在允许暖场的活跃时段",
  quiet_hours: "当前处于静默时段",
  queued: "暖场消息已进入发送队列",
  slot_already_claimed: "当前时间槽已处理",
  would_trigger: "条件满足；正式运行时会发起暖场",
};

export function parseWxbotTab(value: string | null): WxbotTab {
  return WXBOT_TABS.includes(value as WxbotTab) ? (value as WxbotTab) : "overview";
}

export const WXBOT_ONBOARDING_TABS: Record<string, WxbotTab> = {
  connect: "overview",
  groups: "overview",
  participation: "policy",
  launch: "policy",
  test: "send",
};

export function readWxbotTabFromLocation(search: string, hash: string): WxbotTab {
  const searchParams = new URLSearchParams(search);
  const hashParams = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const explicitTab = searchParams.get("tab") || hashParams.get("tab");
  if (explicitTab) {
    return parseWxbotTab(explicitTab);
  }
  const onboardingStep = searchParams.get("onboarding") || hashParams.get("onboarding") || "";
  return WXBOT_ONBOARDING_TABS[onboardingStep.trim().toLowerCase()] || "overview";
}

export function normalizeGroupParticipationPolicy(
  value?: Partial<GroupParticipationPolicy> | null,
): GroupParticipationPolicy {
  return {
    ...DEFAULT_GROUP_PARTICIPATION_POLICY,
    ...(value || {}),
  };
}

export function createDefaultGroupActivityConfig(tenantId: string, sessionId: string): GroupActivityConfig {
  return {
    tenant_id: tenantId,
    session_id: sessionId,
    ...DEFAULT_GROUP_ACTIVITY_FORM,
  };
}

export function normalizeGroupActivityConfig(
  value: Partial<GroupActivityConfig> | null | undefined,
  tenantId: string,
  sessionId: string,
): GroupActivityConfig {
  const normalized = {
    ...createDefaultGroupActivityConfig(tenantId, sessionId),
    ...(value || {}),
    tenant_id: value?.tenant_id || tenantId,
    session_id: value?.session_id || sessionId,
    enabled: Boolean(value?.enabled),
  };
  return {
    ...normalized,
    idle_minutes: boundedNumber(normalized.idle_minutes, 180, 180),
    lookback_minutes: boundedNumber(normalized.lookback_minutes, 120, 60),
    min_send_interval_minutes: boundedNumber(
      normalized.min_send_interval_minutes,
      180,
      60,
    ),
    max_per_day: boundedNumber(normalized.max_per_day, 1, 1, 3),
    topic_repeat_window_minutes: boundedNumber(
      normalized.topic_repeat_window_minutes,
      1440,
      60,
      10080,
    ),
    temperature: boundedNumber(normalized.temperature, 0.9, 0, 2),
  };
}

function boundedNumber(
  value: unknown,
  fallback: number,
  minimum: number,
  maximum = Number.POSITIVE_INFINITY,
) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.min(maximum, Math.max(minimum, parsed));
}

export function groupActivityReasonLabel(reason?: string | null) {
  const normalized = String(reason || "").trim();
  return GROUP_ACTIVITY_REASON_LABELS[normalized] || normalized || "尚未执行检查";
}

export function groupActivityValidationError(config: GroupActivityConfig) {
  if (config.idle_minutes < 180) {
    return "群空闲时间不能少于 180 分钟";
  }
  if (config.min_send_interval_minutes < 60) {
    return "两次暖场间隔不能少于 60 分钟";
  }
  if (config.max_per_day < 1 || config.max_per_day > 3) {
    return "每日上限必须在 1–3 次之间";
  }
  if (config.topic_repeat_window_minutes < 60 || config.topic_repeat_window_minutes > 10080) {
    return "话题去重窗口必须在 60–10080 分钟之间";
  }
  if (config.temperature < 0 || config.temperature > 2) {
    return "生成温度必须在 0–2 之间";
  }
  return "";
}

export function selfReviewPublishStatus(
  item: Pick<SelfReviewPreview, "kb_publish_status" | "kb_doc_id"> & {
    review_payload?: SelfReviewJob["review_payload"];
  },
) {
  return item.kb_publish_status
    || String(item.review_payload?.kb_publish_status || "")
    || (item.kb_doc_id ? "published" : "pending_review");
}

export function selfReviewPublishLabel(status: string) {
  if (status === "published") {
    return "已发布";
  }
  if (status === "publishing") {
    return "发布中";
  }
  return "待人工审核";
}

export function replyParticipationSummary(item: ReplyQueueMessage) {
  const reasonCodes = Array.isArray(item.delivery?.participation_reason_codes)
    ? item.delivery.participation_reason_codes.map(String).filter(Boolean)
    : [];
  const decision = item.participation_status || String(item.delivery?.participation_status || "");
  const cancellation = item.status === "cancelled" ? item.error || "cancelled" : "";
  return [decision, ...reasonCodes, cancellation].filter(Boolean).join(" / ") || "-";
}

export function isGroupSession(session: Pick<WxbotSession, "session_id" | "kind">) {
  return (
    session.session_id.endsWith("@chatroom") ||
    session.kind === "group" ||
    session.kind === "chatroom"
  );
}

export function resolveExplicitVerifiedGroupSessionId(
  localSelection: string,
  globalSelection: string,
  verifiedGroups: readonly WxbotSession[],
) {
  const local = localSelection.trim();
  const global = globalSelection.trim();
  if (!local || local !== global) {
    return "";
  }
  return verifiedGroups.some((item) => item.session_id === local) ? local : "";
}

export function sessionDisplayName(session?: Pick<WxbotSession, "session_id" | "session_name"> | null) {
  if (!session) {
    return "-";
  }
  return session.session_name?.trim() || session.session_id;
}

export const AUDIT_RESULT_PREVIEW_LIMIT = 150;

export function clipAuditText(value: string, limit = AUDIT_RESULT_PREVIEW_LIMIT) {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= limit) {
    return text || "-";
  }
  return `${text.slice(0, limit - 1)}...`;
}

export function summarizeAuditInline(value: unknown): string {
  if (value === null || value === undefined) {
    return "-";
  }
  if (typeof value === "string") {
    return clipAuditText(value, 80);
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return `${value.length} 项`;
  }
  if (typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>);
    return keys.length ? `{ ${keys.slice(0, 4).join(", ")}${keys.length > 4 ? ", ..." : ""} }` : "{}";
  }
  return clipAuditText(String(value), 80);
}

export function summarizeAuditResult(value: unknown) {
  if (value === undefined) {
    return "-";
  }
  if (value === null) {
    return "null";
  }
  if (typeof value === "string") {
    return clipAuditText(value);
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    if (!value.length) {
      return "空数组";
    }
    return clipAuditText(`数组 ${value.length} 项 · 首项 ${summarizeAuditInline(value[0])}`);
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record);
    if (!keys.length) {
      return "空对象";
    }
    const priorityKeys = [
      "ok",
      "status",
      "enabled",
      "count",
      "total",
      "member_count",
      "session_name",
      "session_id",
      "error",
      "message",
      "summary",
      "items",
      "members",
      "sessions",
      "events",
      "result",
    ];
    const pickedKeys = priorityKeys.filter((key) => key in record).slice(0, 4);
    const displayKeys = pickedKeys.length ? pickedKeys : keys.slice(0, 4);
    const summary = displayKeys
      .map((key) => `${key}: ${summarizeAuditInline(record[key])}`)
      .join(" · ");
    return clipAuditText(summary || `对象 ${keys.length} 字段`);
  }
  return clipAuditText(String(value));
}

export function formatAgentToolScope(values?: string[]) {
  if (!values?.length) {
    return "全部";
  }
  return values.join(", ");
}

export function wxbotBooleanLabel(value?: boolean | null, trueLabel = "是", falseLabel = "否") {
  return value ? trueLabel : falseLabel;
}

export function wxbotQueueStatusLabel(status?: string | null) {
  const labels: Record<string, string> = {
    pending: "等待发送",
    running: "发送中",
    sent: "已发送",
    failed: "发送失败",
    cancelled: "已取消",
    uncertain: "结果待核对",
    cleared: "已清理",
  };
  return labels[status || ""] || status || "未知";
}

export function wxbotReplyModeLabel(mode?: string | null) {
  const labels: Record<string, string> = {
    all: "全部消息",
    contains: "包含触发词",
    off: "关闭",
    on: "开启",
    inherit: "继承全局策略",
  };
  return labels[mode || ""] || mode || "未知";
}

export function wxbotEventTypeLabel(eventType?: string | null) {
  const labels: Record<string, string> = {
    "group.member.joined": "成员加入群聊",
    "group.member.left": "成员离开群聊",
  };
  return labels[eventType || ""] || eventType || "未知事件";
}

export function wxbotSessionKindLabel(kind?: string | null) {
  const labels: Record<string, string> = {
    group: "群聊",
    chatroom: "群聊",
    private: "私聊",
    direct: "私聊",
  };
  return labels[kind || ""] || kind || "未知";
}

export function wxbotBridgeModeLabel(mode?: string | null) {
  const labels: Record<string, string> = {
    "unified-sse": "统一 SSE 接入",
    sse: "SSE 事件流",
    standby: "待机",
    polling: "轮询",
  };
  return labels[mode || ""] || mode || "-";
}

export function wxbotMessageTypeLabel(messageType?: string | null) {
  const labels: Record<string, string> = {
    text: "文本",
    image: "图片",
    file: "文件",
  };
  return labels[messageType || ""] || messageType || "未知";
}

export function wxbotOperationStatusLabel(status?: string | null) {
  const labels: Record<string, string> = {
    idle: "尚未读取",
    loading: "读取中",
    loaded: "已读取",
    saving: "保存中",
    saved: "已保存",
    conflict: "版本冲突",
    error: "出错",
    pending: "等待中",
    running: "运行中",
    completed: "已完成",
    succeeded: "已完成",
    failed: "失败",
    skipped: "已跳过",
    blocked: "已拦截",
    sent: "已发送",
    enabled: "已启用",
    off: "已关闭",
  };
  return labels[status || ""] || status || "未知";
}

export function wxbotSessionStateLabel(state?: string | null) {
  const labels: Record<string, string> = {
    chatting: "自动回复中",
    escalated: "已暂停，等待人工接管",
  };
  return labels[state || ""] || state || "未知";
}

export function wxbotModelTierLabel(tier?: string | null) {
  const labels: Record<string, string> = {
    "tier-1": "轻量模型",
    "tier-2": "默认模型",
    "tier-3": "复杂场景模型",
  };
  return labels[tier || ""] || tier || "未知";
}

export function wxbotJobStageLabel(stage?: string | null) {
  const labels: Record<string, string> = {
    queued: "已排队",
    collect_messages: "收集消息",
    summarize: "生成总结",
    publish: "发布中",
    completed: "已完成",
    done: "已完成",
  };
  const normalized = stage || "";
  const chunkMatch = normalized.match(/^summarize_chunk_(\d+)$/);
  if (chunkMatch) return `总结分段 ${chunkMatch[1]}`;
  return labels[normalized] || normalized || "-";
}

export function wxbotReportPeriodLabel(period?: string | null) {
  const labels: Record<string, string> = {
    daily: "日报",
    weekly: "周报",
    monthly: "月报",
  };
  return labels[period || ""] || period || "-";
}

export function isExpandableAuditResult(value: unknown) {
  if (value === undefined || value === null) {
    return false;
  }
  if (Array.isArray(value) || typeof value === "object") {
    return true;
  }
  return typeof value === "string" && value.replace(/\s+/g, " ").trim().length > AUDIT_RESULT_PREVIEW_LIMIT;
}

export function AgentAuditResultCell({ item }: { item: AgentToolAuditItem }) {
  const hasResult = item.tool_result !== undefined;
  const fullResult = hasResult ? formatJson(item.tool_result) : "";
  const canExpand = isExpandableAuditResult(item.tool_result);

  return (
    <div className="agent-audit-result">
      <div className="agent-audit-status-row">
        {item.tool_error ? (
          <span className="pill pill-danger" title={item.tool_error}>{clipAuditText(item.tool_error, 96)}</span>
        ) : (
          <span className="pill pill-ok">成功</span>
        )}
        <span className="agent-audit-result-summary">
          {hasResult ? summarizeAuditResult(item.tool_result) : "-"}
        </span>
      </div>
      {canExpand && (
        <details className="agent-audit-result-details">
          <summary>完整结果</summary>
          <pre>{fullResult}</pre>
        </details>
      )}
    </div>
  );
}

export function normalizeGroupReplyMode(mode?: string | null) {
  if ((mode || "").trim().toLowerCase() === "all") {
    return "contains";
  }
  return mode || "off";
}

export function normalizeSessionReplyMode(mode?: string | null, isGroup = false) {
  const cleaned = mode || "inherit";
  if (isGroup && cleaned === "all") {
    return "contains";
  }
  return cleaned;
}

export function formatEventTime(ts?: number) {
  if (!ts) {
    return "-";
  }
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return String(ts);
  }
}

export function formatDateValue(value?: string | null) {
  if (!value) {
    return "-";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

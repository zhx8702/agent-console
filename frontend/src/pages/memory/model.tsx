import { ApiError } from "../../lib/api";

type WxbotSession = {
  session_id: string;
  session_name: string;
  kind?: string;
};

type GroupRosterCandidate = {
  wxid: string;
  display_name?: string;
  name?: string;
  alias?: string;
  remark?: string;
  nick_name?: string;
  msg_count?: number;
};

type IdentityProfile = {
  tenant_id: string;
  channel: string;
  source_key: string;
  user_id: string;
  long_term_memory?: string;
  manual_notes?: string;
  message_count?: number;
  imported_message_count?: number;
  last_session_id?: string;
  updated_at?: string | null;
};

type SessionProfile = {
  tenant_id: string;
  channel: string;
  source_key: string;
  session_id: string;
  user_id: string;
  short_term_memory?: string;
  manual_notes?: string;
  message_count?: number;
  imported_message_count?: number;
  updated_at?: string | null;
};

type RuntimeProfile = {
  tenant_id: string;
  channel: string;
  source_key: string;
  session_id: string;
  user_id: string;
  short_term_memory?: string;
  long_term_memory?: string;
  manual_notes?: string;
  identity_manual_notes?: string;
  session_manual_notes?: string;
  message_count?: number;
  identity_message_count?: number;
  session_message_count?: number;
  imported_message_count?: number;
  session_imported_message_count?: number;
  last_session_id?: string;
  identity_profile?: IdentityProfile;
  session_profile?: SessionProfile;
};

type MemoryEvent = {
  id: number;
  tenant_id: string;
  channel: string;
  source_key: string;
  user_id: string;
  session_id?: string;
  user_text?: string;
  assistant_text?: string;
  trace_id?: string;
  created_at?: string | null;
};

type MemoryItem = {
  id: number;
  tenant_id: string;
  channel: string;
  source_key: string;
  user_id: string;
  session_id?: string;
  scope_type: string;
  source_type: string;
  source_kind?: string;
  audience_scope?: string;
  origin_session_kind?: string;
  allowed_session_ids?: string[];
  memory_type: string;
  content: string;
  status: string;
  confidence: number;
  acceptance_status?: string;
  acceptance_score?: number | null;
  acceptance_reason?: string;
  acceptance_signals?: Record<string, number>;
  acceptance_history?: AcceptanceReviewHistoryEntry[];
  extraction_confidence?: number | null;
  superseded_by_item_id?: number | null;
  supersedes_item_id?: number | null;
  duplicate_hint?: MemoryDuplicateHint;
  possible_conflicts?: MemoryPossibleConflicts;
  value?: {
    acceptance?: {
      history?: AcceptanceReviewHistoryEntry[];
      superseded_by_item_id?: number | null;
      supersedes_item_id?: number | null;
    };
  };
  pinned: boolean;
  priority: number;
  sensitivity: string;
  occurrence_count: number;
  last_seen_at?: string | null;
  expires_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
};

type AcceptanceReviewHistoryEntry = {
  action?: string;
  status?: string;
  reason?: string;
  reviewed_at?: string;
  reviewed_by?: string;
  previous_status?: string;
  previous_acceptance_status?: string;
  previous_item_status?: string;
  current_item_status?: string;
  superseded_by_item_id?: number | null;
  supersedes_item_id?: number | null;
};

type MemoryDuplicateHint = {
  count?: number;
  ids?: Array<number | string | null>;
  normalized_key?: string;
};

type MemoryPossibleConflictItem = {
  id?: number | string | null;
  status?: string;
  acceptance_status?: string;
  normalized_key?: string;
};

type MemoryPossibleConflicts = {
  type?: string;
  count?: number;
  ids?: Array<number | string | null>;
  normalized_key?: string;
  items?: MemoryPossibleConflictItem[];
};

type MemoryGraphEntity = {
  id: number;
  entity_type?: string;
  name?: string;
  normalized_name?: string;
  aliases?: string[];
  status?: string;
  confidence?: number;
  memory_item_id?: number | null;
  source_event_id?: number | null;
  memory_item_ids?: number[];
  source_event_ids?: number[];
  event_ids?: number[];
  updated_at?: string | null;
};

type MemoryGraphFact = {
  id: number;
  subject_name?: string;
  subject_entity_id?: number;
  predicate?: string;
  object_name?: string;
  object_entity_id?: number;
  object_value?: string;
  status?: string;
  confidence?: number;
  memory_item_id?: number | null;
  source_event_id?: number | null;
  valid_at?: string | null;
  invalid_at?: string | null;
  updated_at?: string | null;
};

type MemoryGraphEpisode = {
  id: number;
  title?: string;
  summary?: string;
  status?: string;
  importance?: number;
  session_id?: string;
  event_ids?: number[];
  memory_item_ids?: number[];
  updated_at?: string | null;
};

type MemoryGraphPreview = {
  counts?: {
    entities?: number;
    facts?: number;
    episodes?: number;
  };
  entities?: MemoryGraphEntity[];
  facts?: MemoryGraphFact[];
  episodes?: MemoryGraphEpisode[];
};

type MemoryGraphMode = "overview" | "explore" | "review" | "raw";
type MemoryGraphTab = "preview" | "entities" | "facts" | "episodes";
type MemoryGraphSort = "updated_desc" | "confidence_desc" | "name_asc";
type MemoryGraphEmptyContext = "preview" | "entities" | "facts" | "episodes" | "review";
type MemoryListUserScope = "all" | "current_user";

type MemoryGraphSelection =
  | { kind: "entity"; item: MemoryGraphEntity }
  | { kind: "fact"; item: MemoryGraphFact }
  | { kind: "episode"; item: MemoryGraphEpisode };

type MemoryGraphSelectionKind = MemoryGraphSelection["kind"];

type GraphQualityKey = "low_confidence" | "no_evidence" | "stale" | "inactive_status";

type MemoryGraphReviewEntry = {
  key: string;
  kind: MemoryGraphSelectionKind;
  item: MemoryGraphEntity | MemoryGraphFact | MemoryGraphEpisode;
  label: string;
};

type ExtractionJobStatus = "pending" | "running" | "succeeded" | "failed" | "dead";

type ExtractionJobAction = "retry" | "mark_dead" | "reset_stale" | "cleanup_smoke";

type AcceptanceReviewAction = "accept" | "reject" | "needs_review" | "mark_joke" | "expire" | "supersede";
type AcceptanceQueueFilter = "" | "candidate" | "needs_review" | "rejected" | "accepted" | "superseded" | "expired";
type ProfileEnrichmentReviewState = "" | "candidate" | "needs_review" | "accepted" | "rejected" | "hidden";
type ProfileEnrichmentReviewAction = "accept" | "reject" | "hide";

const ACCEPTANCE_REVIEW_ACTIONS = [
  { action: "accept", label: "接受记忆", effect: "记忆会进入 accepted 状态，并可被正常召回用于回复。" },
  { action: "reject", label: "拒绝记忆", effect: "记忆会进入 rejected 状态，不再参与正常召回。" },
  { action: "needs_review", label: "标记待复核", effect: "记忆会回到 needs_review 队列，等待后续人工判断。" },
  { action: "mark_joke", label: "标记为玩笑", effect: "记忆会被标记为玩笑内容，避免把玩笑当成稳定事实。" },
  { action: "expire", label: "使记忆过期", effect: "记忆会进入 expired 状态，不再参与正常召回。" },
] satisfies Array<{
  action: Exclude<AcceptanceReviewAction, "supersede">;
  label: string;
  effect: string;
}>;

const PROFILE_ENRICHMENT_REVIEW_ACTIONS = [
  { action: "accept", label: "通过候选", effect: "候选会进入 accepted 状态，并可用于更新成员画像。" },
  { action: "reject", label: "拒绝候选", effect: "候选会进入 rejected 状态，不会用于更新成员画像。" },
  { action: "hide", label: "隐藏候选", effect: "候选会进入 hidden 状态，并从默认待复核列表中隐藏。" },
] satisfies Array<{
  action: ProfileEnrichmentReviewAction;
  label: string;
  effect: string;
}>;

type ExtractionJobScopeCount = {
  tenant_id?: string | null;
  channel?: string | null;
  source_key?: string | null;
  user_id?: string | null;
  session_id?: string | null;
  status?: string | null;
  error_type?: string | null;
  count?: number;
};

type ExtractionJobStats = {
  counts?: Record<string, number>;
  status_counts?: Record<string, number>;
  error_type_counts?: Record<string, number>;
  scope_counts?: ExtractionJobScopeCount[];
};

type ExtractionJobMaintenanceActionResult = {
  action?: string;
  dry_run?: boolean;
  would_affect?: number;
  affected?: number;
  ids?: number[];
  error?: string;
};

type ExtractionJobMaintenanceResult = {
  dry_run?: boolean;
  limit?: number;
  would_affect?: number;
  affected?: number;
  ids?: number[];
  results?: ExtractionJobMaintenanceActionResult[];
};

type AcceptanceLegacyAuditGroup = {
  scope_type?: string;
  status?: string;
  memory_type?: string;
  source_type?: string;
  count?: number;
  ids_preview?: Array<number | string | null>;
  ids_truncated?: number;
  suggested_action?: string;
};

type AcceptanceStats = {
  total?: number;
  counts?: Record<string, number>;
  sensitivity_counts?: Record<string, number>;
  status_counts?: Record<string, number>;
  source_type_counts?: Record<string, number>;
  memory_type_counts?: Record<string, number>;
  scope_type_counts?: Record<string, number>;
  ids_preview?: Array<number | string | null>;
  ids_truncated?: number;
  limit?: number;
};

type AcceptanceLegacyAudit = {
  dry_run?: boolean;
  missing_acceptance?: number;
  suggested_action?: string;
  groups?: AcceptanceLegacyAuditGroup[];
  ids_preview?: Array<number | string | null>;
  ids_truncated?: number;
  limit?: number;
};

type AcceptanceLegacyBackfillResult = {
  dry_run?: boolean;
  mark_missing_as?: string;
  would_affect?: number;
  affected?: number;
  ids_preview?: Array<number | string | null>;
  ids?: Array<number | string | null>;
  ids_truncated?: number;
  skipped_ids?: Array<number | string | null>;
  skipped_truncated?: number;
  limit?: number;
};

type BackfillResult = {
  ok?: boolean;
  user_id?: string;
  user_id_scope?: string;
  user_id_auto?: boolean;
  processed_count?: number;
  imported_count?: number;
  skipped_count?: number;
  duplicate_count?: number;
  events_inserted?: number;
  items_created?: number;
  items_updated?: number;
  jobs_enqueued?: number;
  session_count?: number;
  sessions?: Array<{
    session_id: string;
    imported_count?: number;
    first_timestamp?: string;
    last_timestamp?: string;
  }>;
};

type ProfileEnrichmentCandidate = MemoryItem & {
  created_at?: string | null;
  normalized_key?: string;
  value?: {
    report?: Record<string, unknown>;
    review?: {
      state?: string;
      notes?: string;
      reviewed_by?: string;
      reviewed_at?: string;
      created_by?: string;
      created_at?: string;
    };
    acceptance?: {
      status?: string;
      score?: number | null;
      reason?: string;
      review_reason?: string;
      reviewed_by?: string;
      reviewed_at?: string;
      history?: AcceptanceReviewHistoryEntry[];
    };
  };
};

const EXTRACTION_JOB_STATUSES: ExtractionJobStatus[] = ["pending", "running", "succeeded", "failed", "dead"];

const GRAPH_LOW_CONFIDENCE_THRESHOLD = 0.5;
const GRAPH_STALE_DAYS = 30;

const GRAPH_QUALITY_LABELS: Record<GraphQualityKey, string> = {
  low_confidence: "低置信度",
  no_evidence: "缺少证据",
  stale: "长期未更新",
  inactive_status: "已失效或删除",
};

const GRAPH_REVIEW_GROUPS: Array<{ key: GraphQualityKey; label: string; hint: string }> = [
  { key: "low_confidence", label: "低置信度", hint: `置信度或重要性低于 ${GRAPH_LOW_CONFIDENCE_THRESHOLD}` },
  { key: "no_evidence", label: "缺少证据", hint: "没有关联的记忆、来源或事件 ID" },
  { key: "stale", label: `超过 ${GRAPH_STALE_DAYS} 天未更新`, hint: "更新时间早于复核窗口" },
  { key: "inactive_status", label: "已失效或删除", hint: "状态为 invalidated 或 deleted" },
];

const EXTRACTION_JOB_ACTIONS: Array<{ value: ExtractionJobAction; label: string; hint: string }> = [
  { value: "retry", label: "重试（retry）", hint: "将失败、终止或待处理任务重置为待处理" },
  { value: "mark_dead", label: "标记终止（mark_dead）", hint: "将匹配任务标记为已终止" },
  { value: "reset_stale", label: "释放超时任务（reset_stale）", hint: "释放长期停留在运行中的任务" },
  { value: "cleanup_smoke", label: "清理测试任务（cleanup_smoke）", hint: "仅清理 smoke/test 测试范围" },
];

const MEMORY_ITEM_STATUS_LABELS: Record<string, string> = {
  active: "有效",
  pending: "待处理",
  archived: "已归档",
  invalidated: "已失效",
  deleted: "已删除",
};

const MEMORY_SOURCE_TYPE_LABELS: Record<string, string> = {
  manual: "管理员手工添加",
  explicit_user: "用户明确提供",
  auto: "自动提取",
  backfill: "历史回填",
};

const MEMORY_SCOPE_TYPE_LABELS: Record<string, string> = {
  session: "当前会话",
  identity: "全局身份",
};

const MEMORY_SENSITIVITY_LABELS: Record<string, string> = {
  normal: "普通",
  pii: "个人信息",
  private: "私密",
  sensitive: "敏感",
};

const MEMORY_AUDIENCE_SCOPE_LABELS: Record<string, string> = {
  private: "仅本人",
  session: "当前群会话",
  explicit: "指定会话",
};

const MEMORY_ORIGIN_SESSION_KIND_LABELS: Record<string, string> = {
  private: "私聊",
  group: "群聊",
  unknown: "未知来源",
};

const ACCEPTANCE_STATUS_LABELS: Record<string, string> = {
  missing: "缺少复核元数据",
  "legacy/absent": "旧版记录（未复核）",
  candidate: "候选",
  needs_review: "待复核",
  accepted: "已接受",
  rejected: "已拒绝",
  superseded: "已被取代",
  expired: "已过期",
  joke: "玩笑内容",
};

const EXTRACTION_JOB_STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  dead: "已终止",
};

const EXTRACTION_JOB_ID_PREVIEW_LIMIT = 25;
const ACCEPTANCE_ID_PREVIEW_LIMIT = 25;
const PLACEHOLDER_USER_IDS = new Set(["default-user", "default_user", "user", "test-user", "test_user"]);
const PLACEHOLDER_SESSION_IDS = new Set(["default-session", "default_session", "session", "test-session", "test_session"]);

function isPlaceholderUserId(value?: string | null) {
  return PLACEHOLDER_USER_IDS.has(String(value || "").trim().toLowerCase());
}

function isPlaceholderSessionId(value?: string | null) {
  return PLACEHOLDER_SESSION_IDS.has(String(value || "").trim().toLowerCase());
}

function hasIdentityProfileContent(profile: IdentityProfile) {
  return Boolean(
    profile.long_term_memory?.trim() ||
      profile.manual_notes?.trim() ||
      Number(profile.message_count || 0) > 0 ||
      Number(profile.imported_message_count || 0) > 0 ||
      profile.updated_at,
  );
}

function isGroupSession(session: Pick<WxbotSession, "session_id" | "kind">) {
  return (
    session.session_id.endsWith("@chatroom") ||
    session.kind === "group" ||
    session.kind === "chatroom"
  );
}

function mergeSessions(
  sessions: WxbotSession[],
  rosterGroups: WxbotSession[],
) {
  const merged = new Map<string, WxbotSession>();
  for (const item of sessions) {
    if (!item.session_id) {
      continue;
    }
    merged.set(item.session_id, item);
  }
  for (const item of rosterGroups) {
    if (!item.session_id) {
      continue;
    }
    const current = merged.get(item.session_id);
    merged.set(item.session_id, {
      session_id: item.session_id,
      session_name: item.session_name || current?.session_name || item.session_id,
      kind: item.kind || current?.kind || "group",
    });
  }
  return Array.from(merged.values());
}

function getMemberDisplayName(member: GroupRosterCandidate) {
  return member.display_name || member.name || member.remark || member.alias || member.nick_name || member.wxid;
}

function getMemberProfileQuery(member: GroupRosterCandidate) {
  return member.display_name || member.name || member.remark || member.alias || member.nick_name || member.wxid;
}

function parseSessionIds(raw: string) {
  return Array.from(
    new Set(
      raw
        .split(/\r?\n|,/)
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

function formatConfidence(value?: number | null) {
  if (value === undefined || value === null) {
    return "-";
  }
  const parsed = Number(value);
  return Number.isNaN(parsed) ? "-" : parsed.toFixed(2);
}

function acceptanceStatusOf(item: MemoryItem) {
  return item.acceptance_status || "legacy/absent";
}

function acceptanceSignalEntries(item: MemoryItem) {
  return Object.entries(item.acceptance_signals || {})
    .filter(([, value]) => value !== null && value !== undefined)
    .sort(([left], [right]) => left.localeCompare(right));
}

function acceptanceHistoryOf(item: MemoryItem) {
  const direct = Array.isArray(item.acceptance_history) ? item.acceptance_history : [];
  const nested = Array.isArray(item.value?.acceptance?.history) ? item.value.acceptance.history : [];
  return (direct.length ? direct : nested).slice(-5).reverse();
}

function supersededByItemIdOf(item: MemoryItem) {
  return item.superseded_by_item_id ?? item.value?.acceptance?.superseded_by_item_id ?? null;
}

function supersedesItemIdOf(item: MemoryItem) {
  return item.supersedes_item_id ?? item.value?.acceptance?.supersedes_item_id ?? null;
}

function optionalText(value: string) {
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

function friendlyApiError(err: unknown, fallback: string) {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) {
      return `${fallback}: 当前身份无权限或登录已失效`;
    }
    return `${fallback}: ${err.message}`;
  }
  return err instanceof Error ? `${fallback}: ${err.message}` : fallback;
}

function hasSmokeOrTestScope(filters: Partial<Record<string, string | number | boolean | undefined>>) {
  return ["tenant_id", "channel", "source_key", "user_id", "session_id"].some((key) => {
    const value = String(filters[key] || "").toLowerCase();
    return value.includes("smoke") || value.includes("test");
  });
}

function summarizeExtractionJobResult(result: ExtractionJobMaintenanceResult) {
  const ids = result.ids || [];
  return {
    dry_run: Boolean(result.dry_run),
    limit: result.limit,
    would_affect: result.would_affect ?? 0,
    affected: result.affected ?? 0,
    ids: ids.slice(0, EXTRACTION_JOB_ID_PREVIEW_LIMIT),
    ids_truncated: Math.max(0, ids.length - EXTRACTION_JOB_ID_PREVIEW_LIMIT),
    results: (result.results || []).map((item) => {
      const actionIds = item.ids || [];
      return {
        action: item.action,
        dry_run: Boolean(item.dry_run),
        would_affect: item.would_affect ?? 0,
        affected: item.affected ?? 0,
        ids: actionIds.slice(0, EXTRACTION_JOB_ID_PREVIEW_LIMIT),
        ids_truncated: Math.max(0, actionIds.length - EXTRACTION_JOB_ID_PREVIEW_LIMIT),
        error: item.error,
      };
    }),
  };
}

function previewIds(ids?: Array<number | string | null>, truncated?: number) {
  const preview = (ids || [])
    .filter((item) => item !== null && item !== undefined)
    .slice(0, ACCEPTANCE_ID_PREVIEW_LIMIT)
    .map((item) => `#${item}`)
    .join(", ");
  const suffix = Number(truncated || 0) > 0 ? `，另有 ${truncated} 条` : "";
  return preview ? `${preview}${suffix}` : "-";
}

function graphStatusPillClass(status?: string) {
  if (status === "active") {
    return "pill pill-ok";
  }
  if (status === "deleted" || status === "invalidated") {
    return "pill pill-danger";
  }
  return "pill pill-muted";
}

const GRAPH_ENTITY_TYPE_LABELS: Record<string, string> = {
  brand: "品牌",
  product: "产品",
  person: "人",
  user: "用户",
  organization: "组织",
  org: "组织",
  location: "地点",
  place: "地点",
  topic: "话题",
  preference: "偏好",
  constraint: "约束",
  note: "备注",
};

const GRAPH_PREDICATE_LABELS: Record<string, string> = {
  likes: "喜欢",
  dislikes: "不喜欢",
  prefers: "偏好",
  preference: "偏好",
  wants: "想要",
  needs: "需要",
  owns: "拥有",
  uses: "使用",
  asked_about: "问过",
  interested_in: "关注",
  constraint: "约束",
  note: "备注",
  related_to: "相关",
};

const GRAPH_STATUS_LABELS: Record<string, string> = {
  active: "有效",
  pending: "待确认",
  archived: "归档",
  invalidated: "已失效",
  deleted: "已删除",
};

function graphHumanLabel(value?: string, labels: Record<string, string> = {}) {
  if (!value) {
    return "-";
  }
  const normalized = String(value).trim();
  return labels[normalized] ? `${labels[normalized]} (${normalized})` : normalized.replace(/_/g, " ");
}

function graphStatusLabel(status?: string) {
  return graphHumanLabel(status, GRAPH_STATUS_LABELS);
}

function memoryItemStatusLabel(status?: string) {
  return graphHumanLabel(status, MEMORY_ITEM_STATUS_LABELS);
}

function memorySourceTypeLabel(sourceType?: string) {
  return graphHumanLabel(sourceType, MEMORY_SOURCE_TYPE_LABELS);
}

function memoryScopeTypeLabel(scopeType?: string) {
  return graphHumanLabel(scopeType, MEMORY_SCOPE_TYPE_LABELS);
}

function memorySensitivityLabel(sensitivity?: string) {
  return graphHumanLabel(sensitivity, MEMORY_SENSITIVITY_LABELS);
}

function memoryAudienceScopeLabel(audienceScope?: string) {
  return graphHumanLabel(audienceScope, MEMORY_AUDIENCE_SCOPE_LABELS);
}

function memoryOriginSessionKindLabel(originSessionKind?: string) {
  return graphHumanLabel(originSessionKind, MEMORY_ORIGIN_SESSION_KIND_LABELS);
}

function acceptanceStatusLabel(status?: string) {
  return graphHumanLabel(status || "legacy/absent", ACCEPTANCE_STATUS_LABELS);
}

function extractionJobStatusLabel(status?: string) {
  return graphHumanLabel(status, EXTRACTION_JOB_STATUS_LABELS);
}

function graphEntityLabel(item: MemoryGraphEntity) {
  return item.name || item.normalized_name || `实体 #${item.id}`;
}

function graphFactSubject(item: MemoryGraphFact) {
  return item.subject_name || (item.subject_entity_id ? `实体 #${item.subject_entity_id}` : "未命名主体");
}

function graphFactObject(item: MemoryGraphFact) {
  return item.object_name || item.object_value || (item.object_entity_id ? `实体 #${item.object_entity_id}` : "未命名对象");
}

function graphFactSentence(item: MemoryGraphFact) {
  return `${graphFactSubject(item)} ${graphHumanLabel(item.predicate, GRAPH_PREDICATE_LABELS)} ${graphFactObject(item)}`;
}

function graphEpisodeLabel(item: MemoryGraphEpisode) {
  return item.title || `事件片段 #${item.id}`;
}

function graphReviewItemLabel(
  kind: MemoryGraphSelectionKind,
  item: MemoryGraphEntity | MemoryGraphFact | MemoryGraphEpisode,
) {
  if (kind === "entity") {
    return graphEntityLabel(item as MemoryGraphEntity);
  }
  if (kind === "fact") {
    return graphFactSentence(item as MemoryGraphFact);
  }
  return graphEpisodeLabel(item as MemoryGraphEpisode);
}

function countDefinedIds(values: Array<number | string | null | undefined> | undefined) {
  return (values || []).filter((value) => value !== null && value !== undefined && value !== "").length;
}

function hasDefinedId(value: number | string | null | undefined) {
  return value !== null && value !== undefined && value !== "";
}

function graphEntityHasEvidence(item: MemoryGraphEntity) {
  return (
    hasDefinedId(item.memory_item_id) ||
    hasDefinedId(item.source_event_id) ||
    countDefinedIds(item.memory_item_ids) > 0 ||
    countDefinedIds(item.source_event_ids) > 0 ||
    countDefinedIds(item.event_ids) > 0
  );
}

function graphFactHasEvidence(item: MemoryGraphFact) {
  return hasDefinedId(item.memory_item_id) || hasDefinedId(item.source_event_id);
}

function graphEpisodeHasEvidence(item: MemoryGraphEpisode) {
  return countDefinedIds(item.memory_item_ids) > 0 || countDefinedIds(item.event_ids) > 0;
}

function graphItemId(kind: MemoryGraphSelectionKind, id: number) {
  return `${kind}:${id}`;
}

function graphTabForKind(kind: MemoryGraphSelectionKind): MemoryGraphTab {
  if (kind === "entity") {
    return "entities";
  }
  if (kind === "fact") {
    return "facts";
  }
  return "episodes";
}

function isGraphItemStale(item: { updated_at?: string | null }, now = Date.now()) {
  if (!item.updated_at) {
    return false;
  }
  const updatedAt = new Date(item.updated_at).getTime();
  if (Number.isNaN(updatedAt)) {
    return false;
  }
  return now - updatedAt > GRAPH_STALE_DAYS * 24 * 60 * 60 * 1000;
}

function isGraphInactiveStatus(status?: string) {
  return status === "deleted" || status === "invalidated";
}

function getGraphQualityKeys(
  kind: MemoryGraphSelectionKind,
  item: MemoryGraphEntity | MemoryGraphFact | MemoryGraphEpisode,
  now = Date.now(),
): GraphQualityKey[] {
  const flags: GraphQualityKey[] = [];
  const score = kind === "episode" ? (item as MemoryGraphEpisode).importance : (item as MemoryGraphEntity | MemoryGraphFact).confidence;
  if (score !== undefined && score !== null && Number(score) < GRAPH_LOW_CONFIDENCE_THRESHOLD) {
    flags.push("low_confidence");
  }
  const hasEvidence =
    kind === "entity"
      ? graphEntityHasEvidence(item as MemoryGraphEntity)
      : kind === "fact"
        ? graphFactHasEvidence(item as MemoryGraphFact)
        : graphEpisodeHasEvidence(item as MemoryGraphEpisode);
  if (!hasEvidence) {
    flags.push("no_evidence");
  }
  if (isGraphItemStale(item, now)) {
    flags.push("stale");
  }
  if (isGraphInactiveStatus(item.status)) {
    flags.push("inactive_status");
  }
  return flags;
}

function graphQualityBadges(
  kind: MemoryGraphSelectionKind,
  item: MemoryGraphEntity | MemoryGraphFact | MemoryGraphEpisode,
) {
  const flags = getGraphQualityKeys(kind, item);
  if (!flags.length) {
    return null;
  }
  return (
    <span className="memory-graph-quality-badges" aria-label="质量提醒">
      {flags.map((flag) => (
        <span className={`memory-graph-quality-badge is-${flag}`} key={flag}>
          {GRAPH_QUALITY_LABELS[flag]}
        </span>
      ))}
    </span>
  );
}

function graphQualityReportItems(
  entities: MemoryGraphEntity[],
  facts: MemoryGraphFact[],
  episodes: MemoryGraphEpisode[],
) {
  return [
    ...entities.map((item) => ({ kind: "entity" as const, item })),
    ...facts.map((item) => ({ kind: "fact" as const, item })),
    ...episodes.map((item) => ({ kind: "episode" as const, item })),
  ];
}

function copyTextLabelList(values: Array<number | string | null | undefined> | undefined) {
  const ids = (values || []).filter((value) => value !== null && value !== undefined && value !== "");
  return ids.map(String).join("\n");
}

function safeMemoryGraphSelectionPayload(selection: MemoryGraphSelection) {
  if (selection.kind === "entity") {
    const item = selection.item;
    return {
      kind: selection.kind,
      id: item.id,
      entity_type: item.entity_type,
      name: item.name,
      normalized_name: item.normalized_name,
      aliases: item.aliases,
      status: item.status,
      confidence: item.confidence,
      memory_item_id: item.memory_item_id,
      source_event_id: item.source_event_id,
      memory_item_ids: item.memory_item_ids,
      source_event_ids: item.source_event_ids,
      event_ids: item.event_ids,
      updated_at: item.updated_at,
    };
  }
  if (selection.kind === "fact") {
    const item = selection.item;
    return {
      kind: selection.kind,
      id: item.id,
      subject_name: item.subject_name,
      subject_entity_id: item.subject_entity_id,
      predicate: item.predicate,
      object_name: item.object_name,
      object_entity_id: item.object_entity_id,
      object_value: item.object_value,
      status: item.status,
      confidence: item.confidence,
      memory_item_id: item.memory_item_id,
      source_event_id: item.source_event_id,
      valid_at: item.valid_at,
      invalid_at: item.invalid_at,
      updated_at: item.updated_at,
    };
  }
  const item = selection.item;
  return {
    kind: selection.kind,
    id: item.id,
    title: item.title,
    summary: item.summary ? "[redacted]" : undefined,
    status: item.status,
    importance: item.importance,
    session_id: item.session_id,
    event_ids: item.event_ids,
    memory_item_ids: item.memory_item_ids,
    updated_at: item.updated_at,
  };
}

function safeRuntimeProfileDebug(result: RuntimeProfile) {
  return {
    status: "runtime_profile_loaded",
    scope: {
      tenant_id: result.tenant_id,
      channel: result.channel,
      source_key: result.source_key,
      user_id: result.user_id,
      session_id: result.session_id,
    },
    counts: {
      identity_message_count: result.identity_message_count ?? result.message_count ?? 0,
      session_message_count: result.session_message_count ?? 0,
      imported_message_count: result.imported_message_count ?? 0,
      session_imported_message_count: result.session_imported_message_count ?? 0,
    },
    present_fields: Object.keys(result).sort(),
    omitted_fields: [
      "short_term_memory",
      "long_term_memory",
      "manual_notes",
      "identity_manual_notes",
      "session_manual_notes",
      "identity_profile",
      "session_profile",
    ].filter((field) => field in result),
    note: "技术详情仅保留摘要，不提供聊天正文。",
  };
}

function safeEventListDebug(result: { items?: MemoryEvent[] }) {
  const items = result.items || [];
  return {
    status: "events_loaded",
    count: items.length,
    ids: items.map((item) => item.id),
    scope_preview: items.slice(0, 10).map((item) => ({
      id: item.id,
      tenant_id: item.tenant_id,
      channel: item.channel,
      source_key: item.source_key,
      user_id: item.user_id,
      session_id: item.session_id,
      trace_id: item.trace_id,
      created_at: item.created_at,
    })),
    omitted_fields: ["user_text", "assistant_text"],
    note: "技术详情仅保留摘要，事件表不提供聊天正文。",
  };
}

function safeBackfillResultDebug(result: BackfillResult) {
  return {
    status: result.ok === false ? "backfill_completed_with_warning" : "backfill_completed",
    summary: {
      ok: result.ok,
      session_count: result.session_count ?? 0,
      processed_count: result.processed_count ?? 0,
      imported_count: result.imported_count ?? 0,
      skipped_count: result.skipped_count ?? 0,
      duplicate_count: result.duplicate_count ?? 0,
      events_inserted: result.events_inserted ?? 0,
      items_created: result.items_created ?? 0,
      items_updated: result.items_updated ?? 0,
      jobs_enqueued: result.jobs_enqueued ?? 0,
    },
    user_id_scope: result.user_id_scope ?? result.user_id,
    user_id_auto: result.user_id_auto,
    omitted_fields: ["sessions", "identity_profile", "session_profiles"].filter((field) => field in result),
    note: "技术详情仅保留摘要，回填输出不提供聊天正文。",
  };
}


function memoryItemDisplayTitle(item: MemoryItem) {
  const parts = [
    item.memory_type,
    item.scope_type,
    item.source_type,
    item.session_id || item.user_id,
  ].filter(Boolean);
  return parts.length ? parts.join(" / ") : `记忆 #${item.id}`;
}

function safeMemoryItemPayload(item?: MemoryItem | null) {
  if (!item) return null;
  return {
    id: item.id,
    tenant_id: item.tenant_id,
    channel: item.channel,
    source_key: item.source_key,
    user_id: item.user_id,
    session_id: item.session_id,
    scope_type: item.scope_type,
    source_type: item.source_type,
    source_kind: item.source_kind,
    audience_scope: item.audience_scope,
    origin_session_kind: item.origin_session_kind,
    allowed_session_ids: item.allowed_session_ids,
    memory_type: item.memory_type,
    status: item.status,
    acceptance_status: item.acceptance_status,
    confidence: item.confidence,
    sensitivity: item.sensitivity,
    occurrence_count: item.occurrence_count,
    pinned: item.pinned,
    priority: item.priority,
    expires_at: item.expires_at,
    updated_at: item.updated_at,
  };
}

function safeMemoryItemsDebug(result: { items?: MemoryItem[] }) {
  const items = result.items || [];
  return {
    status: "memory_items_loaded",
    count: items.length,
    items: items.map(safeMemoryItemPayload),
    omitted_fields: ["content", "original_text", "value_json"],
    note: "技术详情仅保留摘要，不提供记忆正文。",
  };
}

function safeMemoryItemMutationDebug(action: string, item?: MemoryItem | null, extra: Record<string, unknown> = {}) {
  return {
    status: action,
    item: safeMemoryItemPayload(item),
    ...extra,
    omitted_fields: ["content", "original_text", "value_json"],
    note: "技术详情仅保留摘要，不提供记忆正文。",
  };
}

function profileEnrichmentStateOf(item?: ProfileEnrichmentCandidate | null) {
  return (
    item?.value?.review?.state ||
    item?.value?.acceptance?.status ||
    item?.acceptance_status ||
    "candidate"
  );
}

function profileEnrichmentReportOf(item?: ProfileEnrichmentCandidate | null) {
  const report = item?.value?.report;
  return report && typeof report === "object" ? report : {};
}

function profileEnrichmentProfileOf(item?: ProfileEnrichmentCandidate | null) {
  const profile = profileEnrichmentReportOf(item).profile;
  return profile && typeof profile === "object" ? profile as Record<string, unknown> : {};
}

function profileEnrichmentTargetOf(item?: ProfileEnrichmentCandidate | null) {
  const target = profileEnrichmentReportOf(item).target;
  return target && typeof target === "object" ? target as Record<string, unknown> : {};
}

function profileEnrichmentTitle(item: ProfileEnrichmentCandidate) {
  const profile = profileEnrichmentProfileOf(item);
  const names = Array.isArray(profile.display_names) ? profile.display_names.filter(Boolean).map(String) : [];
  const displayName = names[0] || String(profile.display_name || profile.name || "");
  const target = profileEnrichmentTargetOf(item);
  return displayName || String(target.query || "") || item.user_id || `候选 #${item.id}`;
}

function profileEnrichmentSummary(item?: ProfileEnrichmentCandidate | null) {
  const profile = profileEnrichmentProfileOf(item);
  return String(profile.summary || profile.bio || item?.content || "").trim();
}

function profileEnrichmentPillClass(state?: string) {
  if (state === "accepted") {
    return "pill pill-ok";
  }
  if (state === "rejected" || state === "hidden") {
    return "pill pill-danger";
  }
  if (state === "needs_review") {
    return "pill pill-feature";
  }
  return "pill pill-muted";
}

function profileEnrichmentStateLabel(state?: string) {
  const labels: Record<string, string> = {
    candidate: "候选",
    needs_review: "待复核",
    accepted: "已通过",
    rejected: "已拒绝",
    hidden: "已隐藏",
  };
  return labels[state || ""] || state || "候选";
}

function copyToClipboard(value: string | number | null | undefined) {
  const text = String(value ?? "").trim();
  if (!text || typeof navigator === "undefined" || !navigator.clipboard) {
    return;
  }
  void navigator.clipboard.writeText(text).catch(() => undefined);
}

function copyGraphScope(fields: Array<{ label: string; value: string }>) {
  const text = fields.map((field) => `${field.label}: ${field.value || "-"}`).join("\n");
  copyToClipboard(text);
}

function allCounts(values: Array<string | undefined>) {
  const counts = new Map<string, number>();
  for (const value of values) {
    const key = value || "unknown";
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return Array.from(counts.entries())
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
}

function matchesGraphSearch(values: Array<string | number | null | undefined>, query: string) {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) {
    return true;
  }
  return values.some((value) => String(value ?? "").toLowerCase().includes(normalizedQuery));
}

function compareGraphUpdatedDesc(
  left: { updated_at?: string | null; id: number },
  right: { updated_at?: string | null; id: number },
) {
  const leftTime = left.updated_at ? new Date(left.updated_at).getTime() : 0;
  const rightTime = right.updated_at ? new Date(right.updated_at).getTime() : 0;
  const leftSort = Number.isNaN(leftTime) ? 0 : leftTime;
  const rightSort = Number.isNaN(rightTime) ? 0 : rightTime;
  return rightSort - leftSort || right.id - left.id;
}

function compareGraphNumberDesc(
  leftValue: number | null | undefined,
  rightValue: number | null | undefined,
) {
  return Number(rightValue ?? -1) - Number(leftValue ?? -1);
}

function sortGraphEntities(items: MemoryGraphEntity[], sort: MemoryGraphSort) {
  const next = [...items];
  if (sort === "confidence_desc") {
    return next.sort((left, right) => compareGraphNumberDesc(left.confidence, right.confidence) || compareGraphUpdatedDesc(left, right));
  }
  if (sort === "name_asc") {
    return next.sort((left, right) => graphEntityLabel(left).localeCompare(graphEntityLabel(right), "zh-CN") || left.id - right.id);
  }
  return next.sort(compareGraphUpdatedDesc);
}

function sortGraphFacts(items: MemoryGraphFact[], sort: MemoryGraphSort) {
  const next = [...items];
  if (sort === "confidence_desc") {
    return next.sort((left, right) => compareGraphNumberDesc(left.confidence, right.confidence) || compareGraphUpdatedDesc(left, right));
  }
  if (sort === "name_asc") {
    return next.sort((left, right) => graphFactSentence(left).localeCompare(graphFactSentence(right), "zh-CN") || left.id - right.id);
  }
  return next.sort(compareGraphUpdatedDesc);
}

function sortGraphEpisodes(items: MemoryGraphEpisode[], sort: MemoryGraphSort) {
  const next = [...items];
  if (sort === "confidence_desc") {
    return next.sort((left, right) => compareGraphNumberDesc(left.importance, right.importance) || compareGraphUpdatedDesc(left, right));
  }
  if (sort === "name_asc") {
    return next.sort((left, right) => (left.title || `事件片段 #${left.id}`).localeCompare(right.title || `事件片段 #${right.id}`, "zh-CN") || left.id - right.id);
  }
  return next.sort(compareGraphUpdatedDesc);
}

export type {
  WxbotSession,
  GroupRosterCandidate,
  IdentityProfile,
  SessionProfile,
  RuntimeProfile,
  MemoryEvent,
  MemoryItem,
  AcceptanceReviewHistoryEntry,
  MemoryDuplicateHint,
  MemoryPossibleConflictItem,
  MemoryPossibleConflicts,
  MemoryGraphEntity,
  MemoryGraphFact,
  MemoryGraphEpisode,
  MemoryGraphPreview,
  MemoryGraphMode,
  MemoryGraphTab,
  MemoryGraphSort,
  MemoryGraphEmptyContext,
  MemoryListUserScope,
  MemoryGraphSelection,
  MemoryGraphSelectionKind,
  GraphQualityKey,
  MemoryGraphReviewEntry,
  ExtractionJobStatus,
  ExtractionJobAction,
  AcceptanceReviewAction,
  AcceptanceQueueFilter,
  ProfileEnrichmentReviewState,
  ProfileEnrichmentReviewAction,
  ExtractionJobScopeCount,
  ExtractionJobStats,
  ExtractionJobMaintenanceActionResult,
  ExtractionJobMaintenanceResult,
  AcceptanceLegacyAuditGroup,
  AcceptanceStats,
  AcceptanceLegacyAudit,
  AcceptanceLegacyBackfillResult,
  BackfillResult,
  ProfileEnrichmentCandidate,
};

export {
  ACCEPTANCE_REVIEW_ACTIONS,
  PROFILE_ENRICHMENT_REVIEW_ACTIONS,
  EXTRACTION_JOB_STATUSES,
  GRAPH_LOW_CONFIDENCE_THRESHOLD,
  GRAPH_STALE_DAYS,
  GRAPH_QUALITY_LABELS,
  GRAPH_REVIEW_GROUPS,
  EXTRACTION_JOB_ACTIONS,
  MEMORY_ITEM_STATUS_LABELS,
  MEMORY_SOURCE_TYPE_LABELS,
  MEMORY_SCOPE_TYPE_LABELS,
  MEMORY_SENSITIVITY_LABELS,
  MEMORY_AUDIENCE_SCOPE_LABELS,
  MEMORY_ORIGIN_SESSION_KIND_LABELS,
  ACCEPTANCE_STATUS_LABELS,
  EXTRACTION_JOB_STATUS_LABELS,
  EXTRACTION_JOB_ID_PREVIEW_LIMIT,
  ACCEPTANCE_ID_PREVIEW_LIMIT,
  PLACEHOLDER_USER_IDS,
  isPlaceholderUserId,
  isPlaceholderSessionId,
  hasIdentityProfileContent,
  isGroupSession,
  mergeSessions,
  getMemberDisplayName,
  getMemberProfileQuery,
  parseSessionIds,
  formatTimestamp,
  formatConfidence,
  acceptanceStatusOf,
  acceptanceSignalEntries,
  acceptanceHistoryOf,
  supersededByItemIdOf,
  supersedesItemIdOf,
  optionalText,
  friendlyApiError,
  hasSmokeOrTestScope,
  summarizeExtractionJobResult,
  previewIds,
  graphStatusPillClass,
  GRAPH_ENTITY_TYPE_LABELS,
  GRAPH_PREDICATE_LABELS,
  GRAPH_STATUS_LABELS,
  graphHumanLabel,
  graphStatusLabel,
  memoryItemStatusLabel,
  memorySourceTypeLabel,
  memoryScopeTypeLabel,
  memorySensitivityLabel,
  memoryAudienceScopeLabel,
  memoryOriginSessionKindLabel,
  acceptanceStatusLabel,
  extractionJobStatusLabel,
  graphEntityLabel,
  graphFactSubject,
  graphFactObject,
  graphFactSentence,
  graphEpisodeLabel,
  graphReviewItemLabel,
  countDefinedIds,
  hasDefinedId,
  graphEntityHasEvidence,
  graphFactHasEvidence,
  graphEpisodeHasEvidence,
  graphItemId,
  graphTabForKind,
  isGraphItemStale,
  isGraphInactiveStatus,
  getGraphQualityKeys,
  graphQualityBadges,
  graphQualityReportItems,
  copyTextLabelList,
  safeMemoryGraphSelectionPayload,
  safeRuntimeProfileDebug,
  safeEventListDebug,
  safeBackfillResultDebug,
  memoryItemDisplayTitle,
  safeMemoryItemPayload,
  safeMemoryItemsDebug,
  safeMemoryItemMutationDebug,
  profileEnrichmentStateOf,
  profileEnrichmentReportOf,
  profileEnrichmentProfileOf,
  profileEnrichmentTargetOf,
  profileEnrichmentTitle,
  profileEnrichmentSummary,
  profileEnrichmentPillClass,
  profileEnrichmentStateLabel,
  copyToClipboard,
  copyGraphScope,
  allCounts,
  matchesGraphSearch,
  compareGraphUpdatedDesc,
  compareGraphNumberDesc,
  sortGraphEntities,
  sortGraphFacts,
  sortGraphEpisodes,
};

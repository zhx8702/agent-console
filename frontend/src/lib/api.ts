import {
  AUTH_INVALID_EVENT,
  COOKIE_SESSION_MARKER,
  type ConsoleConfig,
} from "../state/console-config";
import type { MessageFlowRuntimeConfig } from "./flow-runtime";

export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(status: number, message: string, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

type QueryValue = string | number | boolean | undefined | null;

type RequestOptions = {
  auth?: boolean;
  query?: Record<string, QueryValue>;
  init?: RequestInit;
};

export const API_BROWSER_PREFIX = "/api";

export function apiBrowserPath(path: string) {
  const parsed = new URL(path || "/", "http://agent-console.local");
  const pathname =
    parsed.pathname === API_BROWSER_PREFIX || parsed.pathname.startsWith(`${API_BROWSER_PREFIX}/`)
      ? parsed.pathname
      : `${API_BROWSER_PREFIX}${parsed.pathname.startsWith("/") ? parsed.pathname : `/${parsed.pathname}`}`;
  return `${pathname}${parsed.search}${parsed.hash}`;
}

export class VersionConflictError extends ApiError {
  serverEtag: string | null;

  constructor(message: string, payload: unknown, serverEtag: string | null) {
    super(409, message, payload);
    this.name = "VersionConflictError";
    this.serverEtag = serverEtag;
  }
}

export function apiUrl(config: ConsoleConfig, path: string) {
  const baseUrl = config.apiBaseUrl.endsWith("/") ? config.apiBaseUrl : `${config.apiBaseUrl}/`;
  return new URL(apiBrowserPath(path), baseUrl);
}

export function apiDocumentUrl(config: ConsoleConfig, path: "/docs" | "/redoc") {
  return apiUrl(config, path).toString();
}

function bearerToken(config: ConsoleConfig) {
  const token = config.adminToken.trim();
  return token && token !== COOKIE_SESSION_MARKER ? token : "";
}

function notifyInvalidAdminAuth(status: number, detail: string) {
  if (
    status === 401 &&
    [
      "missing_admin_bearer",
      "invalid_or_expired_admin_session",
      "admin_session_required",
    ].includes(detail)
  ) {
    window.dispatchEvent(new Event(AUTH_INVALID_EVENT));
  }
}

export type CapabilityHealth = "ready" | "action_required" | "blocked" | "degraded";

export type AdminPrincipalResponse = {
  authenticated: true;
  subject: string;
  roles: string[];
  tenant_ids: string[];
  group_ids: string[];
  default_tenant_id: string;
  access_scope: "tenant" | "group";
  auth_kind: "session" | "bearer" | string;
};

export type CapabilityDependency = {
  id: string;
  required: boolean;
  state: CapabilityHealth;
  reason: string;
};

export type CapabilityRecoveryAction = {
  type: "retry" | "configure" | "install" | "contact_admin";
  label: string;
  target: string;
  requires_admin: boolean;
};

export type TenantCapability = {
  id: string;
  label: string;
  category: string;
  enabled: boolean;
  available: boolean;
  health: CapabilityHealth;
  status_reason: string;
  dependencies: CapabilityDependency[];
  recovery_actions: CapabilityRecoveryAction[];
  source: string;
  plugin: string | null;
  permissions: string[];
  entry_route: string;
  version?: string;
  description?: string;
};

export type CapabilityNavigationItem = {
  path: string;
  capability_id: string;
  required_permission: string;
  visible: boolean;
  reason: string;
};

export type LaunchChecklistStep = {
  id: string;
  label: string;
  description: string;
  state: CapabilityHealth;
  dependencies: CapabilityDependency[];
  recovery_actions: CapabilityRecoveryAction[];
  optional?: boolean;
};

export type TenantCapabilitiesResponse = {
  schema_version: string;
  tenant_id: string;
  state: CapabilityHealth;
  access: {
    subject: string;
    roles: string[];
    tenant_ids: string[];
    permissions: string[];
    scope: "tenant" | "group";
  };
  capabilities: TenantCapability[];
  navigation: CapabilityNavigationItem[];
  onboarding: {
    state: CapabilityHealth;
    steps: LaunchChecklistStep[];
  };
  message_flow_runtime?: MessageFlowRuntimeConfig;
  summary: {
    total: number;
    ready: number;
    attention: number;
    visible_navigation: number;
  };
};

export type CapabilityLoadState =
  | { status: "idle" | "loading"; data: null; error: string }
  | { status: "ready"; data: TenantCapabilitiesResponse; error: string }
  | { status: "degraded"; data: TenantCapabilitiesResponse | null; error: string };

export type ParticipationKillSwitches = {
  global_enabled: boolean;
  tenant_enabled: boolean;
  group_enabled: boolean;
};

export type ParticipationPolicyValues = {
  threshold: number;
  quiet_start_hour: number;
  quiet_end_hour: number;
  timezone: string;
  max_soft_replies_10m: number;
  max_soft_replies_hour: number;
  max_bot_ratio_last_40: number;
  max_consecutive_bot_messages: number;
  proactive_enabled: boolean;
  max_proactive_per_day: number;
  proactive_min_silence_seconds: number;
  mention_sender_strategy: "never" | "reply_or_ambiguous";
  prompt_context_retention_seconds: number;
  file_send_enabled: boolean;
};

export type VoiceProfile = {
  profile_id: string;
  version: number;
  enabled: boolean;
  sample_source: "manual" | "persona" | "authorized_group_samples";
  sample_scope: "none" | "current_group";
  authorized_sample_session_ids: string[];
  authorization_reference: string;
  valid_from: string | null;
  expires_at: string | null;
  display_name: string;
  tone: string;
  verbosity: "terse" | "concise" | "balanced";
  phrase_preferences: string[];
  emoji_frequency: number;
  list_format_policy: "avoid_by_default" | "allow";
  identity_disclosure: "contextual" | "always";
  source_persona_version: number;
};

export type VoiceProfilePreviewRequest = {
  voice_profile: VoiceProfile;
  reply_text: string;
  source_text?: string;
  explicitly_detailed?: boolean | null;
};

export type VoiceProfilePreviewDocument = {
  profile_id: string;
  version: number;
  runtime_reason: string;
  applied: boolean;
  output_text: string;
  mode: string;
  transformed: boolean;
  emoji: string;
  catchphrase: string;
  identity_disclosed: boolean;
  reason_codes: string[];
};

export type GroupParticipationPolicyDocument = {
  tenant_id: string;
  session_id: string;
  version: number;
  kill_switches: ParticipationKillSwitches;
  effective_enabled: boolean;
  policy: ParticipationPolicyValues;
  voice_profile: VoiceProfile | null;
  updated_by: string;
  updated_at: string | null;
};

export type GroupParticipationPolicyUpdate = {
  kill_switches: ParticipationKillSwitches;
  policy: ParticipationPolicyValues;
  voice_profile: VoiceProfile | null;
  change_reason: string;
};

export type ParticipationPreviewRequest = {
  message_id: string;
  now: string;
  mentioned_me: boolean;
  replied_to_bot: boolean;
  explicit_command: boolean;
  safety_response_required: boolean;
  explicit_question_to_bot: boolean;
  keyword_triggered: boolean;
  topic_continuation: boolean;
  unfinished_task_continuation: boolean;
  directed_to_other_member: boolean;
  rapid_multi_party_chat: boolean;
  bot_replied_within_60s: boolean;
  valid_member_answer_exists: boolean;
  intent_confidence: number;
  base_eligible: boolean;
  base_reason:
    | ""
    | "base_policy_not_eligible"
    | "not_addressed"
    | "channel_suppressed"
    | "member_opt_out"
    | "group_disabled";
  bot_messages_last_40: number;
  total_messages_last_40: number;
  soft_replies_last_10m: number;
  soft_replies_last_hour: number;
  consecutive_bot_messages: number;
  proactive_messages_today: number;
  group_silence_seconds: number;
  is_self_sent: boolean;
  topic_changed: boolean;
  superseded_by_newer_message: boolean;
  requested_proactive: boolean;
  response_kind: "short" | "tool_progress" | "tool_result";
  reply_target_ambiguous: boolean;
};

export type ParticipationDecisionStatus =
  | "must_reply"
  | "may_reply"
  | "observe_only"
  | "defer"
  | "cancel";

export type ParticipationDecisionDocument = {
  event_id: string;
  tenant_id: string;
  session_id: string;
  policy_version: number;
  status: ParticipationDecisionStatus;
  score: number;
  reason_codes: string[];
  not_before: string | null;
  expires_at: string | null;
  mention_sender: boolean;
};

export type ParticipationEventDocument = {
  event_id: string;
  tenant_id: string;
  session_id: string;
  policy_version: number;
  event_kind: "preview" | "runtime";
  status: ParticipationDecisionStatus;
  score: number;
  reason_codes: string[];
  signal_summary: Record<string, boolean | number | string>;
  trace_id: string;
  created_at: string;
};

export type ParticipationEventPage = {
  items: ParticipationEventDocument[];
  next_before: string | null;
};

export type MemberPrivacyValues = {
  memory_enabled: boolean;
  allow_group_recall: boolean;
  allow_private_recall: boolean;
  proactive_participation_enabled: boolean;
  soft_reply_opt_out: boolean;
  no_group_mentions: boolean;
  retention_days: number;
  audience_scope: "private" | "session" | "explicit";
  allowed_session_ids: string[];
  sensitive_memory_enabled: boolean;
  correction_enabled: boolean;
  deletion_enabled: boolean;
};

export type MemberPrivacyPolicyDocument = {
  tenant_id: string;
  session_id: string;
  user_id: string;
  version: number;
  configured_policy?: MemberPrivacyValues | null;
  effective_policy?: MemberPrivacyValues | null;
  policy: MemberPrivacyValues;
  updated_by: string;
  updated_at: string | null;
};

export type MemberPrivacyPolicyUpdate = {
  policy: MemberPrivacyValues;
  change_reason: string;
};

export type VersionedResourceStatus =
  | "idle"
  | "loading"
  | "loaded"
  | "saving"
  | "error"
  | "conflict";

export type VersionedResourceState<T> = {
  status: VersionedResourceStatus;
  value: T | null;
  draft: T | null;
  etag: string | null;
  dirty: boolean;
  error: string;
};

export function createVersionedResourceState<T>(): VersionedResourceState<T> {
  return {
    status: "idle",
    value: null,
    draft: null,
    etag: null,
    dirty: false,
    error: "",
  };
}

export function markVersionedResourceLoaded<T>(
  value: T,
  etag: string | null,
): VersionedResourceState<T> {
  return {
    status: "loaded",
    value,
    draft: value,
    etag,
    dirty: false,
    error: "",
  };
}

export function editVersionedResource<T>(
  state: VersionedResourceState<T>,
  draft: T,
): VersionedResourceState<T> {
  return {
    ...state,
    status: "loaded",
    draft,
    dirty: true,
    error: "",
  };
}

export function markVersionedResourceError<T>(
  state: VersionedResourceState<T>,
  caught: unknown,
): VersionedResourceState<T> {
  return {
    ...state,
    status: caught instanceof VersionConflictError ? "conflict" : "error",
    error: caught instanceof Error ? caught.message : "资源请求失败",
  };
}

export type VersionedResourceResponse<T> = {
  value: T;
  etag: string | null;
};

export type VersionedResourceRequestOptions<TBody = never> = {
  auth?: boolean;
  query?: Record<string, QueryValue>;
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: TBody;
  ifMatch?: string | null;
  idempotencyKey?: string;
  signal?: AbortSignal;
  headers?: HeadersInit;
};

export function formatJson(value: unknown) {
  return JSON.stringify(value, null, 2);
}

export function parseJsonInput<T>(value: string, fallback: T): T {
  if (!value.trim()) {
    return fallback;
  }
  return JSON.parse(value) as T;
}

export function splitCsv(value: string) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function createMessageId(prefix = "frontend") {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export type GroupGraphNode = {
  id: string;
  type: string;
  label: string;
  display_label?: string;
  technical_label?: string;
  aliases?: string[];
  canonical_key?: string;
  tenant_id?: string;
  source_key?: string;
  channel?: string;
  session_id?: string;
  confidence?: number | null;
  acceptance_status?: string;
  evidence_count?: number;
  first_seen?: string | null;
  last_seen?: string | null;
  review_state?: string;
  source_ref_count?: number;
  status?: string;
  metadata?: Record<string, unknown>;
};

export type GroupGraphEdge = {
  id: string;
  from?: string;
  to?: string;
  source?: string;
  target?: string;
  type: string;
  label?: string;
  confidence?: number | null;
  acceptance_status?: string;
  evidence_count?: number;
  first_seen?: string | null;
  last_seen?: string | null;
  source_event_ids?: Array<string | number>;
  source_episode_ids?: Array<string | number>;
  source_message_count?: number;
  memory_item_ids?: Array<string | number>;
  extraction_method?: string;
  review_state?: string;
  acceptance?: Record<string, unknown>;
  history?: unknown[];
};

export type GroupGraphResponse = {
  schema?: {
    version?: string;
    node_types?: string[];
    edge_types?: string[];
  };
  scope?: Record<string, string | null | undefined>;
  filters?: Record<string, unknown>;
  nodes: GroupGraphNode[];
  edges: GroupGraphEdge[];
  counts?: {
    nodes?: number;
    edges?: number;
  };
  generated_from?: string[];
};

export type GroupGraphEdgeEvidenceEntity = {
  id?: string | number;
  tenant_id?: string;
  channel?: string;
  source_key?: string;
  user_id?: string;
  session_id?: string;
  trace_id?: string;
  event_key?: string;
  scope_type?: string;
  source_type?: string;
  memory_type?: string;
  normalized_key?: string;
  confidence?: number | null;
  importance?: number | null;
  sensitivity?: string;
  acceptance_status?: string;
  status?: string;
  source_event_id?: string | number | null;
  event_ids?: Array<string | number>;
  memory_item_ids?: Array<string | number>;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
};

export type GroupGraphEdgeEvidenceResponse = {
  schema?: {
    version?: string;
  };
  edge?: GroupGraphEdge & {
    status?: string;
    created_at?: string | null;
    updated_at?: string | null;
    valid_at?: string | null;
    invalid_at?: string | null;
  };
  evidence_ids?: {
    memory_item_ids?: Array<string | number>;
    event_ids?: Array<string | number>;
    episode_ids?: Array<string | number>;
  };
  evidence_counts?: {
    memory_items?: number;
    events?: number;
    episodes?: number;
    [key: string]: number | undefined;
  };
  memory_items?: GroupGraphEdgeEvidenceEntity[];
  events?: GroupGraphEdgeEvidenceEntity[];
  episodes?: GroupGraphEdgeEvidenceEntity[];
  [key: string]: unknown;
};

export type GroupGraphQuery = {
  tenant_id: string;
  channel?: string;
  source_key?: string;
  session_id?: string;
  from?: string;
  to?: string;
  acceptance_status?: string;
  node_type?: string;
  edge_type?: string;
  relation_type?: string;
  min_confidence?: string | number;
  limit?: string | number;
};

export type GroupGraphEdgeEvidenceQuery = {
  tenant_id: string;
  channel?: string;
  source_key?: string;
  session_id?: string;
};

export type MemoryBackfillRequest = {
  tenant_id: string;
  connection_id: string;
  channel?: string;
  source_key?: string;
  user_id?: string;
  session_ids: string[];
  days_limit?: number;
  max_messages_per_session?: number;
  enqueue_llm_jobs?: boolean;
  target_date?: string;
};

export type MemoryBackfillResponse = {
  ok?: boolean;
  tenant_id?: string;
  channel?: string;
  source_key?: string;
  user_id?: string;
  user_id_scope?: string;
  user_id_auto?: boolean;
  days_limit?: number;
  max_messages_per_session?: number;
  session_count?: number;
  processed_count?: number;
  imported_count?: number;
  skipped_count?: number;
  duplicate_count?: number;
  events_inserted?: number;
  events_duplicate?: number;
  items_created?: number;
  items_updated?: number;
  items_pending?: number;
  jobs_enqueued?: number;
  llm_jobs_enabled?: boolean;
  sessions?: unknown[];
  identity_profile?: unknown;
  session_profiles?: unknown[];
  [key: string]: unknown;
};

export type GroupGraphHistoryDateStatus = "extracted" | "partial" | "not_extracted";

export type GroupGraphHistoryDateRow = {
  date: string;
  raw_message_count: number;
  imported_count: number;
  job_counts?: MemoryExtractionJobStatusCounts;
  status: GroupGraphHistoryDateStatus;
};

export type GroupGraphHistoryDatesResponse = {
  ok?: boolean;
  tenant_id?: string;
  channel?: string;
  source_key?: string;
  session_id?: string;
  user_id?: string;
  user_id_scope?: string;
  user_id_auto?: boolean;
  recent_days?: number;
  items: GroupGraphHistoryDateRow[];
};

export type GroupGraphHistoryDatesQuery = {
  tenant_id: string;
  channel?: string;
  source_key?: string;
  session_id: string;
  user_id?: string;
  recent_days?: string | number;
};

export type MemoryExtractionJobStatusCounts = {
  pending?: number;
  running?: number;
  succeeded?: number;
  failed?: number;
  dead?: number;
  [key: string]: number | undefined;
};

export type MemoryExtractionJobStatsResponse = {
  counts?: MemoryExtractionJobStatusCounts;
  status_counts?: MemoryExtractionJobStatusCounts;
  retry_counts?: Record<string, number>;
  graph_result_counts?: Record<string, number>;
  latency_seconds?: Record<string, number>;
};

export type MemoryExtractionJobStatsQuery = {
  tenant_id?: string;
  channel?: string;
  source_key?: string;
  user_id?: string;
  session_id?: string;
  status?: string;
  error_type?: string;
  created_before?: string;
  created_after?: string;
  updated_before?: string;
  updated_after?: string;
  limit?: string | number;
};

export type GroupGraphDailyExtractionRequest = {
  tenant_id: string;
  channel?: string;
  source_key?: string;
  session_id: string;
  date: string;
  user_id?: string;
  limit?: number;
  batch_limit?: number;
  max_jobs?: number;
  continuous?: boolean;
  time_budget_seconds?: number;
};

export type GroupGraphDailyExtractionResponse = {
  ok?: boolean;
  status?: string;
  result_status?: string;
  skipped_reason?: string;
  counts?: Record<string, number>;
  job_counts_before?: MemoryExtractionJobStatusCounts;
  job_counts_after?: MemoryExtractionJobStatusCounts;
  job_counts?: MemoryExtractionJobStatusCounts;
  jobs?: {
    claimed?: number;
    succeeded?: number;
    failed?: number;
    dead?: number;
    batches?: number;
    [key: string]: number | undefined;
  };
  controls?: {
    batch_limit?: number;
    max_jobs?: number;
    continuous?: boolean;
    time_budget_seconds?: number;
    stop_reason?: string;
    [key: string]: string | number | boolean | undefined;
  };
  limit?: number;
  more_remain?: boolean;
  [key: string]: unknown;
};

export type GroupGraphWindowExtractionRequest = {
  tenant_id: string;
  channel?: string;
  source_key?: string;
  session_id: string;
  date: string;
  user_id?: string;
  window_size?: number;
  max_windows?: number;
  cursor_event_id?: number;
  dry_run?: boolean;
};

export type GroupGraphWindowCatchupRequest = {
  tenant_id: string;
  channel?: string;
  source_key?: string;
  session_id: string;
  date: string;
  user_id?: string;
  window_size?: number;
  max_windows_per_run?: number;
  cursor_event_id?: number;
  dry_run?: boolean;
  time_budget_seconds?: number;
};

export type GroupGraphWindowExtractionResponse = {
  ok?: boolean;
  status?: string;
  skipped_reason?: string;
  scope?: Record<string, unknown>;
  date?: string;
  controls?: {
    window_size?: number;
    max_windows?: number;
    cursor_event_id?: number;
    dry_run?: boolean;
    [key: string]: string | number | boolean | undefined;
  };
  windows?: Array<{
    index?: number;
    event_count?: number;
    first_event_id?: number;
    last_event_id?: number;
    sender_count?: number;
    candidate_count?: number;
    applied_count?: number;
    skipped_count?: number;
    [key: string]: number | string | undefined;
  }>;
  totals?: {
    events?: number;
    windows?: number;
    candidates?: number;
    applied?: number;
    skipped?: number;
    [key: string]: number | undefined;
  };
  next_cursor_event_id?: number;
  more_remain?: boolean;
  generated_from?: string[];
  [key: string]: unknown;
};

export type GroupGraphWindowCatchupResponse = {
  ok?: boolean;
  status?: string;
  scope?: Record<string, unknown>;
  date?: string;
  controls?: {
    window_size?: number;
    max_windows_per_run?: number;
    cursor_event_id?: number;
    dry_run?: boolean;
    time_budget_seconds?: number;
    [key: string]: string | number | boolean | undefined;
  };
  totals?: {
    events?: number;
    windows?: number;
    candidates?: number;
    applied?: number;
    skipped?: number;
    [key: string]: number | undefined;
  };
  windows_processed?: number;
  next_cursor_event_id?: number;
  more_remain?: boolean;
  stop_reason?: string;
  generated_from?: string[];
  [key: string]: unknown;
};

export type GroupGraphWindowStatsQuery = {
  tenant_id: string;
  channel?: string;
  source_key?: string;
  session_id?: string;
  user_id?: string;
  date?: string;
};

export type GroupGraphWindowStatsResponse = {
  ok?: boolean;
  scope?: Record<string, unknown>;
  totals?: Record<string, number>;
  status_counts?: Record<string, number>;
  acceptance_counts?: Record<string, number>;
  predicate_counts?: Record<string, number>;
  generated_from?: string[];
  [key: string]: unknown;
};

export async function getGroupGraph(config: ConsoleConfig, query: GroupGraphQuery) {
  return apiRequest<GroupGraphResponse>(config, "/plugins/memory/group-graph", { query });
}

export async function getGroupGraphEdgeEvidence(
  config: ConsoleConfig,
  edgeId: string,
  query: GroupGraphEdgeEvidenceQuery,
) {
  return apiRequest<GroupGraphEdgeEvidenceResponse>(
    config,
    `/plugins/memory/group-graph/evidence/${encodeURIComponent(edgeId)}`,
    { query },
  );
}

export async function getGroupGraphHistoryDates(config: ConsoleConfig, query: GroupGraphHistoryDatesQuery) {
  return apiRequest<GroupGraphHistoryDatesResponse>(config, "/plugins/memory/group-graph/history-dates", { query });
}

export async function getMemoryExtractionJobStats(config: ConsoleConfig, query: MemoryExtractionJobStatsQuery) {
  return apiRequest<MemoryExtractionJobStatsResponse>(config, "/plugins/memory/extraction-jobs/stats", {
    auth: true,
    query,
  });
}

export async function getGroupGraphWindowStats(config: ConsoleConfig, query: GroupGraphWindowStatsQuery) {
  return apiRequest<GroupGraphWindowStatsResponse>(config, "/plugins/memory/group-graph/window-stats", {
    auth: true,
    query,
  });
}

export async function runGroupGraphDailyExtraction(config: ConsoleConfig, body: GroupGraphDailyExtractionRequest) {
  return apiRequest<GroupGraphDailyExtractionResponse>(config, "/plugins/memory/group-graph/extract-daily", {
    auth: true,
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  });
}

export async function runGroupGraphWindowExtraction(config: ConsoleConfig, body: GroupGraphWindowExtractionRequest) {
  return apiRequest<GroupGraphWindowExtractionResponse>(config, "/plugins/memory/group-graph/extract-window", {
    auth: true,
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  });
}

export async function runGroupGraphWindowCatchup(config: ConsoleConfig, body: GroupGraphWindowCatchupRequest) {
  return apiRequest<GroupGraphWindowCatchupResponse>(config, "/plugins/memory/group-graph/extract-window-catchup", {
    auth: true,
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  });
}

export async function backfillMemoryHistory(config: ConsoleConfig, body: MemoryBackfillRequest) {
  return apiRequest<MemoryBackfillResponse>(config, "/plugins/memory/backfill", {
    init: {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  });
}

function detailText(value: unknown, fallback: string): string {
  if (typeof value === "string") {
    return value.trim() || fallback;
  }
  if (typeof value === "object" && value !== null) {
    const detail = value as Record<string, unknown>;
    for (const key of ["code", "message"]) {
      if (typeof detail[key] === "string" && detail[key].trim()) {
        return detail[key].trim();
      }
    }
    try {
      const encoded = JSON.stringify(value);
      if (encoded && encoded !== "{}") {
        return encoded;
      }
    } catch {
      // Fall through to the HTTP status text for non-serializable values.
    }
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

function responseDetail(payload: unknown, fallback: string) {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    return detailText((payload as { detail: unknown }).detail, fallback);
  }
  return detailText(payload, fallback);
}

async function readResponsePayload(response: Response) {
  const text = await response.text();
  if (!text) {
    return {};
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

/**
 * Load or mutate an optimistic-concurrency resource without losing its ETag.
 * Mutation callers pass the previously loaded tag and, for high-risk writes,
 * an idempotency key. Only version/ETag 409 responses are promoted to a typed
 * conflict; state-machine and idempotency 409s remain ordinary API errors.
 */
export async function apiVersionedResource<TResponse, TBody = never>(
  config: ConsoleConfig,
  path: string,
  options: VersionedResourceRequestOptions<TBody> = {},
): Promise<VersionedResourceResponse<TResponse>> {
  const url = apiUrl(config, path);
  if (options.query) {
    for (const [key, value] of Object.entries(options.query)) {
      if (value !== undefined && value !== null && `${value}` !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }

  const headers = new Headers(options.headers);
  const token = bearerToken(config);
  if (options.auth && token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.ifMatch) {
    headers.set("If-Match", options.ifMatch);
  }
  if (options.idempotencyKey) {
    headers.set("Idempotency-Key", options.idempotencyKey);
  }
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(url.toString(), {
    method: options.method || "GET",
    credentials: "include",
    headers,
    signal: options.signal,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const payload = await readResponsePayload(response);
  const etag = response.headers.get("ETag");

  if (!response.ok) {
    const detail = responseDetail(payload, response.statusText);
    if (options.auth) {
      notifyInvalidAdminAuth(response.status, detail);
    }
    const message = `${response.status} ${detail}`;
    const rawDetail = typeof payload === "object" && payload !== null
      ? (payload as { detail?: unknown }).detail
      : undefined;
    const conflictCode = typeof rawDetail === "object" && rawDetail !== null
      ? String((rawDetail as { code?: unknown }).code ?? "")
      : String(rawDetail ?? "");
    if (
      response.status === 409
      && ["version_conflict", "resource_version_conflict", "etag_mismatch"]
        .some((code) => conflictCode.toLowerCase().includes(code))
    ) {
      throw new VersionConflictError(message, payload, etag);
    }
    throw new ApiError(response.status, message, payload);
  }

  return { value: payload as TResponse, etag };
}

export async function apiRequest<T>(
  config: ConsoleConfig,
  path: string,
  options: RequestOptions = {},
) {
  const url = apiUrl(config, path);
  if (options.query) {
    for (const [key, value] of Object.entries(options.query)) {
      if (value !== undefined && value !== null && `${value}` !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }

  const headers = new Headers(options.init?.headers || {});
  const token = bearerToken(config);
  if (options.auth && token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(url.toString(), {
    ...options.init,
    credentials: "include",
    headers,
  });

  const text = await response.text();
  let payload: unknown = text;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = text;
  }

  if (!response.ok) {
    const detail = responseDetail(payload, response.statusText);
    if (options.auth) {
      notifyInvalidAdminAuth(response.status, detail);
    }
    throw new ApiError(response.status, `${response.status} ${detail}`, payload);
  }
  return payload as T;
}

export async function apiBlobRequest(
  config: ConsoleConfig,
  path: string,
  options: { signal?: AbortSignal } = {},
) {
  const headers = new Headers();
  const token = bearerToken(config);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(apiUrl(config, path).toString(), {
    credentials: "include",
    headers,
    signal: options.signal,
  });
  if (!response.ok) {
    const text = await response.text();
    let payload: unknown = text;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch {
      payload = text;
    }
    const detail = responseDetail(payload, response.statusText);
    notifyInvalidAdminAuth(response.status, detail);
    throw new ApiError(response.status, `${response.status} ${detail}`, payload);
  }
  return response.blob();
}

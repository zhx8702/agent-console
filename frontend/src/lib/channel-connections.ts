import {
  apiRequest,
  apiVersionedResource,
  type VersionedResourceResponse,
} from "./api";
import type { ConsoleConfig } from "../state/console-config";

export type ChannelHealthState =
  | "ready"
  | "action_required"
  | "blocked"
  | "degraded"
  | "unknown";

export type ChannelManagedBy = "platform" | "environment" | "external" | "unknown";

export type ChannelAdapterConfigField = {
  type: "string" | "number" | "integer" | "boolean" | "object" | "array" | "null";
  title: string;
  description: string;
  format: string;
  defaultValue: unknown;
  minimum: number | null;
  maximum: number | null;
  minLength: number | null;
  maxLength: number | null;
  enumValues: unknown[];
};

export type ChannelAdapterSecretField = {
  name: string;
  label: string;
  description: string;
  required: boolean;
  acceptedRefSchemes: string[];
  environmentVariable: string;
};

export type ChannelAdapter = {
  id: string;
  displayName: string;
  description: string;
  pluginName: string;
  version: string;
  installed: boolean;
  enabled: boolean;
  available: boolean;
  supportsMultipleConnections: boolean;
  capabilities: string[];
  runtimeModes: string[];
  configRequired: string[];
  configOrder: string[];
  configFields: Record<string, ChannelAdapterConfigField>;
  secretFields: ChannelAdapterSecretField[];
};

export type ChannelConnectionHealth = {
  aggregate: ChannelHealthState;
  configured: ChannelHealthState;
  auth: ChannelHealthState;
  runtime: ChannelHealthState;
  probe: ChannelHealthState;
  reason: string;
  lastProbeAt: string | null;
};

export type ChannelConnectionConfig = {
  endpointUrl: string;
  pollIntervalSeconds: number;
  sendIntervalSeconds: number;
  extra: Record<string, unknown>;
  raw: Record<string, unknown>;
};

export type ChannelConnection = {
  tenantId: string;
  id: string;
  adapterId: string;
  adapterLabel: string;
  displayName: string;
  config: ChannelConnectionConfig;
  secretRef: string;
  secretStatus: string;
  secretFingerprint: string;
  requiredForLaunch: boolean;
  priority: number;
  desiredState: string;
  effectiveState: string;
  lastProbeStatus: string;
  lastErrorCode: string;
  managedBy: ChannelManagedBy;
  readOnly: boolean;
  version: number;
  health: ChannelConnectionHealth;
  createdAt: string | null;
  updatedAt: string | null;
};

export type ChannelAdapterCollection = {
  items: ChannelAdapter[];
  readOnly: boolean;
};

export type ChannelConnectionCollection = {
  items: ChannelConnection[];
  readOnly: boolean;
};

export type ChannelConnectionWrite = {
  adapterId: string;
  displayName: string;
  endpointUrl: string;
  pollIntervalSeconds: number;
  sendIntervalSeconds: number;
  extraConfig?: Record<string, unknown>;
  configValues?: Record<string, unknown>;
  configFieldNames?: string[];
  secretRef: string;
  requiredForLaunch: boolean;
  desiredState: string;
};

export type ChannelConnectionActionResult = {
  ok: boolean;
  status: string;
  summary: string;
  checks: Array<{
    id: string;
    label: string;
    state: ChannelHealthState;
    detail: string;
  }>;
  connection: ChannelConnection | null;
};

const ADAPTERS_PATH = "/v1/admin/channel-adapters";
const CONNECTIONS_PATH = "/v1/admin/channel-connections";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function asString(value: unknown, fallback = "") {
  return typeof value === "string" ? value.trim() : fallback;
}

function asBoolean(value: unknown, fallback = false) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "1", "yes", "on", "enabled", "ready"].includes(normalized)) return true;
    if (["false", "0", "no", "off", "disabled"].includes(normalized)) return false;
  }
  return fallback;
}

function asNumber(value: unknown, fallback = 0) {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function asStringList(value: unknown) {
  if (!Array.isArray(value)) return [];
  return Array.from(new Set(value.map((item) => asString(item)).filter(Boolean)));
}

function firstString(record: Record<string, unknown>, keys: string[], fallback = "") {
  for (const key of keys) {
    const value = asString(record[key]);
    if (value) return value;
  }
  return fallback;
}

function normalizeHealthState(value: unknown, fallback: ChannelHealthState = "unknown"): ChannelHealthState {
  const normalized = asString(value).toLowerCase();
  if (["ready", "healthy", "ok", "valid", "passed", "online", "authenticated", "configured", "synced", "active", "enabled"].includes(normalized)) {
    return "ready";
  }
  if (["action_required", "needs_action", "auth_required", "expired", "draft", "pending", "stale", "unverified", "missing"].includes(normalized)) {
    return "action_required";
  }
  if (["blocked", "failed", "error", "offline", "stopped", "disabled", "revoked", "invalid", "validation_failed"].includes(normalized)) {
    return "blocked";
  }
  if (["degraded", "lagging", "partial", "warning", "starting", "syncing"].includes(normalized)) {
    return "degraded";
  }
  return fallback;
}

function normalizeManagedBy(value: unknown): ChannelManagedBy {
  const normalized = asString(value).toLowerCase();
  if (["platform", "database", "ui", "console", "managed"].includes(normalized)) return "platform";
  if (["environment", "env", "deployment", "legacy_env", "legacy"].includes(normalized)) return "environment";
  if (["external", "secret_provider", "operator"].includes(normalized)) return "external";
  return "unknown";
}

type EvidenceFreshness = "missing" | "fresh" | "stale" | "invalid";

const PROBE_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const FUTURE_CLOCK_SKEW_MS = 5 * 60 * 1000;

function evidenceFreshness(value: string | null, maxAgeMs: number): EvidenceFreshness {
  if (!value) return "missing";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "invalid";
  const age = Date.now() - timestamp;
  if (age < -FUTURE_CLOCK_SKEW_MS) return "invalid";
  return age <= maxAgeMs ? "fresh" : "stale";
}

export function normalizeChannelAdapter(value: unknown): ChannelAdapter {
  const record = asRecord(value);
  const configSchema = asRecord(record.config_schema);
  const uiSchema = asRecord(record.ui_schema);
  const rawProperties = asRecord(configSchema.properties);
  const configFields = Object.fromEntries(Object.entries(rawProperties).map(([name, value]) => {
    const field = asRecord(value);
    const type = firstString(field, ["type"], "string");
    const normalizedType: ChannelAdapterConfigField["type"] = [
      "string",
      "number",
      "integer",
      "boolean",
      "object",
      "array",
      "null",
    ].includes(type)
      ? type as ChannelAdapterConfigField["type"]
      : "string";
    const enumValues = Array.isArray(field.enum) ? field.enum : [];
    return [name, {
      type: normalizedType,
      title: firstString(field, ["title", "label"], name),
      description: firstString(field, ["description", "help"]),
      format: firstString(field, ["format"]),
      defaultValue: field.default,
      minimum: field.minimum === undefined ? null : asNumber(field.minimum),
      maximum: field.maximum === undefined ? null : asNumber(field.maximum),
      minLength: field.minLength === undefined ? null : asNumber(field.minLength),
      maxLength: field.maxLength === undefined ? null : asNumber(field.maxLength),
      enumValues,
    }];
  }));
  const rawSecrets = Array.isArray(record.secret_fields) ? record.secret_fields : [];
  const id = firstString(record, ["adapter_id", "id", "platform_id", "provider_key", "name"]);
  // A descriptor returned by the runtime catalog is registered and therefore
  // installed even when the backend intentionally omits package metadata.
  const installed = asBoolean(record.installed, Boolean(id));
  const enabled = asBoolean(record.enabled, installed);
  return {
    id,
    displayName: firstString(record, ["display_name", "label", "name"], id || "未命名平台"),
    description: firstString(record, ["description", "summary"]),
    pluginName: firstString(record, ["plugin_name", "plugin", "adapter_plugin"], id),
    version: firstString(record, ["version", "adapter_version"]),
    installed,
    enabled,
    available: asBoolean(record.available, installed && enabled),
    supportsMultipleConnections: asBoolean(record.supports_multiple_connections, false),
    capabilities: asStringList(record.capabilities ?? record.supported_capabilities),
    runtimeModes: asStringList(record.runtime_modes),
    configRequired: asStringList(configSchema.required),
    configOrder: asStringList(uiSchema.order),
    configFields,
    secretFields: rawSecrets.map((value) => {
      const secret = asRecord(value);
      return {
        name: firstString(secret, ["name"], "credential"),
        label: firstString(secret, ["label", "name"], "凭据引用"),
        description: firstString(secret, ["description"]),
        required: asBoolean(secret.required, true),
        acceptedRefSchemes: asStringList(secret.accepted_ref_schemes),
        environmentVariable: firstString(secret, ["environment_variable"]),
      };
    }),
  };
}

export function normalizeChannelConnection(value: unknown): ChannelConnection {
  const record = asRecord(value);
  const config = asRecord(record.config ?? record.config_json ?? record.connection_config);
  const adapterId = firstString(record, ["adapter_id", "platform_id", "provider_key", "channel"], "unknown");
  const desiredState = firstString(record, ["desired_state", "lifecycle_state"], "draft");
  const effectiveState = firstString(record, ["effective_state", "status"], desiredState);
  const managedBy = normalizeManagedBy(record.managed_by ?? record.config_source);
  const lastProbeAt = firstString(record, ["last_probe_at", "last_probed_at"]) || null;
  const probeFreshness = evidenceFreshness(lastProbeAt, PROBE_MAX_AGE_MS);
  const rawLastProbeStatus = asString(record.last_probe_status).toLowerCase();
  const lastProbeState = normalizeHealthState(rawLastProbeStatus, "unknown");
  // The current API stores both schema validation and an active network probe
  // in last_probed_at. A `valid` result proves configuration shape only; it
  // must not be promoted into authentication or transport health.
  const validationOnlyStatus = ["valid", "validation_failed", "invalid"].includes(rawLastProbeStatus);
  const connectivityProbeState: ChannelHealthState = rawLastProbeStatus && !validationOnlyStatus
    ? lastProbeState
    : "unknown";
  const lastErrorCode = asString(record.last_error_code);
  const effective = effectiveState.toLowerCase();
  const desired = desiredState.toLowerCase();
  const effectiveStopped = /^(disabled|stopped|off)$/.test(effective);
  const effectiveEnabled = /^(enabled|active|running)$/.test(effective);
  const desiredStopped = /^(disabled|stopped|off)$/.test(desired);
  const desiredEnabled = /^(enabled|active|running)$/.test(desired);
  const lifecycleMismatch = (desiredStopped && !effectiveStopped) || (desiredEnabled && !effectiveEnabled);
  const freshConnectivityProbe = probeFreshness === "fresh" && connectivityProbeState === "ready";
  const staleConnectivityProbe = probeFreshness === "stale" && connectivityProbeState === "ready";
  // Lifecycle values are eventually consistent. Until the bridge has supplied
  // real connectivity evidence, an `enabled -> unverified` mismatch means
  // "awaiting verification", not a degraded runtime. Only a connection that
  // was actually observed as ready can subsequently drift.
  const lifecycleDrift = lifecycleMismatch && (freshConnectivityProbe || staleConnectivityProbe);
  const awaitingConnectivityEvidence = desiredEnabled
    && !freshConnectivityProbe
    && !staleConnectivityProbe
    && connectivityProbeState !== "blocked";

  let configured: ChannelHealthState = "unknown";
  if (rawLastProbeStatus === "validation_failed" || /^config_/.test(lastErrorCode)) {
    configured = "blocked";
  } else if (rawLastProbeStatus === "valid") {
    configured = "ready";
  }

  let probe: ChannelHealthState = connectivityProbeState;
  if (probeFreshness === "invalid") probe = "unknown";
  else if (staleConnectivityProbe) probe = "degraded";
  else if (awaitingConnectivityEvidence && probe === "unknown") probe = "action_required";

  let auth: ChannelHealthState = "unknown";
  if (/(auth|credential|secret|token)/i.test(lastErrorCode)) auth = "blocked";

  let runtime: ChannelHealthState = "unknown";
  if (/^(error|failed)$/.test(effective)) runtime = "blocked";
  else if (effectiveStopped) runtime = "blocked";
  else if (lifecycleDrift) runtime = "degraded";
  else if (freshConnectivityProbe && effectiveEnabled) runtime = "ready";
  else if (awaitingConnectivityEvidence) runtime = "action_required";

  const dimensions = { configured, auth, runtime, probe };
  let aggregate: ChannelHealthState;
  if (Object.values(dimensions).includes("blocked") || /^(error|failed)$/.test(effective)) {
    aggregate = "blocked";
  } else if (lifecycleDrift || staleConnectivityProbe) {
    aggregate = "degraded";
  } else if (/^(draft|unverified|pending|validating)$/.test(effective)) {
    aggregate = "action_required";
  } else if (desiredEnabled && effectiveEnabled && freshConnectivityProbe) {
    aggregate = "ready";
  } else if (awaitingConnectivityEvidence) {
    aggregate = "action_required";
  } else {
    aggregate = "unknown";
  }
  const knownConfigKeys = new Set([
    "endpoint_url",
    "sdk_url",
    "gateway_url",
    "poll_interval_seconds",
    "poll_interval",
    "send_interval_seconds",
    "send_interval",
  ]);
  const extra = Object.fromEntries(Object.entries(config).filter(([key]) => !knownConfigKeys.has(key)));

  return {
    tenantId: firstString(record, ["tenant_id"]),
    id: firstString(record, ["connection_id", "id", "instance_id"]),
    adapterId,
    adapterLabel: firstString(record, ["adapter_label", "platform_label", "provider_label"], adapterId),
    displayName: firstString(record, ["display_name", "name", "label"], "未命名连接"),
    config: {
      endpointUrl: firstString(config, ["endpoint_url", "sdk_url", "gateway_url"], firstString(record, ["endpoint_url", "sdk_url", "gateway_url"])),
      pollIntervalSeconds: asNumber(config.poll_interval_seconds ?? config.poll_interval ?? record.poll_interval, 3),
      sendIntervalSeconds: asNumber(config.send_interval_seconds ?? config.send_interval ?? record.send_interval, 2),
      extra,
      raw: config,
    },
    secretRef: firstString(record, ["secret_ref", "credential_ref"]),
    secretStatus: asString(record.secret_status),
    secretFingerprint: asString(record.secret_fingerprint),
    requiredForLaunch: asBoolean(record.required_for_launch, false),
    priority: asNumber(record.priority, 100),
    desiredState,
    effectiveState,
    lastProbeStatus: rawLastProbeStatus,
    lastErrorCode,
    managedBy,
    readOnly: asBoolean(record.read_only, false) || managedBy === "environment",
    version: asNumber(record.version, 0),
    health: {
      aggregate,
      ...dimensions,
      reason: lastErrorCode
        || (staleConnectivityProbe
          ? "最近一次连接探测已过期，请重新探测"
          : awaitingConnectivityEvidence
            ? "尚未收到有效连接探测结果，等待运行时自动同步"
            : ""),
      lastProbeAt,
    },
    createdAt: firstString(record, ["created_at"]) || null,
    updatedAt: firstString(record, ["updated_at"]) || null,
  };
}

function collectionItems(payload: unknown, keys: string[]) {
  if (Array.isArray(payload)) return payload;
  const record = asRecord(payload);
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return [];
}

export function normalizeChannelAdapterCollection(payload: unknown): ChannelAdapterCollection {
  const record = asRecord(payload);
  return {
    items: collectionItems(payload, ["adapters", "items", "platforms"])
      .map(normalizeChannelAdapter)
      .filter((item) => item.id),
    readOnly: asBoolean(record.read_only, false) || record.mutable === false,
  };
}

export function normalizeChannelConnectionCollection(payload: unknown): ChannelConnectionCollection {
  const record = asRecord(payload);
  return {
    items: collectionItems(payload, ["connections", "items", "instances"])
      .map(normalizeChannelConnection)
      .filter((item) => item.id),
    readOnly: asBoolean(record.read_only, false) || record.mutable === false,
  };
}

function configPayload(input: ChannelConnectionWrite) {
  const rawConfig = input.configValues ?? {
    ...input.extraConfig,
    endpoint_url: input.endpointUrl,
    poll_interval_seconds: input.pollIntervalSeconds,
    send_interval_seconds: input.sendIntervalSeconds,
  };
  const allowedFields = input.configFieldNames
    ? new Set(input.configFieldNames)
    : null;
  return Object.fromEntries(
    Object.entries(rawConfig).filter(([name, value]) => (
      (!allowedFields || allowedFields.has(name))
      && value !== undefined
      && !(typeof value === "string" && !value.trim())
    )),
  );
}

function createPayload(input: ChannelConnectionWrite) {
  return {
    adapter_id: input.adapterId,
    display_name: input.displayName,
    config_json: configPayload(input),
    secret_ref: input.secretRef,
    required_for_launch: false,
    desired_state: input.desiredState,
  };
}

function updatePayload(input: ChannelConnectionWrite) {
  return {
    display_name: input.displayName,
    config_json: configPayload(input),
    secret_ref: input.secretRef,
    required_for_launch: false,
  };
}

function normalizeActionResult(payload: unknown): ChannelConnectionActionResult {
  const record = asRecord(payload);
  const rawConnection = record.connection
    ?? record.value
    ?? (firstString(record, ["connection_id", "instance_id"]) ? record : null);
  const connection = rawConnection ? normalizeChannelConnection(rawConnection) : null;
  const status = firstString(record, ["status", "state"], connection?.effectiveState || "unknown");
  const ok = record.ok === undefined
    ? Boolean(connection)
    : asBoolean(record.ok, normalizeHealthState(status) === "ready");
  const errorCodes = asStringList(record.error_codes);
  const checks: ChannelConnectionActionResult["checks"] = [];
  if (record.ok !== undefined) {
    checks.push({
      id: "connection-check",
      label: "服务端连接检查",
      state: ok ? "ready" : "blocked",
      detail: errorCodes.length ? errorCodes.join("、") : status,
    });
  }
  return {
    ok,
    status,
    summary: ok
      ? `连接操作已完成：${status}`
      : errorCodes.length
        ? `连接检查未通过：${errorCodes.join("、")}`
        : `连接操作未完成：${status}`,
    checks,
    connection,
  };
}

export async function getChannelAdapters(config: ConsoleConfig) {
  const payload = await apiRequest<unknown>(config, ADAPTERS_PATH, {
    auth: true,
    query: { tenant_id: config.tenantId },
  });
  return normalizeChannelAdapterCollection(payload);
}

export async function getChannelConnections(config: ConsoleConfig) {
  const payload = await apiRequest<unknown>(config, CONNECTIONS_PATH, {
    auth: true,
    query: { tenant_id: config.tenantId },
  });
  return normalizeChannelConnectionCollection(payload);
}

export async function getChannelConnection(
  config: ConsoleConfig,
  connectionId: string,
): Promise<VersionedResourceResponse<ChannelConnection>> {
  const result = await apiVersionedResource<unknown>(
    config,
    `${CONNECTIONS_PATH}/${encodeURIComponent(connectionId)}`,
    { auth: true, query: { tenant_id: config.tenantId } },
  );
  return { value: normalizeChannelConnection(result.value), etag: result.etag };
}

export async function createChannelConnection(
  config: ConsoleConfig,
  input: ChannelConnectionWrite,
  idempotencyKey: string,
): Promise<VersionedResourceResponse<ChannelConnection>> {
  const result = await apiVersionedResource<unknown, ReturnType<typeof createPayload>>(
    config,
    CONNECTIONS_PATH,
    {
      auth: true,
      query: { tenant_id: config.tenantId },
      method: "POST",
      body: createPayload(input),
      idempotencyKey,
    },
  );
  return { value: normalizeChannelConnection(result.value), etag: result.etag };
}

export async function updateChannelConnection(
  config: ConsoleConfig,
  connectionId: string,
  input: ChannelConnectionWrite,
  etag: string,
  idempotencyKey: string,
): Promise<VersionedResourceResponse<ChannelConnection>> {
  const result = await apiVersionedResource<unknown, ReturnType<typeof updatePayload>>(
    config,
    `${CONNECTIONS_PATH}/${encodeURIComponent(connectionId)}`,
    {
      auth: true,
      query: { tenant_id: config.tenantId },
      method: "PATCH",
      body: updatePayload(input),
      ifMatch: etag,
      idempotencyKey,
    },
  );
  return { value: normalizeChannelConnection(result.value), etag: result.etag };
}

export async function deleteChannelConnection(
  config: ConsoleConfig,
  connectionId: string,
  etag: string,
  idempotencyKey: string,
) {
  return apiVersionedResource<unknown>(
    config,
    `${CONNECTIONS_PATH}/${encodeURIComponent(connectionId)}`,
    {
      auth: true,
      query: { tenant_id: config.tenantId },
      method: "DELETE",
      ifMatch: etag,
      idempotencyKey,
    },
  );
}

async function runConnectionAction(
  config: ConsoleConfig,
  connectionId: string,
  action: "probe" | "enable" | "disable" | "validate",
  etag: string,
  idempotencyKey: string,
) {
  const result = await apiVersionedResource<unknown>(
    config,
    `${CONNECTIONS_PATH}/${encodeURIComponent(connectionId)}/${action}`,
    {
      auth: true,
      query: { tenant_id: config.tenantId },
      method: "POST",
      ifMatch: etag,
      idempotencyKey,
    },
  );
  return normalizeActionResult(result.value);
}

export const probeChannelConnection = (
  config: ConsoleConfig,
  connectionId: string,
  etag: string,
  idempotencyKey: string,
) => runConnectionAction(config, connectionId, "probe", etag, idempotencyKey);

export const enableChannelConnection = (
  config: ConsoleConfig,
  connectionId: string,
  etag: string,
  idempotencyKey: string,
) => runConnectionAction(config, connectionId, "enable", etag, idempotencyKey);

export const disableChannelConnection = (
  config: ConsoleConfig,
  connectionId: string,
  etag: string,
  idempotencyKey: string,
) => runConnectionAction(config, connectionId, "disable", etag, idempotencyKey);

export const validateChannelConnection = (
  config: ConsoleConfig,
  connectionId: string,
  etag: string,
  idempotencyKey: string,
) => runConnectionAction(config, connectionId, "validate", etag, idempotencyKey);

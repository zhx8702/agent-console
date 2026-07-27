import type {
  ChannelAdapter,
  ChannelConnection,
  ChannelConnectionWrite,
  ChannelHealthState,
  ChannelManagedBy,
} from "../../lib/channel-connections";

export type ConnectionFilterState = "all" | "healthy" | "attention" | "draft" | "disabled";

export type ConnectionEditorErrors = Partial<Record<
  "adapterId" | "displayName" | "endpointUrl" | "pollIntervalSeconds" | "sendIntervalSeconds" | "secretRef",
  string
>> & { config?: Record<string, string> };

export const HEALTH_LABELS: Record<ChannelHealthState, string> = {
  ready: "正常",
  action_required: "待操作",
  blocked: "被阻塞",
  degraded: "已降级",
  unknown: "未知",
};

export const HEALTH_DESCRIPTIONS: Record<keyof ChannelConnection["health"], string> = {
  aggregate: "基于生命周期和最近探测证据汇总的连接状态",
  configured: "最近一次服务端配置校验是否通过",
  auth: "密钥解析或主动探测是否提供了鉴权证据",
  runtime: "有效生命周期与主动探测是否提供了运行证据",
  probe: "最近一次主动连接探测的结果",
  reason: "最近一次状态说明",
  lastProbeAt: "最近一次主动探测",
};

export function healthTone(state: ChannelHealthState) {
  if (state === "ready") return "ok";
  if (state === "blocked") return "danger";
  if (state === "action_required" || state === "degraded") return "warning";
  return "muted";
}

export function lifecycleLabel(value: string) {
  const normalized = value.trim().toLowerCase();
  const labels: Record<string, string> = {
    active: "已启用",
    enabled: "已启用",
    // The backend uses `ready` after a successful probe but before the
    // connector runtime has converged to `enabled`.
    ready: "已就绪",
    draft: "草稿",
    disabled: "已停用",
    stopped: "已停用",
    deleting: "删除中",
    pending: "等待生效",
    unverified: "待验证",
    validating: "验证中",
    error: "异常",
  };
  return labels[normalized] || value || "未知";
}

export function managedByLabel(value: ChannelManagedBy) {
  const labels: Record<ChannelManagedBy, string> = {
    platform: "平台托管",
    environment: "部署环境托管",
    external: "外部系统托管",
    unknown: "来源未知",
  };
  return labels[value];
}

export function connectionCategory(connection: ChannelConnection): Exclude<ConnectionFilterState, "all"> {
  const desiredState = connection.desiredState.toLowerCase();
  const effectiveState = connection.effectiveState.toLowerCase();
  // A disable request is not the same thing as a stopped runtime. Keep the
  // instance visible as needing attention until reconciliation confirms the
  // effective state has actually stopped.
  if (/disabled|stopped|off/.test(effectiveState)) return "disabled";
  if (/disabled|stopped|off/.test(desiredState)) return "attention";
  const lifecycle = `${desiredState} ${effectiveState}`;
  if (/draft|pending_config/.test(lifecycle) || connection.health.configured === "action_required") return "draft";
  if (connection.health.aggregate === "ready") return "healthy";
  return "attention";
}

export function connectionStats(connections: ChannelConnection[]) {
  return connections.reduce(
    (stats, connection) => {
      stats[connectionCategory(connection)] += 1;
      return stats;
    },
    { healthy: 0, attention: 0, draft: 0, disabled: 0 },
  );
}

export function adapterDisplayName(adapterId: string, adapters: ChannelAdapter[], fallback = "") {
  return adapters.find((item) => item.id === adapterId)?.displayName || fallback || adapterId || "未知平台";
}

export function filterConnections(
  connections: ChannelConnection[],
  adapterId: string,
  state: ConnectionFilterState,
) {
  return connections.filter((connection) => (
    (!adapterId || connection.adapterId === adapterId)
    && (state === "all" || connectionCategory(connection) === state)
  ));
}

export function adapterConfigDefaults(adapter?: ChannelAdapter) {
  if (!adapter) return {};
  return Object.fromEntries(
    Object.entries(adapter.configFields)
      .filter(([, field]) => field.defaultValue !== undefined)
      .map(([name, field]) => [name, field.defaultValue]),
  );
}

export function adapterConfigFieldNames(adapter?: ChannelAdapter) {
  return adapter ? Object.keys(adapter.configFields) : [];
}

export function configValuesForAdapter(
  values: Record<string, unknown> | undefined,
  adapter?: ChannelAdapter,
) {
  if (!adapter) return values ?? {};
  const allowed = new Set(adapterConfigFieldNames(adapter));
  return Object.fromEntries(
    Object.entries(values ?? {}).filter(([name]) => allowed.has(name)),
  );
}

export function emptyConnectionDraft(
  adapterId = "",
  adapter?: ChannelAdapter,
): ChannelConnectionWrite {
  return {
    adapterId,
    displayName: "",
    endpointUrl: "",
    pollIntervalSeconds: 3,
    sendIntervalSeconds: 2,
    extraConfig: {},
    configValues: adapterConfigDefaults(adapter),
    configFieldNames: adapter ? adapterConfigFieldNames(adapter) : undefined,
    secretRef: "",
    requiredForLaunch: false,
    desiredState: "draft",
  };
}

export function connectionDraftFromValue(
  connection: ChannelConnection,
  adapter?: ChannelAdapter,
): ChannelConnectionWrite {
  return {
    adapterId: connection.adapterId,
    displayName: connection.displayName,
    endpointUrl: connection.config.endpointUrl,
    pollIntervalSeconds: connection.config.pollIntervalSeconds,
    sendIntervalSeconds: connection.config.sendIntervalSeconds,
    extraConfig: connection.config.extra,
    configValues: adapter ? configValuesForAdapter(connection.config.raw, adapter) : connection.config.raw,
    configFieldNames: adapter ? adapterConfigFieldNames(adapter) : undefined,
    secretRef: connection.secretRef,
    requiredForLaunch: false,
    desiredState: connection.desiredState,
  };
}

function validEndpoint(value: string) {
  if (
    !value
    || value !== value.trim()
    || /\s/.test(value)
    || /[?#]/.test(value)
    || !/^https?:\/\//i.test(value)
  ) {
    return false;
  }
  const authority = value.slice(value.indexOf("//") + 2).split("/", 1)[0];
  if (!authority || authority.includes("@")) return false;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol)
      && Boolean(url.hostname)
      && !url.username
      && !url.password;
  } catch {
    return false;
  }
}

function validSecretReference(value: string, adapter?: ChannelAdapter) {
  if (!value) return true;
  const match = /^([a-z][a-z0-9-]*):(?:(?:\/\/)?)([^\s]+)$/i.exec(value);
  if (!match) return false;
  const scheme = match[1].toLowerCase();
  const locator = match[2];
  const acceptedSchemes = new Set(
    adapter?.secretFields.flatMap((field) => field.acceptedRefSchemes) ?? [
      "env",
      "vault",
      "secret-manager",
    ],
  );
  if (!acceptedSchemes.has(scheme)) return false;
  if (scheme !== "env") return true;
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(locator)) return false;
  const allowedVariables = new Set(
    adapter?.secretFields.map((field) => field.environmentVariable).filter(Boolean),
  );
  return !allowedVariables.size || allowedVariables.has(locator);
}

export function validateConnectionDraft(
  draft: ChannelConnectionWrite,
  adapter?: ChannelAdapter,
): ConnectionEditorErrors {
  const errors: ConnectionEditorErrors = {};
  if (!draft.adapterId.trim()) errors.adapterId = "请选择消息平台适配器";
  if (!draft.displayName.trim()) {
    errors.displayName = "请输入便于运营识别的连接名称";
  } else if (draft.displayName.trim().length > 128) {
    errors.displayName = "连接名称不能超过 128 个字符";
  }
  if (adapter) {
    const configErrors: Record<string, string> = {};
    const config = draft.configValues ?? {};
    for (const [name, field] of Object.entries(adapter.configFields)) {
      const value = config[name];
      const required = adapter.configRequired.includes(name);
      const blankString = typeof value === "string" && !value.trim();
      if (required && (value === undefined || value === null || blankString)) {
        configErrors[name] = `请填写${field.title}`;
        continue;
      }
      if (value === undefined || blankString) continue;
      if (field.type === "number" || field.type === "integer") {
        const numeric = Number(value);
        if (!Number.isFinite(numeric) || (field.type === "integer" && !Number.isInteger(numeric))) {
          configErrors[name] = `${field.title}格式不正确`;
        } else if (field.minimum !== null && numeric < field.minimum) {
          configErrors[name] = `${field.title}不能小于 ${field.minimum}`;
        } else if (field.maximum !== null && numeric > field.maximum) {
          configErrors[name] = `${field.title}不能大于 ${field.maximum}`;
        }
      } else if (field.type === "boolean" && typeof value !== "boolean") {
        configErrors[name] = `${field.title}必须为开关值`;
      } else if (field.type === "object" && (
        !value || typeof value !== "object" || Array.isArray(value)
      )) {
        configErrors[name] = `${field.title}必须为 JSON 对象`;
      } else if (field.type === "array" && !Array.isArray(value)) {
        configErrors[name] = `${field.title}必须为 JSON 数组`;
      } else if (field.type === "null" && value !== null) {
        configErrors[name] = `${field.title}必须为 null`;
      } else if (field.type === "string" && typeof value !== "string") {
        configErrors[name] = `${field.title}必须为文本`;
      } else if (field.type === "string" && typeof value === "string" && field.minLength !== null && value.length < field.minLength) {
        configErrors[name] = `${field.title}不能少于 ${field.minLength} 个字符`;
      } else if (field.type === "string" && typeof value === "string" && field.maxLength !== null && value.length > field.maxLength) {
        configErrors[name] = `${field.title}不能超过 ${field.maxLength} 个字符`;
      } else if (field.format === "uri" && !validEndpoint(String(value))) {
        configErrors[name] = `${field.title}需为不含空白、账号密码、查询参数或片段的 http/https 地址`;
      }
    }
    if (Object.keys(configErrors).length) errors.config = configErrors;
  } else {
    // Compatibility for callers which have not loaded the adapter catalog yet.
    if (!draft.endpointUrl.trim()) {
      errors.endpointUrl = "请输入 SDK 或网关地址";
    } else if (!validEndpoint(draft.endpointUrl)) {
      errors.endpointUrl = "需为不含空白、账号密码、查询参数或片段的 http/https 地址";
    }
    if (!Number.isInteger(draft.pollIntervalSeconds) || draft.pollIntervalSeconds < 1 || draft.pollIntervalSeconds > 3600) {
      errors.pollIntervalSeconds = "轮询间隔需为 1–3600 秒的整数";
    }
    if (!Number.isInteger(draft.sendIntervalSeconds) || draft.sendIntervalSeconds < 1 || draft.sendIntervalSeconds > 3600) {
      errors.sendIntervalSeconds = "发送间隔需为 1–3600 秒的整数";
    }
  }
  if (adapter?.secretFields.some((field) => field.required) && !draft.secretRef.trim()) {
    errors.secretRef = "该适配器需要配置凭据引用";
  }
  if (!validSecretReference(draft.secretRef.trim(), adapter)) {
    errors.secretRef = "凭据引用不符合当前适配器声明的 scheme 或环境变量范围";
  }
  return errors;
}

export function isWechatAdapter(adapterId: string) {
  return /(^|[-_.])(wechat|wxbot|weixin)([-_.]|$)/i.test(adapterId);
}

export function formatConnectionTime(value: string | null) {
  if (!value) return "尚无记录";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

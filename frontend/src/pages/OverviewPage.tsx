import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { StatusTile } from "../components/StatusTile";
import { TechnicalDetails, friendlyErrorMessage } from "../components/TechnicalDetails";
import {
  ApiError,
  apiDocumentUrl,
  apiRequest,
  formatJson,
  type CapabilityHealth,
  type CapabilityLoadState,
  type CapabilityRecoveryAction,
  type LaunchChecklistStep,
  type TenantCapability,
} from "../lib/api";
import { useConsoleConfig } from "../state/console-config";

type OverviewData = {
  health?: { status: string };
  ready?: {
    status: string;
    checks?: {
      redis?: { ok?: boolean };
      db?: { backend?: string };
      qdrant?: { backend?: string };
      knowledge_features?: { enabled?: boolean };
    };
  };
  pluginPaths: string[];
  pluginSummary?: {
    plugins: Array<{ name: string; version: string; description: string }>;
    plugin_routes: string[];
    hooks: Record<string, string[]>;
    channels: string[];
    channel_labels: Record<string, string>;
  } | null;
};

type OverviewErrors = Partial<Record<"health" | "ready" | "spec" | "plugins", string>>;
type OverviewCheckKey = keyof OverviewErrors;

const OVERVIEW_CHECK_LABELS: Record<OverviewCheckKey, string> = {
  health: "运行健康",
  ready: "启动就绪",
  spec: "API 规范",
  plugins: "插件摘要",
};

function serviceStateLabel(value: string | undefined, error: boolean) {
  if (error) return "不可用";
  if (!value) return "-";
  const labels: Record<string, string> = {
    ok: "正常",
    ready: "已就绪",
    healthy: "正常",
    degraded: "信息不完整",
    error: "异常",
    unavailable: "不可用",
    enabled: "已启用",
    off: "已关闭",
    unknown: "未知",
  };
  return labels[value.toLowerCase()] || "可用";
}

const EMPTY_CAPABILITY_STATE: CapabilityLoadState = {
  status: "idle",
  data: null,
  error: "",
};

const CAPABILITY_STATE_LABELS: Record<CapabilityHealth, string> = {
  ready: "已就绪",
  action_required: "待操作",
  blocked: "被阻塞",
  degraded: "已降级",
};

const CAPABILITY_REASON_LABELS: Record<string, string> = {
  plugin_not_active: "插件未启用",
  plugin_not_loaded: "插件未加载",
  plugin_not_configured: "未配置",
  plugin_disabled_for_tenant: "当前租户已停用",
  plugin_active_for_tenant: "当前租户已启用",
  tenant_scope_state_unavailable: "租户插件状态暂时不可用",
  required_plugin_dependency_unavailable: "必需依赖不可用",
  adapter_connection_verified: "连接已验证",
  adapter_connection_configured_unverified: "连接已配置，尚未验证",
  adapter_available_connection_unverified: "适配器可用，连接尚未验证",
  connection_required: "需要完成平台连接",
  service_registered: "服务已注册",
};

function capabilityReasonLabel(reason: string) {
  const exact = CAPABILITY_REASON_LABELS[reason];
  if (exact) return exact;
  if (reason.startsWith("plugin_initialization_failed")) return "插件初始化失败";
  return "";
}

type OverviewPageProps = {
  capabilityState?: CapabilityLoadState;
  accessScope?: "tenant" | "group";
  onRetryCapabilities?: () => void;
};

function RecoveryActionLink({
  action,
  onRetry,
  surface = "dark",
}: {
  action: CapabilityRecoveryAction;
  onRetry: () => void;
  surface?: "dark" | "paper";
}) {
  const className = surface === "paper" ? "button button-secondary button-compact" : "launch-action";
  const adminMark = action.requires_admin ? (
    <span className={surface === "paper" ? "pill pill-muted" : "admin-chip"}>管理员</span>
  ) : null;
  if (action.type === "retry") {
    return (
      <button className={className} type="button" onClick={onRetry}>
        {action.label}
        {adminMark}
      </button>
    );
  }
  return (
    <Link className={className} to={action.target}>
      {action.label}
      {adminMark}
    </Link>
  );
}

function LaunchStep({
  step,
  index,
  onRetry,
}: {
  step: LaunchChecklistStep;
  index: number;
  onRetry: () => void;
}) {
  const blockers = step.dependencies.filter((item) => item.state !== "ready");
  return (
    <li className={`launch-step is-${step.state}`}>
      <div className="launch-step-index" aria-hidden="true">
        {String(index + 1).padStart(2, "0")}
      </div>
      <div className="launch-step-body">
        <div className="launch-step-heading">
          <strong>{step.label}</strong>
          <span className="launch-state-badge">
            {step.optional && step.state !== "ready" ? "可选" : CAPABILITY_STATE_LABELS[step.state]}
          </span>
        </div>
        <p>{step.description}</p>
        {blockers.length > 0 && (
          <ul className="launch-blockers" aria-label={`${step.label}的依赖状态`}>
            {blockers.map((dependency) => (
              <li key={dependency.id}>
                <span>{dependency.reason}</span>
                <TechnicalDetails summary="查看依赖标识" value={dependency.id} />
              </li>
            ))}
          </ul>
        )}
        {step.recovery_actions.length > 0 && (
          <div className="launch-actions">
            {step.recovery_actions.map((action) => (
              <RecoveryActionLink
                key={`${action.type}-${action.target}`}
                action={action}
                onRetry={onRetry}
              />
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

function CapabilityDiagnostic({
  capability,
  onRetry,
}: {
  capability: TenantCapability;
  onRetry: () => void;
}) {
  const dependencyIssues = capability.dependencies.filter((item) => item.state !== "ready");
  const reasonLabel = capabilityReasonLabel(capability.status_reason);
  return (
    <article className={`capability-diagnostic is-${capability.health}`}>
      <div>
        <span className="capability-diagnostic-source">能力检查</span>
        <h3>{capability.label}</h3>
        {reasonLabel ? <p>{reasonLabel}</p> : null}
      </div>
      <div className="capability-diagnostic-state">
        <span>{CAPABILITY_STATE_LABELS[capability.health]}</span>
        <small>
          {capability.enabled ? "已启用" : "未启用"} · {capability.available ? "入口可用" : "入口不可用"}
        </small>
      </div>
      {dependencyIssues.length > 0 && (
        <ul className="capability-dependency-list">
          {dependencyIssues.map((dependency) => (
            <li key={dependency.id}>
              <strong>{dependency.required ? "必需" : "可选"}</strong>
              {capabilityReasonLabel(dependency.reason) ? (
                <span>{capabilityReasonLabel(dependency.reason)}</span>
              ) : null}
              <TechnicalDetails summary="查看依赖标识" value={dependency.id} />
            </li>
          ))}
        </ul>
      )}
      {capability.recovery_actions.length > 0 && (
        <div className="launch-actions capability-actions">
          {capability.recovery_actions.map((action) => (
            <RecoveryActionLink
              key={`${action.type}-${action.target}`}
              action={action}
              onRetry={onRetry}
              surface="paper"
            />
          ))}
        </div>
      )}
    </article>
  );
}

function settledPayload<T>(result: PromiseSettledResult<T>) {
  if (result.status === "fulfilled") {
    return result.value;
  }
  if (
    result.reason instanceof ApiError &&
    typeof result.reason.payload === "object" &&
    result.reason.payload !== null
  ) {
    return result.reason.payload as T;
  }
  return undefined;
}

function settledError(result: PromiseSettledResult<unknown>) {
  if (result.status === "fulfilled") {
    return "";
  }
  return friendlyErrorMessage(result.reason, "检查失败，请稍后重试。");
}

export function OverviewPage({
  capabilityState = EMPTY_CAPABILITY_STATE,
  accessScope = "tenant",
  onRetryCapabilities = () => undefined,
}: OverviewPageProps) {
  const { config } = useConsoleConfig();
  const [data, setData] = useState<OverviewData | null>(null);
  const [errors, setErrors] = useState<OverviewErrors>({});
  const [lastSuccessAt, setLastSuccessAt] = useState<Partial<Record<OverviewCheckKey, string>>>({});
  const [loading, setLoading] = useState(false);
  const [checklistDismissed, setChecklistDismissed] = useState(false);
  const groupScoped =
    accessScope === "group" || capabilityState.data?.access.scope === "group";
  const checklistStorageKey = `agent-console:launch-checklist:v1:${config.tenantId || "default"}`;

  useEffect(() => {
    const stored = window.localStorage.getItem(checklistStorageKey);
    if (stored === "dismissed") {
      setChecklistDismissed(true);
      return;
    }
    if (stored === "open") {
      setChecklistDismissed(false);
      return;
    }
    const steps = capabilityState.data?.onboarding?.steps || [];
    setChecklistDismissed(steps.length > 0 && steps.every((step) => step.state === "ready"));
  }, [capabilityState.data?.onboarding, checklistStorageKey]);

  const loadOverview = async () => {
    setLoading(true);
    setErrors({});
    if (groupScoped) {
      setData({ pluginPaths: [], pluginSummary: null });
      setLoading(false);
      return;
    }
    try {
      const [healthResult, readyResult, specResult, pluginSummaryResult] = await Promise.allSettled([
        apiRequest<{ status: string }>(config, "/healthz"),
        apiRequest<OverviewData["ready"]>(config, "/readyz"),
        apiRequest<{ paths: Record<string, unknown> }>(config, "/openapi.json"),
        apiRequest<NonNullable<OverviewData["pluginSummary"]>>(config, "/v1/admin/plugins/summary", {
          auth: true,
        }),
      ]);

      const health = settledPayload(healthResult);
      const ready = settledPayload(readyResult);
      const spec = settledPayload(specResult);
      const pluginSummary = settledPayload(pluginSummaryResult) || null;
      const nextErrors: OverviewErrors = {};

      const resultEntries = [
        ["health", healthResult],
        ["ready", readyResult],
        ["spec", specResult],
        ["plugins", pluginSummaryResult],
      ] as const;
      const succeededAt = new Date().toISOString();
      setLastSuccessAt((current) => {
        const next = { ...current };
        for (const [key, result] of resultEntries) {
          if (result.status === "fulfilled") {
            next[key] = succeededAt;
          }
        }
        return next;
      });
      for (const [key, result] of resultEntries) {
        const message = settledError(result);
        if (message) {
          nextErrors[key] = message;
        }
      }

      setData({
        health,
        ready,
        pluginPaths: Object.keys(spec?.paths || {}).filter((path) => path.startsWith("/plugins/")),
        pluginSummary,
      });
      setErrors(nextErrors);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadOverview();
  }, [config.apiBaseUrl, groupScoped]);

  const output = formatJson(
    data
      ? {
          ...data,
          capability_registry: capabilityState.data,
          capability_registry_error: capabilityState.error,
          request_errors: errors,
        }
      : {
          message: loading ? "loading" : "等待加载",
          capability_registry: capabilityState.data,
          capability_registry_error: capabilityState.error,
          request_errors: errors,
        },
  );
  const errorEntries = Object.entries(errors);
  const pluginInformationUnavailable =
    Boolean(errors.plugins || errors.spec) &&
    !data?.pluginSummary?.plugins?.length &&
    !data?.pluginPaths.length;
  const checklist = capabilityState.data?.onboarding;
  const readySteps = checklist?.steps.filter((step) => step.state === "ready").length || 0;
  const attentionCapabilities = (capabilityState.data?.capabilities || []).filter(
    (capability) => capability.health !== "ready",
  );

  return (
    <div className="page-grid">
      {checklistDismissed && capabilityState.status === "ready" ? (
        <section className="panel launch-checklist-collapsed span-3" aria-label="上线清单">
          <div>
            <p className="section-kicker">上线顺序</p>
            <strong>上线清单已收起</strong>
          </div>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => {
              window.localStorage.setItem(checklistStorageKey, "open");
              setChecklistDismissed(false);
            }}
          >
            重新打开清单
          </button>
        </section>
      ) : capabilityState.status === "loading" || capabilityState.status === "idle" ? (
        <section
          className="panel launch-checklist launch-checklist-loading span-3"
          aria-busy="true"
          aria-label="正在加载上线清单"
        >
          <div className="launch-checklist-heading">
            <div>
              <p className="section-kicker">上线顺序</p>
              <h2>正在读取租户能力</h2>
            </div>
            <span className="launch-orbit" aria-hidden="true" />
          </div>
          <div className="launch-loading-lines" aria-hidden="true">
            {Array.from({ length: 7 }, (_, index) => <span key={index} />)}
          </div>
        </section>
      ) : capabilityState.status === "degraded" ? (
        <section className="panel launch-checklist launch-checklist-error span-3" role="alert">
          <div>
            <p className="section-kicker">上线顺序 · 信息不完整</p>
            <h2>暂时无法生成可信的上线清单</h2>
            <p>{capabilityState.error || "能力服务未返回可用数据。"}</p>
          </div>
          <button className="button button-primary" type="button" onClick={onRetryCapabilities}>
            重试能力检查
          </button>
        </section>
      ) : checklist ? (
        <section className="panel launch-checklist span-3" aria-labelledby="launch-checklist-title">
          <div className="launch-checklist-heading">
            <div>
              <p className="section-kicker">首次登录 · 上线顺序</p>
              <h2 id="launch-checklist-title">
                {groupScoped ? "从授权群到安全参与" : "从依赖检查到正式参与"}
              </h2>
              {!groupScoped ? (
                <p>
                  这是租户 <code>{capabilityState.data?.tenant_id}</code> 的当前状态，按顺序完成后再让机器人加入真实群聊。
                </p>
              ) : null}
            </div>
            <div
              className={`launch-readiness is-${checklist.steps.length ? checklist.state : "empty"}`}
              aria-label={checklist.steps.length ? "上线步骤就绪进度" : "上线步骤尚未生成"}
            >
              {checklist.steps.length ? (
                <>
                  <span>{readySteps}/{checklist.steps.length}</span>
                  <small>{CAPABILITY_STATE_LABELS[checklist.state]}</small>
                </>
              ) : (
                <>
                  <span>暂无步骤</span>
                  <small>尚未生成</small>
                </>
              )}
            </div>
          </div>
          {checklist.steps.length ? (
            <ol className="launch-step-list">
              {checklist.steps.map((step, index) => (
                <LaunchStep
                  key={step.id}
                  step={step}
                  index={index}
                  onRetry={onRetryCapabilities}
                />
              ))}
            </ol>
          ) : (
            <div className="launch-checklist-empty" role="status">
              <strong>暂无上线步骤</strong>
              <p>当前没有可执行的上线步骤。</p>
            </div>
          )}
          <div className="launch-checklist-footer">
            <button
              type="button"
              onClick={() => {
                window.localStorage.setItem(checklistStorageKey, "dismissed");
                setChecklistDismissed(true);
              }}
            >
              稍后处理
            </button>
          </div>
        </section>
      ) : null}

      {groupScoped ? (
        <section className="panel panel-hero span-3" aria-labelledby="group-workspace-title">
          <PageHeader
            eyebrow="群聊工作区"
            title="授权群工作台"
            description="从已授权群进入参与策略、消息演练和成员知识。"
          />
          <h2 id="group-workspace-title" className="sr-only">授权群工作台入口</h2>
          <div className="status-grid">
            <StatusTile
              label="可用入口"
              value={String(capabilityState.data?.summary.visible_navigation ?? 0)}
            />
            <StatusTile
              label="能力状态"
              value={CAPABILITY_STATE_LABELS[capabilityState.data?.state || "degraded"]}
            />
            <StatusTile label="数据范围" value="仅授权群" />
          </div>
          <div className="action-row">
            <Link className="button button-primary" to="/group-behavior">复核群参与策略</Link>
            <Link className="button button-secondary" to="/playground">进行消息演练</Link>
            <Link className="button button-secondary" to="/knowledge">管理群知识</Link>
          </div>
        </section>
      ) : (
        <>
      <section className="panel panel-hero span-2">
        <PageHeader
          eyebrow="系统"
          title="后端运行概览"
          description="查看服务接口、插件路由和关键依赖状态，确认控制台与服务端连接是否正常。"
          actions={
            <div className="action-row">
              <button
                className="button button-primary"
                onClick={() => void loadOverview()}
                disabled={loading}
              >
                {loading ? "刷新中..." : "刷新状态"}
              </button>
              <a className="button button-secondary" href={apiDocumentUrl(config, "/docs")} target="_blank" rel="noreferrer">
                接口文档
              </a>
            </div>
          }
        />
        {errorEntries.length > 0 && (
          <div className="overview-degraded-notice" role="status">
            <strong>控制面可访问，但部分检查处于降级状态</strong>
            <ul>
              {errorEntries.map(([key, message]) => (
                <li key={key}>
                  <span>{OVERVIEW_CHECK_LABELS[key as OverviewCheckKey]}</span>
                  <span>{friendlyErrorMessage(message, "检查失败，请稍后重试。")}</span>
                  <button type="button" onClick={() => void loadOverview()} disabled={loading}>
                    重试
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="status-grid">
          <StatusTile label="运行健康" value={serviceStateLabel(data?.health?.status, Boolean(errors.health))} />
          <StatusTile label="启动就绪" value={serviceStateLabel(data?.ready?.status, Boolean(errors.ready))} />
          <StatusTile label="缓存队列" value={data?.ready?.checks?.redis?.ok ? "正常" : errors.ready ? "不可用" : "-"} />
          <StatusTile label="关系数据库" value={data?.ready?.checks?.db?.backend ? "可用" : errors.ready ? "不可用" : "-"} />
          <StatusTile label="向量索引" value={data?.ready?.checks?.qdrant?.backend ? "可用" : errors.ready ? "不可用" : "-"} />
          <StatusTile
            label="知识检索"
            value={
              data?.ready?.checks?.knowledge_features
                ? data.ready.checks.knowledge_features.enabled
                  ? "已启用"
                  : "已关闭"
                : errors.ready
                  ? "未知"
                  : "-"
            }
          />
        </div>
        <details className="overview-check-meta-wrap">
          <summary>检查记录</summary>
          <ul className="overview-check-meta" aria-label="检查更新时间与恢复动作">
          {(Object.keys(OVERVIEW_CHECK_LABELS) as OverviewCheckKey[]).map((key) => (
            <li key={key}>
              <span>{OVERVIEW_CHECK_LABELS[key]}</span>
              <time dateTime={lastSuccessAt[key] || undefined}>
                {lastSuccessAt[key]
                  ? `最后成功：${new Date(lastSuccessAt[key] as string).toLocaleString("zh-CN", { hour12: false })}`
                  : "尚无成功记录"}
              </time>
              {errors[key] && (
                <button type="button" onClick={() => void loadOverview()} disabled={loading}>
                  重新检查
                </button>
              )}
            </li>
          ))}
        </ul>
        </details>
      </section>

      <section className="panel panel-scroll">
        <div className="panel-header">
          <div>
            <p className="section-kicker">插件</p>
            <h2>插件与适配器摘要</h2>
          </div>
        </div>
        <p className="muted-copy">
          {pluginInformationUnavailable
            ? "插件信息暂不可用，请查看上方原因。"
            : `已发现 ${data?.pluginSummary?.plugins?.length || 0} 个插件，已声明 ${data?.pluginSummary?.channels?.length || 0} 个通道。`}
        </p>
        <TechnicalDetails
          summary="查看插件与适配器技术清单"
          value={formatJson({
            plugins: data?.pluginSummary?.plugins || [],
            plugin_paths: data?.pluginPaths || [],
            channels: data?.pluginSummary?.channels || [],
            channel_labels: data?.pluginSummary?.channel_labels || {},
          })}
        />
      </section>

      {capabilityState.status === "ready" && (
        <section
          className="panel capability-diagnostics-panel span-3"
          aria-labelledby="capability-diagnostics-title"
        >
          <details open={attentionCapabilities.length > 0}>
            <summary>
              <span>
                <span className="section-kicker">能力诊断</span>
                <strong id="capability-diagnostics-title">能力与依赖诊断</strong>
              </span>
              <span className={`capability-attention-count${attentionCapabilities.length > 0 ? "" : " is-clear"}`}>
                {attentionCapabilities.length > 0
                  ? `${attentionCapabilities.length} 项需处理`
                  : "全部就绪"}
              </span>
            </summary>
            <div className="capability-diagnostic-grid">
              {(attentionCapabilities.length > 0
                ? attentionCapabilities
                : capabilityState.data.capabilities
              ).map((capability) => (
                <CapabilityDiagnostic
                  key={capability.id}
                  capability={capability}
                  onRetry={onRetryCapabilities}
                />
              ))}
            </div>
          </details>
        </section>
      )}

      <div className="span-3">
        <OutputPanel flush title="系统响应" value={output} />
      </div>
        </>
      )}
    </div>
  );
}

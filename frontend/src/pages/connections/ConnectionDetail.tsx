import { Link } from "react-router-dom";

import { Alert, DangerAction, EmptyState, TechnicalDetails } from "../../components";
import type {
  ChannelAdapter,
  ChannelConnection,
  ChannelConnectionActionResult,
  ChannelHealthState,
} from "../../lib/channel-connections";
import {
  adapterDisplayName,
  formatConnectionTime,
  HEALTH_DESCRIPTIONS,
  HEALTH_LABELS,
  healthTone,
  isWechatAdapter,
  lifecycleLabel,
  managedByLabel,
} from "./model";

type DetailStatus = "idle" | "loading" | "ready" | "error" | "conflict";

type ConnectionDetailProps = {
  adapters: ChannelAdapter[];
  connection: ChannelConnection | null;
  status: DetailStatus;
  error: string;
  etag: string | null;
  actionKey: string;
  actionError: string;
  notice: string;
  actionResult: ChannelConnectionActionResult | null;
  collectionReadOnly: boolean;
  onEdit: () => void;
  onReload: () => void;
  onRunAction: (action: "probe" | "validate" | "enable" | "disable", connectionId: string) => Promise<unknown>;
  onDelete: (connectionId: string) => Promise<void>;
};

const HEALTH_DIMENSIONS: Array<{
  key: "configured" | "auth" | "runtime" | "probe";
  label: string;
}> = [
  { key: "configured", label: "配置校验" },
  { key: "auth", label: "鉴权证据" },
  { key: "runtime", label: "运行证据" },
  { key: "probe", label: "主动探测" },
];

function actionBusy(actionKey: string, action: string, connectionId: string) {
  return actionKey === `${action}:${connectionId}`;
}

function HealthCell({ label, state, description }: { label: string; state: ChannelHealthState; description: string }) {
  return (
    <article className={`connection-health-cell is-${healthTone(state)}`} title={description}>
      <span>{label}</span>
      <strong>{HEALTH_LABELS[state]}</strong>
      <small>{description}</small>
    </article>
  );
}

export function ConnectionDetail({
  adapters,
  connection,
  status,
  error,
  etag,
  actionKey,
  actionError,
  notice,
  actionResult,
  collectionReadOnly,
  onEdit,
  onReload,
  onRunAction,
  onDelete,
}: ConnectionDetailProps) {
  if (!connection && (status === "idle" || status === "error")) {
    return (
      <section className="panel connection-detail-panel" aria-labelledby="connection-detail-empty-title">
        {status === "error" ? (
          <Alert variant="warning" title="连接详情加载失败">
            <span>{error}</span>{" "}
            <button type="button" className="connection-inline-action" onClick={onReload}>重试</button>
          </Alert>
        ) : (
          <EmptyState
            compact
            title={<span id="connection-detail-empty-title">选择一个连接查看详情</span>}
            description="连接详情会分开显示配置校验、鉴权证据、运行证据和最近一次主动探测。"
          />
        )}
      </section>
    );
  }

  if (!connection) {
    return (
      <section className="panel connection-detail-panel" aria-busy="true" aria-label="正在加载连接详情">
        <div className="connection-detail-skeleton">
          <span />
          <span />
          <span />
        </div>
      </section>
    );
  }

  const readOnly = collectionReadOnly || connection.readOnly;
  const desiredDisabled = /^(disabled|stopped|off)$/i.test(connection.desiredState);
  const desiredEnabled = /^(enabled|active|running)$/i.test(connection.desiredState);
  const effectiveDisabled = /^(disabled|stopped|off)$/i.test(connection.effectiveState);
  const stopping = desiredDisabled && !effectiveDisabled;
  const disabled = desiredDisabled && effectiveDisabled;
  const deleteBlocked = /enabled|active|running/i.test(
    `${connection.desiredState} ${connection.effectiveState}`,
  );
  const adapter = adapters.find((item) => item.id === connection.adapterId);
  const canProbe = Boolean(adapter?.capabilities.includes("health_probe"));
  const hasConnectorRuntime = Boolean(adapter?.runtimeModes.length);
  const requiresPlatformCredential = Boolean(adapter?.secretFields.length);
  const platformLabel = adapterDisplayName(connection.adapterId, adapters, connection.adapterLabel);
  const configNames = Array.from(new Set([
    ...(adapter?.configOrder ?? []),
    ...Object.keys(connection.config.raw),
  ])).filter((name) => Object.prototype.hasOwnProperty.call(connection.config.raw, name));
  const configValue = (value: unknown) => {
    if (value === null || value === undefined || value === "") return "未配置";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  };
  const technicalSnapshot = {
    tenant_id: connection.tenantId,
    connection_id: connection.id,
    adapter_id: connection.adapterId,
    display_name: connection.displayName,
    config_json: connection.config.raw,
    ...(requiresPlatformCredential ? {
      secret_ref: connection.secretRef || null,
      secret_status: connection.secretStatus || null,
      secret_fingerprint: connection.secretFingerprint || null,
    } : {}),
    required_for_launch: connection.requiredForLaunch,
    priority: connection.priority,
    desired_state: connection.desiredState,
    effective_state: connection.effectiveState,
    last_probed_at: connection.health.lastProbeAt,
    last_probe_status: connection.lastProbeStatus || null,
    last_error_code: connection.lastErrorCode || null,
    managed_by: connection.managedBy,
    read_only: connection.readOnly,
    version: connection.version,
    created_at: connection.createdAt,
    updated_at: connection.updatedAt,
    response_etag: etag,
  };

  return (
    <section
      className="panel connection-detail-panel"
      aria-labelledby="connection-detail-title"
      aria-busy={status === "loading"}
    >
      <div className="connection-detail-heading">
        <div>
          <div className="connection-detail-platform-line">
            <span>{platformLabel}</span>
            <span className={`connection-state-badge is-${disabled ? "muted" : healthTone(connection.health.aggregate)}`}>
              {stopping ? "停用中" : disabled ? "已停用" : HEALTH_LABELS[connection.health.aggregate]}
            </span>
            <span className="connection-state-badge is-muted">{managedByLabel(connection.managedBy)}</span>
          </div>
          <h2 id="connection-detail-title">{connection.displayName}</h2>
          <p className="mono">{connection.id}</p>
        </div>
        <div className="connection-detail-actions">
          <button
            type="button"
            className="button button-primary"
            onClick={() => void onRunAction("probe", connection.id).catch(() => undefined)}
            disabled={readOnly || !etag || Boolean(actionKey) || !canProbe}
            aria-busy={actionBusy(actionKey, "probe", connection.id)}
            title={canProbe ? undefined : "该适配器未声明健康探测能力"}
          >
            {actionBusy(actionKey, "probe", connection.id) ? "探测中…" : "测试连接"}
          </button>
          <button
            type="button"
            className="button button-secondary"
            onClick={() => void onRunAction("validate", connection.id).catch(() => undefined)}
            disabled={readOnly || !etag || Boolean(actionKey)}
          >
            校验配置
          </button>
          <button type="button" className="button button-secondary" onClick={onEdit} disabled={readOnly || status !== "ready" || !etag}>
            编辑配置
          </button>
          {isWechatAdapter(connection.adapterId) && (
            <Link className="button button-secondary" to="/wxbot">打开微信扩展控制台</Link>
          )}
        </div>
      </div>

      {status === "loading" && <p className="connection-refresh-note" role="status">正在刷新详情，当前显示上一次成功结果…</p>}
      {!canProbe && (
        <p className="connection-refresh-note">
          当前适配器未声明 health_probe 能力，因此不能从控制台发起主动连接探测。
        </p>
      )}
      {!readOnly && hasConnectorRuntime && !desiredEnabled && (
        <p className="connection-refresh-note">
          启用后，请按连接 ID 以 {adapter?.runtimeModes.join("、")} 模式启动对应连接器。
        </p>
      )}
      {status === "conflict" && (
        <Alert variant="warning" title="连接配置已被其他操作者更新">
          当前草稿没有覆盖服务器版本。请重新读取详情，再核对后提交。
          <button type="button" className="connection-inline-action" onClick={onReload}>重新读取</button>
        </Alert>
      )}
      {status === "error" && (
        <Alert variant="warning" title="详情刷新失败，正在显示上次结果">
          <span>{error}</span>{" "}
          <button type="button" className="connection-inline-action" onClick={onReload}>重试</button>
        </Alert>
      )}
      {readOnly && (
        <Alert variant="info" title={connection.managedBy === "environment" ? "部署环境托管 / 只读" : "当前连接为只读"}>
          {connection.managedBy === "environment"
            ? "这条连接来自部署环境变量。控制台只显示安全摘要，不会探测、覆盖部署配置或回显凭据；迁移为控制台连接后才能执行写操作。"
            : "当前权限或配置来源不允许在控制台修改这条连接。"}
        </Alert>
      )}
      {connection.health.reason && connection.health.aggregate !== "ready" && (
        <Alert variant={connection.health.aggregate === "blocked" ? "danger" : "warning"} title="最近状态说明">
          {connection.health.reason}
        </Alert>
      )}
      {actionError && <Alert variant="danger" title="连接操作未完成">{actionError}</Alert>}
      {notice && <Alert variant={actionResult?.ok === false ? "warning" : "success"} title="连接操作结果">{notice}</Alert>}

      <div className="connection-health-grid" aria-label="连接分维度健康状态">
        {HEALTH_DIMENSIONS.filter(({ key }) => key !== "auth" || requiresPlatformCredential).map(({ key, label }) => (
          <HealthCell
            key={key}
            label={label}
            state={connection.health[key]}
            description={HEALTH_DESCRIPTIONS[key]}
          />
        ))}
      </div>

      {actionResult?.checks.length ? (
        <section className="connection-probe-results" aria-labelledby="connection-probe-results-title">
          <div>
            <p className="section-kicker">最近操作</p>
            <h3 id="connection-probe-results-title">服务端连接检查</h3>
          </div>
          <ul>
            {actionResult.checks.map((check) => (
              <li key={check.id}>
                <span className={`connection-state-badge is-${healthTone(check.state)}`}>{HEALTH_LABELS[check.state]}</span>
                <strong>{check.label}</strong>
                <span>{check.detail || "未返回额外说明"}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className="connection-detail-columns">
        <section aria-labelledby="connection-runtime-title">
          <div className="connection-subheading">
            <p className="section-kicker">实例状态</p>
            <h3 id="connection-runtime-title">生命周期与归属</h3>
          </div>
          <dl className="connection-definition-list">
            <div><dt>期望状态</dt><dd>{lifecycleLabel(connection.desiredState)}</dd></div>
            <div><dt>实际状态</dt><dd>{lifecycleLabel(connection.effectiveState)}</dd></div>
            <div><dt>管理来源</dt><dd>{managedByLabel(connection.managedBy)}</dd></div>
            <div><dt>优先级</dt><dd>{connection.priority}</dd></div>
          </dl>
        </section>

        <section aria-labelledby="connection-config-title">
          <div className="connection-subheading">
            <p className="section-kicker">配置摘要</p>
            <h3 id="connection-config-title">连接参数</h3>
          </div>
          <dl className="connection-definition-list">
            {configNames.map((name) => (
              <div key={name}>
                <dt>{adapter?.configFields[name]?.title || name}</dt>
                <dd className="mono">{configValue(connection.config.raw[name])}</dd>
              </div>
            ))}
            {!configNames.length && <div><dt>适配器参数</dt><dd>无需配置</dd></div>}
            {requiresPlatformCredential ? (
              <>
                <div><dt>凭据来源</dt><dd className="mono">{connection.secretRef || "未设置引用"}</dd></div>
                <div><dt>凭据状态</dt><dd>{connection.secretStatus || "尚无记录"}</dd></div>
                <div><dt>凭据指纹</dt><dd className="mono">{connection.secretFingerprint || "尚无记录"}</dd></div>
              </>
            ) : (
              <div><dt>平台凭据</dt><dd>无需配置</dd></div>
            )}
            <div><dt>最近探测</dt><dd>{formatConnectionTime(connection.health.lastProbeAt)}</dd></div>
            <div><dt>探测结果</dt><dd>{connection.lastProbeStatus || "尚无记录"}</dd></div>
            <div><dt>最近错误码</dt><dd className="mono">{connection.lastErrorCode || "无"}</dd></div>
          </dl>
        </section>
      </div>

      <div className="connection-detail-footer">
        <div className="connection-detail-lifecycle-actions">
          {stopping ? (
            <span className="connection-refresh-note" role="status">
              {hasConnectorRuntime ? "正在等待连接器停止" : "正在等待实际状态收敛"}；状态收敛后才能删除。
            </span>
          ) : disabled || /draft/i.test(connection.desiredState) ? (
            <button
              type="button"
              className="button button-primary"
              onClick={() => void onRunAction("enable", connection.id).catch(() => undefined)}
              disabled={readOnly || !etag || Boolean(actionKey)}
            >
              启用连接
            </button>
          ) : (
            <DangerAction
              label="停用连接"
              title={`确认停用 ${connection.displayName}`}
              impact={<p>该连接的期望状态将改为停用；实际状态由运行时逐步收敛，期间不应继续承担消息流量。</p>}
              confirmLabel="确认停用"
              pendingLabel="正在停用…"
              disabled={readOnly || !etag || Boolean(actionKey)}
              onConfirm={async () => {
                await onRunAction("disable", connection.id);
              }}
            />
          )}
          {!readOnly && connection.managedBy !== "environment" && (
            <DangerAction
              label="删除连接"
              title={`确认删除 ${connection.displayName}`}
              impact={(
                <dl className="connection-delete-impact">
                  <div><dt>平台</dt><dd>{platformLabel}</dd></div>
                  <div><dt>连接 ID</dt><dd className="mono">{connection.id}</dd></div>
                  <div><dt>运行影响</dt><dd>依赖这条连接 ID 的运行路径将失去配置来源</dd></div>
                </dl>
              )}
              confirmLabel="确认删除连接"
              pendingLabel="正在删除…"
              disabled={!etag || Boolean(actionKey) || deleteBlocked}
              onConfirm={() => onDelete(connection.id)}
            />
          )}
        </div>
        <TechnicalDetails
          summary="查看连接技术详情"
          label={`${connection.displayName}技术详情`}
          value={technicalSnapshot}
        />
      </div>
    </section>
  );
}

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { DangerAction } from "../components/DangerAction";
import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { StatusTile } from "../components/StatusTile";
import { TechnicalDetails, friendlyErrorMessage } from "../components/TechnicalDetails";
import { apiRequest, formatJson } from "../lib/api";
import { useStableIdempotencyKeys } from "../lib/idempotency";
import { useConsoleConfig } from "../state/console-config";

type MarketplacePermission = {
  id: string;
  level?: string;
  description?: string;
};

type MarketplaceDependency = {
  name: string;
  version?: string;
  required?: boolean;
};

type MarketplaceItem = {
  name: string;
  display_name: string;
  version: string;
  description: string;
  source: string;
  package_type: string;
  installed: boolean;
  installed_version: string;
  enabled: boolean;
  compatible: boolean;
  status: string;
  restart_required: boolean;
  permissions: MarketplacePermission[];
  dependencies: MarketplaceDependency[];
  capabilities: Record<string, string[]>;
  restart_policy: string;
  warnings: string[];
};

type MarketplaceResponse = {
  items: MarketplaceItem[];
  restart_required: boolean;
};

type PreviewResponse = {
  name: string;
  version: string;
  compatible: boolean;
  installed_version: string;
  permission_changes: {
    added: string[];
    removed: string[];
  };
  restart_required: boolean;
  permissions: MarketplacePermission[];
  dependencies: MarketplaceDependency[];
  warnings: string[];
};

type RestartInstructions = {
  actionable: boolean;
  restart_required: boolean;
  message: string;
};

type PendingPluginChange = {
  kind: "install" | "upgrade";
  pluginName: string;
  preview: PreviewResponse;
};

function marketplaceStateLabel(item: MarketplaceItem) {
  if (item.restart_required) return "等待重启";
  if (item.installed) return "已安装";
  if (item.compatible) return "可安装";
  return "不兼容";
}

export function PluginMarketplacePage() {
  const { config } = useConsoleConfig();
  const [searchParams] = useSearchParams();
  const selectedPluginName = searchParams.get("plugin")?.trim() || "";
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [data, setData] = useState<MarketplaceResponse | null>(null);
  const [restart, setRestart] = useState<RestartInstructions | null>(null);
  const [loading, setLoading] = useState(false);
  const [workingPlugin, setWorkingPlugin] = useState("");
  const [pendingChange, setPendingChange] = useState<PendingPluginChange | null>(null);
  const [error, setError] = useState("");
  const [output, setOutput] = useState(`{
  "status": "waiting"
}`);

  const installedCount = data?.items.filter((item) => item.installed).length ?? 0;
  const availableCount = data?.items.filter((item) => !item.installed && item.compatible).length ?? 0;
  const blockedCount = data?.items.filter((item) => !item.compatible || item.warnings.length).length ?? 0;
  const restartRequired = Boolean(data?.restart_required || restart?.restart_required);

  const sortedItems = useMemo(() => {
    return [...(data?.items || [])].sort((a, b) => {
      if (a.restart_required !== b.restart_required) return a.restart_required ? -1 : 1;
      if (a.installed !== b.installed) return a.installed ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
  }, [data]);

  const loadMarketplace = async () => {
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest<MarketplaceResponse>(config, "/v1/admin/plugins/marketplace", {
        auth: true,
      });
      setData(result);
      setOutput(formatJson(result));
      if (result.restart_required) {
        await loadRestartInstructions();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  };

  const loadRestartInstructions = async () => {
    const result = await apiRequest<RestartInstructions>(config, "/v1/admin/runtime/restart-instructions", {
      auth: true,
      init: { method: "POST" },
    });
    setRestart(result);
    return result;
  };

  const previewPluginChange = async (kind: PendingPluginChange["kind"], item: MarketplaceItem) => {
    setWorkingPlugin(item.name);
    setError("");
    try {
      const path = kind === "install"
        ? "/v1/admin/plugins/install/preview"
        : `/v1/admin/plugins/${item.name}/upgrade/preview`;
      const preview = await apiRequest<PreviewResponse>(config, path, {
        auth: true,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(kind === "install" ? { name: item.name } : {}),
        },
      });
      setOutput(formatJson(preview));
      setPendingChange({ kind, pluginName: item.name, preview });
    } catch (err) {
      setError(err instanceof Error ? err.message : `${kind === "install" ? "安装" : "升级"}预检失败`);
    } finally {
      setWorkingPlugin("");
    }
  };

  const applyPluginChange = async (
    kind: PendingPluginChange["kind"],
    item: MarketplaceItem,
    preview: PreviewResponse,
  ) => {
    const permissionIds = preview.permissions.map((permission) => permission.id);
    const intent = `plugin:${kind}:${item.name}:${preview.version}:${permissionIds.slice().sort().join(",")}:${preview.restart_required}`;
    setWorkingPlugin(item.name);
    setError("");
    try {
      const path = kind === "install"
        ? "/v1/admin/plugins/install"
        : `/v1/admin/plugins/${item.name}/upgrade`;
      const result = await apiRequest(config, path, {
        auth: true,
        init: {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": keyFor(intent),
          },
          body: JSON.stringify({
            ...(kind === "install" ? { name: item.name } : {}),
            confirm_permissions: permissionIds,
            confirm_restart_required: preview.restart_required,
          }),
        },
      });
      setOutput(formatJson(result));
      await loadMarketplace();
      await loadRestartInstructions();
      setPendingChange(null);
      clear(intent);
    } catch (err) {
      setError(err instanceof Error ? err.message : `${kind === "install" ? "安装" : "升级"}失败`);
      throw err;
    } finally {
      setWorkingPlugin("");
    }
  };

  const uninstallPlugin = async (item: MarketplaceItem) => {
    const intent = `plugin:uninstall:${item.name}:${item.installed_version || item.version}`;
    setWorkingPlugin(item.name);
    setError("");
    try {
      const result = await apiRequest(config, `/v1/admin/plugins/${item.name}/uninstall`, {
        auth: true,
        init: {
          method: "POST",
          headers: { "Idempotency-Key": keyFor(intent) },
        },
      });
      setOutput(formatJson(result));
      await loadMarketplace();
      await loadRestartInstructions();
      clear(intent);
    } catch (err) {
      setError(err instanceof Error ? err.message : "卸载失败");
      throw err;
    } finally {
      setWorkingPlugin("");
    }
  };

  useEffect(() => {
    void loadMarketplace();
  }, [config.apiBaseUrl, config.adminToken]);

  useEffect(() => {
    if (!selectedPluginName) {
      return;
    }
    const card = document.getElementById(`marketplace-card-${selectedPluginName}`);
    if (card && typeof card.scrollIntoView === "function") {
      card.scrollIntoView({
        block: "nearest",
        behavior: "smooth",
      });
    }
  }, [selectedPluginName, data?.items.length]);

  return (
    <div>
      <PageHeader
        eyebrow="插件市场"
        title="插件市场"
        description="从本地插件清单查看、预览和安装内置插件；安装和卸载只写入状态，重启后生效。"
        actions={
          <button className="button button-secondary" onClick={() => void loadMarketplace()} disabled={loading}>
            {loading ? "刷新中" : "刷新"}
          </button>
        }
      />

      {restartRequired && (
        <section className="marketplace-banner">
          <strong>存在待重启的插件变更</strong>
          <span>请通过部署系统滚动重启服务，完成待生效的插件变更。</span>
          <button className="button button-secondary" onClick={() => void loadRestartInstructions()}>
            查看重启指引
          </button>
          {restart?.message ? <TechnicalDetails summary="查看服务端重启说明" value={restart.message} /> : null}
        </section>
      )}

      {error && (
        <div className="form-error" role="alert">
          <p>{friendlyErrorMessage(error, "插件市场操作失败，请稍后重试。")}</p>
          <TechnicalDetails summary="查看错误技术详情" value={error} />
        </div>
      )}

      <section className="status-grid page-hero-metrics">
        <StatusTile label="已安装" value={`${installedCount}`} />
        <StatusTile label="可安装" value={`${availableCount}`} />
        <StatusTile label="风险提示" value={`${blockedCount}`} />
      </section>

      <section className="page-grid u-mt-5">
        <div className="panel span-3">
          <div className="panel-heading-row">
            <div>
              <p className="section-kicker">清单信息</p>
              <h3>本地插件清单</h3>
            </div>
          </div>

          <div className="plugin-card-grid marketplace-grid">
            {sortedItems.map((item) => {
              const busy = workingPlugin === item.name;
              const blocked = !item.compatible || item.package_type !== "builtin" || restartRequired;
              const canUpgrade = item.installed && item.installed_version && item.installed_version !== item.version;
              const preparedChange = pendingChange?.pluginName === item.name ? pendingChange : null;
              return (
                <article
                  id={`marketplace-card-${item.name}`}
                  className={`plugin-card marketplace-card ${selectedPluginName === item.name ? "is-selected" : ""}`}
                  key={item.name}
                >
                  <div className="plugin-card-header">
                    <div>
                      <strong>{item.display_name}</strong>
                       <span>目标版本 {item.version}</span>
                    </div>
                    <span className={`plugin-badge marketplace-badge${item.installed ? " installed" : ""}`}>
                       {marketplaceStateLabel(item)}
                    </span>
                  </div>
                  <p className="plugin-card-copy">{item.description || "未提供描述"}</p>
                   <dl className="plugin-meta-list">
                     <div>
                       <dt>兼容性</dt>
                       <dd>{item.compatible ? "兼容当前版本" : "不兼容当前版本"}</dd>
                     </div>
                     <div>
                       <dt>运行状态</dt>
                       <dd>{marketplaceStateLabel(item)}</dd>
                     </div>
                    <div>
                      <dt>当前版本</dt>
                      <dd>{item.installed_version || "未安装"}</dd>
                    </div>
                     <div><dt>权限声明</dt><dd>{item.permissions.length} 项</dd></div>
                     <div><dt>运行依赖</dt><dd>{item.dependencies.length} 项</dd></div>
                   </dl>

                   <TechnicalDetails
                     summary="查看插件技术清单"
                     value={{
                       name: item.name,
                       source: item.source,
                       package_type: item.package_type,
                       status: item.status,
                       restart_policy: item.restart_policy,
                       permissions: item.permissions,
                       dependencies: item.dependencies,
                       capabilities: item.capabilities,
                     }}
                   />

                  {item.warnings.length > 0 && (
                     <div className="marketplace-warning">
                       <p>此插件有 {item.warnings.length} 项风险提示，请在操作前查看详情。</p>
                       <TechnicalDetails summary="查看风险技术详情" value={item.warnings} />
                     </div>
                  )}

                  <div className="action-row">
                    {!item.installed && (
                      <button
                        className="button button-primary"
                        disabled={blocked || busy}
                        onClick={() => void previewPluginChange("install", item)}
                      >
                        {busy ? "预检中" : preparedChange?.kind === "install" ? "重新预览安装" : "预览安装"}
                      </button>
                    )}
                    {!item.installed && preparedChange?.kind === "install" && (
                      <DangerAction
                        label="确认安装"
                        title={`确认安装 ${item.display_name}`}
                        confirmLabel="确认安装"
                        pendingLabel="正在提交安装…"
                        disabled={blocked || busy || !preparedChange.preview.compatible}
                        impact={(
                          <dl>
                            <div><dt>插件</dt><dd>{item.display_name}</dd></div>
                            <div><dt>目标版本</dt><dd>{preparedChange.preview.version}</dd></div>
                            <div><dt>权限变化</dt><dd>新增 {preparedChange.preview.permission_changes.added.length} 项，移除 {preparedChange.preview.permission_changes.removed.length} 项</dd></div>
                            {preparedChange.preview.permission_changes.added.length > 0 && (
                              <div><dt>新增权限</dt><dd>{preparedChange.preview.permission_changes.added.map((permission) => <code key={permission}>{permission}</code>)}</dd></div>
                            )}
                            {preparedChange.preview.permission_changes.removed.length > 0 && (
                              <div><dt>移除权限</dt><dd>{preparedChange.preview.permission_changes.removed.map((permission) => <code key={permission}>{permission}</code>)}</dd></div>
                            )}
                            <div><dt>目标权限</dt><dd>{preparedChange.preview.permissions.length} 项</dd></div>
                            <div><dt>依赖</dt><dd>{preparedChange.preview.dependencies.length} 项</dd></div>
                            {preparedChange.preview.dependencies.length > 0 && (
                              <div><dt>依赖明细</dt><dd>{preparedChange.preview.dependencies.map((dependency) => <code key={dependency.name}>{dependency.name}</code>)}</dd></div>
                            )}
                            <div><dt>风险提示</dt><dd>{preparedChange.preview.warnings.length} 项</dd></div>
                            {preparedChange.preview.warnings.length > 0 && (
                              <div><dt>风险明细</dt><dd>{preparedChange.preview.warnings.map((warning) => <span key={warning}>{warning}</span>)}</dd></div>
                            )}
                            <div><dt>运行影响</dt><dd>{preparedChange.preview.restart_required ? "提交后需要重启服务才会生效。" : "提交后无需重启即可按服务端策略生效。"}</dd></div>
                          </dl>
                        )}
                        onConfirm={() => applyPluginChange("install", item, preparedChange.preview)}
                      />
                    )}
                    {canUpgrade && !item.restart_required && (
                      <button
                        className="button button-primary"
                        disabled={blocked || busy}
                        onClick={() => void previewPluginChange("upgrade", item)}
                      >
                        {busy ? "预检中" : preparedChange?.kind === "upgrade" ? "重新预览升级" : "预览升级"}
                      </button>
                    )}
                    {canUpgrade && !item.restart_required && preparedChange?.kind === "upgrade" && (
                      <DangerAction
                        label="确认升级"
                        title={`确认升级 ${item.display_name}`}
                        confirmLabel="确认升级"
                        pendingLabel="正在提交升级…"
                        disabled={blocked || busy || !preparedChange.preview.compatible}
                        impact={(
                          <dl>
                            <div><dt>插件</dt><dd>{item.display_name}</dd></div>
                            <div><dt>版本变化</dt><dd>{item.installed_version || "未知"} → {preparedChange.preview.version}</dd></div>
                            <div><dt>权限变化</dt><dd>新增 {preparedChange.preview.permission_changes.added.length} 项，移除 {preparedChange.preview.permission_changes.removed.length} 项</dd></div>
                            {preparedChange.preview.permission_changes.added.length > 0 && (
                              <div><dt>新增权限</dt><dd>{preparedChange.preview.permission_changes.added.map((permission) => <code key={permission}>{permission}</code>)}</dd></div>
                            )}
                            {preparedChange.preview.permission_changes.removed.length > 0 && (
                              <div><dt>移除权限</dt><dd>{preparedChange.preview.permission_changes.removed.map((permission) => <code key={permission}>{permission}</code>)}</dd></div>
                            )}
                            <div><dt>目标权限</dt><dd>{preparedChange.preview.permissions.length} 项</dd></div>
                            <div><dt>依赖</dt><dd>{preparedChange.preview.dependencies.length} 项</dd></div>
                            {preparedChange.preview.dependencies.length > 0 && (
                              <div><dt>依赖明细</dt><dd>{preparedChange.preview.dependencies.map((dependency) => <code key={dependency.name}>{dependency.name}</code>)}</dd></div>
                            )}
                            <div><dt>风险提示</dt><dd>{preparedChange.preview.warnings.length} 项</dd></div>
                            {preparedChange.preview.warnings.length > 0 && (
                              <div><dt>风险明细</dt><dd>{preparedChange.preview.warnings.map((warning) => <span key={warning}>{warning}</span>)}</dd></div>
                            )}
                            <div><dt>运行影响</dt><dd>{preparedChange.preview.restart_required ? "提交后需要重启服务才会生效。" : "提交后无需重启即可按服务端策略生效。"}</dd></div>
                          </dl>
                        )}
                        onConfirm={() => applyPluginChange("upgrade", item, preparedChange.preview)}
                      />
                    )}
                    {item.installed && !item.restart_required && (
                      <DangerAction
                        label="卸载插件"
                        title={`确认卸载 ${item.display_name}`}
                        confirmLabel="确认卸载"
                        pendingLabel="正在提交卸载…"
                        disabled={busy || item.name === "commands" || item.name === "wxbot" || restartRequired}
                        impact={(
                          <dl>
                            <div><dt>插件</dt><dd>{item.display_name}</dd></div>
                            <div><dt>当前版本</dt><dd>{item.installed_version || item.version}</dd></div>
                            <div><dt>运行影响</dt><dd>插件会被标记为已卸载，重启服务后停止提供相关能力。</dd></div>
                            <div><dt>保留内容</dt><dd>代码与现有业务数据不会被自动删除。</dd></div>
                            <div><dt>后续动作</dt><dd>提交后需要按控制台指引重启服务。</dd></div>
                          </dl>
                        )}
                        onConfirm={() => uninstallPlugin(item)}
                      />
                    )}
                  </div>
                </article>
              );
            })}
            {!sortedItems.length && <p className="plugin-card-empty">本地插件清单暂无可用条目。</p>}
          </div>
          <OutputPanel flush title="插件市场接口响应" value={output} />
        </div>
      </section>
    </div>
  );
}

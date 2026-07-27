import { Link } from "react-router-dom";

import { Alert, EmptyState } from "../../components";
import type { ChannelAdapter, ChannelConnection } from "../../lib/channel-connections";

type AdapterCatalogProps = {
  adapters: ChannelAdapter[];
  connections: ChannelConnection[];
  status: "idle" | "loading" | "refreshing" | "ready" | "error";
  error: string;
  readOnly: boolean;
  onRetry: () => void;
  onAdd: (adapterId: string) => void;
};

function adapterState(adapter: ChannelAdapter, atCapacity: boolean) {
  if (!adapter.installed) return { label: "未安装", tone: "muted" };
  if (!adapter.enabled) return { label: "已停用", tone: "warning" };
  if (!adapter.available) return { label: "暂不可用", tone: "danger" };
  if (atCapacity) return { label: "已达单实例上限", tone: "muted" };
  return { label: "可创建连接", tone: "ok" };
}

const CAPABILITY_LABELS: Record<string, string> = {
  inbound_text: "入站文本",
  outbound_text: "出站文本",
  outbound_image: "图片发送",
  group_mentions: "群提及",
  session_roster: "会话目录",
  member_sync: "成员同步",
  media_proxy: "媒体代理",
  health_probe: "健康探测",
};

function capabilityLabel(value: string) {
  return CAPABILITY_LABELS[value] || value;
}

export function AdapterCatalog({
  adapters,
  connections,
  status,
  error,
  readOnly,
  onRetry,
  onAdd,
}: AdapterCatalogProps) {
  const firstLoad = (status === "idle" || status === "loading") && !adapters.length;
  return (
    <section className="panel span-3 connection-adapter-panel" aria-labelledby="connection-adapter-title" aria-busy={firstLoad}>
      <div className="panel-header connection-section-heading">
        <div>
          <p className="section-kicker">平台适配器</p>
          <h2 id="connection-adapter-title">可接入的消息平台</h2>
          <p>插件负责提供协议适配能力；只有创建并验证连接实例，才代表租户真正接入了该平台。</p>
        </div>
        <Link className="button button-secondary" to="/plugins/marketplace">打开插件市场</Link>
      </div>

      {error && (
        <Alert variant="warning" title="平台适配器目录未完整加载">
          <span>{error}</span>{" "}
          <button type="button" className="connection-inline-action" onClick={onRetry}>重新读取</button>
        </Alert>
      )}

      {firstLoad ? (
        <div className="connection-skeleton-grid" aria-label="正在加载消息平台适配器">
          {Array.from({ length: 3 }, (_, index) => <span key={index} />)}
        </div>
      ) : adapters.length ? (
        <div className="connection-adapter-grid">
          {adapters.map((adapter) => {
            const atCapacity = !adapter.supportsMultipleConnections
              && connections.some((connection) => connection.adapterId === adapter.id);
            const state = adapterState(adapter, atCapacity);
            const canAdd = adapter.installed
              && adapter.enabled
              && adapter.available
              && !atCapacity
              && !readOnly;
            const runtimeSummary = `运行模式：${adapter.runtimeModes.join("、")}`;
            return (
              <article className="connection-adapter-card" key={adapter.id}>
                <div className="connection-adapter-heading">
                  <div>
                    <strong>{adapter.displayName}</strong>
                    <span className="mono">{adapter.id}</span>
                  </div>
                  <span className={`connection-state-badge is-${state.tone}`}>{state.label}</span>
                </div>
                <p>{adapter.description || "该适配器尚未提供产品说明。"}</p>
                <div className="connection-capability-list" aria-label={`${adapter.displayName}支持的能力`}>
                  {(adapter.capabilities.length ? adapter.capabilities : ["能力清单待同步"])
                    .slice(0, 5)
                    .map((capability) => <span key={capability}>{capabilityLabel(capability)}</span>)}
                  {adapter.capabilities.length > 5 && <span>+{adapter.capabilities.length - 5}</span>}
                </div>
                <div className="connection-adapter-footer">
                  <small>
                    {adapter.supportsMultipleConnections ? "可配置多个连接实例" : "每个租户仅支持一个连接实例"}
                    {adapter.runtimeModes.length ? `；${runtimeSummary}` : ""}
                  </small>
                  <button
                    type="button"
                    className="button button-secondary button-compact"
                    disabled={!canAdd}
                    onClick={() => onAdd(adapter.id)}
                    title={atCapacity ? "该适配器每个租户仅支持一个连接实例" : undefined}
                  >
                    添加此平台连接
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <EmptyState
          compact
          title="尚未发现消息平台适配器"
          description="安装平台适配器后，才能在这里创建实际的连接实例。"
          action={<Link className="button button-primary" to="/plugins/marketplace">查看可安装插件</Link>}
        />
      )}
    </section>
  );
}

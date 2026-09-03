import { Alert, EmptyState } from "../../components";
import type { ChannelAdapter, ChannelConnection } from "../../lib/channel-connections";
import {
  adapterDisplayName,
  connectionCategory,
  HEALTH_LABELS,
  healthTone,
  lifecycleLabel,
  managedByLabel,
  type ConnectionFilterState,
} from "./model";

type ConnectionListProps = {
  adapters: ChannelAdapter[];
  allConnections: ChannelConnection[];
  connections: ChannelConnection[];
  selectedId: string;
  adapterFilter: string;
  stateFilter: ConnectionFilterState;
  status: "idle" | "loading" | "refreshing" | "ready" | "error";
  error: string;
  readOnly: boolean;
  onAdapterFilter: (value: string) => void;
  onStateFilter: (value: ConnectionFilterState) => void;
  onSelect: (connectionId: string) => void;
  onRetry: () => void;
  onAdd: () => void;
};

const FILTER_LABELS: Record<ConnectionFilterState, string> = {
  all: "全部状态",
  healthy: "健康",
  attention: "待处理",
  draft: "草稿",
  disabled: "已停用",
};

export function ConnectionList({
  adapters,
  allConnections,
  connections,
  selectedId,
  adapterFilter,
  stateFilter,
  status,
  error,
  readOnly,
  onAdapterFilter,
  onStateFilter,
  onSelect,
  onRetry,
  onAdd,
}: ConnectionListProps) {
  const firstLoad = (status === "idle" || status === "loading") && !allConnections.length;
  return (
    <section className="panel connection-list-panel" aria-labelledby="connection-list-title" aria-busy={firstLoad || status === "refreshing"}>
      <div className="connection-list-heading">
        <div>
          <p className="section-kicker">连接实例</p>
          <h2 id="connection-list-title">已配置连接</h2>
        </div>
      </div>

      <div className="connection-list-filters" aria-label="连接筛选">
        <label className="field">
          <span>平台</span>
          <select value={adapterFilter} onChange={(event) => onAdapterFilter(event.target.value)}>
            <option value="">全部平台</option>
            {adapters.map((adapter) => <option key={adapter.id} value={adapter.id}>{adapter.displayName}</option>)}
          </select>
        </label>
        <label className="field">
          <span>状态</span>
          <select value={stateFilter} onChange={(event) => onStateFilter(event.target.value as ConnectionFilterState)}>
            {Object.entries(FILTER_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
      </div>

      {error && (
        <Alert variant="warning" title={allConnections.length ? "刷新失败，正在显示上次结果" : "连接列表加载失败"}>
          <span>{error}</span>{" "}
          <button type="button" className="connection-inline-action" onClick={onRetry}>重试</button>
        </Alert>
      )}

      {firstLoad ? (
        <div className="connection-list-skeleton" aria-label="正在加载连接列表">
          {Array.from({ length: 3 }, (_, index) => <span key={index} />)}
        </div>
      ) : !allConnections.length ? (
        <EmptyState
          compact
          title="还没有消息平台连接"
          description="先选择适配器并创建连接，再按适配器声明完成配置校验、生命周期操作和可用的主动探测。"
          action={<button type="button" className="button button-primary" onClick={onAdd} disabled={readOnly}>添加第一个连接</button>}
        />
      ) : !connections.length ? (
        <EmptyState
          compact
          title="没有符合当前筛选的连接"
          description="调整平台或状态筛选即可查看其他连接。"
          action={<button type="button" className="button button-secondary" onClick={() => { onAdapterFilter(""); onStateFilter("all"); }}>清除筛选</button>}
        />
      ) : (
        <div className="connection-instance-list" role="list" aria-label="消息平台连接实例">
          {connections.map((connection) => {
            const category = connectionCategory(connection);
            return (
              <div role="listitem" key={connection.id}>
                <button
                  type="button"
                  className={`connection-instance-card${connection.id === selectedId ? " is-selected" : ""}`}
                  aria-current={connection.id === selectedId ? "true" : undefined}
                  onClick={() => onSelect(connection.id)}
                >
                  <span className="connection-instance-topline">
                    <span>{adapterDisplayName(connection.adapterId, adapters, connection.adapterLabel)}</span>
                    <span className={`connection-state-badge is-${category === "disabled" ? "muted" : healthTone(connection.health.aggregate)}`}>
                      {category === "disabled" ? "已停用" : HEALTH_LABELS[connection.health.aggregate]}
                    </span>
                  </span>
                  <strong>{connection.displayName}</strong>
                  <span className="connection-instance-meta">
                    <span>{lifecycleLabel(connection.effectiveState)}</span>
                    <span>{managedByLabel(connection.managedBy)}</span>
                    <span>版本 {connection.version}</span>
                  </span>
                  <span className={`connection-instance-category is-${category}`}>
                    {FILTER_LABELS[category]}
                  </span>
                </button>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

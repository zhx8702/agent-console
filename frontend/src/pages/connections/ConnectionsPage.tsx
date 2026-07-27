import { useMemo, useState } from "react";

import { Alert, PageHeader } from "../../components";
import { StatusTile } from "../../components/StatusTile";
import type { ChannelConnection } from "../../lib/channel-connections";
import { AdapterCatalog } from "./AdapterCatalog";
import { ConnectionDetail } from "./ConnectionDetail";
import { ConnectionEditorDialog } from "./ConnectionEditorDialog";
import { ConnectionList } from "./ConnectionList";
import { connectionStats } from "./model";
import { useConnectionsController } from "./useConnectionsController";
export function ConnectionsPage() {
  const controller = useConnectionsController();
  const {
    actionError,
    actionKey,
    actionResult,
    adapterFilter,
    adapterQuery,
    adapters,
    connections,
    detail,
    filteredConnections,
    loadAdapters,
    loadConnections,
    loadSelected,
    notice,
    refreshAll,
    removeConnection,
    runAction,
    saveConnection,
    selectConnection,
    selectedConnectionId,
    setAdapterFilter,
    setStateFilter,
    stateFilter,
  } = controller;
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorConnection, setEditorConnection] = useState<ChannelConnection | null>(null);
  const [initialAdapterId, setInitialAdapterId] = useState("");
  const stats = useMemo(() => connectionStats(connections.items), [connections.items]);
  const refreshing = adapters.status === "refreshing" || connections.status === "refreshing";
  const globallyReadOnly = adapters.readOnly || connections.readOnly;

  const openCreate = (adapterId = adapterQuery) => {
    setEditorConnection(null);
    setInitialAdapterId(adapterId);
    setEditorOpen(true);
  };

  const openEdit = () => {
    if (!detail.value) return;
    setEditorConnection(detail.value);
    setInitialAdapterId(detail.value.adapterId);
    setEditorOpen(true);
  };

  const closeEditor = () => {
    if (actionKey.startsWith("create:") || actionKey.startsWith("update:")) return;
    setEditorOpen(false);
  };

  return (
    <div className="page-grid connections-page">
      <section className="panel panel-hero span-3 connections-hero">
        <div className="connections-hero-heading">
          <PageHeader
            eyebrow="消息接入"
            title="消息平台连接中心"
            description="统一管理平台适配器、租户连接实例，以及每条连接的配置校验、生命周期和主动探测状态。"
          />
          <div className="action-row connections-hero-actions">
            <button type="button" className="button button-primary" onClick={() => openCreate()} disabled={globallyReadOnly}>
              添加连接
            </button>
            <button type="button" className="button button-secondary" onClick={() => void refreshAll()} disabled={refreshing}>
              {refreshing ? "刷新中…" : "刷新全部状态"}
            </button>
          </div>
        </div>

        <ol className="connection-domain-model" aria-label="消息平台连接的四个管理环节">
          <li>
            <span>01</span>
            <div><strong>消息平台</strong><small>微信、飞信等协议和能力类型</small></div>
          </li>
          <li>
            <span>02</span>
            <div><strong>连接实例</strong><small>租户真正配置并验证的账号或 SDK</small></div>
          </li>
          <li>
            <span>03</span>
            <div><strong>配置校验</strong><small>按适配器声明校验参数与凭据引用</small></div>
          </li>
          <li>
            <span>04</span>
            <div><strong>主动探测</strong><small>仅在适配器支持时验证真实连接能力</small></div>
          </li>
        </ol>
        <p className="connection-model-note">
          <strong>边界说明：</strong>插件声明适配能力；创建并校验配置、按需启用，再完成适配器支持的主动探测后，才算真正接入。
        </p>

        <div className="status-grid connections-status-grid" aria-label="连接状态摘要">
          <StatusTile label="健康连接" value={String(stats.healthy)} />
          <StatusTile label="待处理" value={String(stats.attention)} />
          <StatusTile label="配置草稿" value={String(stats.draft)} />
          <StatusTile label="已停用" value={String(stats.disabled)} />
          <StatusTile label="平台适配器" value={String(adapters.items.length)} />
        </div>
      </section>

      {globallyReadOnly && (
        <div className="span-3">
          <Alert variant="info" title="当前连接中心为只读模式">
            你可以查看平台、连接生命周期与最近探测结果，但当前身份不能创建、编辑、启停或删除连接。
          </Alert>
        </div>
      )}

      <AdapterCatalog
        adapters={adapters.items}
        connections={connections.items}
        status={adapters.status}
        error={adapters.error}
        readOnly={globallyReadOnly}
        onRetry={() => void loadAdapters(true).catch(() => undefined)}
        onAdd={openCreate}
      />

      <div className="span-3 connection-center-layout">
        <ConnectionList
          adapters={adapters.items}
          allConnections={connections.items}
          connections={filteredConnections}
          selectedId={selectedConnectionId}
          adapterFilter={adapterFilter}
          stateFilter={stateFilter}
          status={connections.status}
          error={connections.error}
          readOnly={globallyReadOnly}
          onAdapterFilter={setAdapterFilter}
          onStateFilter={setStateFilter}
          onSelect={selectConnection}
          onRetry={() => void loadConnections(true).catch(() => undefined)}
          onAdd={() => openCreate()}
        />

        <ConnectionDetail
          adapters={adapters.items}
          connection={detail.value}
          status={detail.status}
          error={detail.error}
          etag={detail.etag}
          actionKey={actionKey}
          actionError={actionError}
          notice={notice}
          actionResult={actionResult}
          collectionReadOnly={globallyReadOnly}
          onEdit={openEdit}
          onReload={() => void loadSelected().catch(() => undefined)}
          onRunAction={runAction}
          onDelete={removeConnection}
        />
      </div>

      <ConnectionEditorDialog
        open={editorOpen}
        adapters={adapters.items}
        connection={editorConnection}
        connectionAdapterIds={connections.items.map((item) => item.adapterId)}
        initialAdapterId={initialAdapterId}
        busy={actionKey.startsWith("create:") || actionKey.startsWith("update:")}
        conflict={detail.status === "conflict" && Boolean(editorConnection)}
        onClose={closeEditor}
        onSave={saveConnection}
        onReloadAfterConflict={async () => {
          await loadSelected().catch(() => undefined);
          setEditorOpen(false);
        }}
      />
    </div>
  );
}

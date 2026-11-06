import { useStableIdempotencyKeys } from "../lib/idempotency";
import { Alert } from "../components/Alert";
import { useConsoleConfig } from "../state/console-config";
import { DrawConfigSection } from "./plugins/DrawConfigSection";
import { FlowRuntimeSection } from "./plugins/FlowRuntimeSection";
import { GroupPluginScopeSection } from "./plugins/GroupPluginScopeSection";
import { InstalledPluginsSection } from "./plugins/InstalledPluginsSection";
import { PluginDiagnosticsSections } from "./plugins/PluginDiagnosticsSections";
import { PluginOverviewSection } from "./plugins/PluginOverviewSection";
import { usePluginsPageController } from "./plugins/usePluginsPageController";

export function PluginsPage() {
  const { config } = useConsoleConfig();
  const idempotencyKeys = useStableIdempotencyKeys();
  // A new authority/scope gets a fresh state tree, including open confirmation
  // dialogs and unsaved secrets. Old promises cannot target the new page.
  const scope = JSON.stringify([config.apiBaseUrl, config.adminToken, config.tenantId, config.sessionId]);
  return <ScopedPluginsPage key={scope} idempotencyKeys={idempotencyKeys} />;
}

function ScopedPluginsPage({ idempotencyKeys }: { idempotencyKeys: ReturnType<typeof useStableIdempotencyKeys> }) {
  const page = usePluginsPageController(idempotencyKeys);

  return (
    <div className="page-grid plugins-page">
      <PluginOverviewSection
        data={page.data}
        pluginCards={page.pluginCards}
        runtime={page.runtime}
        loading={page.loading}
        canRefresh={page.canManage}
        onRefresh={() => void page.refreshSummary()}
      />

      {page.error && <Alert variant="danger" title="插件状态">{page.error}</Alert>}

      <FlowRuntimeSection
        flowStatus={page.flowStatus}
        readyzFlow={page.readyzFlow}
        effectLog={page.effectLog}
        effectSummary={page.effectSummary}
        effectTraceFilter={page.effectTraceFilter}
        effectAuditFilters={page.effectAuditFilters}
        traceAggregate={page.traceAggregate}
        traceAggregateLoading={page.traceAggregateLoading}
        traceAggregateError={page.traceAggregateError}
        flowLoading={page.flowLoading}
        flowError={page.flowError}
        onRefresh={() => void page.refreshFlowRuntime()}
        onSelectTrace={page.selectEffectTrace}
        onClearTraceFilter={page.clearEffectTraceFilter}
        onSelectAuditFilters={page.selectEffectAuditFilters}
        onClearAuditFilters={page.clearEffectAuditFilters}
        onClearAllFilters={page.clearAllEffectFilters}
      />

      <DrawConfigSection
        config={page.drawConfig}
        apiUrl={page.drawApiUrl}
        apiKey={page.drawApiKey}
        apiModel={page.drawApiModel}
        apiEditUrl={page.drawApiEditUrl}
        loading={page.drawLoading}
        saving={page.drawSaving}
        error={page.drawError}
        notice={page.drawNotice}
        canManage={page.canManage}
        onApiUrlChange={page.setDrawApiUrl}
        onApiKeyChange={page.setDrawApiKey}
        onApiModelChange={page.setDrawApiModel}
        onApiEditUrlChange={page.setDrawApiEditUrl}
        onSave={page.saveDrawConfig}
        onDisable={page.disableDrawConfig}
      />

      <InstalledPluginsSection
        runtimeErrors={page.runtimeErrors}
        pluginCards={page.pluginCards}
        runtime={page.runtime}
        selectedPluginName={page.selectedPluginName}
        restartRequired={page.restartRequired}
        canManage={page.canManage}
        onSetPluginEnabled={page.setPluginEnabled}
      />

      <GroupPluginScopeSection
        scopeReady={page.scopeReady}
        groups={page.groups}
        groupPlugins={page.groupScopedPlugins}
        managedGroupSessionId={page.managedGroupSessionId}
        groupPluginState={page.groupPluginState}
        runtime={page.runtime}
        onSelectGroup={page.selectGroup}
        onSetGroupPluginEnabled={page.setGroupPluginEnabled}
      />

      <PluginDiagnosticsSections
        data={page.data}
        pluginEvents={page.pluginEvents}
        output={page.output}
        groupOutput={page.groupOutput}
      />
    </div>
  );
}

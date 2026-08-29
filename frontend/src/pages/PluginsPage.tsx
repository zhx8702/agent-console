import { FlowRuntimeSection } from "./plugins/FlowRuntimeSection";
import { GroupPluginScopeSection } from "./plugins/GroupPluginScopeSection";
import { InstalledPluginsSection } from "./plugins/InstalledPluginsSection";
import { PluginDiagnosticsSections } from "./plugins/PluginDiagnosticsSections";
import { PluginOverviewSection } from "./plugins/PluginOverviewSection";
import { usePluginsPageController } from "./plugins/usePluginsPageController";

export function PluginsPage() {
  const page = usePluginsPageController();

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

      <InstalledPluginsSection
        pluginCards={page.pluginCards}
        runtime={page.runtime}
        selectedPluginName={page.selectedPluginName}
        restartRequired={page.restartRequired}
        canManage={page.canManage}
        onSetPluginEnabled={page.setPluginEnabled}
      />

      <GroupPluginScopeSection
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

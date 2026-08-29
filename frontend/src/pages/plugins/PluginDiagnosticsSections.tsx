import { OutputPanel } from "../../components/OutputPanel";
import type { PluginEvent, PluginSummary } from "./models";

type PluginDiagnosticsSectionsProps = {
  data: PluginSummary | null;
  pluginEvents: PluginEvent[];
  output: string;
  groupOutput: string;
};

export function PluginDiagnosticsSections({
  data,
  pluginEvents,
  output,
  groupOutput,
}: PluginDiagnosticsSectionsProps) {
  const channels = data?.channels ?? [];
  const channelLabels = data?.channel_labels ?? {};
  const channelAdapters = data?.channel_adapters ?? [];
  const pluginRoutes = data?.plugin_routes ?? [];

  return (
    <>
      <section className="panel panel-scroll plugins-compact-panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">扩展钩子</p>
            <h3>插件 Hook</h3>
          </div>
        </div>
        <ul className="route-list">
          {Object.entries(data?.hooks || {}).map(([point, hooks]) => (
            <li key={point}>{point}: {hooks.join(", ")}</li>
          ))}
          {!Object.keys(data?.hooks || {}).length && <li>暂无 Hook 摘要</li>}
        </ul>
      </section>

      <section className="panel panel-scroll plugins-compact-panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">审计记录</p>
            <h3>最近插件事件</h3>
          </div>
        </div>
        <ul className="route-list">
          {pluginEvents.map((event) => (
            <li key={event.id}>
              {event.plugin_name}: {event.event_type} [{event.status}] {event.created_at}
            </li>
          ))}
          {!pluginEvents.length && <li>暂无插件事件</li>}
        </ul>
      </section>

      <section className="panel panel-scroll plugins-compact-panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">适配器声明</p>
            <h3>可用消息平台类型</h3>
          </div>
        </div>
        <ul className="route-list">
          {channelAdapters.map((adapter) => (
            <li key={adapter.adapter_id}>
              {adapter.adapter_id} · {adapter.display_name} ({adapter.channel})
            </li>
          ))}
          {!channelAdapters.length && channels.map((channel) => (
            <li key={channel}>
              {channel}
              {channelLabels[channel] ? ` (${channelLabels[channel]})` : ""}
            </li>
          ))}
          {!channelAdapters.length && !channels.length && <li>暂无适配器声明</li>}
        </ul>
      </section>

      <section className="panel panel-scroll plugins-compact-panel plugins-routes-panel span-2">
        <div className="panel-header">
          <div>
            <p className="section-kicker">接口路由</p>
            <h3>插件路由</h3>
          </div>
        </div>
        <ul className="route-list">
          {pluginRoutes.map((path) => (
            <li key={path}>{path}</li>
          ))}
          {!pluginRoutes.length && <li>未发现插件路由</li>}
        </ul>
      </section>

      <div className="plugins-output-panel span-3">
        <OutputPanel flush title="插件摘要响应" value={output} />
      </div>
      <div className="plugins-output-panel span-3">
        <OutputPanel flush title="群级插件控制响应" value={groupOutput} />
      </div>
    </>
  );
}

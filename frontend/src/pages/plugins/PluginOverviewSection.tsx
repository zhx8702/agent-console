import { Link } from "react-router-dom";

import { PageHeader } from "../../components/PageHeader";
import { StatusTile } from "../../components/StatusTile";
import type { InstalledPlugin, PluginRuntime, PluginSummary } from "./models";
import { pluginEnablementLabel } from "./models";

type PluginOverviewSectionProps = {
  data: PluginSummary | null;
  pluginCards: InstalledPlugin[];
  runtime: PluginRuntime;
  loading: boolean;
  canRefresh: boolean;
  onRefresh: () => void;
};

const OVERVIEW_PLUGINS: Array<[string, string]> = [
  ["命令中心", "commands"],
  ["高德地图", "amap"],
  ["积分运营", "credits"],
  ["成员记忆", "memory"],
  ["内容审核", "moderation"],
  ["微信适配器", "wxbot"],
  ["回复风格", "persona_extract"],
  ["复读策略", "repeater"],
  ["Tibo 重置", "tibo_reset"],
];

export function PluginOverviewSection({
  data,
  pluginCards,
  runtime,
  loading,
  canRefresh,
  onRefresh,
}: PluginOverviewSectionProps) {
  const loadSummary = onRefresh;
  const pluginStatus = (name: string) => (
    pluginEnablementLabel(
      pluginCards.find((item) => item.name === name),
      runtime,
    )
  );

  return (
      <section className="panel panel-hero plugins-hero span-3">
        <PageHeader
          eyebrow="插件管理"
          title="插件管理总览"
          description="核对插件挂载、Hook 注入与消息平台适配器声明。适配器已加载不代表已经建立平台连接。"
          actions={
            <button className="button button-primary" onClick={() => void loadSummary()} disabled={!canRefresh || loading}>
              {loading ? "刷新中..." : "刷新插件摘要"}
            </button>
          }
        />
        <div className="status-grid plugins-overview-grid">
          <StatusTile label="插件" value={`${pluginCards.length}`} />
          <StatusTile label="路由" value={`${(data?.plugin_routes ?? []).length}`} />
          <StatusTile label="钩子" value={`${Object.keys(data?.hooks || {}).length}`} />
          <StatusTile label="适配器声明" value={`${(data?.channels ?? []).length}`} />
        </div>
        <ul className="plugin-enablement-list" aria-label="插件启用状态">
          {OVERVIEW_PLUGINS.map(([label, name]) => {
            const status = pluginStatus(name);
            return (
              <li key={name} data-enabled={status === "已启用" ? "true" : "false"}>
                <Link to={`/plugins?plugin=${encodeURIComponent(name)}`}>
                  <span>{label}</span>
                  <strong>{status}</strong>
                </Link>
              </li>
            );
          })}
        </ul>
      </section>
  );
}

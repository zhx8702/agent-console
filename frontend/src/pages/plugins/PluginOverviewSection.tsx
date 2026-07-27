import { PageHeader } from "../../components/PageHeader";
import { StatusTile } from "../../components/StatusTile";
import type { InstalledPlugin, PluginSummary } from "./models";

type PluginOverviewSectionProps = {
  data: PluginSummary | null;
  pluginCards: InstalledPlugin[];
  loading: boolean;
  canRefresh: boolean;
  onRefresh: () => void;
};

export function PluginOverviewSection({
  data,
  pluginCards,
  loading,
  canRefresh,
  onRefresh,
}: PluginOverviewSectionProps) {
  const loadSummary = onRefresh;
  const pluginStatus = (name: string) => (
    pluginCards.some((item) => item.name === name) ? "已启用" : "未加载"
  );

  return (
      <section className="panel panel-hero plugins-hero span-3">
        <PageHeader
          eyebrow="插件管理"
          title="插件管理总览"
          description="核对插件挂载、Hook 注入与消息平台适配器声明。适配器已加载不代表已经建立平台连接。"
        />
        <div className="action-row">
          <button className="button button-primary" onClick={() => void loadSummary()} disabled={!canRefresh || loading}>
            {loading ? "刷新中..." : "刷新插件摘要"}
          </button>
        </div>
        <div className="status-grid plugins-overview-grid">
          <StatusTile label="插件" value={`${pluginCards.length}`} />
          <StatusTile label="路由" value={`${(data?.plugin_routes ?? []).length}`} />
          <StatusTile label="钩子" value={`${Object.keys(data?.hooks || {}).length}`} />
          <StatusTile label="适配器声明" value={`${(data?.channels ?? []).length}`} />
          <StatusTile label="命令中心" value={pluginStatus("commands")} />
          <StatusTile label="高德地图" value={pluginStatus("amap")} />
          <StatusTile label="积分运营" value={pluginStatus("credits")} />
          <StatusTile label="成员记忆" value={pluginStatus("memory")} />
          <StatusTile label="内容审核" value={pluginStatus("moderation")} />
          <StatusTile label="微信适配器" value={pluginStatus("wxbot")} />
          <StatusTile label="回复风格" value={pluginStatus("persona_extract")} />
          <StatusTile label="复读策略" value={pluginStatus("repeater")} />
          <StatusTile label="Tibo 重置" value={pluginStatus("tibo_reset")} />
        </div>
      </section>
  );
}

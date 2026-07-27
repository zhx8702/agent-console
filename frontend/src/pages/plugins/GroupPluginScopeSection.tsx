import { DangerAction } from "../../components/DangerAction";
import type { GroupPluginState, InstalledPlugin, PluginRuntime, WxbotSession } from "./models";

type GroupPluginScopeSectionProps = {
  groups: WxbotSession[];
  groupPlugins: InstalledPlugin[];
  managedGroupSessionId: string;
  groupPluginState: GroupPluginState;
  runtime: PluginRuntime;
  onSelectGroup: (sessionId: string) => void;
  onSetGroupPluginEnabled: (
    pluginName: string,
    enabled: boolean,
  ) => Promise<void>;
};

export function GroupPluginScopeSection({
  groups,
  groupPlugins,
  managedGroupSessionId,
  groupPluginState,
  runtime,
  onSelectGroup,
  onSetGroupPluginEnabled,
}: GroupPluginScopeSectionProps) {
  const setGroupPluginEnabled = onSetGroupPluginEnabled;
  const tiboStats = runtime.tibo_reset?.stats;
  const groupSelected = Boolean(managedGroupSessionId);
  const selectedGroupName = groups.find(
    (item) => item.session_id === managedGroupSessionId,
  )?.session_name || managedGroupSessionId;
  const scopeAction = (
    pluginName: string,
    label: string,
    enabled: boolean,
  ) => (
    <DangerAction
      label={enabled ? "关闭" : "开启"}
      title={`${enabled ? "关闭" : "开启"}${label}`}
      impact={(
        <p>
          目标群：{selectedGroupName || "尚未选择"}。提交后只改变该群的插件状态，不影响其他群。
        </p>
      )}
      confirmLabel={enabled ? "确认关闭" : "确认开启"}
      disabled={!groupSelected}
      onConfirm={() => setGroupPluginEnabled(pluginName, !enabled)}
    />
  );
  return (
    <section className="panel panel-scroll plugins-scope-panel span-3">
      <div className="panel-header">
        <div>
          <p className="section-kicker">群范围</p>
          <h3>群级插件开关</h3>
        </div>
      </div>
      <div className="form-grid">
        <label className="field span-2">
          <span>目标群</span>
          <select
            value={managedGroupSessionId}
            onChange={(event) => {
              const nextSessionId = event.target.value;
              onSelectGroup(nextSessionId.trim());
            }}
          >
            <option value="">请选择群会话</option>
            {groups.map((item) => (
              <option key={item.session_id} value={item.session_id}>
                {item.session_name || item.session_id}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="muted-copy">不开就不启用。这里的开关按群单独保存，不同群互不影响。</p>
      <div className="plugin-card-grid">
        {groupPlugins.map((plugin) => {
          const enabled = Boolean(groupPluginState[plugin.name]);
          const label = plugin.admin_ui?.label || plugin.name;
          const summary = plugin.admin_ui?.summary || "按群独立控制此插件。";
          return (
            <article key={plugin.name} className={`plugin-card plugins-scope-card ${enabled ? "is-enabled" : "is-disabled"}`}>
              <div className="plugin-card-header">
                <div>
                  <strong>{plugin.name}</strong>
                  <span>{label}</span>
                </div>
                <span className={`plugin-badge ${enabled ? "" : "is-muted"}`}>{enabled ? "on" : "off"}</span>
              </div>
              <p className="muted-copy">{summary}</p>
              {plugin.name === "tibo_reset" && tiboStats && (
                <p className="muted-copy">
                  本周 {tiboStats.week_count ?? 0} 次（面向所有用户 {tiboStats.week_everyone_count ?? 0} 次）
                  · 今天{tiboStats.today_has_reset ? `有 ${tiboStats.today_count ?? 0} 次` : "暂无"}
                  · 历史保留 {tiboStats.history_count ?? 0} 条
                </p>
              )}
              <div className="action-row plugin-card-actions">
                {scopeAction(plugin.name, label, enabled)}
              </div>
            </article>
          );
        })}
        {!groupPlugins.length && <div className="plugin-card plugin-card-empty">当前没有声明群级控制面的插件。</div>}
        {!groups.length && <div className="plugin-card plugin-card-empty">暂无可管理群会话，需要先连接 wx-bot 群列表。</div>}
      </div>
    </section>
  );
}

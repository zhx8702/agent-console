import { Link } from "react-router-dom";

import { DangerAction } from "../../components/DangerAction";
import type { InstalledPlugin, PluginRuntime } from "./models";
import { PLUGIN_LINKS } from "./models";

type InstalledPluginsSectionProps = {
  pluginCards: InstalledPlugin[];
  runtime: PluginRuntime;
  canManage: boolean;
  onSetPluginEnabled: (pluginName: string, enabled: boolean) => Promise<void>;
};

export function InstalledPluginsSection({
  pluginCards,
  runtime,
  canManage,
  onSetPluginEnabled,
}: InstalledPluginsSectionProps) {
  const setPluginEnabled = onSetPluginEnabled;
  return (
      <section className="panel panel-scroll plugins-loaded-panel span-3">
        <div className="panel-header">
          <div>
            <p className="section-kicker">已加载</p>
            <h3>已加载插件</h3>
          </div>
        </div>
        <div className="plugin-card-grid">
          {pluginCards.map((plugin) => (
            <article
              key={plugin.name}
              className={`plugin-card plugin-plugin-card ${plugin.enabled ? "is-enabled" : "is-disabled"} ${plugin.restart_required ? "is-restart-required" : ""} ${plugin.last_error ? "has-error" : ""}`}
            >
              <div className="plugin-card-header">
                <div>
                  <strong>{plugin.name}</strong>
                  <span>v{plugin.version}</span>
                </div>
                <span className={`plugin-badge ${plugin.restart_required ? "is-warning" : plugin.last_error ? "is-danger" : plugin.enabled ? "" : "is-muted"}`}>
                  {plugin.restart_required
                    ? "待重启"
                    : plugin.last_error
                      ? "异常"
                      : plugin.enabled
                        ? "已启用"
                        : "已停用"}
                </span>
              </div>
              <p className="plugin-card-copy">{plugin.description || "无描述"}</p>
              <dl className="plugin-meta-list">
                <div><dt>全局状态</dt><dd>{plugin.enabled ? "已启用" : "已停用"}</dd></div>
                <div><dt>类型</dt><dd>{plugin.system ? "系统插件" : "普通插件"}</dd></div>
                <div><dt>权限</dt><dd>{plugin.permissions?.length ?? 0}</dd></div>
                <div><dt>重启</dt><dd>{plugin.restart_required ? "需要" : "不需要"}</dd></div>
              </dl>
              {plugin.last_error && (
                <details className="technical-details">
                  <summary>技术详情：最近错误</summary>
                  <p className="muted-copy">{plugin.last_error}</p>
                </details>
              )}

              {plugin.name === "amap" && (
                <dl className="plugin-meta-list">
                  <div><dt>接口密钥</dt><dd>{runtime.amap?.api_key_configured ? "已配置" : "缺失"}</dd></div>
                  <div><dt>作用范围</dt><dd>{runtime.amap?.agent_scope || "群个人地图"}</dd></div>
                  <div><dt>工具数</dt><dd>{runtime.amap?.tools?.length ?? 0}</dd></div>
                  <div><dt>二维码目录</dt><dd>{runtime.amap?.storage_dir_writable ? "可写" : "需检查"}</dd></div>
                </dl>
              )}

              {plugin.name === "commands" && (
                <dl className="plugin-meta-list">
                  <div><dt>管理员</dt><dd>{runtime.commands?.admins ?? 0}</dd></div>
                  <div><dt>普通命令</dt><dd>{runtime.commands?.user_commands ?? 0}</dd></div>
                  <div><dt>管理员命令</dt><dd>{runtime.commands?.admin_commands ?? 0}</dd></div>
                </dl>
              )}

              {plugin.name === "credits" && (
                <dl className="plugin-meta-list">
                  <div><dt>启用</dt><dd>{runtime.credits?.enabled ? "是" : "否"}</dd></div>
                  <div><dt>积分名</dt><dd>{runtime.credits?.credit_name || "-"}</dd></div>
                  <div><dt>单次扣费</dt><dd>{runtime.credits?.cost_per_chat ?? "-"}</dd></div>
                </dl>
              )}

              {plugin.name === "moderation" && (
                <dl className="plugin-meta-list">
                  <div><dt>启用</dt><dd>{runtime.moderation?.enabled ? "是" : "否"}</dd></div>
                  <div><dt>提醒模式</dt><dd>{runtime.moderation?.reminder_mode || "-"}</dd></div>
                  <div><dt>事件通知</dt><dd>{runtime.moderation?.webhook_enabled ? "开启" : "关闭"}</dd></div>
                </dl>
              )}

              {plugin.name === "memory" && (
                <dl className="plugin-meta-list">
                  <div><dt>画像</dt><dd>{runtime.memory?.profiles ?? 0}</dd></div>
                  <div><dt>事件</dt><dd>{runtime.memory?.events ?? 0}</dd></div>
                  <div><dt>匹配维度</dt><dd>identity(user) + session(session + user)</dd></div>
                </dl>
              )}

              {plugin.name === "persona_extract" && (
                <dl className="plugin-meta-list">
                  <div><dt>人物画像</dt><dd>{runtime.persona_extract?.profiles ?? 0}</dd></div>
                  <div><dt>任务</dt><dd>{runtime.persona_extract?.jobs ?? 0}</dd></div>
                  <div><dt>匹配维度</dt><dd>channel + source</dd></div>
                </dl>
              )}

              {plugin.name === "wxbot" && (
                <dl className="plugin-meta-list">
                  <div><dt>桥接</dt><dd>{runtime.wxbot?.running ? "运行中" : "未运行"}</dd></div>
                  <div><dt>微信连接</dt><dd>{runtime.wxbot?.sdk_online ? "在线" : "离线"}</dd></div>
                  <div><dt>待发送</dt><dd>{runtime.wxbot?.pending ?? 0}</dd></div>
                  <div><dt>会话</dt><dd>{runtime.wxbot?.sessions ?? 0}</dd></div>
                </dl>
              )}

              {plugin.name === "repeater" && (
                <dl className="plugin-meta-list">
                  <div><dt>启用</dt><dd>{runtime.repeater?.enabled ? "是" : "否"}</dd></div>
                  <div><dt>冷却时间</dt><dd>{runtime.repeater?.cooldown_seconds ?? 300} 秒</dd></div>
                </dl>
              )}

              <div className="action-row plugin-card-actions">
                {PLUGIN_LINKS[plugin.name] && (
                  <Link className="button button-secondary" to={PLUGIN_LINKS[plugin.name]}>
                    打开插件页
                  </Link>
                )}
                <DangerAction
                  label={plugin.enabled ? "全局停用" : "全局启用"}
                  title={`${plugin.enabled ? "停用" : "启用"}插件 ${plugin.name}`}
                  impact={(
                    <p>
                      这会改变该插件在全部允许范围内的运行状态；若插件注册了路由或能力，可能还需要重启服务。
                    </p>
                  )}
                  confirmLabel={plugin.enabled ? "确认停用" : "确认启用"}
                  disabled={plugin.system || !canManage}
                  onConfirm={() => setPluginEnabled(plugin.name, !plugin.enabled)}
                />
              </div>
            </article>
          ))}
          {!pluginCards.length && <div className="plugin-card plugin-card-empty">暂无已安装插件</div>}
        </div>
      </section>
  );
}

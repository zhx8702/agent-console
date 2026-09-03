import { DangerAction } from "../../components/DangerAction";
import { OutputPanel } from "../../components/OutputPanel";
import { formatJson } from "../../lib/api";
import type { WxbotPageController } from "./useWxbotPageController";
import { formatEventTime, wxbotBooleanLabel, wxbotEventTypeLabel } from "./model";

export function WxbotEventsTab({ controller }: { controller: WxbotPageController }) {
  const {
    config,
    chooseVerifiedGroup,
    deleteSubscription,
    eventLimit,
    eventOutput,
    eventSessionFilter,
    eventTypeFilter,
    filteredEvents,
    groupOutput,
    groupSettingsDirty,
    groupSettingsEtag,
    groupSettingsStatus,
    groupSessionId,
    groupSessions,
    loadGroupSettings,
    loadMemberEvents,
    loadSubscriptions,
    saveGroupSettings,
    saveSubscription,
    setEventLimit,
    setEventSessionFilter,
    setEventTypeFilter,
    setSubscriptionEnabled,
    setSubscriptionEventType,
    setSubscriptionId,
    setSubscriptionSessionId,
    setSubscriptionTargetUrl,
    setWelcomeEnabled,
    setWelcomeMention,
    setWelcomeTemplate,
    subscriptionEnabled,
    subscriptionEventType,
    subscriptionId,
    subscriptionSessionId,
    subscriptionTargetUrl,
    subscriptions,
    subscriptionsEtag,
    subscriptionsStatus,
    welcomeEnabled,
    welcomeMention,
    welcomeTemplate,
  } = controller;

  return (
        <>
          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">成员事件 Webhook</p>
                <h3>入群 / 退群 webhook</h3>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>事件类型</span>
                <select value={subscriptionEventType} onChange={(event) => setSubscriptionEventType(event.target.value)}>
                  <option value="group.member.joined">{wxbotEventTypeLabel("group.member.joined")}</option>
                  <option value="group.member.left">{wxbotEventTypeLabel("group.member.left")}</option>
                </select>
              </label>
              <label className="field">
                <span>目标群聊</span>
                <select value={subscriptionSessionId} onChange={(event) => setSubscriptionSessionId(event.target.value)}>
                  <option value="">请选择授权群</option>
                  {groupSessions.map((item) => (
                    <option key={item.session_id} value={item.session_id}>
                      {item.session_name || item.session_id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field span-2">
                <span>Webhook 地址</span>
                <input
                  value={subscriptionTargetUrl}
                  onChange={(event) => setSubscriptionTargetUrl(event.target.value)}
                  placeholder="https://agent-console.example.com/webhooks/wxbot-member-events"
                />
              </label>
              <label className="field">
                <span>当前订阅记录</span>
                <input value={subscriptionId || "新建订阅"} readOnly />
              </label>
              <label className="field">
                <span>是否启用</span>
                <select value={subscriptionEnabled} onChange={(event) => setSubscriptionEnabled(event.target.value)}>
                  <option value="true">启用</option>
                  <option value="false">停用</option>
                </select>
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadSubscriptions()} disabled={!config.adminToken}>
                读取订阅
              </button>
              <DangerAction
                label="保存订阅"
                title="确认保存成员事件 Webhook"
                impact={<p>将对已验证群 {subscriptionSessionId || "-"} 启用或更新事件外发；目标地址只显示在本次草稿中。</p>}
                confirmLabel="确认保存"
                pendingLabel="正在保存…"
                disabled={!config.adminToken || !subscriptionSessionId || !subscriptionsEtag || subscriptionsStatus === "saving"}
                onConfirm={saveSubscription}
              />
              <DangerAction
                label="删除订阅"
                title="删除成员事件 Webhook 订阅"
                impact={<p>将停止当前订阅的后续事件推送，不会删除已经产生的成员事件。</p>}
                confirmLabel="确认删除"
                pendingLabel="正在删除…"
                disabled={!config.adminToken || !subscriptionId.trim()}
                onConfirm={deleteSubscription}
              />
            </div>
            <p className="form-hint">
              每条订阅必须绑定一个已授权群；全局事件出口应由平台管理员在独立运维流程中配置。
            </p>
            <div className="table-scroll compact-table-scroll">
              <table>
                <caption className="sr-only">Webhook 事件订阅</caption>
                <thead>
                  <tr>
                    <th scope="col">ID</th>
                    <th scope="col">事件</th>
                    <th scope="col">群</th>
                    <th scope="col">URL</th>
                    <th scope="col">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {subscriptions.map((item) => (
                    <tr
                      key={item.id}
                    >
                      <td className="mono">
                        <button
                          type="button"
                          className="data-table-row-action mono"
                          aria-pressed={String(item.id) === subscriptionId}
                          onClick={() => {
                            setSubscriptionId(String(item.id || ""));
                            setSubscriptionEventType(item.event_type || "group.member.joined");
                            setSubscriptionTargetUrl(item.target_url || "");
                            setSubscriptionSessionId(item.session_id || "");
                            setSubscriptionEnabled(String(Boolean(item.enabled)));
                          }}
                        >
                          {item.id}
                        </button>
                      </td>
                      <td>{wxbotEventTypeLabel(item.event_type)}</td>
                      <td className="mono">{item.session_id || "全部群"}</td>
                      <td className="mono">{item.target_url}</td>
                      <td>{wxbotBooleanLabel(Boolean(item.enabled), "已启用", "已停用")}</td>
                    </tr>
                  ))}
                  {!subscriptions.length && (
                    <tr>
                      <td colSpan={5}>当前条件下还没有 webhook 订阅</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">欢迎语</p>
                <h3>群欢迎语设置</h3>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>目标群聊</span>
                <select
                  value={groupSessionId}
                  onChange={(event) => {
                    const nextSessionId = event.target.value;
                    chooseVerifiedGroup(nextSessionId);
                  }}
                >
                  <option value="">请选择群</option>
                  {groupSessions.map((item) => (
                    <option key={item.session_id} value={item.session_id}>
                      {item.session_name || item.session_id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>启用欢迎语</span>
                <select value={welcomeEnabled} onChange={(event) => setWelcomeEnabled(event.target.value)}>
                  <option value="true">启用</option>
                  <option value="false">停用</option>
                </select>
              </label>
              <label className="field">
                <span>提及新成员</span>
                <select value={welcomeMention} onChange={(event) => setWelcomeMention(event.target.value)}>
                  <option value="true">提及</option>
                  <option value="false">不提及</option>
                </select>
              </label>
              <label className="field span-2">
                <span>欢迎语模板</span>
                <textarea
                  rows={6}
                  value={welcomeTemplate}
                  onChange={(event) => setWelcomeTemplate(event.target.value)}
                  placeholder="欢迎 {{member_name}} 加入群聊"
                />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadGroupSettings()} disabled={!config.adminToken}>
                读取欢迎语
              </button>
              <DangerAction
                label="保存欢迎语"
                title="确认更新群欢迎语"
                impact={<p>新成员入群时将按当前开关发送欢迎语；模板内容不会在审计日志中明文保存。</p>}
                confirmLabel="确认保存"
                pendingLabel="正在保存…"
                disabled={!config.adminToken || !groupSessionId || !groupSettingsEtag || !groupSettingsDirty || groupSettingsStatus === "saving"}
                onConfirm={saveGroupSettings}
              />
            </div>
            <p className="muted-copy">
              成员加入群聊时欢迎语会自动入队，是否 @ 新群友由上方“提及新成员”开关控制。
            </p>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">成员事件</p>
                <h3>最近成员事件</h3>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>最多读取条数</span>
                <input type="number" value={eventLimit} onChange={(event) => setEventLimit(Number(event.target.value))} />
              </label>
              <label className="field">
                <span>事件类型筛选</span>
                <select value={eventTypeFilter} onChange={(event) => setEventTypeFilter(event.target.value)}>
                  <option value="">全部</option>
                  <option value="group.member.joined">{wxbotEventTypeLabel("group.member.joined")}</option>
                  <option value="group.member.left">{wxbotEventTypeLabel("group.member.left")}</option>
                </select>
              </label>
              <label className="field">
                <span>群聊筛选</span>
                <select value={eventSessionFilter} onChange={(event) => setEventSessionFilter(event.target.value)}>
                  <option value="">全部群</option>
                  {groupSessions.map((item) => (
                    <option key={item.session_id} value={item.session_id}>
                      {item.session_name || item.session_id}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadMemberEvents()} disabled={!config.adminToken}>
                刷新事件
              </button>
            </div>
            <div className="table-scroll">
              <table>
                <caption className="sr-only">最近群成员事件</caption>
                <thead>
                  <tr>
                    <th scope="col">时间</th>
                    <th scope="col">事件</th>
                    <th scope="col">群</th>
                    <th scope="col">成员</th>
                    <th scope="col">实体 WXID</th>
                    <th scope="col">技术详情</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredEvents.map((item) => (
                    <tr key={item.sdk_event_id}>
                      <td>{formatEventTime(item.created_ts)}</td>
                      <td>{wxbotEventTypeLabel(item.event_type)}</td>
                      <td>{item.session_name || item.session_id}</td>
                      <td>{item.entity_name || "-"}</td>
                      <td className="mono">{item.entity_wxid || "-"}</td>
                      <td>
                        <details>
                          <summary>{Object.keys(item.payload || {}).length} 个字段</summary>
                          <pre className="mono">{formatJson(item.payload || {})}</pre>
                        </details>
                      </td>
                    </tr>
                  ))}
                  {!filteredEvents.length && (
                    <tr>
                      <td colSpan={6}>暂无成员事件</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <OutputPanel flush title="事件订阅 / 成员事件响应" value={eventOutput} />
          <OutputPanel flush title="群欢迎语响应" value={groupOutput} />
        </>
      );
}

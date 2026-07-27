import { PageHeader } from "../../components/PageHeader";
import { Tabs } from "../../components/Tabs";
import { UnsavedChangesGuard } from "../../components/UnsavedChangesGuard";
import { WxbotOverviewTab } from "./WxbotOverviewTab";
import { WxbotPolicyTab } from "./WxbotPolicyTab";
import { WxbotAgentTab } from "./WxbotAgentTab";
import { WxbotEventsTab } from "./WxbotEventsTab";
import { WxbotReportsTab } from "./WxbotReportsTab";
import { WxbotSendTab } from "./WxbotSendTab";
import { useWxbotPageController } from "./useWxbotPageController";
import { wxbotBridgeModeLabel } from "./model";


export function WxbotPageView() {
  const controller = useWxbotPageController();
  const {
    activeTab,
    adminConfigDirty,
    agentToolPolicySnapshot,
    bridgeStatus,
    bridgeSummaryText,
    chooseVerifiedGroup,
    effectiveGroupSessionId,
    effectiveSessionId,
    error,
    groupSessions,
    groupActivityDirty,
    loadMemberEvents,
    loadReplyPolicy,
    loadReportSubscriptions,
    loading,
    memberEvents,
    queueStats,
    replyPolicyDirty,
    refresh,
    reportSubscriptions,
    rosterGroups,
    sdkQueueStats,
    sdkSummaryText,
    selectTab,
    selfReviewSubscriptions,
    sessions,
  } = controller;

  return (
    <div className="page-grid">
      <UnsavedChangesGuard when={groupActivityDirty || replyPolicyDirty || adminConfigDirty} />
      <section className="panel panel-hero span-3">
        <PageHeader
          eyebrow="微信机器人"
          title="微信机器人桥接"
          description="统一管理桥接状态、会话回复策略、群成员事件、欢迎语和日报周报月报订阅。报表由本地微信解密数据库生成，不依赖平台留存对话轮次。"
        />
        <div className="action-row">
          <button className="button button-primary" onClick={() => void refresh()} disabled={loading}>
            {loading ? "刷新中..." : "刷新桥接状态"}
          </button>
          <button className="button button-secondary" onClick={() => void loadReplyPolicy()}>
            读取回复策略
          </button>
          <button className="button button-secondary" onClick={() => void loadMemberEvents()}>
            读取成员事件
          </button>
          <button className="button button-secondary" onClick={() => void loadReportSubscriptions()}>
            读取日报周报月报
          </button>
        </div>
        <div className="summary-grid">
          <div
            className="summary-card"
            data-status={bridgeStatus ? (bridgeStatus.running ? "ok" : "error") : error ? "error" : undefined}
          >
            <span>桥接</span>
            <strong>{bridgeSummaryText}</strong>
          </div>
          <div
            className="summary-card"
            data-status={bridgeStatus ? (bridgeStatus.sdk_online ? "ok" : "error") : error ? "error" : undefined}
          >
            <span>SDK</span>
            <strong>{sdkSummaryText}</strong>
          </div>
          <div className="summary-card">
            <span>消息模式</span>
            <strong>{wxbotBridgeModeLabel(bridgeStatus?.ingest_mode)}</strong>
          </div>
          <div className="summary-card">
            <span>事件模式</span>
            <strong>{wxbotBridgeModeLabel(bridgeStatus?.event_mode)}</strong>
          </div>
        </div>
        <div className="muted-copy">
          {error ? `状态接口失败：${error}` : "状态已从服务端读取；技术地址收在总览的高级信息中。"}
        </div>
      </section>

      <section className="panel span-3">
        <div className="panel-header">
          <div>
            <p className="section-kicker">群聊</p>
            <h3>群列表</h3>
          </div>
        </div>
        <div className="table-scroll compact-table-scroll">
          <table>
            <caption className="sr-only">已同步微信群聊</caption>
            <thead>
              <tr>
                <th scope="col">群名称</th>
                <th scope="col">技术标识</th>
                <th scope="col">来源</th>
              </tr>
            </thead>
            <tbody>
              {groupSessions.map((item) => (
                <tr
                  key={item.session_id}
                  className={item.session_id === effectiveGroupSessionId ? "table-row-active" : ""}
                >
                  <td>
                    <button
                      type="button"
                      className="data-table-row-action"
                      aria-pressed={item.session_id === effectiveGroupSessionId}
                      onClick={() => chooseVerifiedGroup(item.session_id)}
                    >
                      {item.session_name || "未命名群聊"}
                    </button>
                  </td>
                  <td className="mono">{item.session_id}</td>
                  <td>{rosterGroups.length ? "后端名册" : "会话列表"}</td>
                </tr>
              ))}
              {!groupSessions.length && (
                <tr>
                  <td colSpan={3}>暂无群列表</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="span-3 wxbot-workspace" aria-labelledby="wxbot-workspace-title">
        <p className="section-kicker">工作区</p>
        <h2 id="wxbot-workspace-title">微信机器人控制台</h2>
        <Tabs
          ariaLabel="微信机器人功能"
          className="wxbot-tabs"
          activeId={activeTab}
          onChange={(id) => selectTab(id as typeof activeTab)}
          tabs={[
            {
              id: "overview",
              label: <>总览 <span className="tab-count">{sessions.length}</span></>,
              content: <WxbotOverviewTab controller={controller} />,
            },
            {
              id: "policy",
              label: <>回复策略 <span className="tab-count">{effectiveSessionId ? 1 : 0}</span></>,
              content: <WxbotPolicyTab controller={controller} />,
            },
            {
              id: "agent",
              label: <>智能体工具 <span className="tab-count">{agentToolPolicySnapshot ? agentToolPolicySnapshot.effective_tools.length : "-"}</span></>,
              content: <WxbotAgentTab controller={controller} />,
            },
            {
              id: "events",
              label: <>群成员事件 <span className="tab-count">{memberEvents.length}</span></>,
              content: <WxbotEventsTab controller={controller} />,
            },
            {
              id: "reports",
              label: <>报表复盘 <span className="tab-count">{reportSubscriptions.length + selfReviewSubscriptions.length}</span></>,
              content: <WxbotReportsTab controller={controller} />,
            },
            {
              id: "send",
              label: <>队列诊断 <span className="tab-count">{(queueStats.pending ?? 0) + (Number(sdkQueueStats.pending) || 0)}</span></>,
              content: <WxbotSendTab controller={controller} />,
            },
          ]}
        />
      </section>
    </div>
  );
}

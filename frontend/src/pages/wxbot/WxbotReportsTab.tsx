import { DangerAction } from "../../components/DangerAction";
import { OutputPanel } from "../../components/OutputPanel";
import type { WxbotPageController } from "./useWxbotPageController";
import {
  formatDateValue,
  selfReviewPublishLabel,
  selfReviewPublishStatus,
  wxbotBooleanLabel,
  wxbotJobStageLabel,
  wxbotMessageTypeLabel,
  wxbotOperationStatusLabel,
  wxbotReportPeriodLabel,
} from "./model";

export function WxbotReportsTab({ controller }: { controller: WxbotPageController }) {
  const {
    chooseVerifiedGroup,
    config,
    deleteReportSubscription,
    deleteSelfReviewSubscription,
    effectiveGroupSessionId,
    groupSessionId,
    groupSessions,
    loadReportMessages,
    loadReportSubscriptions,
    loadSelfReviewJobs,
    loadSelfReviewSubscriptions,
    previewReport,
    previewSelfReview,
    publishSelfReviewJob,
    reportDailyEnabled,
    reportDailyHour,
    reportDate,
    reportMessages,
    reportMonthlyDay,
    reportMonthlyEnabled,
    reportOutput,
    reportPreview,
    reportPreviewType,
    reportSubscriptions,
    reportSubscriptionDirty,
    reportSubscriptionsEtag,
    reportSubscriptionsStatus,
    reportTz,
    reportWeeklyDay,
    reportWeeklyEnabled,
    reportWeeklyHour,
    reportYearMonth,
    saveReportSubscription,
    saveSelfReviewSubscription,
    selectedGroupSubscription,
    selectedSelfReviewSubscription,
    selfReviewDailyHour,
    selfReviewDate,
    selfReviewEnabled,
    selfReviewJobs,
    selfReviewOutput,
    selfReviewPreview,
    selfReviewPublishingJobId,
    selfReviewSubscriptions,
    selfReviewSubscriptionDirty,
    selfReviewSubscriptionsEtag,
    selfReviewSubscriptionsStatus,
    selfReviewTz,
    sendReport,
    setReportDailyEnabled,
    setReportDailyHour,
    setReportDate,
    setReportMonthlyDay,
    setReportMonthlyEnabled,
    setReportTz,
    setReportWeeklyDay,
    setReportWeeklyEnabled,
    setReportWeeklyHour,
    setReportYearMonth,
    setSelfReviewDailyHour,
    setSelfReviewDate,
    setSelfReviewEnabled,
    setSelfReviewTz,
    weeklyReportFocused,
  } = controller;

  return (
        <>
          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">质量复盘</p>
                <h3>自我迭代 / 质量复盘</h3>
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
                <span>时区</span>
                <input value={selfReviewTz} onChange={(event) => setSelfReviewTz(event.target.value)} />
              </label>
              <label className="field">
                <span>启用开关</span>
                <select value={selfReviewEnabled} onChange={(event) => setSelfReviewEnabled(event.target.value)}>
                  <option value="true">启用</option>
                  <option value="false">停用</option>
                </select>
              </label>
              <label className="field">
                <span>执行小时</span>
                <input type="number" value={selfReviewDailyHour} onChange={(event) => setSelfReviewDailyHour(Number(event.target.value))} />
              </label>
              <label className="field">
                <span>手动日期</span>
                <input value={selfReviewDate} onChange={(event) => setSelfReviewDate(event.target.value)} placeholder="YYYY-MM-DD，可留空" />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadSelfReviewSubscriptions()} disabled={!config.adminToken}>
                读取订阅
              </button>
              <DangerAction
                label="保存订阅"
                title="确认更新质量复盘订阅"
                impact={<p>将改变当前已验证群的定时复盘开关与执行时间；生成结果仍保持草稿，需再次人工发布。</p>}
                confirmLabel="确认保存"
                pendingLabel="正在保存…"
                disabled={!config.adminToken || !effectiveGroupSessionId || !selfReviewSubscriptionsEtag || !selfReviewSubscriptionDirty || selfReviewSubscriptionsStatus === "saving"}
                onConfirm={saveSelfReviewSubscription}
              />
              <DangerAction
                label="删除订阅"
                title="删除当前群的质量复盘订阅"
                impact={<p>将停止后续定时复盘；已有复盘草稿与已发布知识不会被删除。</p>}
                confirmLabel="确认删除"
                pendingLabel="正在删除…"
                disabled={!config.adminToken || !effectiveGroupSessionId || !selectedSelfReviewSubscription || !selfReviewSubscriptionsEtag || selfReviewSubscriptionsStatus === "saving"}
                onConfirm={deleteSelfReviewSubscription}
              />
              <button className="button button-primary" onClick={() => void previewSelfReview()} disabled={!config.adminToken || !effectiveGroupSessionId}>
                立即生成复盘
              </button>
              <button className="button button-secondary" onClick={() => void loadSelfReviewJobs()} disabled={!config.adminToken}>
                刷新任务
              </button>
              <DangerAction
                label={selfReviewPreview?.job_id === selfReviewPublishingJobId ? "发布中…" : "审核通过并发布"}
                title="确认发布质量复盘到知识库"
                impact={<p>任务 {selfReviewPreview?.job_id ?? "-"} 的复盘草稿将成为当前租户可检索的知识；发布后请通过知识库版本流程修订。</p>}
                confirmLabel="确认发布"
                pendingLabel="正在发布…"
                disabled={
                  !config.adminToken
                  || !effectiveGroupSessionId
                  || !selfReviewPreview?.job_id
                  || selfReviewPreview.status !== "completed"
                  || selfReviewPublishStatus(selfReviewPreview) === "published"
                  || selfReviewPublishingJobId !== null
                }
                onConfirm={() => publishSelfReviewJob(selfReviewPreview?.job_id || 0)}
              />
            </div>
            <p className="muted-copy">
              当前群：<span className="mono">{effectiveGroupSessionId || "-"}</span>
              {selectedSelfReviewSubscription ? "，已开启自我迭代配置" : "，尚未配置自我迭代"}
            </p>
            <p className="muted-copy">
              安全发布流程：复盘任务只生成草稿，不会自动写入知识库；请先阅读复盘内容，再由管理员点击“审核通过并发布”。
            </p>
            <p className="muted-copy">
              当前任务状态：
              <span> {wxbotOperationStatusLabel(selfReviewPreview?.status)}</span>
              ，阶段：
              <span> {wxbotJobStageLabel(selfReviewPreview?.current_stage)}</span>
              ，周期：
              <span> {wxbotReportPeriodLabel(selfReviewPreview?.period)}</span>
              ，知识库文档：
              <span className="mono"> {selfReviewPreview?.kb_doc_id ?? "-"}</span>
              ，发布状态：
              <span className="mono">
                {" "}
                {selfReviewPreview
                  ? selfReviewPublishLabel(selfReviewPublishStatus(selfReviewPreview))
                  : "-"}
              </span>
            </p>
            <pre className="code-view code-view-compact">
              {selfReviewPreview?.report || "这里会生成上一天重点交互的质量复盘文档，默认优先关注与机器人相关的消息片段。"}
            </pre>
            <div className="table-scroll compact-table-scroll">
              <table>
                <caption className="sr-only">群自复盘订阅</caption>
                <thead>
                  <tr>
                    <th scope="col">群</th>
                    <th scope="col">启用</th>
                    <th scope="col">执行时间</th>
                    <th scope="col">时区</th>
                    <th scope="col">发布机制</th>
                  </tr>
                </thead>
                <tbody>
                  {selfReviewSubscriptions.map((item) => (
                    <tr key={item.session_id}>
                      <td>
                        <button
                          type="button"
                          className="button button-secondary"
                          aria-pressed={effectiveGroupSessionId === item.session_id}
                          onClick={() => {
                            chooseVerifiedGroup(item.session_id);
                            setSelfReviewEnabled(String(Boolean(item.enabled)));
                            setSelfReviewDailyHour(Number(item.daily_hour ?? 23));
                            setSelfReviewTz(item.tz || "Asia/Shanghai");
                          }}
                        >
                          {item.session_name || item.session_id}
                        </button>
                      </td>
                      <td>{wxbotBooleanLabel(Boolean(item.enabled), "已启用", "已停用")}</td>
                      <td>{item.daily_hour}:00</td>
                      <td className="mono">{item.tz}</td>
                      <td>草稿 → 人工审核</td>
                    </tr>
                  ))}
                  {!selfReviewSubscriptions.length && (
                    <tr>
                      <td colSpan={5}>当前还没有自我迭代订阅</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">定时计划</p>
                <h3>日报 / 周报 / 月报订阅</h3>
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
                <span>时区</span>
                <input value={reportTz} onChange={(event) => setReportTz(event.target.value)} />
              </label>
              <label className="field">
                <span>日报开关</span>
                <select value={reportDailyEnabled} onChange={(event) => setReportDailyEnabled(event.target.value)}>
                  <option value="true">启用</option>
                  <option value="false">停用</option>
                </select>
              </label>
              <label className="field">
                <span>日报小时</span>
                <input type="number" value={reportDailyHour} onChange={(event) => setReportDailyHour(Number(event.target.value))} />
              </label>
              <label className={`field${weeklyReportFocused ? " report-focus-field" : ""}`}>
                <span>周报开关</span>
                <select value={reportWeeklyEnabled} onChange={(event) => setReportWeeklyEnabled(event.target.value)}>
                  <option value="true">启用</option>
                  <option value="false">停用</option>
                </select>
              </label>
              <label className={`field${weeklyReportFocused ? " report-focus-field" : ""}`}>
                <span>周报星期</span>
                <input type="number" value={reportWeeklyDay} onChange={(event) => setReportWeeklyDay(Number(event.target.value))} />
              </label>
              <label className={`field${weeklyReportFocused ? " report-focus-field" : ""}`}>
                <span>周报小时</span>
                <input type="number" value={reportWeeklyHour} onChange={(event) => setReportWeeklyHour(Number(event.target.value))} />
              </label>
              <label className="field">
                <span>月报开关</span>
                <select value={reportMonthlyEnabled} onChange={(event) => setReportMonthlyEnabled(event.target.value)}>
                  <option value="true">启用</option>
                  <option value="false">停用</option>
                </select>
              </label>
              <label className="field">
                <span>月报日期</span>
                <input type="number" value={reportMonthlyDay} onChange={(event) => setReportMonthlyDay(Number(event.target.value))} />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadReportSubscriptions()} disabled={!config.adminToken}>
                读取订阅
              </button>
              <DangerAction
                label="保存订阅"
                title="确认更新定时报表订阅"
                impact={<p>将改变当前已验证群的日报、周报、月报自动生成与发送计划。</p>}
                confirmLabel="确认保存"
                pendingLabel="正在保存…"
                disabled={!config.adminToken || !effectiveGroupSessionId || !reportSubscriptionsEtag || !reportSubscriptionDirty || reportSubscriptionsStatus === "saving"}
                onConfirm={saveReportSubscription}
              />
              <DangerAction
                label="删除订阅"
                title="删除当前群的定时报表订阅"
                impact={<p>将停止后续日报、周报和月报定时发送；历史任务不会被删除。</p>}
                confirmLabel="确认删除"
                pendingLabel="正在删除…"
                disabled={!config.adminToken || !effectiveGroupSessionId || !selectedGroupSubscription || !reportSubscriptionsEtag || reportSubscriptionsStatus === "saving"}
                onConfirm={deleteReportSubscription}
              />
            </div>
            <p className="muted-copy">
              当前群：<span className="mono">{effectiveGroupSessionId || "-"}</span>
              {selectedGroupSubscription ? "，已存在定时配置" : "，尚未配置定时发送"}
              {weeklyReportFocused ? "，当前入口已聚焦微信群周报" : ""}
            </p>
            <div className="table-scroll compact-table-scroll">
              <table>
                <caption className="sr-only">日报周报月报订阅</caption>
                <thead>
                  <tr>
                    <th scope="col">群</th>
                    <th scope="col">日报</th>
                    <th scope="col">日报时间</th>
                    <th scope="col">周报</th>
                    <th scope="col">周报星期</th>
                    <th scope="col">周报时间</th>
                    <th scope="col">月报</th>
                    <th scope="col">月报日期</th>
                    <th scope="col">时区</th>
                  </tr>
                </thead>
                <tbody>
                  {reportSubscriptions.map((item) => (
                    <tr key={item.session_id}>
                      <td>
                        <button
                          type="button"
                          className="button button-secondary"
                          aria-pressed={effectiveGroupSessionId === item.session_id}
                          onClick={() => {
                            chooseVerifiedGroup(item.session_id);
                            setReportDailyEnabled(String(Boolean(item.daily_enabled)));
                            setReportWeeklyEnabled(String(Boolean(item.weekly_enabled ?? true)));
                            setReportMonthlyEnabled(String(Boolean(item.monthly_enabled)));
                            setReportDailyHour(Number(item.daily_hour ?? 9));
                            setReportWeeklyDay(Number(item.weekly_day ?? 1));
                            setReportWeeklyHour(Number(item.weekly_hour ?? 9));
                            setReportMonthlyDay(Number(item.monthly_day ?? 1));
                            setReportTz(item.tz || "Asia/Shanghai");
                          }}
                        >
                          {item.session_name || item.session_id}
                        </button>
                      </td>
                      <td>{wxbotBooleanLabel(Boolean(item.daily_enabled), "已启用", "已停用")}</td>
                      <td>{item.daily_hour}:00</td>
                      <td>{wxbotBooleanLabel(Boolean(item.weekly_enabled ?? true), "已启用", "已停用")}</td>
                      <td>{item.weekly_day ?? 1}</td>
                      <td>{item.weekly_hour ?? 9}:00</td>
                      <td>{wxbotBooleanLabel(Boolean(item.monthly_enabled), "已启用", "已停用")}</td>
                      <td>{item.monthly_day}</td>
                      <td className="mono">{item.tz}</td>
                    </tr>
                  ))}
                  {!reportSubscriptions.length && (
                    <tr>
                      <td colSpan={9}>当前还没有日报周报月报订阅</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">手动操作</p>
                <h3>手动预览 / 发送</h3>
              </div>
            </div>
            <div className="action-row">
              <button
                className={`button ${reportPreviewType === "daily" ? "button-primary" : "button-secondary"}`}
                onClick={() => void previewReport("daily")}
                disabled={!config.adminToken || !effectiveGroupSessionId}
              >
                预览日报
              </button>
              <button
                className={`button ${reportPreviewType === "weekly" ? "button-primary" : "button-secondary"}${weeklyReportFocused ? " report-focus-button" : ""}`}
                onClick={() => void previewReport("weekly")}
                disabled={!config.adminToken || !effectiveGroupSessionId}
              >
                预览周报
              </button>
              <button
                className={`button ${reportPreviewType === "monthly" ? "button-primary" : "button-secondary"}`}
                onClick={() => void previewReport("monthly")}
                disabled={!config.adminToken || !effectiveGroupSessionId}
              >
                预览月报
              </button>
              <DangerAction
                label="确认发送"
                title="确认向当前微信群发送报表"
                impact={<p>{reportPreviewType === "daily" ? "日报" : reportPreviewType === "weekly" ? "周报" : "月报"}将进入当前群的真实发送队列；请先核对预览内容与群范围。</p>}
                confirmLabel="确认发送到群"
                pendingLabel="正在提交…"
                disabled={!config.adminToken || !effectiveGroupSessionId || !reportPreview?.report || reportPreview.status !== "completed"}
                onConfirm={sendReport}
              />
            </div>
            <p className="muted-copy">日报从 SDK 原始消息生成；周报只汇总已完成日报，月报只汇总已完成周报，最终发送仍走 SDK 消息队列。</p>
            <p className="muted-copy">留空时按旧版微信机器人的口径处理：日报预览昨天，周报预览上周，月报预览上个月。</p>
            <p className="muted-copy">
              当前预览状态：
              <span> {wxbotOperationStatusLabel(reportPreview?.status)}</span>
              ，阶段：
              <span> {wxbotJobStageLabel(reportPreview?.current_stage)}</span>
              ，周期：
              <span> {wxbotReportPeriodLabel(reportPreview?.period)}</span>
            </p>
            <pre className="code-view code-view-compact">
              {reportPreview?.report || "请先预览日报、周报或月报；若任务仍在等待或运行，页面会自动轮询直到完成。"}
            </pre>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">历史记录</p>
                <h3>自我复盘任务</h3>
              </div>
            </div>
            <p className="muted-copy">默认只看当前群最近 20 条任务。复盘完成后先保留为草稿；管理员审核并发布成功后，才会显示知识库文档 ID。</p>
            <div className="table-scroll">
              <table>
                <caption className="sr-only">报表生成与发布任务</caption>
                <thead>
                  <tr>
                    <th scope="col">时间</th>
                    <th scope="col">群</th>
                    <th scope="col">周期</th>
                    <th scope="col">状态</th>
                    <th scope="col">阶段</th>
                    <th scope="col">消息数</th>
                    <th scope="col">发布状态</th>
                    <th scope="col">知识文档</th>
                    <th scope="col">异常</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {selfReviewJobs.map((item) => (
                    <tr key={item.id}>
                      <td>{formatDateValue(item.created_at)}</td>
                      <td>{item.session_name || item.session_id}</td>
                      <td className="mono">{item.period_label || item.period_key || "-"}</td>
                      <td>{wxbotOperationStatusLabel(item.status)}</td>
                      <td>{wxbotJobStageLabel(item.current_stage)}</td>
                      <td>{item.msg_count ?? 0}</td>
                      <td>{selfReviewPublishLabel(selfReviewPublishStatus(item))}</td>
                      <td className="mono">{item.kb_doc_id ?? "-"}</td>
                      <td className="mono">{item.error || "-"}</td>
                      <td>
                        <DangerAction
                          label={item.id === selfReviewPublishingJobId
                            ? "发布中…"
                            : selfReviewPublishStatus(item) === "published"
                              ? "已发布"
                              : "审核并发布"}
                          title={`确认发布复盘任务 ${item.id}`}
                          impact={<p>该任务的复盘草稿将发布到知识库并可被后续检索使用。</p>}
                          confirmLabel="确认发布"
                          pendingLabel="正在发布…"
                          disabled={
                            !config.adminToken
                            || item.status !== "completed"
                            || selfReviewPublishStatus(item) === "published"
                            || selfReviewPublishingJobId !== null
                          }
                          onConfirm={() => publishSelfReviewJob(item.id)}
                        />
                      </td>
                    </tr>
                  ))}
                  {!selfReviewJobs.length && (
                    <tr>
                      <td colSpan={10}>当前还没有自我复盘任务</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel span-3">
            <details>
              <summary>技术详情：日报原始消息</summary>
              <div className="panel-header">
                <div>
                  <p className="section-kicker">高级诊断</p>
                  <h3>日报原始消息读取</h3>
                </div>
              </div>
            <div className="form-grid">
              <label className="field">
                <span>日报日期</span>
                <input value={reportDate} onChange={(event) => setReportDate(event.target.value)} placeholder="YYYY-MM-DD，可留空" />
              </label>
              <label className="field">
                <span>月报月份</span>
                <input value={reportYearMonth} onChange={(event) => setReportYearMonth(event.target.value)} placeholder="YYYY-MM，可留空" />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadReportMessages("daily")}>
                读取日报原始记录
              </button>
            </div>
            <p className="muted-copy">原始聊天记录只用于日报；周报和月报不会读取原始消息。</p>
            <p className="muted-copy">
              当前周期：<span>{wxbotReportPeriodLabel(reportMessages?.period)}</span>
              ，共 <span className="mono">{reportMessages?.count ?? 0}</span> 条
            </p>
            <div className="table-scroll">
              <table>
                <caption className="sr-only">报表原始消息技术详情</caption>
                <thead>
                  <tr>
                    <th scope="col">时间</th>
                    <th scope="col">发送人</th>
                    <th scope="col">WXID</th>
                    <th scope="col">自己发送</th>
                    <th scope="col">类型</th>
                    <th scope="col">内容</th>
                  </tr>
                </thead>
                <tbody>
                  {(reportMessages?.messages || []).map((item, index) => (
                    <tr key={`${item.ts}-${item.sender_wxid}-${index}`}>
                      <td className="mono">{item.timestamp}</td>
                      <td>{item.sender_name || "-"}</td>
                      <td className="mono">{item.sender_wxid || "-"}</td>
                      <td>{wxbotBooleanLabel(item.is_self_sent)}</td>
                      <td>{wxbotMessageTypeLabel(item.msg_type)}</td>
                      <td className="mono">{item.text || "-"}</td>
                    </tr>
                  ))}
                  {!reportMessages?.messages?.length && (
                    <tr>
                      <td colSpan={6}>请先读取日报原始聊天记录</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            </details>
          </section>

          <OutputPanel flush title="日报 / 周报 / 月报响应" value={reportOutput} />
          <OutputPanel flush title="自我迭代响应" value={selfReviewOutput} />
        </>
      );
}

import { Alert } from "../../components/Alert";
import { DangerAction } from "../../components/DangerAction";
import { OutputPanel } from "../../components/OutputPanel";
import { Link } from "react-router-dom";
import type { WxbotPageController } from "./useWxbotPageController";
import {
  formatDateValue,
  formatEventTime,
  replyParticipationSummary,
  wxbotBooleanLabel,
  wxbotMessageTypeLabel,
  wxbotQueueStatusLabel,
} from "./model";

export function WxbotSendTab({ controller }: { controller: WxbotPageController }) {
  const {
    actionOutput,
    config,
    loadReplyQueueMessages,
    loadSdkQueueMessages,
    queueLimit,
    queueStatusFilter,
    reconcileSdkQueueMessage,
    replyQueueItems,
    sdkQueueItems,
    sdkQueueReconcileBusy,
    sdkQueueStatusFilter,
    setQueueLimit,
    setQueueStatusFilter,
    setSdkQueueStatusFilter,
  } = controller;

  return (
        <>
          <section className="panel">
            <div className="panel-header">
              <div>
                <p className="section-kicker">端到端测试</p>
                <h3>从安全消息入口发起</h3>
              </div>
            </div>
            <p className="muted-copy">
              手工直写 SDK 队列会绕过参与决策与入站幂等，因此控制台只保留真实链路模拟器。
              目标群由已认证群名册校验，租户签名密钥不会进入浏览器。
            </p>
            <div className="action-row">
              <Link className="button button-primary" to="/playground">打开消息入口模拟器</Link>
            </div>
          </section>

          <section className="panel">
            <div className="panel-header">
              <div>
                <p className="section-kicker">发送说明</p>
                <h3>发送说明</h3>
              </div>
            </div>
            <ul className="route-list">
              <li>控制台不会接受任意会话 ID；文件发送仅接受 SDK 主机授权目录中的绝对路径。</li>
              <li>文件消息不支持 file_url 或浏览器上传，文件名、大小与摘要会随发送队列透传。</li>
              <li>图片发送仍必须使用后端签发、绑定当前租户且未过期的媒体 ID。</li>
              <li>日报周报月报的自动发送最终也是通过这里同一套 SDK 队列投递出去。</li>
            </ul>
          </section>

          <section className="panel span-2">
            <div className="panel-header">
              <div>
                <p className="section-kicker">系统发送队列</p>
                <h3>系统待发送消息</h3>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>状态</span>
                <select value={queueStatusFilter} onChange={(event) => setQueueStatusFilter(event.target.value)}>
                  <option value="pending">{wxbotQueueStatusLabel("pending")}</option>
                  <option value="sent">{wxbotQueueStatusLabel("sent")}</option>
                  <option value="failed">{wxbotQueueStatusLabel("failed")}</option>
                  <option value="cancelled">{wxbotQueueStatusLabel("cancelled")}</option>
                  <option value="">全部</option>
                </select>
              </label>
              <label className="field">
                <span>最多读取条数</span>
                <input type="number" value={queueLimit} onChange={(event) => setQueueLimit(Number(event.target.value) || 50)} />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadReplyQueueMessages()} disabled={!config.adminToken}>
                刷新系统队列
              </button>
            </div>
            <div className="table-scroll compact-table-scroll">
              <table>
                <caption className="sr-only">系统待发送队列</caption>
                <thead>
                  <tr>
                    <th scope="col">ID</th>
                    <th scope="col">群 / 会话</th>
                    <th scope="col">发送人</th>
                    <th scope="col">回复目标</th>
                    <th scope="col">类型</th>
                    <th scope="col">内容</th>
                    <th scope="col">追踪 ID</th>
                    <th scope="col">参与决策 / 取消原因</th>
                    <th scope="col">状态</th>
                    <th scope="col">时间</th>
                  </tr>
                </thead>
                <tbody>
                  {replyQueueItems.map((item) => (
                    <tr key={`cs-${item.id}`}>
                      <td className="mono">{item.id}</td>
                      <td>{item.session_name || item.session_id}</td>
                      <td className="mono">{item.sender_name || item.sender_wxid || "-"}</td>
                      <td className="mono">{item.reply_to_msg_svr_id || "-"}</td>
                      <td>{wxbotMessageTypeLabel(item.msg_type || "text")}</td>
                      <td className="mono">{item.file_name || item.reply_text || item.media_id || "-"}</td>
                      <td className="mono">{item.trace_id || "-"}</td>
                      <td className="mono">{replyParticipationSummary(item)}</td>
                      <td>{wxbotQueueStatusLabel(item.status)}</td>
                      <td>{formatDateValue(item.created_at)}</td>
                    </tr>
                  ))}
                  {!replyQueueItems.length && (
                    <tr>
                      <td colSpan={10}>当前系统待发送队列为空</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="panel">
            <div className="panel-header">
              <div>
                <p className="section-kicker">SDK 发送队列</p>
                <h3>SDK 本地待发送</h3>
              </div>
            </div>
            <p className="muted-copy">
              SDK 进入“发送中”状态后已开始接触微信客户端；若进程中断会转为“结果待核对”，
              控制台不会自动重发，必须由人工核对微信实际结果。
            </p>
            <div className="form-grid">
              <label className="field">
                <span>状态</span>
                <select value={sdkQueueStatusFilter} onChange={(event) => setSdkQueueStatusFilter(event.target.value)}>
                  <option value="uncertain">{wxbotQueueStatusLabel("uncertain")}</option>
                  <option value="pending">{wxbotQueueStatusLabel("pending")}</option>
                  <option value="running">{wxbotQueueStatusLabel("running")}</option>
                  <option value="failed">{wxbotQueueStatusLabel("failed")}</option>
                  <option value="sent">{wxbotQueueStatusLabel("sent")}</option>
                  <option value="cleared">{wxbotQueueStatusLabel("cleared")}</option>
                  <option value="">全部</option>
                </select>
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadSdkQueueMessages()} disabled={!config.adminToken}>
                刷新 SDK 队列
              </button>
            </div>
            <div className="table-scroll compact-table-scroll">
              <table>
                <caption className="sr-only">微信 SDK 本地待发送队列</caption>
                <thead>
                  <tr>
                    <th scope="col">ID</th>
                    <th scope="col">命令 ID</th>
                    <th scope="col">群 / 会话</th>
                    <th scope="col">发送人</th>
                    <th scope="col">回复目标</th>
                    <th scope="col">类型</th>
                    <th scope="col">内容</th>
                    <th scope="col">@发送者</th>
                    <th scope="col">状态 / 异常</th>
                    <th scope="col">尝试</th>
                    <th scope="col">时间</th>
                    <th scope="col">人工对账</th>
                  </tr>
                </thead>
                <tbody>
                  {sdkQueueItems.map((item) => (
                    <tr key={`sdk-${item.id}`}>
                      <td className="mono">{item.id}</td>
                      <td className="mono">{item.command_id || "-"}</td>
                      <td>{item.session_name || item.session_id}</td>
                      <td className="mono">{item.sender_name || item.sender_wxid || "-"}</td>
                      <td className="mono">{item.reply_to_msg_svr_id || "-"}</td>
                      <td>{wxbotMessageTypeLabel(item.msg_type || "text")}</td>
                      <td className="mono">{item.file_name || item.reply_text || item.media_id || "-"}</td>
                      <td>{wxbotBooleanLabel(Boolean(item.mention_sender))}</td>
                      <td>
                        <strong>{wxbotQueueStatusLabel(item.status)}</strong>
                        {item.status === "uncertain" && (
                          <Alert variant="warning" title="发送结果待人工核对">
                            微信客户端可能已经发送，也可能尚未发送。请先查看目标会话，再选择确认已发送或重试。
                          </Alert>
                        )}
                        {item.status === "running" && (
                          <Alert variant="info" title="正在与微信客户端交互">
                            当前禁止清理或重试；若进程中断，系统会隔离为“结果待核对”。
                          </Alert>
                        )}
                        {item.error && <div className="mono">{item.error}</div>}
                      </td>
                      <td className="mono">{item.attempt_count ?? 0}</td>
                      <td className="mono">
                        <div>创建：{item.created_ts ? formatEventTime(item.created_ts) : "-"}</div>
                        <div>领取：{item.claimed_ts ? formatEventTime(item.claimed_ts) : "-"}</div>
                        <div>发送：{item.sent_ts ? formatEventTime(item.sent_ts) : "-"}</div>
                      </td>
                      <td>
                        {item.status === "uncertain" ? (
                          <div className="action-row">
                            <DangerAction
                              label="确认已发送"
                              title={`确认消息 #${item.id} 已发送`}
                              impact={(
                                <p>
                                  将该消息标记为“已发送”，不会再次投递。只有在微信目标会话中确认消息已经出现后才能执行。
                                </p>
                              )}
                              confirmLabel="确认已发送"
                              pendingLabel="正在登记…"
                              disabled={!config.adminToken || Boolean(sdkQueueReconcileBusy)}
                              onConfirm={async () => {
                                await reconcileSdkQueueMessage(item.id, "confirm_sent");
                              }}
                            />
                            <DangerAction
                              label="确认未发送并重试"
                              title={`重新投递消息 #${item.id}`}
                              impact={(
                                <p>
                                  将该消息退回“等待发送”并允许再次投递；若此前其实已经发送，会造成重复消息。请先核对微信目标会话。
                                </p>
                              )}
                              confirmLabel="确认未发送，允许重试"
                              pendingLabel="正在登记…"
                              disabled={!config.adminToken || Boolean(sdkQueueReconcileBusy)}
                              onConfirm={async () => {
                                await reconcileSdkQueueMessage(item.id, "retry");
                              }}
                            />
                          </div>
                        ) : (
                          <span className="muted-copy">无需对账</span>
                        )}
                      </td>
                    </tr>
                  ))}
                  {!sdkQueueItems.length && (
                    <tr>
                      <td colSpan={12}>当前筛选条件下没有 SDK 队列消息</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <OutputPanel title="发送响应" value={actionOutput} />
        </>
      );
}

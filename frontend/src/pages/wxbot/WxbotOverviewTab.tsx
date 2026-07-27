import { OutputPanel } from "../../components/OutputPanel";
import { formatJson } from "../../lib/api";
import type { WxbotPageController } from "./useWxbotPageController";

export function WxbotOverviewTab({ controller }: { controller: WxbotPageController }) {
  const {
    bridgeStatus,
    config,
    error,
    output,
    sdkQueueStats,
  } = controller;

  return (
        <>
          <section className="panel span-2">
            <div className="panel-header">
              <div>
                <p className="section-kicker">桥接服务</p>
                <h3>桥接详情</h3>
              </div>
            </div>
            <div className="summary-grid">
              <div className="summary-card">
                <span>SDK 待发送</span>
                <strong>{Number(sdkQueueStats.pending || 0)}</strong>
              </div>
              <div className="summary-card">
                <span>SDK 失败</span>
                <strong>{Number(sdkQueueStats.failed || 0)}</strong>
              </div>
              <div className="summary-card">
                <span>成员事件</span>
                <strong>{Object.values(bridgeStatus?.member_event_stats || {}).reduce((sum, value) => sum + Number(value || 0), 0)}</strong>
              </div>
              <div className="summary-card">
                <span>消息游标</span>
                <strong>{bridgeStatus?.cursor ?? "-"}</strong>
              </div>
            </div>
            <details>
              <summary>技术详情：桥接地址、游标与队列</summary>
            <table>
              <caption className="sr-only">微信桥接运行详情</caption>
              <tbody>
                <tr>
                  <th scope="row">控制台 API</th>
                  <td className="mono">{config.apiBaseUrl}</td>
                </tr>
                <tr>
                  <th scope="row">SDK 地址</th>
                  <td className="mono">{bridgeStatus?.sdk_url || "-"}</td>
                </tr>
                <tr>
                  <th scope="row">租户 ID</th>
                  <td className="mono">{bridgeStatus?.tenant_id || "-"}</td>
                </tr>
                <tr>
                  <th scope="row">消息游标</th>
                  <td className="mono">{bridgeStatus?.cursor ?? "-"}</td>
                </tr>
                <tr>
                  <th scope="row">事件游标</th>
                  <td className="mono">{bridgeStatus?.event_cursor ?? "-"}</td>
                </tr>
                <tr>
                  <th scope="row">SDK 队列</th>
                  <td>
                    <details>
                      <summary>{Object.keys(sdkQueueStats).length} 个状态字段</summary>
                      <pre className="mono">{formatJson(sdkQueueStats)}</pre>
                    </details>
                  </td>
                </tr>
                <tr>
                  <th scope="row">成员事件统计</th>
                  <td>
                    <details>
                      <summary>{Object.keys(bridgeStatus?.member_event_stats || {}).length} 个事件类型</summary>
                      <pre className="mono">{formatJson(bridgeStatus?.member_event_stats || {})}</pre>
                    </details>
                  </td>
                </tr>
                <tr>
                  <th scope="row">状态接口错误</th>
                  <td className="mono">{error || "-"}</td>
                </tr>
              </tbody>
            </table>
            </details>
          </section>

          <section className="panel">
            <div className="panel-header">
              <div>
                <p className="section-kicker">接入范围</p>
                <h3>当前接入说明</h3>
              </div>
            </div>
            <ul className="route-list">
              <li>群聊是否必须先 @机器人 才会进入系统，取决于 SDK 侧“群消息必须 @我 才入站”开关。</li>
              <li>“包含触发词”模式既支持 @我，也支持关键词命中，并非只识别 @。</li>
              <li>群聊默认关闭回复；如果会话设为“全部消息”或“包含触发词”，可能在没有 @ 的场景下触发回复。</li>
              <li>日报/周报/月报数据直接来自本地微信解密数据库，适合按群做定时发送和人工预览发送。</li>
            </ul>
          </section>

          <OutputPanel title="微信桥接响应" value={output} />
        </>
      );
}

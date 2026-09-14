import { DangerAction } from "../../components/DangerAction";
import { OutputPanel } from "../../components/OutputPanel";
import type { WxbotPageController } from "./useWxbotPageController";

export function WxbotWebhookTab({ controller }: { controller: WxbotPageController }) {
  const {
    copyGroupWebhookUrl,
    createGroupWebhook,
    groupSessions,
    groupWebhookOutput,
    groupWebhookSessionId,
    groupWebhookStatus,
    groupWebhooks,
    loadGroupWebhooks,
    revealedGroupWebhook,
    revokeGroupWebhook,
    rotateGroupWebhook,
    setGroupWebhookSessionId,
  } = controller;
  const busy = groupWebhookStatus === "loading" || groupWebhookStatus === "saving";

  return (
    <>
      <section className="panel span-3">
        <div className="panel-header">
          <div>
            <p className="section-kicker">外部推送</p>
            <h3>群 webhook</h3>
          </div>
        </div>
        <p className="muted-copy">
          每个群一条独立地址。第三方对这个地址 POST JSON <code>{`{"text":"消息内容"}`}</code>{" "}
          即可把文本推到对应群，无需登录控制台。复制完整 https 地址（含{" "}
          <code>/api/v1/webhook/groups/</code>
          ），不要只用服务器 IP。地址里的 token 请当密钥保管。
        </p>
        <div className="form-grid">
          <label className="field span-2">
            <span>目标群聊</span>
            <select
              value={groupWebhookSessionId}
              onChange={(event) => setGroupWebhookSessionId(event.target.value)}
            >
              <option value="">请选择授权群</option>
              {groupSessions.map((item) => (
                <option key={item.session_id} value={item.session_id}>
                  {item.session_name || item.session_id}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="action-row">
          <button className="button button-primary" onClick={() => void createGroupWebhook()} disabled={busy}>
            {busy ? "处理中..." : "生成 webhook"}
          </button>
          <button className="button button-secondary" onClick={() => void loadGroupWebhooks()} disabled={busy}>
            刷新列表
          </button>
        </div>
        {revealedGroupWebhook?.url && (
          <div className="form-grid">
            <label className="field span-2">
              <span>完整地址（只显示一次）</span>
              <input value={revealedGroupWebhook.url} readOnly />
            </label>
            <div className="action-row">
              <button
                className="button button-secondary button-compact"
                onClick={() => void copyGroupWebhookUrl(revealedGroupWebhook.url || "")}
              >
                复制地址
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="panel span-3">
        <div className="panel-header">
          <div>
            <p className="section-kicker">已开通</p>
            <h3>群 webhook 列表</h3>
          </div>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>群</th>
                <th>状态</th>
                <th>token 尾号</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {groupWebhooks.map((item) => (
                <tr key={item.webhook_id}>
                  <td>{item.session_name || item.session_id}</td>
                  <td>{item.enabled ? "启用" : "已停用"}</td>
                  <td>{item.token_hint || "-"}</td>
                  <td>
                    <div className="action-row">
                      <button
                        className="button button-secondary button-compact"
                        onClick={() => void rotateGroupWebhook(item.webhook_id)}
                        disabled={busy || !item.enabled}
                      >
                        轮换
                      </button>
                      {item.enabled ? (
                        <DangerAction
                          label="停用"
                          title={`停用 ${item.session_name || item.session_id} 的 webhook`}
                          impact={<p>停用后原地址立即失效，第三方再推送会被拒绝。需要推送时请重新生成。</p>}
                          confirmLabel="确认停用"
                          pendingLabel="正在停用…"
                          disabled={busy}
                          onConfirm={() => revokeGroupWebhook(item.webhook_id)}
                        />
                      ) : (
                        <DangerAction
                          label="删除"
                          title={`删除 ${item.session_name || item.session_id} 的停用记录`}
                          impact={<p>将从列表里彻底删掉这条已停用记录，不影响当前有效地址。</p>}
                          confirmLabel="确认删除"
                          pendingLabel="正在删除…"
                          disabled={busy}
                          onConfirm={() => revokeGroupWebhook(item.webhook_id)}
                        />
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {!groupWebhooks.length && (
                <tr>
                  <td colSpan={4}>还没有群 webhook</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <OutputPanel title="接口返回" value={groupWebhookOutput} />
    </>
  );
}

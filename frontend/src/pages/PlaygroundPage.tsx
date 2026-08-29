import { useState } from "react";

import { GroupScopeEmpty } from "../components/GroupScopeEmpty";
import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { apiRequest, formatJson } from "../lib/api";
import { useStableIdempotencyKeys } from "../lib/idempotency";
import {
  GroupSelectionRequiredError,
  requireSelectedGroup,
  useConsoleConfig,
} from "../state/console-config";

type SimulationResponse = {
  status: "accepted";
  message_id: string;
  trace_id: string;
  session_id: string;
  session_name: string;
};

export function PlaygroundPage() {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [message, setMessage] = useState("");
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');
  const [sending, setSending] = useState(false);

  const selectedGroupIsVerified = Boolean(
    config.sessionId && verifiedGroupIds.has(config.sessionId),
  );

  const sendInbound = async () => {
    const normalizedMessage = message.trim();
    if (!normalizedMessage) {
      setOutput(formatJson({ error: "请先填写模拟群消息" }));
      return;
    }

    let groupId: string;
    try {
      groupId = requireSelectedGroup(config, verifiedGroupIds);
    } catch (error) {
      setOutput(
        formatJson({
          error:
            error instanceof GroupSelectionRequiredError
              ? error.message
              : "请选择已同步群聊",
        }),
      );
      return;
    }

    const intent = `playground:${config.tenantId}:${groupId}:${normalizedMessage}`;
    setSending(true);
    try {
      const result = await apiRequest<SimulationResponse>(
        config,
        `/plugins/wxbot/admin/tenants/${encodeURIComponent(config.tenantId)}`
          + `/groups/${encodeURIComponent(groupId)}/simulate-inbound`,
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({ message: normalizedMessage }),
          },
        },
      );
      setOutput(
        formatJson({
          status: "已进入消息处理队列",
          group: result.session_name || result.session_id,
          trace_id: result.trace_id,
          message_id: result.message_id,
        }),
      );
      clear(intent);
    } catch (error) {
      setOutput(
        formatJson({
          error: error instanceof Error ? error.message : "发送失败",
          recovery: "确认微信桥接、消息总线和目标群在线后重试；重试会复用同一幂等键。",
        }),
      );
    } finally {
      setSending(false);
    }
  };

  return !selectedGroupIsVerified ? (
    <GroupScopeEmpty
      eyebrow="消息测试"
      title="群消息入口模拟器"
      description="从已验证群聊发起一条服务端模拟消息，走真实消息总线、参与决策和回复链路。浏览器不会接触租户签名密钥。"
    />
  ) : (
    <div className="page-grid">
      <section className="panel span-3">
        <PageHeader
          eyebrow="消息测试"
          title="群消息入口模拟器"
          description="从已验证群聊发起一条服务端模拟消息，走真实消息总线、参与决策和回复链路。浏览器不会接触租户签名密钥。"
        />
        <p className="page-meta-line">
          <span>处理方式 <strong>真实异步队列</strong></span>
          <span>发送身份 <strong>服务端模拟群成员</strong></span>
        </p>
        <div className="form-grid page-compose">
          <label className="field span-2">
            <span>模拟群消息</span>
            <textarea
              rows={5}
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="例如：@机器人 帮我总结一下刚才的讨论"
              maxLength={20_000}
            />
            <small>{message.length.toLocaleString("zh-CN")} / 20,000 字符</small>
          </label>
        </div>
        <div className="action-row">
          <button
            type="button"
            className="button button-primary"
            onClick={() => void sendInbound()}
            disabled={sending || !selectedGroupIsVerified || !message.trim()}
          >
            {sending ? "正在进入队列…" : "发送模拟消息"}
          </button>
        </div>
        <OutputPanel flush title="处理结果（技术详情）" value={output} />
      </section>
    </div>
  );
}

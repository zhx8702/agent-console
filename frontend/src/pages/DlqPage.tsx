import { useState } from "react";

import { DangerAction } from "../components/DangerAction";
import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { apiRequest, formatJson } from "../lib/api";
import { useStableIdempotencyKeys } from "../lib/idempotency";
import { useConsoleConfig } from "../state/console-config";

export function DlqPage() {
  const { config } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [entryId, setEntryId] = useState("");
  const [beforeId, setBeforeId] = useState("");
  const [limit, setLimit] = useState(20);
  const [deleteAfterReplay, setDeleteAfterReplay] = useState("true");
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');

  const listMessages = async () => {
    try {
      const result = await apiRequest(config, "/v1/admin/dlq/messages", {
        auth: true,
        query: {
          tenant_id: config.tenantId,
          limit,
          before_id: beforeId,
        },
      });
      setOutput(formatJson(result));
    } catch (err) {
      setOutput(formatJson({ error: err instanceof Error ? err.message : "读取列表失败" }));
    }
  };

  const getMessage = async () => {
    try {
      const result = await apiRequest(config, `/v1/admin/dlq/messages/${entryId}`, { auth: true });
      setOutput(formatJson(result));
    } catch (err) {
      setOutput(formatJson({ error: err instanceof Error ? err.message : "读取消息失败" }));
    }
  };

  const replayMessage = async () => {
    const normalizedEntryId = entryId.trim();
    if (!normalizedEntryId) {
      const error = new Error("请先填写要重放的消息标识");
      setOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `dlq:replay:${config.tenantId}:${normalizedEntryId}:${deleteAfterReplay}`;
    try {
      const result = await apiRequest(config, `/v1/admin/dlq/messages/${normalizedEntryId}/replay`, {
        auth: true,
        init: {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": keyFor(intent),
          },
          body: JSON.stringify({ delete_after_replay: deleteAfterReplay === "true" }),
        },
      });
      setOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setOutput(formatJson({ error: err instanceof Error ? err.message : "重放失败" }));
      throw err;
    }
  };

  const deleteMessage = async () => {
    const normalizedEntryId = entryId.trim();
    if (!normalizedEntryId) {
      const error = new Error("请先填写要删除的消息标识");
      setOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `dlq:delete:${config.tenantId}:${normalizedEntryId}`;
    try {
      const result = await apiRequest(config, `/v1/admin/dlq/messages/${normalizedEntryId}`, {
        auth: true,
        init: {
          method: "DELETE",
          headers: { "Idempotency-Key": keyFor(intent) },
        },
      });
      setOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setOutput(formatJson({ error: err instanceof Error ? err.message : "删除失败" }));
      throw err;
    }
  };

  return (
    <div className="page-grid">
      <section className="panel span-3">
        <PageHeader
          eyebrow="失败消息队列"
          title="死信队列处置台"
          description="按消息标识列出、查看、重放或删除死信记录。"
          actions={
            <div className="action-row">
              <button className="button button-primary" onClick={() => void listMessages()}>
                列出消息
              </button>
              <button className="button button-secondary" onClick={() => void getMessage()}>
                查看单条
              </button>
            </div>
          }
        />
        <div className="page-ops-bar">
          <label className="field">
            <span>消息标识</span>
            <input value={entryId} onChange={(event) => setEntryId(event.target.value)} />
          </label>
          <label className="field">
            <span>返回上限</span>
            <input type="number" value={limit} onChange={(event) => setLimit(Number(event.target.value))} />
          </label>
          <label className="field">
            <span>截止消息标识</span>
            <input value={beforeId} onChange={(event) => setBeforeId(event.target.value)} />
          </label>
          <label className="field">
            <span>重放成功后删除原记录</span>
            <select value={deleteAfterReplay} onChange={(event) => setDeleteAfterReplay(event.target.value)}>
              <option value="true">是</option>
              <option value="false">否</option>
            </select>
          </label>
        </div>
        <div className="action-row">
          <DangerAction
            label="重放消息"
            title="确认重放死信消息"
            confirmLabel="确认重放"
            pendingLabel="正在重放…"
            disabled={!config.adminToken || !entryId.trim() || !config.tenantId}
            impact={(
              <dl>
                <div><dt>消息</dt><dd><code>{entryId.trim() || "未选择"}</code></dd></div>
                <div><dt>租户</dt><dd><code>{config.tenantId || "未选择"}</code></dd></div>
                <div><dt>重放后</dt><dd>{deleteAfterReplay === "true" ? "从死信队列移除原记录" : "保留原死信记录"}</dd></div>
              </dl>
            )}
            onConfirm={replayMessage}
          />
          <DangerAction
            label="删除死信"
            title="确认删除死信记录"
            confirmLabel="确认删除"
            pendingLabel="正在删除…"
            disabled={!config.adminToken || !entryId.trim() || !config.tenantId}
            impact={(
              <dl>
                <div><dt>消息</dt><dd><code>{entryId.trim() || "未选择"}</code></dd></div>
                <div><dt>租户</dt><dd><code>{config.tenantId || "未选择"}</code></dd></div>
                <div><dt>结果</dt><dd>死信记录会被移除，之后无法再从此队列重放。</dd></div>
              </dl>
            )}
            onConfirm={deleteMessage}
          />
        </div>
        <OutputPanel flush title="失败消息队列响应" value={output} />
      </section>
    </div>
  );
}

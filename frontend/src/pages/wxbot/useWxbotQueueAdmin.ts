import { useCallback, useEffect, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import type { ReplyQueueMessage } from "./model";

export type SdkQueueReconciliationAction = "confirm_sent" | "retry";

type QueueAdminOptions = {
  clearIdempotencyKey: (intent: string) => void;
  config: ConsoleConfig;
  effectiveGroupSessionId: string;
  keyFor: (intent: string) => string;
  setActionOutput: (value: string) => void;
};

export function useWxbotQueueAdmin({
  clearIdempotencyKey,
  config,
  effectiveGroupSessionId,
  keyFor,
  setActionOutput,
}: QueueAdminOptions) {
  const [replyQueueItems, setReplyQueueItems] = useState<ReplyQueueMessage[]>([]);
  const [sdkQueueItems, setSdkQueueItems] = useState<ReplyQueueMessage[]>([]);
  const [queueLimit, setQueueLimit] = useState(50);
  const [queueStatusFilter, setQueueStatusFilter] = useState("pending");
  const [sdkQueueStatusFilter, setSdkQueueStatusFilter] = useState("uncertain");
  const [sdkQueueReconcileBusy, setSdkQueueReconcileBusy] = useState("");

  const loadReplyQueueMessages = useCallback(async () => {
    try {
      const result = await apiRequest<{ items?: ReplyQueueMessage[]; count?: number }>(
        config,
        "/plugins/wxbot/admin/reply-queue/messages",
        {
          auth: true,
          query: {
            tenant_id: config.tenantId,
            status: queueStatusFilter,
            session_id: effectiveGroupSessionId,
            limit: queueLimit,
          },
        },
      );
      setReplyQueueItems(result.items || []);
      return true;
    } catch (err) {
      setActionOutput(formatJson({ error: err instanceof Error ? err.message : "读取系统待发送队列失败" }));
      return false;
    }
  }, [config, effectiveGroupSessionId, queueLimit, queueStatusFilter, setActionOutput]);

  const loadSdkQueueMessages = useCallback(async () => {
    try {
      const result = await apiRequest<{ items?: ReplyQueueMessage[]; count?: number }>(
        config,
        "/plugins/wxbot/admin/sdk/queue/messages",
        {
          auth: true,
          query: {
            status: sdkQueueStatusFilter,
            limit: queueLimit,
          },
        },
      );
      setSdkQueueItems(result.items || []);
      return true;
    } catch (err) {
      setActionOutput(formatJson({ error: err instanceof Error ? err.message : "读取 SDK 待发送队列失败" }));
      return false;
    }
  }, [config, queueLimit, sdkQueueStatusFilter, setActionOutput]);

  const reconcileSdkQueueMessage = useCallback(async (
    rowId: number,
    action: SdkQueueReconciliationAction,
  ) => {
    const intent = `wxbot:sdk-queue-reconcile:${rowId}:${action}`;
    const busyKey = `${rowId}:${action}`;
    setSdkQueueReconcileBusy(busyKey);
    try {
      const result = await apiRequest<Record<string, unknown>>(
        config,
        `/plugins/wxbot/admin/sdk/queue/messages/${rowId}/reconcile`,
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({ action }),
          },
        },
      );
      const refreshed = await loadSdkQueueMessages();
      if (!refreshed) {
        throw new Error("对账请求已提交，但队列刷新失败；请使用同一操作重试以确认最终状态");
      }
      clearIdempotencyKey(intent);
      setActionOutput(formatJson({
        ...result,
        operator_notice: action === "confirm_sent"
          ? "已人工确认发送成功，不会再次发送"
          : "已退回待发送；仅应在确认此前未发送成功后执行",
      }));
      return result;
    } catch (err) {
      setActionOutput(formatJson({
        error: err instanceof Error ? err.message : "SDK 队列对账失败",
        row_id: rowId,
        action,
        recovery: "确认微信客户端实际发送结果后，使用同一操作重试",
      }));
      throw err instanceof Error ? err : new Error("SDK 队列对账失败");
    } finally {
      setSdkQueueReconcileBusy("");
    }
  }, [clearIdempotencyKey, config, keyFor, loadSdkQueueMessages, setActionOutput]);

  useEffect(() => {
    if (!config.adminToken) return;
    void loadReplyQueueMessages();
    void loadSdkQueueMessages();
  }, [config.adminToken, loadReplyQueueMessages, loadSdkQueueMessages]);

  return {
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
  } as const;
}

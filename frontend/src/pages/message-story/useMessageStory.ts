import { useCallback, useEffect, useRef, useState } from "react";

import { apiRequest, type ParticipationEventPage } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";
import type { TraceReplyQueueItem, TraceStreamMessage } from "../plugins/models";
import {
  deriveMessageStory,
  type MessageStoryModel,
  type StoryParticipationEvent,
} from "./model";

export type MessageStoryLoadState = {
  loading: boolean;
  story: MessageStoryModel | null;
  errors: string[];
  sessionId: string;
};

const emptyStoryState = (): MessageStoryLoadState => ({
  loading: false,
  story: null,
  errors: [],
  sessionId: "",
});

export function useMessageStory(traceId: string) {
  const { config } = useConsoleConfig();
  const configRef = useRef(config);
  configRef.current = config;
  const [state, setState] = useState<MessageStoryLoadState>(emptyStoryState);

  const load = useCallback(async () => {
    const currentConfig = configRef.current;
    const normalizedTraceId = traceId.trim();
    if (!normalizedTraceId || !currentConfig.adminToken) {
      setState(emptyStoryState());
      return;
    }
    setState((current) => ({ ...current, loading: true, errors: [] }));
    const errors: string[] = [];
    const settle = async <T,>(label: string, task: Promise<T>, fallback: T) => {
      try {
        return await task;
      } catch (error) {
        errors.push(`${label}：${error instanceof Error ? error.message : String(error)}`);
        return fallback;
      }
    };

    const [inboundResult, outboundResult, replyQueueResult] = await Promise.all([
      settle(
        "入站消息",
        apiRequest<{ items?: TraceStreamMessage[] }>(currentConfig, "/v1/admin/streams/recent-messages", {
          auth: true,
          query: { stream: "inbound", limit: 20, trace_id: normalizedTraceId, include_media_events: false },
        }),
        { items: [] },
      ),
      settle(
        "出站消息",
        apiRequest<{ items?: TraceStreamMessage[] }>(currentConfig, "/v1/admin/streams/recent-messages", {
          auth: true,
          query: { stream: "outbound", limit: 20, trace_id: normalizedTraceId, include_media_events: false },
        }),
        { items: [] },
      ),
      settle(
        "回复队列",
        apiRequest<{ items?: TraceReplyQueueItem[] }>(currentConfig, "/plugins/wxbot/admin/reply-queue/messages", {
          auth: true,
          query: { tenant_id: currentConfig.tenantId, trace_id: normalizedTraceId, limit: 20 },
        }),
        { items: [] },
      ),
    ]);

    const inbound = inboundResult.items || [];
    const outbound = outboundResult.items || [];
    const replyQueue = replyQueueResult.items || [];
    const sessionId = String(
      inbound[0]?.session_id
      || replyQueue[0]?.session_id
      || outbound[0]?.session_id
      || currentConfig.sessionId
      || "",
    ).trim();

    let events: StoryParticipationEvent[] = [];
    if (sessionId) {
      const eventsResult = await settle(
        "参与决策",
        apiRequest<ParticipationEventPage>(
          currentConfig,
          `/v1/admin/tenants/${encodeURIComponent(currentConfig.tenantId)}/groups/${encodeURIComponent(sessionId)}/participation-events`,
          { auth: true, query: { limit: 20, trace_id: normalizedTraceId, source: "runtime" } },
        ),
        { items: [], next_before: null },
      );
      events = eventsResult.items || [];
    }

    setState({
      loading: false,
      sessionId,
      errors,
      story: deriveMessageStory({ inbound, outbound, replyQueue, events }),
    });
  }, [
    config.adminToken,
    config.apiBaseUrl,
    config.sessionId,
    config.tenantId,
    traceId,
  ]);

  useEffect(() => {
    void load();
  }, [load]);

  return { ...state, refresh: load };
}

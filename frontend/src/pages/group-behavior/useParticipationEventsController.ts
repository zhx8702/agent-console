import { useState } from "react";

import {
  apiRequest,
  createMessageId,
  type ParticipationDecisionDocument,
  type ParticipationEventDocument,
  type ParticipationEventPage,
  type ParticipationPreviewRequest,
} from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import type { ParticipationEventFilters } from "./ParticipationEventsPanel";

export type RuntimeParticipationEvent = ParticipationEventDocument & {
  runtime_stage?: string;
  delivery_stage?: string;
};

const EMPTY_EVENT_FILTERS: ParticipationEventFilters = {
  source: "",
  status: "",
  version: "",
  reason: "",
  runtimeStage: "",
  deliveryStage: "",
};

type ParticipationEventsControllerOptions = {
  config: ConsoleConfig;
  selectedGroupForWrite: () => string;
};

export function useParticipationEventsController({
  config,
  selectedGroupForWrite,
}: ParticipationEventsControllerOptions) {
  const [events, setEvents] = useState<RuntimeParticipationEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsLoadingMore, setEventsLoadingMore] = useState(false);
  const [eventsError, setEventsError] = useState("");
  const [eventsNextCursor, setEventsNextCursor] = useState<string | null>(null);
  const [eventFilters, setEventFilters] = useState<ParticipationEventFilters>(
    EMPTY_EVENT_FILTERS,
  );

  const eventsPath = (sessionId: string) =>
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(sessionId)}/participation-events`;
  const previewPath = (sessionId: string) =>
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(sessionId)}/participation-preview`;

  const resetEvents = () => {
    setEvents([]);
    setEventsError("");
    setEventsNextCursor(null);
  };

  const loadEvents = async (
    sessionId: string,
    options: {
      cursor?: string;
      append?: boolean;
      filters?: ParticipationEventFilters;
    } = {},
  ) => {
    const append = Boolean(options.append);
    append ? setEventsLoadingMore(true) : setEventsLoading(true);
    setEventsError("");
    try {
      const filters = options.filters || eventFilters;
      const result = await apiRequest<ParticipationEventPage & { next_cursor?: string | null }>(
        config,
        eventsPath(sessionId),
        {
          auth: true,
          query: {
            limit: 50,
            cursor: options.cursor,
            source: filters.source || undefined,
            status: filters.status || undefined,
            version: filters.version.trim() || undefined,
            reason: filters.reason || undefined,
            runtime_stage: filters.runtimeStage || undefined,
            delivery_stage: filters.deliveryStage || undefined,
          },
        },
      );
      const incoming = result.items || [];
      setEvents((current) => {
        if (!append) return incoming;
        const knownIds = new Set(current.map((item) => item.event_id));
        return [...current, ...incoming.filter((item) => !knownIds.has(item.event_id))];
      });
      setEventsNextCursor(result.next_cursor || null);
    } catch (caught) {
      if (!append) resetEvents();
      setEventsError(caught instanceof Error ? caught.message : "参与事件加载失败");
    } finally {
      append ? setEventsLoadingMore(false) : setEventsLoading(false);
    }
  };

  const runPreview = async (
    preview: ParticipationPreviewRequest,
  ): Promise<ParticipationDecisionDocument> => {
    const sessionId = selectedGroupForWrite();
    const result = await apiRequest<ParticipationDecisionDocument>(
      config,
      previewPath(sessionId),
      {
        auth: true,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...preview,
            message_id: createMessageId("participation-preview"),
          }),
        },
      },
    );
    void loadEvents(sessionId);
    return result;
  };

  return {
    events,
    eventsLoading,
    eventsLoadingMore,
    eventsError,
    eventsNextCursor,
    eventFilters,
    setEventFilters,
    loadEvents,
    runPreview,
    resetEvents,
  };
}

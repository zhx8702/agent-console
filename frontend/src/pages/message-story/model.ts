import type { ParticipationDecisionStatus, ParticipationEventDocument } from "../../lib/api";
import { DECISION_LABELS, reasonLabel } from "../group-behavior/presentation";
import { wxbotQueueStatusLabel } from "../wxbot/model";

export type StoryStreamMessage = {
  id: string;
  tenant_id?: string | null;
  session_id?: string | null;
  user_id?: string | null;
  channel?: string | null;
  created_ts_ms?: number | null;
  payload?: Record<string, unknown>;
};

export type StoryReplyQueueItem = {
  id?: number;
  session_id?: string;
  session_name?: string;
  sender_name?: string;
  reply_text?: string;
  status?: string;
  error?: string;
  created_at?: string;
  sent_at?: string;
  source_message?: Record<string, unknown>;
  participation_status?: string;
};

export type StoryParticipationEvent = ParticipationEventDocument & {
  runtime_stage?: string;
  delivery_stage?: string;
};

export type MessageStoryDelivery =
  | "sent"
  | "failed"
  | "cancelled"
  | "queued"
  | "outbound"
  | "no_reply"
  | "pending"
  | "unknown";

export type MessageStoryModel = {
  inboundText: string;
  replyText: string;
  sender: string;
  sessionName: string;
  sessionId: string;
  channel: string;
  timeLabel: string;
  decision: ParticipationDecisionStatus | "";
  decisionLabel: string;
  score: number | null;
  reasonText: string;
  delivery: MessageStoryDelivery;
  deliveryLabel: string;
  headline: string;
  timeline: Array<{
    key: string;
    time: string;
    title: string;
    detail: string;
    tone: "ok" | "danger" | "muted" | "info";
  }>;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function readString(record: Record<string, unknown> | null | undefined, key: string) {
  const value = record?.[key];
  return typeof value === "string" ? value.trim() : "";
}

export function extractMessageText(payload?: Record<string, unknown> | null, limit = 4000) {
  const record = payload || {};
  const metadata = asRecord(record.metadata);
  const message = asRecord(record.message);
  const raw = asRecord(metadata?.raw);
  const candidates = [record, message, metadata, raw];
  for (const item of candidates) {
    for (const key of ["content", "text", "msg_text", "message_text", "message_preview", "body"]) {
      const content = readString(item, key);
      if (content) {
        return content.length > limit ? `${content.slice(0, limit)}…` : content;
      }
    }
  }
  const types = [
    readString(message, "type"),
    readString(record, "msg_type"),
    readString(metadata, "msg_type"),
  ].map((value) => value.toLowerCase());
  if (types.includes("image")) {
    return "[图片]";
  }
  if (types.includes("file")) {
    return "[文件]";
  }
  return "";
}

export function extractSender(payload?: Record<string, unknown> | null, fallback = "") {
  const record = payload || {};
  const metadata = asRecord(record.metadata);
  const message = asRecord(record.message);
  const raw = asRecord(metadata?.raw);
  const sender = asRecord(record.sender) || asRecord(raw?.sender);
  return (
    readString(metadata, "sender_name")
    || readString(record, "sender_name")
    || readString(message, "sender_name")
    || readString(raw, "sender_name")
    || readString(sender, "name")
    || readString(metadata, "sender_wxid")
    || readString(record, "sender_wxid")
    || fallback
  );
}

export function extractSessionName(payload?: Record<string, unknown> | null, fallback = "") {
  const record = payload || {};
  const metadata = asRecord(record.metadata);
  const message = asRecord(record.message);
  const raw = asRecord(metadata?.raw);
  const session = asRecord(record.session) || asRecord(raw?.session);
  return (
    readString(metadata, "session_name")
    || readString(record, "session_name")
    || readString(message, "session_name")
    || readString(raw, "session_name")
    || readString(session, "name")
    || fallback
  );
}

function formatClock(value?: number | string | null) {
  if (!value) {
    return "";
  }
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function primaryDecision(events: StoryParticipationEvent[]) {
  return events.find((item) => item.event_kind === "runtime") || events[0] || null;
}

function deliveryFromReply(status?: string): MessageStoryDelivery | null {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "sent") return "sent";
  if (normalized === "failed") return "failed";
  if (normalized === "cancelled") return "cancelled";
  if (normalized === "pending" || normalized === "running" || normalized === "uncertain") {
    return "queued";
  }
  return null;
}

export function deriveMessageStory(input: {
  inbound: StoryStreamMessage[];
  outbound: StoryStreamMessage[];
  replyQueue: StoryReplyQueueItem[];
  events: StoryParticipationEvent[];
}): MessageStoryModel {
  const inbound = input.inbound[0];
  const outbound = input.outbound[0];
  const reply = input.replyQueue[0];
  const decision = primaryDecision(input.events);
  const inboundPayload = inbound?.payload || reply?.source_message || null;
  const inboundText = extractMessageText(inboundPayload);
  const replyText = String(reply?.reply_text || extractMessageText(outbound?.payload) || "").trim();
  const sessionId = inbound?.session_id || reply?.session_id || outbound?.session_id || "";
  const delivery = deliveryFromReply(reply?.status)
    || (outbound ? "outbound" : null)
    || (
      decision
      && (decision.status === "observe_only" || decision.status === "cancel")
        ? "no_reply"
        : null
    )
    || (decision && (decision.status === "must_reply" || decision.status === "may_reply") ? "pending" : "unknown");

  const deliveryLabel = reply?.status
    ? wxbotQueueStatusLabel(reply.status)
    : delivery === "outbound"
      ? "已写入出站流"
      : delivery === "no_reply"
        ? "没有回复"
        : delivery === "pending"
          ? "已决定回复，尚未入队"
          : "还看不到投递结果";

  const decisionLabel = decision ? DECISION_LABELS[decision.status] : "";
  const reasonText = decision?.reason_codes.length
    ? decision.reason_codes.map((code) => reasonLabel(code)).join("；")
    : "";

  const headline = delivery === "sent"
    ? "这条消息已经发出去了"
    : delivery === "failed"
      ? "回复发送失败"
      : delivery === "cancelled"
        ? "回复在发出前被取消"
        : delivery === "queued"
          ? "回复正在排队发送"
          : delivery === "no_reply"
            ? "机器人看到了，但决定不回"
            : delivery === "pending"
              ? "已经决定回复，还没进入发送队列"
              : inboundText
                ? "已收到这条消息，后续步骤还不完整"
                : "还没有拼出完整故事";

  const timeline: MessageStoryModel["timeline"] = [];
  if (inbound || inboundText) {
    timeline.push({
      key: "inbound",
      time: formatClock(inbound?.created_ts_ms),
      title: "收到群消息",
      detail: inboundText || "入站记录没有可读文本",
      tone: "info",
    });
  }
  for (const event of input.events) {
    const stage = event.runtime_stage === "revalidation"
      ? "发送前复核"
      : event.runtime_stage === "delivery"
        ? "投递复核"
        : "参与决策";
    timeline.push({
      key: event.event_id,
      time: formatClock(event.created_at),
      title: `${stage} · ${DECISION_LABELS[event.status]}`,
      detail: event.reason_codes.length
        ? event.reason_codes.map((code) => reasonLabel(code)).join("；")
        : `得分 ${event.score}`,
      tone: event.status === "cancel"
        ? "danger"
        : event.status === "must_reply" || event.status === "may_reply"
          ? "ok"
          : "muted",
    });
  }
  if (reply) {
    timeline.push({
      key: `reply-${reply.id || reply.created_at || "queue"}`,
      time: formatClock(reply.sent_at || reply.created_at),
      title: `回复队列 · ${wxbotQueueStatusLabel(reply.status)}`,
      detail: reply.error || replyText || "队列里没有回复正文",
      tone: reply.status === "failed" || reply.status === "cancelled" ? "danger" : "ok",
    });
  } else if (outbound) {
    timeline.push({
      key: `outbound-${outbound.id}`,
      time: formatClock(outbound.created_ts_ms),
      title: "写入通用出站流",
      detail: replyText || "出站记录没有可读文本",
      tone: "ok",
    });
  }

  return {
    inboundText,
    replyText,
    sender: extractSender(inboundPayload, inbound?.user_id || reply?.sender_name || ""),
    sessionName: extractSessionName(inboundPayload, reply?.session_name || sessionId),
    sessionId,
    channel: inbound?.channel || outbound?.channel || (sessionId.endsWith("@chatroom") ? "wechat" : ""),
    timeLabel: formatClock(inbound?.created_ts_ms || reply?.created_at || decision?.created_at),
    decision: decision?.status || "",
    decisionLabel,
    score: decision ? decision.score : null,
    reasonText,
    delivery,
    deliveryLabel,
    headline,
    timeline,
  };
}

export function deliveryTone(delivery: MessageStoryDelivery): "ok" | "danger" | "warning" | "muted" {
  if (delivery === "sent" || delivery === "outbound") return "ok";
  if (delivery === "failed" || delivery === "cancelled") return "danger";
  if (delivery === "queued" || delivery === "pending") return "warning";
  return "muted";
}

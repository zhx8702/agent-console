import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  AuthenticatedImage,
  sdkImageDisplayPath,
  sdkImageProxyPath,
} from "../components/AuthenticatedImage";
import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { StatusTile } from "../components/StatusTile";
import { apiRequest, formatJson } from "../lib/api";
import { useConsoleConfig } from "../state/console-config";

type StreamGroup = {
  name: string;
  consumers: number;
  pending: number;
  last_delivered_id?: string | null;
  lag?: number | null;
  entries_read?: number | null;
};

type StreamSummary = {
  stream_key: string;
  stream: string;
  length: number;
  first_entry?: string | null;
  last_entry?: string | null;
  pending_total: number;
  groups: StreamGroup[];
};

type StreamMessage = {
  id: string;
  source?: string | null;
  stream_key: string;
  stream: string;
  tenant_id?: string | null;
  session_id?: string | null;
  user_id?: string | null;
  trace_id?: string | null;
  channel?: string | null;
  attempts: number;
  reason?: string | null;
  origin_stream?: string | null;
  origin_id?: string | null;
  created_ts_ms?: number | null;
  payload: Record<string, unknown>;
  headers: Record<string, unknown>;
};

type QueueImage = {
  previewUrl: string;
  thumbnailUrl: string;
  label: "图片" | "引用";
};

type QueueQuote = {
  text: string;
  sender: string;
  messageId: string;
};

const AUTO_REFRESH_INTERVAL_MS = 5_000;

function formatTime(value?: number | null) {
  if (!value) {
    return "-";
  }
  try {
    return new Date(value).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return String(value);
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function readString(record: Record<string, unknown> | null, key: string) {
  const value = record?.[key];
  return typeof value === "string" ? value.trim() : "";
}

function variantRecord(record: Record<string, unknown> | null, name: "preview" | "thumbnail") {
  const imageVariants = asRecord(record?.["image_variants"]);
  const variants = asRecord(record?.["variants"]);
  return (
    asRecord(imageVariants?.[name]) ||
    asRecord(variants?.[name]) ||
    asRecord(record?.[name])
  );
}

function mediaSource(record: Record<string, unknown> | null, key: string) {
  const mediaId = readString(record, key);
  return /^mid1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(mediaId) ? `media:${mediaId}` : "";
}

function variantMedia(record: Record<string, unknown> | null, name: "preview" | "thumbnail") {
  const variant = variantRecord(record, name);
  return mediaSource(variant, "media_id");
}

function firstPreviewUrl(...records: Array<Record<string, unknown> | null>) {
  for (const record of records) {
    const imageUrl =
      variantMedia(record, "preview") ||
      mediaSource(record, "preview_media_id");
    if (imageUrl) {
      return imageUrl;
    }
  }
  return "";
}

function firstThumbnailUrl(...records: Array<Record<string, unknown> | null>) {
  for (const record of records) {
    const imageUrl =
      variantMedia(record, "thumbnail") ||
      mediaSource(record, "thumbnail_media_id");
    if (imageUrl) {
      return imageUrl;
    }
  }
  return "";
}

function firstGenericImageUrl(...records: Array<Record<string, unknown> | null>) {
  for (const record of records) {
    const imageUrl = mediaSource(record, "media_id");
    if (imageUrl) {
      return imageUrl;
    }
  }
  return "";
}

function queueImage(
  label: QueueImage["label"],
  ...records: Array<Record<string, unknown> | null>
): QueueImage | null {
  const rawPreviewUrl = firstPreviewUrl(...records);
  const rawThumbnailUrl = firstThumbnailUrl(...records);
  const genericUrl = firstGenericImageUrl(...records);
  const previewUrl = rawPreviewUrl || genericUrl || rawThumbnailUrl;
  const thumbnailUrl = rawThumbnailUrl || genericUrl || rawPreviewUrl;
  if (!previewUrl && !thumbnailUrl) {
    return null;
  }
  return {
    label,
    previewUrl: previewUrl || thumbnailUrl,
    thumbnailUrl: thumbnailUrl || previewUrl,
  };
}

function quoteRecords(payload: Record<string, unknown>) {
  const metadata = asRecord(payload.metadata);
  const message = asRecord(payload.message);
  const quote = asRecord(metadata?.quote) || asRecord(message?.quote) || asRecord(payload.quote);
  return [
    quote,
    asRecord(quote?.message),
    asRecord(quote?.media),
    asRecord(quote?.quoted_message),
    asRecord(quote?.quoted),
    asRecord(quote?.raw),
  ];
}

function firstRecordString(
  records: Array<Record<string, unknown> | null>,
  keys: string[],
) {
  for (const record of records) {
    for (const key of keys) {
      const value = readString(record, key);
      if (value) {
        return value;
      }
    }
  }
  return "";
}

function extractQueueQuote(payload: Record<string, unknown>): QueueQuote | null {
  const metadata = asRecord(payload.metadata);
  const records = quoteRecords(payload);
  const text =
    readString(metadata, "quote_text") ||
    firstRecordString(records, [
      "text",
      "content",
      "message_text",
      "msg_text",
      "msg",
      "body",
      "caption",
    ]);
  const senderRecord = asRecord(records[0]?.sender);
  const sender =
    firstRecordString(records, [
      "sender_name",
      "from_name",
      "nickname",
      "display_name",
      "sender_wxid",
      "from_wxid",
    ]) ||
    readString(senderRecord, "name") ||
    readString(senderRecord, "id");
  const messageId = firstRecordString(records, [
    "message_id",
    "msg_svr_id",
    "msg_id",
    "id",
  ]);
  return text || sender || messageId ? { text, sender, messageId } : null;
}

function extractQueueImages(payload: Record<string, unknown>) {
  const images: QueueImage[] = [];
  const metadata = asRecord(payload.metadata);
  const message = asRecord(payload.message);
  const media = asRecord(metadata?.media);
  const payloadMedia = asRecord(payload.media);
  const raw = asRecord(metadata?.raw);

  const directImage = queueImage("图片", payload, metadata, media, message, payloadMedia, raw);
  if (directImage) {
    images.push(directImage);
  }

  if (message) {
    const attachments = message.attachments;
    if (Array.isArray(attachments)) {
      for (const item of attachments) {
        const image = queueImage("图片", asRecord(item));
        if (image) {
          images.push(image);
        }
      }
    }
  }

  const segments = payload.segments;
  if (Array.isArray(segments)) {
    for (const item of segments) {
      if (!item || typeof item !== "object" || Array.isArray(item)) {
        continue;
      }
      const image = queueImage("图片", asRecord((item as Record<string, unknown>).metadata));
      if (image) {
        images.push(image);
      }
    }
  }

  const quoteImage = queueImage(
    "引用",
    {
      preview_media_id: readString(metadata, "quote_preview_media_id"),
      thumbnail_media_id: readString(metadata, "quote_thumbnail_media_id"),
      media_id: readString(metadata, "quote_media_id"),
    },
    ...quoteRecords(payload),
  );
  if (quoteImage) {
    images.push(quoteImage);
  }

  const deduped: QueueImage[] = [];
  const seen = new Set<string>();
  for (const image of images) {
    const key = `${image.label}:${image.previewUrl}:${image.thumbnailUrl}`;
    if ((!image.previewUrl && !image.thumbnailUrl) || seen.has(key)) {
      continue;
    }
    seen.add(key);
    deduped.push(image);
  }
  return deduped;
}

function hasQuotedImage(payload: Record<string, unknown>) {
  return extractQueueImages(payload).some((item) => item.label === "引用");
}

function hasDirectImage(payload: Record<string, unknown>) {
  return extractQueueImages(payload).some((item) => item.label === "图片");
}

function mediaStatus(payload: Record<string, unknown>) {
  const metadata = asRecord(payload.metadata);
  const media = asRecord(metadata?.media);
  return readString(metadata, "media_status") || readString(media, "status");
}

function mediaVariant(payload: Record<string, unknown>) {
  const metadata = asRecord(payload.metadata);
  const media = asRecord(metadata?.media);
  return readString(media, "variant");
}

function imageBadgeText(payload: Record<string, unknown>, label: QueueImage["label"]) {
  if (readString(payload, "admin_event_source") === "media_event") {
    return "图片";
  }
  if (label === "引用") {
    return "引用";
  }
  const status = mediaStatus(payload);
  const variant = mediaVariant(payload);
  if (status === "thumbnail" || variant === "thumbnail") {
    return "缩略";
  }
  if (status === "pending") {
    return "等待";
  }
  if (status === "failed") {
    return "失败";
  }
  return "图片";
}

function formatMessagePreview(payload: Record<string, unknown>) {
  const metadata = asRecord(payload.metadata);
  const message = asRecord(payload.message);
  const raw = asRecord(metadata?.raw);
  const candidates = [payload, message, metadata, raw];
  const contentKeys = ["content", "text", "msg_text", "message_text", "message_preview", "body"];
  for (const record of candidates) {
    for (const key of contentKeys) {
      const content = readString(record, key);
      if (content) {
        const suffix = hasQuotedImage(payload) ? " [引用图片]" : "";
        return `${content.slice(0, 160)}${suffix}`;
      }
    }
  }
  if (hasQuotedImage(payload)) {
    return "[引用图片]";
  }
  const payloadMedia = asRecord(payload.media);
  const metadataMedia = asRecord(metadata?.media);
  const imageTypes = [
    readString(message, "type"),
    readString(payload, "msg_type"),
    readString(metadata, "msg_type"),
    readString(payloadMedia, "type"),
    readString(metadataMedia, "type"),
  ];
  if (imageTypes.some((value) => value.toLowerCase() === "image")) {
    return "[图片]";
  }
  const replyId = payload.reply_id;
  if (typeof replyId === "string" && replyId.trim()) {
    return `reply_id: ${replyId}`;
  }
  const route = payload.route;
  if (typeof route === "string" && route.trim()) {
    return `route: ${route}`;
  }
  if (hasDirectImage(payload)) {
    return "[图片]";
  }
  return "";
}

function messageSender(item: StreamMessage) {
  const payload = item.payload;
  const metadata = asRecord(payload.metadata);
  const message = asRecord(payload.message);
  const raw = asRecord(metadata?.raw);
  const sender = asRecord(payload.sender) || asRecord(raw?.sender);
  return (
    readString(metadata, "sender_name") ||
    readString(payload, "sender_name") ||
    readString(message, "sender_name") ||
    readString(raw, "sender_name") ||
    readString(sender, "name") ||
    readString(metadata, "sender_wxid") ||
    readString(payload, "sender_wxid") ||
    readString(message, "sender_wxid") ||
    readString(raw, "sender_wxid") ||
    readString(sender, "id") ||
    String(item.user_id || "").trim()
  );
}

function messageSession(item: StreamMessage) {
  const payload = item.payload;
  const metadata = asRecord(payload.metadata);
  const message = asRecord(payload.message);
  const raw = asRecord(metadata?.raw);
  const session = asRecord(payload.session) || asRecord(raw?.session);
  return (
    readString(metadata, "session_name") ||
    readString(payload, "session_name") ||
    readString(message, "session_name") ||
    readString(raw, "session_name") ||
    readString(session, "name") ||
    String(item.session_id || "").trim()
  );
}

function streamMessageKey(item: StreamMessage) {
  return `${item.stream_key}:${item.id}`;
}

function streamLabel(value?: string | null) {
  const labels: Record<string, string> = {
    inbound: "入站消息",
    outbound: "出站消息",
    dlq: "失败消息",
    media_events: "媒体事件",
  };
  return value ? labels[value] || "其他消息流" : "-";
}

export function MessageQueuesPage() {
  const { config } = useConsoleConfig();
  const [summary, setSummary] = useState<StreamSummary[]>([]);
  const [items, setItems] = useState<StreamMessage[]>([]);
  const [selectedMessage, setSelectedMessage] = useState<StreamMessage | null>(null);
  const [stream, setStream] = useState("inbound");
  const [limit, setLimit] = useState(50);
  const [pageCursor, setPageCursor] = useState("");
  const [nextBeforeId, setNextBeforeId] = useState("");
  const [tenantFilter, setTenantFilter] = useState("");
  const [sessionFilter, setSessionFilter] = useState("");
  const [traceFilter, setTraceFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<number | null>(null);
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');
  const loadingRef = useRef(false);

  const effectiveTenantFilter = useMemo(
    () => tenantFilter.trim() || config.tenantId.trim(),
    [tenantFilter, config.tenantId],
  );

  const loadSummary = useCallback(async () => {
    const result = await apiRequest<{ streams: StreamSummary[] }>(config, "/v1/admin/streams/summary", {
      auth: true,
    });
    setSummary(result.streams || []);
    return result;
  }, [config]);

  const loadMessages = useCallback(async (requestCursor = "") => {
    const result = await apiRequest<{ items: StreamMessage[]; next_before_id?: string | null }>(
      config,
      "/v1/admin/streams/recent-messages",
      {
        auth: true,
        query: {
          stream,
          limit,
          before_id: requestCursor,
          tenant_id: effectiveTenantFilter,
          session_id: sessionFilter.trim(),
          trace_id: traceFilter.trim(),
        },
      },
    );
    const nextItems = result.items || [];
    setItems(nextItems);
    setSelectedMessage((current) => {
      if (!current) {
        return nextItems[0] || null;
      }
      const currentKey = streamMessageKey(current);
      return nextItems.find((item) => streamMessageKey(item) === currentKey) || nextItems[0] || null;
    });
    setPageCursor(requestCursor);
    setNextBeforeId(String(result.next_before_id || ""));
    setLastRefreshedAt(Date.now());
    return result;
  }, [config, effectiveTenantFilter, limit, sessionFilter, stream, traceFilter]);

  const refreshAll = useCallback(async (background = false) => {
    if (loadingRef.current) {
      return;
    }
    loadingRef.current = true;
    if (!background) {
      setLoading(true);
    }
    try {
      const [summaryResult, messageResult] = await Promise.all([loadSummary(), loadMessages("")]);
      setOutput(
        formatJson({
          summary: summaryResult,
          messages: messageResult,
        }),
      );
    } catch (err) {
      setOutput(formatJson({ error: err instanceof Error ? err.message : "消息队列读取失败" }));
    } finally {
      loadingRef.current = false;
      if (!background) {
        setLoading(false);
      }
    }
  }, [loadMessages, loadSummary]);

  const loadOlder = async () => {
    if (!nextBeforeId || loadingRef.current) {
      return;
    }
    loadingRef.current = true;
    setLoading(true);
    setAutoRefresh(false);
    try {
      const result = await loadMessages(nextBeforeId);
      setOutput(formatJson(result));
    } catch (err) {
      setOutput(formatJson({ error: err instanceof Error ? err.message : "历史消息读取失败" }));
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  };

  useEffect(() => {
    setPageCursor("");
    setNextBeforeId("");
    setAutoRefresh(true);
    void refreshAll();
  }, [config.apiBaseUrl, config.adminToken, config.tenantId, stream]);

  useEffect(() => {
    if (!autoRefresh || pageCursor) {
      return undefined;
    }
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") {
        void refreshAll(true);
      }
    };
    const timer = window.setInterval(refreshWhenVisible, AUTO_REFRESH_INTERVAL_MS);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [autoRefresh, pageCursor, refreshAll]);

  const currentSummary = summary.find((item) => item.stream_key === stream) || null;
  const selectedImages = selectedMessage ? extractQueueImages(selectedMessage.payload) : [];
  const selectedDirectImages = selectedImages.filter((image) => image.label === "图片");
  const selectedQuoteImages = selectedImages.filter((image) => image.label === "引用");
  const selectedQuote = selectedMessage ? extractQueueQuote(selectedMessage.payload) : null;
  const selectedPreview = selectedMessage ? formatMessagePreview(selectedMessage.payload) : "";
  const refreshStatus = pageCursor
    ? "正在浏览历史消息，自动刷新已暂停"
    : autoRefresh
      ? "自动刷新已开启 · 每 5 秒检查新消息"
      : "自动刷新已暂停";

  return (
    <div className="page-grid queue-page">
      <section className="panel panel-hero span-2">
        <PageHeader
          eyebrow="消息总线"
          title="全局消息队列"
          description="统一查看全局入站、出站和死信流，确认消息有没有真正进队、出队，以及消费者是否积压。"
        />
        <form
          className="form-grid"
          onSubmit={(event) => {
            event.preventDefault();
            void refreshAll();
          }}
        >
          <label className="field">
            <span>消息流</span>
            <select
              value={stream}
              onChange={(event) => {
                setStream(event.target.value);
                setPageCursor("");
                setNextBeforeId("");
                setAutoRefresh(true);
              }}
            >
              <option value="inbound">入站消息</option>
              <option value="outbound">出站消息</option>
              <option value="dlq">失败消息</option>
            </select>
          </label>
          <label className="field">
            <span>返回上限</span>
            <input type="number" min={1} max={200} value={limit} onChange={(event) => setLimit(Number(event.target.value) || 50)} />
          </label>
          <label className="field">
            <span>租户标识</span>
            <input value={tenantFilter} onChange={(event) => setTenantFilter(event.target.value)} placeholder={config.tenantId || "default"} />
          </label>
          <label className="field">
            <span>群聊或会话标识</span>
            <input value={sessionFilter} onChange={(event) => setSessionFilter(event.target.value)} placeholder="可选" />
          </label>
          <label className="field">
            <span>追踪标识</span>
            <input value={traceFilter} onChange={(event) => setTraceFilter(event.target.value)} placeholder="可选" />
          </label>
        </form>
        <div className="queue-refresh-toolbar">
          <div className="action-row">
            <button className="button button-primary" type="button" onClick={() => void refreshAll()} disabled={loading}>
              {loading ? "刷新中..." : "立即刷新"}
            </button>
            {pageCursor && (
              <button
                className="button button-secondary"
                type="button"
                onClick={() => {
                  setPageCursor("");
                  setAutoRefresh(true);
                  void refreshAll();
                }}
                disabled={loading}
              >
                回到最新
              </button>
            )}
          </div>
          <label className="queue-auto-refresh-toggle">
            <input
              type="checkbox"
              checked={autoRefresh && !pageCursor}
              disabled={Boolean(pageCursor)}
              onChange={(event) => setAutoRefresh(event.target.checked)}
            />
            <span>自动刷新</span>
          </label>
        </div>
        <div className="queue-live-status" role="status" aria-live="polite">
          <span className={`queue-live-dot${autoRefresh && !pageCursor ? " active" : ""}`} aria-hidden="true" />
          <span>{refreshStatus}</span>
          <span className="queue-live-time">
            {lastRefreshedAt ? `最近更新 ${formatTime(lastRefreshedAt)}` : "等待首次读取"}
          </span>
        </div>
        <div className="queue-stats-grid">
          <StatusTile label="当前流" value={streamLabel(currentSummary?.stream_key || stream)} />
          <StatusTile label="消息总数" value={String(currentSummary?.length ?? 0)} />
          <StatusTile label="待确认" value={String(currentSummary?.pending_total ?? 0)} />
        </div>
      </section>

      <section className="panel panel-scroll">
        <div className="panel-header">
          <div>
            <p className="section-kicker">队列流</p>
            <h3>流摘要</h3>
          </div>
        </div>
        <div className="queue-summary-list">
          {summary.map((item) => (
            <button
              key={item.stream_key}
              type="button"
              className={`queue-summary-card${item.stream_key === stream ? " active" : ""}`}
              onClick={() => setStream(item.stream_key)}
            >
              <div className="queue-summary-card-top">
                <strong>{streamLabel(item.stream_key)}</strong>
                <span>消息流摘要</span>
              </div>
              <div className="queue-summary-card-stats">
                <span>总量 {item.length}</span>
                <span>积压 {item.pending_total}</span>
                <span>组 {item.groups.length}</span>
              </div>
            </button>
          ))}
          {!summary.length && <p className="muted-copy">暂无队列摘要</p>}
        </div>
        {!!currentSummary?.groups?.length && (
          <div className="table-wrap compact-table-scroll u-mt-4">
            <table>
              <caption className="sr-only">当前消息流消费组摘要</caption>
              <thead>
                <tr>
                  <th scope="col">消费组</th>
                  <th scope="col">消费者</th>
                  <th scope="col">待处理</th>
                  <th scope="col">延迟</th>
                  <th scope="col">投递状态</th>
                </tr>
              </thead>
              <tbody>
                {currentSummary.groups.map((group) => (
                  <tr key={group.name}>
                    <td>消费组</td>
                    <td>{group.consumers}</td>
                    <td>{group.pending}</td>
                    <td>{group.lag ?? "-"}</td>
                    <td>{group.last_delivered_id ? "已有投递" : "暂无投递"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel span-3 queue-browser-panel">
        <div className="panel-header queue-browser-header">
          <div>
            <p className="section-kicker">消息浏览器</p>
            <h3>{pageCursor ? "历史消息" : "最近消息"}</h3>
            <p className="muted-copy">最新一条会自动选中；点击任意消息卡片即可在右侧直接查看内容。</p>
          </div>
          <div className="action-row">
            {pageCursor && <span className="pill pill-muted">历史页</span>}
            <button
              type="button"
              className="button button-secondary"
              onClick={() => void loadOlder()}
              disabled={loading || !nextBeforeId}
            >
              {nextBeforeId ? "加载更早消息" : "没有更早消息"}
            </button>
          </div>
        </div>

        <div className="queue-browser-layout">
          <div className="queue-message-feed" role="list" aria-label="当前消息流最近消息">
            {items.map((item, index) => {
              const images = extractQueueImages(item.payload);
              const quote = extractQueueQuote(item.payload);
              const quoteImage = images.some((image) => image.label === "引用");
              const preview = formatMessagePreview(item.payload);
              const sender = messageSender(item);
              const sessionName = messageSession(item);
              const selected = Boolean(
                selectedMessage && streamMessageKey(selectedMessage) === streamMessageKey(item),
              );
              const messageLabel = !pageCursor && index === 0 ? "最新" : `#${String(index + 1).padStart(2, "0")}`;
              return (
                <article className="queue-message-feed-item" role="listitem" key={streamMessageKey(item)}>
                  <button
                    type="button"
                    className={`queue-message-card${selected ? " active" : ""}`}
                    aria-pressed={selected}
                    aria-controls="queue-message-inspector"
                    aria-label={`选择消息 ${index + 1}：${preview || item.id}${sender ? `，发送人 ${sender}` : ""}`}
                    onClick={() => setSelectedMessage(item)}
                  >
                    <span className="queue-message-card-rail" aria-hidden="true" />
                    <span className="queue-message-card-copy">
                      <span className="queue-message-card-topline">
                        <span className={`queue-message-sequence${!pageCursor && index === 0 ? " latest" : ""}`}>
                          {messageLabel}
                        </span>
                        <time>{formatTime(item.created_ts_ms)}</time>
                        <span className="queue-message-channel">{item.channel || streamLabel(item.stream_key)}</span>
                      </span>
                      <span className="queue-message-card-preview">{preview || "这条消息没有文本摘要"}</span>
                      {(quote || quoteImage) && (
                        <span className="queue-message-card-quote">
                          <span>{quote?.sender ? `引用 · ${quote.sender}` : "引用"}</span>
                          <span>{quote?.text || (quoteImage ? "[图片]" : "原消息内容未随事件返回")}</span>
                        </span>
                      )}
                      <span className="queue-message-card-meta">
                        {sender && <span>发送人 {sender}</span>}
                        {sessionName && <span>会话 {sessionName}</span>}
                        {item.tenant_id && <span>租户已标记</span>}
                        {item.trace_id && <span>可追踪</span>}
                        {item.attempts > 0 && <span>尝试 {item.attempts} 次</span>}
                      </span>
                    </span>
                    {!!images.length && (
                      <span className="queue-message-card-media" aria-label={`${images.length} 张关联图片`}>
                        {images.slice(0, 2).map((image) => (
                          <span className="queue-image-thumb-wrap" key={`${image.label}:${image.previewUrl}:${image.thumbnailUrl}`}>
                            <AuthenticatedImage
                              className="queue-image-thumb"
                              source={image.thumbnailUrl || image.previewUrl}
                              alt={`${image.label}预览`}
                              loading="lazy"
                            />
                            <span className="queue-image-badge">{imageBadgeText(item.payload, image.label)}</span>
                          </span>
                        ))}
                      </span>
                    )}
                  </button>
                </article>
              );
            })}
            {!items.length && (
              <div className="queue-message-empty" role="listitem">
                <strong>当前筛选下暂无消息</strong>
                <span>保持自动刷新开启，新消息到达后会直接显示在这里。</span>
              </div>
            )}
          </div>

          <aside className="queue-message-inspector" id="queue-message-inspector" aria-live="polite">
            <div className="queue-message-inspector-heading">
              <div>
                <p className="section-kicker">当前选中</p>
                <h3>消息详情</h3>
              </div>
              {selectedMessage?.trace_id && (
                <Link
                  className="link-button queue-trace-link"
                  to={`/plugins?trace_id=${encodeURIComponent(selectedMessage.trace_id)}`}
                >
                  查看追踪
                </Link>
              )}
            </div>
            {selectedMessage ? (
              <>
                <div className="queue-message-readable">
                  <p className="queue-message-readable-copy">
                    {selectedPreview || "这条消息没有可读文本，可在下方查看完整数据。"}
                  </p>
                  <dl className="queue-message-facts">
                    <div><dt>时间</dt><dd>{formatTime(selectedMessage.created_ts_ms)}</dd></div>
                    <div><dt>渠道</dt><dd>{selectedMessage.channel || "-"}</dd></div>
                    <div><dt>发送人</dt><dd>{messageSender(selectedMessage) || "-"}</dd></div>
                    <div><dt>会话</dt><dd>{messageSession(selectedMessage) || "-"}</dd></div>
                    <div><dt>消息流</dt><dd>{streamLabel(selectedMessage.stream_key)}</dd></div>
                    <div><dt>尝试</dt><dd>{selectedMessage.attempts}</dd></div>
                  </dl>
                </div>
                {!!selectedDirectImages.length && (
                  <div className="queue-image-preview-wrap">
                    {selectedDirectImages.map((image) => (
                      <div className="queue-image-preview-item" key={`${image.label}:${image.previewUrl}`}>
                        <div className="queue-image-preview-title">消息图片</div>
                        <AuthenticatedImage
                          className="queue-image-preview"
                          source={image.previewUrl || image.thumbnailUrl}
                          alt="消息图片预览"
                        />
                        <p className="muted-copy mono">
                          {sdkImageProxyPath(image.previewUrl || image.thumbnailUrl)
                            ? "通过智能体控制台安全代理加载"
                            : "外部图片地址"}
                          {" · "}
                          {sdkImageDisplayPath(image.previewUrl || image.thumbnailUrl)}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
                {(selectedQuote || selectedQuoteImages.length > 0) && (
                  <section className="queue-quote-preview" aria-label="引用的原消息">
                    <div className="queue-quote-preview-heading">
                      <strong>引用的原消息</strong>
                      {selectedQuote?.sender && <span>{selectedQuote.sender}</span>}
                    </div>
                    <blockquote>
                      {selectedQuote?.text ||
                        (selectedQuoteImages.length > 0 ? "[图片]" : "原消息内容未随事件返回")}
                    </blockquote>
                    {selectedQuote?.messageId && (
                      <p className="muted-copy mono">原消息标识 {selectedQuote.messageId}</p>
                    )}
                    {selectedQuoteImages.map((image) => (
                      <div className="queue-image-preview-item" key={`${image.label}:${image.previewUrl}`}>
                        <div className="queue-image-preview-title">引用原图</div>
                        <AuthenticatedImage
                          className="queue-image-preview"
                          source={image.previewUrl || image.thumbnailUrl}
                          alt="引用原图预览"
                        />
                        <p className="muted-copy mono">
                          {sdkImageProxyPath(image.previewUrl || image.thumbnailUrl)
                            ? "通过智能体控制台安全代理加载"
                            : "外部图片地址"}
                          {" · "}
                          {sdkImageDisplayPath(image.previewUrl || image.thumbnailUrl)}
                        </p>
                      </div>
                    ))}
                  </section>
                )}
                <details className="queue-message-json-details">
                  <summary>查看完整 JSON</summary>
                  <pre className="code-view queue-message-json" aria-label="消息完整 JSON">
                    {formatJson(selectedMessage)}
                  </pre>
                </details>
              </>
            ) : (
              <p className="muted-copy">新消息到达后，这里会自动显示最新一条的详情。</p>
            )}
          </aside>
        </div>
      </section>

      <div className="span-3">
        <OutputPanel title="最近响应" value={output} />
      </div>
    </div>
  );
}

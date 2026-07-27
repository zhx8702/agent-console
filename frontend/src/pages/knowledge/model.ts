export type WxbotSession = {
  session_id: string;
  session_name: string;
  kind?: string;
};

export type FAQItem = {
  id: number;
  tenant_id: string;
  session_id?: string | null;
  scope: "global" | "session";
  question: string;
  answer: string;
  variants: string[];
  tags: string[];
  version?: number;
  status?: string;
};

export type FAQListResponse = {
  scope: "global" | "session";
  session_id?: string | null;
  items: FAQItem[];
};

export type FAQPreviewResponse = {
  scope: "global" | "session";
  session_id?: string | null;
  matched: boolean;
  query: string;
  reply_text: string;
  score?: number | null;
  threshold?: number | null;
  resolved_scope?: string | null;
  resolved_session_id?: string | null;
  rewritten?: boolean;
  citation?: {
    id: string;
    source: string;
    snippet: string;
    score?: number | null;
  } | null;
};

export type KnowledgeDoc = {
  id: number;
  tenant_id: string;
  session_id?: string | null;
  scope: "global" | "session";
  title: string;
  source: string;
  url?: string | null;
  content_hash?: string;
  content?: string;
  metadata?: Record<string, unknown>;
};

export type KnowledgeDocListResponse = {
  scope: "global" | "session";
  session_id?: string | null;
  items: KnowledgeDoc[];
};

export type KnowledgeDocSearchHit = {
  chunk_id: number;
  doc_id: number;
  title?: string | null;
  content: string;
  score: number;
  session_id?: string | null;
  source?: string | null;
  url?: string | null;
  metadata?: Record<string, unknown>;
};

export type KnowledgeDocSearchResponse = {
  scope: "global" | "session";
  session_id?: string | null;
  query: string;
  items: KnowledgeDocSearchHit[];
};

export type ReportMessagesPayload = {
  session_id: string;
  session_name: string;
  report_type: string;
  period?: string;
  count: number;
  messages: Array<{
    ts?: number;
    timestamp?: string;
    sender_wxid?: string;
    sender_name?: string;
    msg_type?: string;
    text?: string;
    is_self_sent?: boolean;
  }>;
};

export type KnowledgeTab = "faq" | "docs" | "import";
export type FAQImportMode = "paste" | "session";
export type KnowledgeScopeMode = "global" | "session";

export type ChatDraftLine = {
  senderName: string;
  text: string;
  timestamp?: string;
  msgType?: string;
  isSelfSent?: boolean;
};

export type FAQDraftItem = {
  draftId: string;
  selected: boolean;
  question: string;
  answer: string;
  variantsText: string;
  tagsText: string;
  sourceExcerpt: string;
};

const CHAT_IMPORT_PREFIXES = [
  "什么是", "怎么", "如何", "为什么", "为啥", "咋", "可以", "能不能", "是否", "有没有",
  "支持", "谁", "哪", "哪里", "哪个", "多少", "请问",
];

const FAQ_IMPORT_TAG_RULES: Array<{ tag: string; keywords: string[] }> = [
  { tag: "chat-import", keywords: [] },
  { tag: "部署", keywords: ["docker", "部署", "安装", "上线"] },
  { tag: "开发", keywords: ["本地", "开发", "源码", "构建", "调试"] },
  { tag: "配置", keywords: ["配置", ".env", "环境变量", "端点", "api key"] },
  { tag: "向量库", keywords: ["qdrant", "向量", "知识库"] },
  { tag: "缓存", keywords: ["redis", "缓存", "锁"] },
  { tag: "模型", keywords: ["responses", "chat", "模型", "openai", "llm"] },
  { tag: "支付", keywords: ["支付", "充值", "订单", "会员"] },
  { tag: "网关", keywords: ["nginx", "反向代理", "underscores_in_headers"] },
  { tag: "插件", keywords: ["插件", "tool", "hook", "路由"] },
];

export function isGroupSession(session: Pick<WxbotSession, "session_id" | "kind">) {
  return session.session_id.endsWith("@chatroom") || session.kind === "group" || session.kind === "chatroom";
}

export function mergeSessions(sessions: WxbotSession[], rosterGroups: WxbotSession[]) {
  const merged = new Map<string, WxbotSession>();
  for (const item of sessions) {
    if (item.session_id) merged.set(item.session_id, item);
  }
  for (const item of rosterGroups) {
    if (!item.session_id) continue;
    const current = merged.get(item.session_id);
    merged.set(item.session_id, {
      session_id: item.session_id,
      session_name: item.session_name || current?.session_name || item.session_id,
      kind: item.kind || current?.kind || (item.session_id.endsWith("@chatroom") ? "group" : "private"),
    });
  }
  return Array.from(merged.values()).sort((left, right) =>
    (left.session_name || left.session_id).localeCompare(right.session_name || right.session_id, "zh-CN"),
  );
}

export function formatSessionLabel(session: WxbotSession) {
  const prefix = isGroupSession(session) ? "[群]" : "[会话]";
  return `${prefix} ${session.session_name || session.session_id} (${session.session_id})`;
}

export function splitMultivalue(value: string) {
  return value.split(/[\n,]+/g).map((item) => item.trim()).filter(Boolean);
}

export function joinMultivalue(items: string[] | undefined) {
  return (items || []).join("\n");
}

function normalizeChatText(value: string) {
  return value.replace(/^(@[^\s\u2005]+\s*)+/u, "").replace(/\s+/g, " ").trim();
}

function isQuestionText(value: string) {
  const text = normalizeChatText(value);
  if (!text || text.length < 4 || text.startsWith("/")) return false;
  return /[?？]/.test(text) || CHAT_IMPORT_PREFIXES.some((prefix) => text.startsWith(prefix));
}

function buildQuestionVariants(question: string) {
  const cleaned = normalizeChatText(question);
  if (!cleaned) return [];
  const variants = new Set<string>();
  const withoutPunctuation = cleaned.replace(/[?？!！。]+$/g, "").trim();
  if (withoutPunctuation && withoutPunctuation !== cleaned) variants.add(withoutPunctuation);
  const withoutCourtesy = withoutPunctuation.replace(/^(请问|想问下|想请教一下|咨询一下)\s*/u, "").trim();
  if (withoutCourtesy && withoutCourtesy !== withoutPunctuation) variants.add(withoutCourtesy);
  return Array.from(variants);
}

function inferDraftTags(question: string, answer: string) {
  const text = `${question}\n${answer}`.toLowerCase();
  const tags = new Set<string>();
  for (const rule of FAQ_IMPORT_TAG_RULES) {
    if (!rule.keywords.length || rule.keywords.some((keyword) => text.includes(keyword))) tags.add(rule.tag);
  }
  return Array.from(tags);
}

export function parsePastedChatLines(raw: string): ChatDraftLine[] {
  return raw.split(/\r?\n/g).map((line) => line.trim()).filter(Boolean).map((line) => {
    const matched = line.match(/^(?:\[[^\]]+\]\s*)?([^:：]{1,40})[:：]\s*(.+)$/u)
      || line.match(/^(?:\d{1,2}:\d{2}\s+)?([^:：]{1,40})\s{1,3}(.+)$/u);
    return matched
      ? { senderName: matched[1].trim(), text: matched[2].trim(), msgType: "text" }
      : { senderName: "", text: line, msgType: "text" };
  });
}

export function extractFaqDrafts(lines: ChatDraftLine[]): FAQDraftItem[] {
  const usableLines = lines.map((item) => ({
    ...item,
    text: normalizeChatText(item.text),
    senderName: item.senderName?.trim() || "",
    timestamp: item.timestamp?.trim() || "",
    msgType: item.msgType || "text",
    isSelfSent: Boolean(item.isSelfSent),
  })).filter((item) => item.text && item.msgType === "text" && !item.isSelfSent && item.text !== "[图片]" && item.text !== "[表情]");
  const drafts: FAQDraftItem[] = [];
  const seen = new Set<string>();
  let draftIndex = 1;
  for (let index = 0; index < usableLines.length; index += 1) {
    const current = usableLines[index];
    if (!isQuestionText(current.text)) continue;
    const answers: string[] = [];
    const source = [`${current.timestamp ? `[${current.timestamp}] ` : ""}${current.senderName || "用户"}：${current.text}`];
    for (let cursor = index + 1; cursor < usableLines.length; cursor += 1) {
      const next = usableLines[cursor];
      if (isQuestionText(next.text)) break;
      const content = next.senderName ? `${next.senderName}：${next.text}` : next.text;
      answers.push(content);
      source.push(`${next.timestamp ? `[${next.timestamp}] ` : ""}${content}`);
      if (answers.length >= 3 || answers.join("\n").length >= 320) break;
    }
    if (!answers.length) continue;
    const question = current.text.replace(/[?？\s]+$/g, "").trim() || current.text.trim();
    const key = normalizeChatText(question).replace(/[?？!！,，。.:：'"“”‘’`()（）\[\]【】]/g, "").toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    drafts.push({
      draftId: `draft-${draftIndex}`,
      selected: true,
      question,
      answer: answers.join("\n"),
      variantsText: joinMultivalue(buildQuestionVariants(question)),
      tagsText: joinMultivalue(inferDraftTags(question, answers.join("\n"))),
      sourceExcerpt: source.join("\n"),
    });
    draftIndex += 1;
  }
  return drafts;
}

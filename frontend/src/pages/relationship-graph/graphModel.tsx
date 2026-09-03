import type {
  GroupGraphEdge,
  GroupGraphEdgeEvidenceEntity,
  GroupGraphEdgeEvidenceResponse,
  GroupGraphNode,
  GroupGraphResponse,
  MemoryBackfillResponse,
  MemoryExtractionJobStatsResponse,
  MemoryExtractionJobStatusCounts,
} from "../../lib/api";

export const ACCEPTANCE_OPTIONS = ["", "accepted", "candidate", "needs_review", "rejected", "superseded", "expired"];
export const DEFAULT_NODE_TYPES = ["person", "group", "topic", "project", "tool", "event", "task", "artifact", "value"];
export const DEFAULT_EDGE_TYPES = [
  "mentioned",
  "replied_to",
  "addressed",
  "asked",
  "answered",
  "co_participated",
  "requested",
  "provided_resource",
  "collaborated_with",
  "works_on",
  "interested_in",
  "maintains",
  "reported_issue",
  "fixed_issue",
  "tested",
];
export const HISTORY_RECENT_DAYS = 14;
export const GRAPH_OVERVIEW_DAYS = 7;
export const GRAPH_CANVAS_WIDTH = 920;
export const GRAPH_CANVAS_HEIGHT = 620;
export const LOW_VALUE_EDGE_TYPES = new Set(["said", "says", "quoted", "mentions_raw", "raw_mention", "message"]);
export const VALUE_NODE_TYPES = new Set(["value", "literal", "text", "quote", "raw_value"]);
export const PERSON_NODE_TYPES = new Set(["person", "user", "member", "contact", "participant", "group"]);
export const TOPIC_NODE_TYPES = new Set(["topic", "project", "product", "tool", "artifact", "task", "event", "brand"]);
export const CORE_EDGE_TYPES = new Set([
  "replied_to",
  "addressed",
  "asked",
  "answered",
  "co_participated",
  "requested",
  "provided_resource",
  "collaborated_with",
  "works_on",
  "interested_in",
  "maintains",
  "reported_issue",
  "fixed_issue",
  "tested",
]);
export const RAW_EVIDENCE_FIELD_NAMES = new Set([
  "content",
  "original_text",
  "user_text",
  "assistant_text",
  "summary",
  "object_value",
  "raw",
]);
export const RELATION_LABELS: Record<string, string> = {
  asked: "询问",
  answered: "回答",
  mentioned: "提到",
  mentions: "提到",
  replied_to: "回复",
  addressed: "点名",
  co_participated: "共同参与",
  requested: "请求",
  provided_resource: "提供资源",
  collaborated_with: "协作",
  works_on: "参与",
  interested_in: "感兴趣",
  maintains: "维护",
  reported_issue: "报告问题",
  fixed_issue: "修复问题",
  tested: "测试",
};

const ACCEPTANCE_LABELS: Record<string, string> = {
  accepted: "已接受",
  candidate: "候选关系",
  needs_review: "待审核",
  rejected: "已拒绝",
  superseded: "已被替代",
  expired: "已过期",
  unknown: "状态未知",
};

const NODE_TYPE_LABELS: Record<string, string> = {
  person: "人物",
  user: "成员",
  member: "成员",
  group: "群聊",
  topic: "主题",
  project: "项目",
  product: "产品",
  tool: "工具",
  event: "事件",
  task: "任务",
  artifact: "产物",
  value: "值",
};

export type Selection =
  | { kind: "node"; item: GroupGraphNode }
  | { kind: "edge"; item: GroupGraphEdge }
  | null;

export type GraphViewMode = "readable" | "core" | "people" | "topics" | "all";

export const GRAPH_VIEW_BUDGETS: Record<GraphViewMode, { nodes: number; edges: number; labels: number }> = {
  readable: { nodes: 32, edges: 62, labels: 18 },
  core: { nodes: 40, edges: 82, labels: 22 },
  people: { nodes: 42, edges: 90, labels: 22 },
  topics: { nodes: 42, edges: 90, labels: 22 },
  all: { nodes: 72, edges: 180, labels: 28 },
};

export const GRAPH_VIEW_MODES: Array<{ value: GraphViewMode; label: string; description: string }> = [
  { value: "readable", label: "可读", description: "核心摘要，隐藏低信号关系和值节点" },
  { value: "core", label: "核心", description: "优先高连接关系" },
  { value: "people", label: "人物", description: "只看人物之间的关系" },
  { value: "topics", label: "主题", description: "人物与主题/项目/工具" },
  { value: "all", label: "全部", description: "显示当前查询返回的关系" },
];

export type NodeVisualType = "person" | "topic" | "product" | "tool" | "value" | "other";

export const NODE_TYPE_LEGEND: Array<{ type: NodeVisualType; label: string }> = [
  { type: "person", label: "人物" },
  { type: "topic", label: "主题" },
  { type: "product", label: "产品" },
  { type: "tool", label: "工具" },
  { type: "value", label: "值" },
  { type: "other", label: "其他" },
];

export function formatTimestamp(value?: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function acceptanceStatusLabel(value?: string | null) {
  return ACCEPTANCE_LABELS[String(value || "unknown").toLowerCase()] || "其他状态";
}

export function normalizedAcceptanceStatus(value?: string | null) {
  return String(value || "").trim().toLowerCase();
}

export function edgeCanAccept(status?: string | null) {
  const normalized = normalizedAcceptanceStatus(status);
  return normalized !== "accepted" && normalized !== "superseded" && normalized !== "expired";
}

export function edgeCanReject(status?: string | null) {
  const normalized = normalizedAcceptanceStatus(status);
  return normalized !== "rejected" && normalized !== "superseded" && normalized !== "expired";
}

export function edgeCanExpire(status?: string | null) {
  const normalized = normalizedAcceptanceStatus(status);
  return normalized !== "expired" && normalized !== "superseded";
}

export function edgeCanReturnToReview(status?: string | null) {
  const normalized = normalizedAcceptanceStatus(status);
  return normalized === "accepted" || normalized === "rejected" || normalized === "candidate";
}

export const PENDING_REVIEW_STATUS = "needs_review,candidate";

export const GRAPH_RANGE_PRESETS = [
  { days: 7, label: "近7天" },
  { days: 14, label: "近14天" },
  { days: 30, label: "近30天" },
  { days: 0, label: "全部时间" },
] as const;

export function localDateSeries(days: number, end = new Date()) {
  return Array.from({ length: Math.max(1, days) }, (_, index) => localDateDaysAgo(days - 1 - index, end));
}

export function extractionMethodLabel(value?: string | null) {
  const normalized = String(value || "").trim().toLowerCase();
  if (!normalized) return "来源未知";
  if (normalized.includes("llm")) return "语义抽取";
  if (normalized.includes("deterministic") || normalized.includes("rule") || normalized.includes("stat")) {
    return "规则抽取";
  }
  return "混合抽取";
}

export function isPendingReviewStatus(status?: string | null) {
  const normalized = normalizedAcceptanceStatus(status);
  return normalized === "needs_review" || normalized === "candidate";
}

export function edgeStrokeWidth(edge: GroupGraphEdge) {
  return Math.min(5, 1.15 + Number(edge.evidence_count || 0) * 0.32);
}

export function edgeRecencyOpacity(edge: GroupGraphEdge) {
  if (!edge.last_seen) return 0.82;
  const ageDays = (Date.now() - new Date(edge.last_seen).getTime()) / 86_400_000;
  if (!Number.isFinite(ageDays) || ageDays <= 7) return 0.9;
  if (ageDays <= 14) return 0.68;
  if (ageDays <= 30) return 0.46;
  return 0.28;
}

export function restoreGraphSelection(selection: Selection, graph: GroupGraphResponse): Selection {
  if (!selection) return null;
  if (selection.kind === "edge") {
    const edge = (graph.edges || []).find((item) => item.id === selection.item.id);
    return edge ? { kind: "edge", item: edge } : null;
  }
  const node = (graph.nodes || []).find((item) => item.id === selection.item.id);
  return node ? { kind: "node", item: node } : null;
}

export function reviewActionMessage(action: string) {
  if (action === "accept") return "已接受该关系。";
  if (action === "reject") return "已拒绝该关系。";
  if (action === "expire") return "已将该关系标记为过期。";
  if (action === "needs_review") return "已将该关系退回待审核。";
  if (action === "supersede") return "已用当前关系替代另一条关系。";
  return "已更新该关系的审核状态。";
}

export function localDateAdd(value: string, days: number) {
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) return value;
  const date = new Date(year, month - 1, day);
  date.setDate(date.getDate() + days);
  return localDateValue(date);
}

export function edgeEvidenceDates(edge: GroupGraphEdge) {
  const dates = (edge.evidence_dates || [])
    .map((value) => String(value || "").slice(0, 10))
    .filter(Boolean);
  if (edge.first_seen) dates.push(String(edge.first_seen).slice(0, 10));
  if (edge.last_seen) dates.push(String(edge.last_seen).slice(0, 10));
  return Array.from(new Set(dates));
}

export function edgeSeenOnDate(edge: GroupGraphEdge, date?: string | null) {
  if (!date) return false;
  return edgeEvidenceDates(edge).includes(date);
}

export function playbackDateSeries(rangeDays: number, fromDate = "") {
  if (rangeDays > 0) return localDateSeries(rangeDays);
  if (fromDate) return [fromDate];
  return localDateSeries(GRAPH_OVERVIEW_DAYS);
}

export function counterpartEdges(edge: GroupGraphEdge, pool: GroupGraphEdge[]) {
  const source = displayEdgeSource(edge);
  const target = displayEdgeTarget(edge);
  return pool.filter((item) => {
    if (!item.id || item.id === edge.id) return false;
    const from = displayEdgeSource(item);
    const to = displayEdgeTarget(item);
    return (from === source && to === target) || (from === target && to === source);
  });
}

export function firstMemoryItemId(edge?: GroupGraphEdge | null) {
  const value = Number(edge?.memory_item_ids?.[0]);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

export function edgePairKey(edge: GroupGraphEdge) {
  const source = displayEdgeSource(edge);
  const target = displayEdgeTarget(edge);
  return source < target ? `${source}::${target}` : `${target}::${source}`;
}

export function edgeBundleOffsets(edges: GroupGraphEdge[]) {
  const groups = new Map<string, GroupGraphEdge[]>();
  for (const edge of edges) {
    const key = edgePairKey(edge);
    const group = groups.get(key) || [];
    group.push(edge);
    groups.set(key, group);
  }
  const offsets = new Map<string, number>();
  for (const group of groups.values()) {
    group.forEach((edge, index) => {
      offsets.set(edgeKey(edge), group.length === 1 ? 0 : index - (group.length - 1) / 2);
    });
  }
  return offsets;
}

export function quadraticEdgePath(
  from: { x: number; y: number },
  to: { x: number; y: number },
  offset: number,
) {
  if (!offset) return `M ${from.x} ${from.y} L ${to.x} ${to.y}`;
  const midX = (from.x + to.x) / 2;
  const midY = (from.y + to.y) / 2;
  const deltaX = to.x - from.x;
  const deltaY = to.y - from.y;
  const length = Math.hypot(deltaX, deltaY) || 1;
  return `M ${from.x} ${from.y} Q ${midX + (-deltaY / length) * offset * 22} ${midY + (deltaX / length) * offset * 22} ${to.x} ${to.y}`;
}

export function evidenceQuality(evidence?: GroupGraphEdgeEvidenceResponse | null) {
  const items = evidence?.memory_items || [];
  const scores = items
    .map((item) => Number(item.acceptance_score))
    .filter((value) => Number.isFinite(value));
  const reasons = items
    .map((item) => String(item.acceptance_reason || "").trim())
    .filter(Boolean);
  const conflicts = items.flatMap((item) => (
    Array.isArray(item.possible_conflicts) ? item.possible_conflicts : []
  ));
  return {
    score: scores.length ? Math.max(...scores) : null,
    reason: reasons[0] || "",
    conflicts: conflicts.length,
  };
}

export function formatAcceptanceScore(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "-";
  return `${Math.round(Number(value) * 100)}%`;
}

export function nodeTypeLabel(value?: string | null) {
  return NODE_TYPE_LABELS[String(value || "").toLowerCase()] || "其他类型";
}

export function formatConfidence(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return "-";
  return `${Math.round(Number(value) * 100)}%`;
}

export function acceptanceClass(value?: string) {
  const normalized = (value || "unknown").toLowerCase();
  if (normalized === "accepted") return "relationship-acceptance is-accepted";
  if (normalized === "needs_review" || normalized === "candidate") return "relationship-acceptance is-review";
  if (normalized === "rejected" || normalized === "expired" || normalized === "superseded") {
    return "relationship-acceptance is-muted";
  }
  return "relationship-acceptance";
}

export function displayEdgeSource(edge: GroupGraphEdge) {
  return edge.from || edge.source || "";
}

export function displayEdgeTarget(edge: GroupGraphEdge) {
  return edge.to || edge.target || "";
}

export function truncateMiddle(value: string, head = 10, tail = 4) {
  const normalized = String(value || "").trim();
  if (normalized.length <= head + tail + 1) return normalized;
  return `${normalized.slice(0, head)}…${normalized.slice(-tail)}`;
}

export function isTechnicalUserId(value: string) {
  return /^(wxid_|gh_|openid_|unionid_|user[_-]?|userid|uid[_:-]?|entity:)/i.test(value)
    || /^[a-z0-9_@.-]{24,}$/i.test(value);
}

export function shortTechnicalId(value?: string | null) {
  const normalized = String(value || "").trim();
  if (!normalized) return "";
  if (normalized.startsWith("entity:")) {
    return `entity:${truncateMiddle(normalized.slice("entity:".length), 10, 4)}`;
  }
  if (normalized.includes("@chatroom")) {
    return truncateMiddle(normalized, 14, 9);
  }
  if (isTechnicalUserId(normalized)) {
    return truncateMiddle(normalized, 9, 4);
  }
  return normalized.length > 28 ? truncateMiddle(normalized, 16, 6) : normalized;
}

export function isTechnicalLabel(value?: string | null) {
  const normalized = String(value || "").trim();
  return !normalized || isTechnicalUserId(normalized) || /^entity:\d+$/i.test(normalized);
}

export function nodeDisplayLabel(node?: GroupGraphNode | null) {
  if (!node) return "";
  const candidates = [node.display_label, node.label, ...(node.aliases || [])];
  const friendly = candidates.find((value) => value && !isTechnicalLabel(value));
  if (friendly) return String(friendly);
  return shortTechnicalId(node.technical_label || node.label || node.id) || shortTechnicalId(node.id) || node.type || "?";
}

export function nodeSecondaryLabel(node?: GroupGraphNode | null) {
  if (!node) return "";
  return nodeTypeLabel(node.type);
}

export function safeNodeDisplayLabel(node?: GroupGraphNode | null) {
  if (!node) return "";
  if (nodeVisualType(node) === "value") {
    const suffix = shortTechnicalId(node.id).replace(/^entity:/i, "#");
    return suffix ? `值节点 ${suffix}` : "值节点";
  }
  return nodeDisplayLabel(node);
}

export function safeNodeStoredLabel(node?: GroupGraphNode | null) {
  if (!node) return "-";
  return nodeVisualType(node) === "value" ? "已隐藏 value 文本" : node.label || "-";
}

export function safeNodeTechnicalLabel(node?: GroupGraphNode | null) {
  if (!node) return "-";
  return nodeVisualType(node) === "value" ? node.id : node.technical_label || node.id;
}

export function safeNodeAliases(node?: GroupGraphNode | null) {
  if (!node) return "-";
  if (nodeVisualType(node) === "value") return "已隐藏 value 文本";
  return node.aliases?.length ? node.aliases.join(", ") : "-";
}

export function graphLabelCandidate(node: GroupGraphNode) {
  if (nodeVisualType(node) === "value") return "值节点";
  const candidates = [node.display_label, node.label, ...(node.aliases || [])];
  const friendly = candidates.find((value) => value && !isTechnicalLabel(value));
  if (friendly) return String(friendly).replace(/^user:/i, "");
  return "";
}

export function buildAnonymousGraphLabels(nodes: GroupGraphNode[]) {
  const technicalNodes = nodes
    .filter((node) => !graphLabelCandidate(node))
    .filter((node) => nodeVisualType(node) !== "value")
    .sort((first, second) => (
      String(first.technical_label || first.label || first.id).localeCompare(String(second.technical_label || second.label || second.id))
    ));
  return new Map(technicalNodes.map((node, index) => [node.id, `成员 ${index + 1}`]));
}

export function graphNodeLabel(node: GroupGraphNode, anonymousLabels: Map<string, string>) {
  const anonymousLabel = anonymousLabels.get(node.id);
  if (anonymousLabel) return anonymousLabel;
  return truncateMiddle(graphLabelCandidate(node) || safeNodeDisplayLabel(node).replace(/^user:/i, ""), 16, 4);
}

export function readableRelationType(value?: string | null) {
  const normalized = String(value || "").trim();
  if (!normalized) return "关联";
  return RELATION_LABELS[normalized] || "其他关系";
}

export function relationLabel(edge: GroupGraphEdge, nodesById: Map<string, GroupGraphNode>) {
  const sourceNode = nodesById.get(displayEdgeSource(edge));
  const targetNode = nodesById.get(displayEdgeTarget(edge));
  const source = safeNodeDisplayLabel(sourceNode) || shortTechnicalId(displayEdgeSource(edge)) || "?";
  const target = safeNodeDisplayLabel(targetNode) || shortTechnicalId(displayEdgeTarget(edge)) || "?";
  return `${source} — ${readableRelationType(edge.label || edge.type)} — ${target}`;
}

export function matchesGraphText(value: string, query: string) {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) return true;
  return value.toLowerCase().includes(normalizedQuery);
}

export function sortedUnique(values: Array<string | undefined>) {
  return Array.from(new Set(values.filter((value): value is string => Boolean(value)))).sort();
}

export function normalizedNodeType(node?: GroupGraphNode | null) {
  return String(node?.type || "").trim().toLowerCase();
}

export function normalizedEdgeType(edge?: GroupGraphEdge | null) {
  return String(edge?.label || edge?.type || "").trim().toLowerCase();
}

export function nodeVisualType(node?: GroupGraphNode | null): NodeVisualType {
  const type = normalizedNodeType(node);
  const label = String(node?.display_label || node?.label || node?.technical_label || node?.id || "").toLowerCase();
  if (type.includes("person") || type === "user" || type.includes("member") || type.includes("contact")) return "person";
  if (type.includes("product") || type.includes("brand")) return "product";
  if (type.includes("tool")) return "tool";
  if (type.includes("topic") || type.includes("project") || type.includes("task") || type.includes("event") || type.includes("artifact")) return "topic";
  if (VALUE_NODE_TYPES.has(type) || type.includes("value") || type.includes("literal") || isTechnicalLabel(label)) return "value";
  return "other";
}

export function isPersonNode(node?: GroupGraphNode | null) {
  const type = normalizedNodeType(node);
  return PERSON_NODE_TYPES.has(type) || nodeVisualType(node) === "person";
}

export function isTopicLikeNode(node?: GroupGraphNode | null) {
  const type = normalizedNodeType(node);
  return TOPIC_NODE_TYPES.has(type) || ["topic", "product", "tool"].includes(nodeVisualType(node));
}

export function isLowValueEdge(edge: GroupGraphEdge) {
  const type = normalizedEdgeType(edge);
  return LOW_VALUE_EDGE_TYPES.has(type) || type.startsWith("said") || type.includes("raw");
}

export function buildDegreeMap(edges: GroupGraphEdge[]) {
  const degree = new Map<string, number>();
  for (const edge of edges) {
    degree.set(displayEdgeSource(edge), (degree.get(displayEdgeSource(edge)) || 0) + 1);
    degree.set(displayEdgeTarget(edge), (degree.get(displayEdgeTarget(edge)) || 0) + 1);
  }
  return degree;
}

export function nodeImportance(node: GroupGraphNode, degree: Map<string, number>) {
  return (degree.get(node.id) || 0) * 10
    + (node.evidence_count || node.source_ref_count || 0)
    + (isPersonNode(node) ? 24 : 0)
    + (isTopicLikeNode(node) ? 10 : 0)
    - (nodeVisualType(node) === "value" ? 20 : 0);
}

export function edgeImportance(edge: GroupGraphEdge, nodesById: Map<string, GroupGraphNode>, degree: Map<string, number>) {
  const sourceId = displayEdgeSource(edge);
  const targetId = displayEdgeTarget(edge);
  const sourceNode = nodesById.get(sourceId);
  const targetNode = nodesById.get(targetId);
  const sourceImportance = sourceNode ? nodeImportance(sourceNode, degree) : 0;
  const targetImportance = targetNode ? nodeImportance(targetNode, degree) : 0;
  const type = normalizedEdgeType(edge);
  const confidence = Number(edge.confidence ?? 0);
  const evidence = edge.evidence_count || 0;
  const connectsPerson = isPersonNode(sourceNode) || isPersonNode(targetNode);
  const connectsTopic = isTopicLikeNode(sourceNode) || isTopicLikeNode(targetNode);
  return evidence * 28
    + confidence * 18
    + (sourceImportance + targetImportance) * 0.7
    + (CORE_EDGE_TYPES.has(type) ? 18 : 0)
    + (connectsPerson && connectsTopic ? 14 : 0)
    + (isPersonNode(sourceNode) && isPersonNode(targetNode) ? 8 : 0)
    - (isLowValueEdge(edge) ? 35 : 0)
    - (nodeVisualType(sourceNode) === "value" || nodeVisualType(targetNode) === "value" ? 28 : 0);
}

export function sortEdgesByImportance(
  edges: GroupGraphEdge[],
  nodesById: Map<string, GroupGraphNode>,
  forcedNodeIds: Set<string>,
) {
  const degree = buildDegreeMap(edges);
  return [...edges].sort((first, second) => {
    const firstForced = forcedNodeIds.has(displayEdgeSource(first)) || forcedNodeIds.has(displayEdgeTarget(first));
    const secondForced = forcedNodeIds.has(displayEdgeSource(second)) || forcedNodeIds.has(displayEdgeTarget(second));
    if (firstForced !== secondForced) return firstForced ? -1 : 1;
    const scoreDiff = edgeImportance(second, nodesById, degree) - edgeImportance(first, nodesById, degree);
    return scoreDiff || edgeKey(first).localeCompare(edgeKey(second));
  });
}

export function sortNodesByImportance(nodes: GroupGraphNode[], degree: Map<string, number>) {
  return [...nodes].sort((first, second) => {
    const scoreDiff = nodeImportance(second, degree) - nodeImportance(first, degree);
    return scoreDiff || nodeDisplayLabel(first).localeCompare(nodeDisplayLabel(second));
  });
}

export function selectGraphNodes(
  nodes: GroupGraphNode[],
  rankedEdges: GroupGraphEdge[],
  limit: number,
  forcedNodeIds: Set<string>,
) {
  const degree = buildDegreeMap(rankedEdges);
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const selected = new Map<string, GroupGraphNode>();
  const addNode = (node?: GroupGraphNode) => {
    if (node && (selected.size < limit || forcedNodeIds.has(node.id))) selected.set(node.id, node);
  };

  for (const id of Array.from(forcedNodeIds).sort()) addNode(nodesById.get(id));
  for (const edge of rankedEdges) {
    addNode(nodesById.get(displayEdgeSource(edge)));
    addNode(nodesById.get(displayEdgeTarget(edge)));
    if (selected.size >= limit) break;
  }
  for (const node of sortNodesByImportance(nodes, degree)) {
    if (selected.size >= limit) break;
    addNode(node);
  }

  return sortNodesByImportance(Array.from(selected.values()), degree);
}

export function shouldKeepEdgeForMode(
  edge: GroupGraphEdge,
  nodesById: Map<string, GroupGraphNode>,
  mode: GraphViewMode,
  forcedNodeIds: Set<string>,
) {
  const sourceNode = nodesById.get(displayEdgeSource(edge));
  const targetNode = nodesById.get(displayEdgeTarget(edge));
  const sourceForced = forcedNodeIds.has(displayEdgeSource(edge));
  const targetForced = forcedNodeIds.has(displayEdgeTarget(edge));
  if (sourceForced || targetForced) return true;
  if (mode === "all") return true;

  const sourceType = nodeVisualType(sourceNode);
  const targetType = nodeVisualType(targetNode);
  if (isLowValueEdge(edge)) return false;
  if (sourceType === "value" || targetType === "value") return false;

  if (mode === "people") return isPersonNode(sourceNode) && isPersonNode(targetNode);
  if (mode === "topics") {
    return (isPersonNode(sourceNode) && isTopicLikeNode(targetNode)) || (isTopicLikeNode(sourceNode) && isPersonNode(targetNode));
  }
  if (mode === "core") {
    if (isPersonNode(sourceNode) && isPersonNode(targetNode)) return true;
    return CORE_EDGE_TYPES.has(normalizedEdgeType(edge)) || (edge.evidence_count || 0) > 1;
  }
  return true;
}

export function filterNodesForMode(
  nodes: GroupGraphNode[],
  edges: GroupGraphEdge[],
  mode: GraphViewMode,
  forcedNodeIds: Set<string>,
) {
  if (mode === "all") return nodes;
  const edgeNodeIds = new Set<string>();
  for (const edge of edges) {
    edgeNodeIds.add(displayEdgeSource(edge));
    edgeNodeIds.add(displayEdgeTarget(edge));
  }
  const degree = buildDegreeMap(edges);
  return nodes.filter((node) => {
    if (forcedNodeIds.has(node.id)) return true;
    if (!edgeNodeIds.has(node.id)) return false;
    if (mode === "people") return isPersonNode(node);
    if (mode === "topics") return isPersonNode(node) || isTopicLikeNode(node);
    if (mode === "core") return nodeVisualType(node) !== "value" && (degree.get(node.id) || 0) >= 2;
    return nodeVisualType(node) !== "value";
  });
}

export type GraphPoint = { x: number; y: number };
export type GraphLabelPlacement = {
  nodeId: string;
  text: string;
  x: number;
  y: number;
  anchor: "start" | "middle" | "end";
};

export function centerSlotOrder(count: number) {
  const slots = Array.from({ length: count }, (_, index) => index);
  const center = (count - 1) / 2;
  return slots.sort((first, second) => Math.abs(first - center) - Math.abs(second - center) || first - second);
}

export function laneY(index: number, count: number, top: number, bottom: number) {
  if (count <= 1) return (top + bottom) / 2;
  return top + ((bottom - top) / (count - 1)) * index;
}

export function connectedPersonAnchor(
  node: GroupGraphNode,
  edges: GroupGraphEdge[],
  personLayout: Map<string, GraphPoint>,
  degree: Map<string, number>,
  nodesById: Map<string, GroupGraphNode>,
) {
  let weightedY = 0;
  let totalWeight = 0;
  for (const edge of edges) {
    const sourceId = displayEdgeSource(edge);
    const targetId = displayEdgeTarget(edge);
    const otherId = sourceId === node.id ? targetId : targetId === node.id ? sourceId : "";
    if (!otherId || !isPersonNode(nodesById.get(otherId))) continue;
    const point = personLayout.get(otherId);
    if (!point) continue;
    const weight = Math.max(1, edge.evidence_count || 0) + (degree.get(otherId) || 0) * 0.35;
    weightedY += point.y * weight;
    totalWeight += weight;
  }
  return totalWeight ? weightedY / totalWeight : undefined;
}

export function buildGraphLayout(nodes: GroupGraphNode[], edges: GroupGraphEdge[] = []) {
  const lanes: Record<string, { x: number; top: number; bottom: number }> = {
    person: { x: 138, top: 82, bottom: 558 },
    core: { x: 360, top: 96, bottom: 528 },
    topic: { x: 604, top: 76, bottom: 540 },
    product: { x: 752, top: 92, bottom: 520 },
    value: { x: 806, top: 430, bottom: 570 },
    other: { x: 472, top: 132, bottom: 548 },
  };
  const degree = buildDegreeMap(edges);
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const sortedNodes = sortNodesByImportance(nodes, degree);
  const topCoreIds = new Set(sortedNodes.filter((node) => !isPersonNode(node)).slice(0, Math.min(6, sortedNodes.length)).map((node) => node.id));
  const laneNodes = new Map<string, GroupGraphNode[]>([
    ["person", []],
    ["core", []],
    ["topic", []],
    ["product", []],
    ["value", []],
    ["other", []],
  ]);

  for (const node of sortedNodes) {
    const visualType = nodeVisualType(node);
    if (isPersonNode(node)) {
      laneNodes.get("person")?.push(node);
    } else if (["product", "tool"].includes(visualType)) {
      laneNodes.get("product")?.push(node);
    } else if (visualType === "topic") {
      laneNodes.get("topic")?.push(node);
    } else if (topCoreIds.has(node.id) && visualType !== "value") {
      laneNodes.get("core")?.push(node);
    } else if (visualType === "value") {
      laneNodes.get("value")?.push(node);
    } else {
      laneNodes.get("other")?.push(node);
    }
  }

  const layout = new Map<string, GraphPoint>();
  const personItems = laneNodes.get("person") || [];
  const personSlots = centerSlotOrder(personItems.length);
  personItems.forEach((node, index) => {
    const lane = lanes.person;
    const slot = personSlots[index] ?? index;
    layout.set(node.id, {
      x: lane.x + Math.min(36, (degree.get(node.id) || 0) * 2.4),
      y: laneY(slot, personItems.length, lane.top, lane.bottom),
    });
  });

  for (const [laneKey, laneItems] of laneNodes.entries()) {
    if (laneKey === "person") continue;
    const lane = lanes[laneKey] || lanes.other;
    const ordered = [...laneItems].sort((first, second) => {
      const firstAnchor = connectedPersonAnchor(first, edges, layout, degree, nodesById);
      const secondAnchor = connectedPersonAnchor(second, edges, layout, degree, nodesById);
      if (firstAnchor !== undefined || secondAnchor !== undefined) {
        return (firstAnchor ?? Number.MAX_SAFE_INTEGER) - (secondAnchor ?? Number.MAX_SAFE_INTEGER);
      }
      const importanceDiff = nodeImportance(second, degree) - nodeImportance(first, degree);
      return importanceDiff || nodeDisplayLabel(first).localeCompare(nodeDisplayLabel(second));
    });
    const count = Math.max(ordered.length, 1);
    ordered.forEach((node, index) => {
      const anchor = connectedPersonAnchor(node, edges, layout, degree, nodesById);
      const spreadY = laneY(index, count, lane.top, lane.bottom);
      const y = anchor === undefined ? spreadY : Math.max(lane.top, Math.min(lane.bottom, anchor * 0.7 + spreadY * 0.3));
      const xPull = Math.min(44, (degree.get(node.id) || 0) * 4);
      layout.set(node.id, {
        x: Math.max(56, Math.min(GRAPH_CANVAS_WIDTH - 56, lane.x - (laneKey === "topic" || laneKey === "product" ? xPull : 0))),
        y,
      });
    });
  }

  return layout;
}

export function estimatedLabelSize(text: string) {
  const width = Array.from(text).reduce((sum, char) => sum + (char.charCodeAt(0) > 255 ? 13 : 7.4), 14);
  return { width: Math.min(148, Math.max(42, width)), height: 18 };
}

export function boxesOverlap(
  first: { left: number; right: number; top: number; bottom: number },
  second: { left: number; right: number; top: number; bottom: number },
) {
  return first.left < second.right && first.right > second.left && first.top < second.bottom && first.bottom > second.top;
}

export function labelCandidates(point: GraphPoint, text: string) {
  const size = estimatedLabelSize(text);
  const middleBox = (x: number, y: number) => ({
    left: x - size.width / 2,
    right: x + size.width / 2,
    top: y - size.height + 4,
    bottom: y + 5,
  });
  const sideBox = (x: number, y: number, anchor: "start" | "end") => ({
    left: anchor === "start" ? x : x - size.width,
    right: anchor === "start" ? x + size.width : x,
    top: y - size.height + 5,
    bottom: y + 5,
  });
  return [
    { x: point.x, y: point.y + 30, anchor: "middle" as const, box: middleBox(point.x, point.y + 30) },
    { x: point.x, y: point.y - 19, anchor: "middle" as const, box: middleBox(point.x, point.y - 19) },
    { x: point.x + 18, y: point.y + 5, anchor: "start" as const, box: sideBox(point.x + 18, point.y + 5, "start") },
    { x: point.x - 18, y: point.y + 5, anchor: "end" as const, box: sideBox(point.x - 18, point.y + 5, "end") },
  ].filter((candidate) => (
    candidate.box.left >= 8
    && candidate.box.right <= GRAPH_CANVAS_WIDTH - 8
    && candidate.box.top >= 42
    && candidate.box.bottom <= GRAPH_CANVAS_HEIGHT - 8
  ));
}

export function buildVisibleLabels(
  nodes: GroupGraphNode[],
  edges: GroupGraphEdge[],
  layout: Map<string, GraphPoint>,
  anonymousLabels: Map<string, string>,
  labelLimit: number,
  selectedNodeId?: string,
  selectedEdge?: GroupGraphEdge | null,
) {
  const degree = buildDegreeMap(edges);
  const requiredIds = new Set<string>();
  if (selectedNodeId) requiredIds.add(selectedNodeId);
  if (selectedEdge) {
    requiredIds.add(displayEdgeSource(selectedEdge));
    requiredIds.add(displayEdgeTarget(selectedEdge));
  }

  const sorted = [...nodes].sort((first, second) => {
    const firstRequired = requiredIds.has(first.id);
    const secondRequired = requiredIds.has(second.id);
    if (firstRequired !== secondRequired) return firstRequired ? -1 : 1;
    const scoreDiff = nodeImportance(second, degree) - nodeImportance(first, degree);
    return scoreDiff || graphNodeLabel(first, anonymousLabels).localeCompare(graphNodeLabel(second, anonymousLabels));
  });
  const placedBoxes: Array<{ left: number; right: number; top: number; bottom: number }> = [];
  const labels = new Map<string, GraphLabelPlacement>();

  for (const node of sorted) {
    const required = requiredIds.has(node.id);
    if (!required && labels.size >= labelLimit) break;
    if (!required && nodeVisualType(node) === "value" && (degree.get(node.id) || 0) <= 2) continue;
    const point = layout.get(node.id);
    if (!point) continue;
    const text = graphNodeLabel(node, anonymousLabels);
    const candidates = labelCandidates(point, text);
    const candidate = candidates.find((item) => !placedBoxes.some((box) => boxesOverlap(item.box, box)))
      || (required ? candidates[0] : undefined);
    if (!candidate) continue;
    labels.set(node.id, { nodeId: node.id, text, x: candidate.x, y: candidate.y, anchor: candidate.anchor });
    placedBoxes.push(candidate.box);
    if (!required && labels.size >= labelLimit) {
      const remainingRequired = sorted.some((item) => requiredIds.has(item.id) && !labels.has(item.id));
      if (!remainingRequired) break;
    }
  }
  return labels;
}

export function edgeKey(edge: GroupGraphEdge) {
  return edge.id || `${displayEdgeSource(edge)}:${normalizedEdgeType(edge)}:${displayEdgeTarget(edge)}`;
}

export function edgeConnectsNode(edge: GroupGraphEdge, nodeId?: string | null) {
  return Boolean(nodeId && (displayEdgeSource(edge) === nodeId || displayEdgeTarget(edge) === nodeId));
}

export function selectedEdgeTouchesNode(edge: GroupGraphEdge | null, nodeId: string) {
  return Boolean(edge && (displayEdgeSource(edge) === nodeId || displayEdgeTarget(edge) === nodeId));
}

export function nodeIsFocused(nodeId: string, selectedNodeId: string | null, selectedEdge: GroupGraphEdge | null, neighborIds: Set<string>) {
  if (!selectedNodeId && !selectedEdge) return true;
  return nodeId === selectedNodeId || neighborIds.has(nodeId) || selectedEdgeTouchesNode(selectedEdge, nodeId);
}

export function visibleEvidenceIds(values?: Array<string | number>, limit = 8) {
  return (values || []).slice(0, limit).join(", ") || "-";
}

export function sanitizeEvidenceValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeEvidenceValue(item));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => {
        const normalized = key.toLowerCase();
        return !RAW_EVIDENCE_FIELD_NAMES.has(normalized) && !normalized.startsWith("raw_");
      })
      .map(([key, nestedValue]) => [key, sanitizeEvidenceValue(nestedValue)]),
  );
}

export function sanitizeEdgeEvidence(payload: GroupGraphEdgeEvidenceResponse): GroupGraphEdgeEvidenceResponse {
  return sanitizeEvidenceValue(payload) as GroupGraphEdgeEvidenceResponse;
}

export function evidenceCountsLabel(evidence?: GroupGraphEdgeEvidenceResponse | null) {
  const counts = evidence?.evidence_counts || {};
  return `记忆项 ${counts.memory_items ?? 0} / 事件 ${counts.events ?? 0} / 片段 ${counts.episodes ?? 0}`;
}

export function evidenceRecordMeta(record: GroupGraphEdgeEvidenceEntity) {
  return [
    record.acceptance_status ? `acceptance: ${record.acceptance_status}` : "",
    record.status ? `status: ${record.status}` : "",
    record.created_at ? `created: ${formatTimestamp(record.created_at)}` : "",
    record.updated_at ? `updated: ${formatTimestamp(record.updated_at)}` : "",
  ].filter(Boolean);
}

export function localDateValue(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function localDateDaysAgo(days: number, date = new Date()) {
  const value = new Date(date);
  value.setDate(value.getDate() - Math.max(0, days));
  return localDateValue(value);
}

export function dateStatusClass(status?: string) {
  if (status === "extracted") return "relationship-date-status is-ready";
  if (status === "partial") return "relationship-date-status is-imported";
  return "relationship-date-status is-empty";
}

export function dateStatusLabel(status?: string) {
  if (status === "extracted") return "已导入";
  if (status === "partial") return "部分导入";
  if (status === "not_extracted") return "未导入";
  return "未加载";
}

export function jobCount(counts: MemoryExtractionJobStatusCounts | undefined, status: string) {
  return counts?.[status] ?? 0;
}

export function jobCountsTotal(counts: MemoryExtractionJobStatusCounts | undefined) {
  if (!counts) return 0;
  return Object.values(counts).reduce<number>((sum, value) => sum + (Number(value) || 0), 0);
}

export function jobStatsSummary(stats: MemoryExtractionJobStatsResponse | null) {
  const counts = stats?.status_counts || stats?.counts;
  return {
    pending: jobCount(counts, "pending"),
    running: jobCount(counts, "running"),
    succeeded: jobCount(counts, "succeeded"),
    failed: jobCount(counts, "failed"),
    dead: jobCount(counts, "dead"),
    total: jobCountsTotal(counts),
    ready: stats?.retry_counts?.ready ?? 0,
    delayed: stats?.retry_counts?.delayed ?? 0,
  };
}

export function backfillSummary(result: MemoryBackfillResponse) {
  return {
    ok: result.ok,
    processed_count: result.processed_count ?? 0,
    imported_count: result.imported_count ?? 0,
    duplicate_count: result.duplicate_count ?? 0,
    skipped_count: result.skipped_count ?? 0,
    events_inserted: result.events_inserted ?? 0,
    items_created: result.items_created ?? 0,
    items_updated: result.items_updated ?? 0,
    items_pending: result.items_pending ?? 0,
    jobs_enqueued: result.jobs_enqueued ?? 0,
    graph_reloaded: true,
  };
}

export function safeBackfillDebug(result: MemoryBackfillResponse) {
  const omittedFields = ["sessions", "identity_profile", "session_profiles"].filter((field) => field in result);
  return {
    summary: backfillSummary(result),
    backend_flags: {
      ok: result.ok,
      llm_jobs_enabled: result.llm_jobs_enabled,
      session_count: result.session_count,
      events_duplicate: result.events_duplicate,
    },
    backend_fields: Object.keys(result).sort(),
    omitted_fields: omittedFields,
    note: "为避免显示原始聊天内容，调试输出仅保留计数、状态和字段名。",
  };
}

export function fieldLabel(label: string, technicalName: string) {
  return (
    <span className="relationship-field-label">
      <strong>{label}</strong>
      <small>{technicalName}</small>
    </span>
  );
}

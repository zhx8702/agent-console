import {
  acceptanceStatusOf,
  acceptanceStatusLabel,
  formatConfidence,
  formatTimestamp,
  memoryItemDisplayTitle,
  memoryItemStatusLabel,
  memoryScopeTypeLabel,
  memorySensitivityLabel,
  memorySourceTypeLabel,
  type MemoryItem,
} from "./model";

type MemoryItemListProps = {
  items: MemoryItem[];
  selectedItemId: number | null;
  emptyText: string;
  onSelect: (item: MemoryItem) => void;
};

export function MemoryItemList({ items, selectedItemId, emptyText, onSelect }: MemoryItemListProps) {
  return (
    <div className="memory-items-list">
      {items.map((item) => (
        <button key={item.id} className={`memory-item-card${selectedItemId === item.id ? " is-active" : ""}`} onClick={() => onSelect(item)}>
          <div className="memory-item-card-head">
            <strong>{memoryItemDisplayTitle(item)}</strong>
            <span className={`pill ${item.status === "active" ? "pill-ok" : item.status === "deleted" ? "pill-danger" : "pill-muted"}`}>{memoryItemStatusLabel(item.status)}</span>
          </div>
          <div className="memory-item-chip-row">
            <span className="pill pill-feature">{memoryScopeTypeLabel(item.scope_type)}</span><span className="pill pill-muted">{memorySourceTypeLabel(item.source_type)}</span>
            <span className="pill pill-muted">{item.memory_type || "未分类"}</span><span className="pill pill-muted">{memorySensitivityLabel(item.sensitivity)}</span>
          </div>
          <div className="memory-item-meta">
            <span>置信度 {Number(item.confidence ?? 0).toFixed(2)}</span><span>采纳状态 {acceptanceStatusLabel(acceptanceStatusOf(item))} {formatConfidence(item.acceptance_score)}</span>
            {item.duplicate_hint && Number(item.duplicate_hint.count || 0) > 0 && <span>疑似重复 {item.duplicate_hint.count} 条：{(item.duplicate_hint.ids || []).join(", ")}</span>}
            {item.extraction_confidence !== undefined && item.extraction_confidence !== null && <span>抽取置信度 {formatConfidence(item.extraction_confidence)}</span>}
            <span>{item.pinned ? "已置顶" : "未置顶"}</span><span>优先级 {item.priority ?? 0}</span><span>出现次数 {item.occurrence_count ?? 0}</span>
            <span>更新于 {formatTimestamp(item.updated_at)}</span><span>最近出现 {formatTimestamp(item.last_seen_at)}</span>
          </div>
          <div className="memory-item-context mono">{item.user_id || "-"} · {item.session_id || "跨会话身份记忆"}</div>
        </button>
      ))}
      {!items.length && <div className="admin-notice">{emptyText}</div>}
    </div>
  );
}

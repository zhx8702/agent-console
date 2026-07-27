import {
  type MemoryGraphSort,
  GRAPH_ENTITY_TYPE_LABELS,
  GRAPH_PREDICATE_LABELS,
  copyToClipboard,
  graphHumanLabel,
  graphStatusLabel,
} from "./model";
import { type MemoryGraphController } from "./useMemoryGraphController";

interface MemoryGraphFiltersProps {
  controller: MemoryGraphController;
  limit: number;
  onLimitChange: (value: number) => void;
  includeReviewControls?: boolean;
}

export function MemoryGraphFilters({
  controller,
  limit,
  onLimitChange,
  includeReviewControls = false,
}: MemoryGraphFiltersProps) {
  const {
    memoryGraphEntities,
    memoryGraphFacts,
    memoryGraphStatusFilter,
    setMemoryGraphStatusFilter,
    memoryGraphSearch,
    setMemoryGraphSearch,
    memoryGraphEntityTypeFilter,
    setMemoryGraphEntityTypeFilter,
    memoryGraphPredicateFilter,
    setMemoryGraphPredicateFilter,
    memoryGraphConfidenceMin,
    setMemoryGraphConfidenceMin,
    memoryGraphEvidenceOnly,
    setMemoryGraphEvidenceOnly,
    memoryGraphHideReviewed,
    setMemoryGraphHideReviewed,
    memoryGraphReviewedIds,
    setMemoryGraphReviewedIds,
    memoryGraphSort,
    setMemoryGraphSort,
    memoryGraphTab,
    clearMemoryGraphFilters,
    graphEntityTypeFilterCounts,
    graphPredicateFilterCounts,
    currentVisibleGraphIds,
    copyVisibleGraphQualityReport,
    hasActiveMemoryGraphFilters,
    visibleMemoryGraphTotal,
  } = controller;

  return (
    <section className="memory-graph-sidebar-section">
      <div className="memory-graph-sidebar-header">
        <p className="section-kicker">图谱筛选</p>
        <strong>搜索、过滤、排序</strong>
      </div>
      <div className="form-grid memory-graph-filters">
        <label className="field">
          <span>图谱状态</span>
          <select value={memoryGraphStatusFilter} onChange={(event) => setMemoryGraphStatusFilter(event.target.value)}>
            <option value="">全部</option>
            {["active", "pending", "archived", "invalidated", "deleted"].map((status) => (
              <option value={status} key={status}>{graphStatusLabel(status)}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>最多显示条数</span>
          <input type="number" min={1} max={500} value={limit} onChange={(event) => onLimitChange(Number(event.target.value) || 50)} />
        </label>
        <label className="field">
          <span>搜索</span>
          <input
            value={memoryGraphSearch}
            onChange={(event) => setMemoryGraphSearch(event.target.value)}
            placeholder="名称、关系或来源 ID"
          />
        </label>
        <label className="field">
          <span>实体类型</span>
          <select value={memoryGraphEntityTypeFilter} onChange={(event) => setMemoryGraphEntityTypeFilter(event.target.value)}>
            <option value="">全部 ({memoryGraphEntities.length})</option>
            {graphEntityTypeFilterCounts.map(([type, count]) => (
              <option value={type} key={type}>{graphHumanLabel(type, GRAPH_ENTITY_TYPE_LABELS)} ({count})</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>关系类型</span>
          <select value={memoryGraphPredicateFilter} onChange={(event) => setMemoryGraphPredicateFilter(event.target.value)}>
            <option value="">全部 ({memoryGraphFacts.length})</option>
            {graphPredicateFilterCounts.map(([predicate, count]) => (
              <option value={predicate} key={predicate}>{graphHumanLabel(predicate, GRAPH_PREDICATE_LABELS)} ({count})</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>最低置信度</span>
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={memoryGraphConfidenceMin}
            onChange={(event) => setMemoryGraphConfidenceMin(event.target.value)}
            placeholder="0..1"
          />
        </label>
        <label className="field">
          <span>排序</span>
          <select value={memoryGraphSort} onChange={(event) => setMemoryGraphSort(event.target.value as MemoryGraphSort)}>
            <option value="updated_desc">最近更新</option>
            <option value="confidence_desc">{memoryGraphTab === "episodes" ? "重要性优先" : "置信度优先"}</option>
            <option value="name_asc">{memoryGraphTab === "facts" ? "关系名称升序" : "名称升序"}</option>
          </select>
        </label>
        <div className="field field-toggle memory-graph-evidence-toggle">
          <span>证据</span>
          <label className="toggle-chip">
            <span>
              <input
                type="checkbox"
                checked={memoryGraphEvidenceOnly}
                onChange={(event) => setMemoryGraphEvidenceOnly(event.target.checked)}
              />
              只看有证据项
            </span>
            <em>按记忆 ID 或来源 ID 判断</em>
          </label>
        </div>
        {includeReviewControls && (
          <div className="field field-toggle memory-graph-evidence-toggle">
            <span>本地复核</span>
            <label className="toggle-chip">
              <span>
                <input
                  type="checkbox"
                  checked={memoryGraphHideReviewed}
                  onChange={(event) => setMemoryGraphHideReviewed(event.target.checked)}
                />
                隐藏已本地复核
              </span>
              <em>只影响当前浏览器视图</em>
            </label>
          </div>
        )}
      </div>
      <div className="action-row memory-graph-actions">
        <button
          className="button button-secondary"
          type="button"
          onClick={clearMemoryGraphFilters}
          disabled={!hasActiveMemoryGraphFilters}
        >
          清空筛选
        </button>
        <button
          className="button button-secondary"
          type="button"
          onClick={() => copyToClipboard(currentVisibleGraphIds.join("\n"))}
          disabled={!currentVisibleGraphIds.length}
        >
          复制当前 ID
        </button>
        {includeReviewControls && (
          <>
            <button
              className="button button-secondary"
              type="button"
              onClick={copyVisibleGraphQualityReport}
              disabled={!visibleMemoryGraphTotal}
            >
              复制复核报告
            </button>
            <button
              className="button button-secondary"
              type="button"
              onClick={() => setMemoryGraphReviewedIds(new Set())}
              disabled={!memoryGraphReviewedIds.size}
            >
              清空本地复核
            </button>
          </>
        )}
      </div>
    </section>
  );
}

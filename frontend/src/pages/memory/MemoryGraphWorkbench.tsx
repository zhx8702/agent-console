import { type ReactNode } from "react";

import { TabList } from "../../components/Tabs";
import { MemoryGraphFilters } from "./MemoryGraphFilters";
import { MemoryGraphInspection } from "./MemoryGraphInspection";
import {
  type MemoryGraphEmptyContext,
  type MemoryGraphMode,
  type MemoryGraphTab,
  GRAPH_ENTITY_TYPE_LABELS,
  GRAPH_PREDICATE_LABELS,
  GRAPH_REVIEW_GROUPS,
  copyGraphScope,
  countDefinedIds,
  formatConfidence,
  formatTimestamp,
  graphEntityLabel,
  graphEpisodeLabel,
  graphFactObject,
  graphFactSentence,
  graphFactSubject,
  graphHumanLabel,
  graphQualityBadges,
  graphStatusLabel,
  graphStatusPillClass,
  hasDefinedId,
} from "./model";
import { useMemoryGraphController } from "./useMemoryGraphController";

interface MemoryGraphWorkbenchProps {
  sessionId: string;
  channel: string;
  sourceKey: string;
  userId: string;
  limit: number;
  onLimitChange: (value: number) => void;
  selectedSessionIsGroup: boolean;
  selectedMemberIsVerified: boolean;
  onOutput: (value: string) => void;
}

export function MemoryGraphWorkbench({
  sessionId,
  channel,
  sourceKey,
  userId,
  limit,
  onLimitChange,
  selectedSessionIsGroup,
  selectedMemberIsVerified,
  onOutput: setMemoryGraphOutput,
}: MemoryGraphWorkbenchProps) {
  const controller = useMemoryGraphController({
    sessionId,
    channel,
    sourceKey,
    userId,
    limit,
    selectedSessionIsGroup,
    selectedMemberIsVerified,
    onOutput: setMemoryGraphOutput,
  });
  const {
    memoryGraphModeTabsId,
    memoryGraphDataTabsId,
    memoryGraphEntities,
    memoryGraphFacts,
    memoryGraphEpisodes,
    memoryGraphHideReviewed,
    setMemoryGraphHideReviewed,
    memoryGraphReviewedIds,
    setMemoryGraphReviewedIds,
    memoryGraphMode,
    setMemoryGraphMode,
    memoryGraphTab,
    setMemoryGraphTab,
    memoryGraphSelection,
    loadMemoryGraph,
    clearMemoryGraphFilters,
    resetMemoryGraphBroadScope,
    memoryGraphLoadedCounts,
    filteredMemoryGraphEntities,
    filteredMemoryGraphFacts,
    filteredMemoryGraphEpisodes,
    graphEvidenceCounts,
    memoryGraphVisibleCounts,
    visibleMemoryGraphTotal,
    loadedMemoryGraphTotal,
    hiddenMemoryGraphTotal,
    memoryGraphScopeFields,
    visibleGraphReviewGroups,
    visibleGraphQualitySummary,
    visibleGraphReviewSummary,
    selectMemoryGraphItem,
    markMemoryGraphReviewed,
    visibleReviewedGraphTotal,
    copyVisibleGraphQualityReport,
    missingMemoryGraphScope,
    isMemoryGraphScopeComplete,
    hasNoMemoryGraphData,
    hasActiveMemoryGraphFilters,
    hasVisibleGraphReviewItems,
    hasHiddenLocalReviewedGraphItems,
    memoryGraphStateLabel,
    memoryGraphNextAction,
    memoryGraphGuideSteps,
    memoryGraphSamples,
    getMemoryGraphEmptyState,
  } = controller;

  const renderMemoryGraphEmptyState = (context: MemoryGraphEmptyContext) => {
    const state = getMemoryGraphEmptyState(context);
    const actions = state.actions.filter((action, index, list) => list.indexOf(action) === index);
    return (
      <div className={`memory-graph-empty-state${state.tone === "warning" ? " is-warning" : ""}`}>
        <strong>{state.title}</strong>
        <span>{state.body}</span>
        {actions.length > 0 && (
          <div className="memory-graph-empty-actions">
            {actions.includes("clearFilters") && (
              <button className="button button-secondary button-compact" type="button" onClick={clearMemoryGraphFilters} disabled={!hasActiveMemoryGraphFilters}>
                清空筛选
              </button>
            )}
            {actions.includes("broadScope") && (
              <button className="button button-secondary button-compact" type="button" onClick={resetMemoryGraphBroadScope}>
                重置当前群筛选
              </button>
            )}
            {actions.includes("refresh") && (
              <button className="button button-secondary button-compact" type="button" onClick={() => void loadMemoryGraph()}>
                刷新图谱
              </button>
            )}
            {actions.includes("clearReviewed") && (
              <button className="button button-secondary button-compact" type="button" onClick={() => setMemoryGraphReviewedIds(new Set())} disabled={!memoryGraphReviewedIds.size}>
                清空本地复核
              </button>
            )}
          </div>
        )}
      </div>
    );
  };

  const renderMemoryGraphSummaryCards = () => (
    <div className="memory-graph-preview-grid">
      <div className="summary-card">
        <span>关系</span>
        <strong>{memoryGraphVisibleCounts.facts} / {memoryGraphLoadedCounts.facts}</strong>
      </div>
      <div className="summary-card">
        <span>实体</span>
        <strong>{memoryGraphVisibleCounts.entities} / {memoryGraphLoadedCounts.entities}</strong>
      </div>
      <div className="summary-card">
        <span>事件片段</span>
        <strong>{memoryGraphVisibleCounts.episodes} / {memoryGraphLoadedCounts.episodes}</strong>
      </div>
      <div className="summary-card">
        <span>证据事件</span>
        <strong>{graphEvidenceCounts.sourceEvents}</strong>
      </div>
      <div className="summary-card">
        <span>关联记忆项</span>
        <strong>{graphEvidenceCounts.memoryItems}</strong>
      </div>
      <div className="summary-card">
        <span>需复核</span>
        <strong>
          {visibleGraphReviewSummary.lowConfidence +
            visibleGraphReviewSummary.noEvidence +
            visibleGraphReviewSummary.stale +
            visibleGraphReviewSummary.inactiveStatus}
        </strong>
      </div>
    </div>
  );

  const renderMemoryGraphGuide = () => (
    <details className="memory-graph-guide" open>
      <summary>
        <span>开始使用</span>
        <em>{memoryGraphStateLabel}</em>
      </summary>
      <p className="memory-graph-guide-next">{memoryGraphNextAction}</p>
      <ol>
        {memoryGraphGuideSteps.slice(0, 4).map((step) => (
          <li
            className={`${step.done ? "is-done" : ""}${step.active ? " is-active" : ""}`}
            key={step.label}
          >
            <span>{step.label}</span>
            <small>{step.detail}</small>
          </li>
        ))}
      </ol>
    </details>
  );

  const renderMemoryGraphSampleList = (
    title: string,
    emptyContext: MemoryGraphEmptyContext,
    children: ReactNode,
    hasItems: boolean,
  ) => (
    <div className="memory-graph-sample-card">
      <div className="memory-graph-sample-title">{title}</div>
      {hasItems ? <ul>{children}</ul> : <div className="memory-graph-sample-empty">{getMemoryGraphEmptyState(emptyContext).body}</div>}
    </div>
  );

  return (
      <section className="panel span-3 memory-graph-panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">记忆图谱</p>
            <h3>记忆图谱工作区</h3>
          </div>
          <span className="pill pill-feature">
            {visibleMemoryGraphTotal} / {loadedMemoryGraphTotal}
          </span>
        </div>
        <div className="memory-graph-querybar">
          <div className="memory-graph-querybar-main">
            <p className="muted-copy">
              图谱固定按当前已验证群聊和成员读取。默认先从“概览”查看关系和摘要，需要深入时再进入“探索”“复核”或“技术详情”。
            </p>
            <div className="admin-notice admin-notice-warning">
              技术详情用于排障和复核；页面只展示图谱元数据和 ID，不作为聊天正文导出入口。
            </div>
            <div className="memory-graph-scope" aria-label="当前图谱查询范围">
              {memoryGraphScopeFields.map((field) => (
                <span className="memory-graph-scope-item" key={field.label}>
                  <span>{field.label}</span>
                  <strong className="mono">{field.value || "-"}</strong>
                </span>
              ))}
            </div>
          </div>
          <div className="action-row memory-graph-query-actions">
            <button className="button button-secondary" type="button" onClick={resetMemoryGraphBroadScope}>
              重置当前群筛选
            </button>
            <button
              className={`button ${isMemoryGraphScopeComplete ? "button-primary memory-graph-refresh" : "button-secondary"}`}
              onClick={() => void loadMemoryGraph()}
            >
              刷新图谱
            </button>
          </div>
        </div>
        {!isMemoryGraphScopeComplete && (
          <div className="admin-notice admin-notice-warning">
            图谱查询范围缺少 {missingMemoryGraphScope.join(" / ")}。请先补全这些字段，然后点击“刷新图谱”重新读取。
          </div>
        )}

        <TabList
          tabs={[
            { id: "overview", label: <>概览 <span className="tab-count">{visibleMemoryGraphTotal}</span></> },
            { id: "explore", label: <>探索 <span className="tab-count">{visibleMemoryGraphTotal}</span></> },
            {
              id: "review",
              label: <>复核 <span className="tab-count">{
                visibleGraphReviewSummary.lowConfidence +
                visibleGraphReviewSummary.noEvidence +
                visibleGraphReviewSummary.stale +
                visibleGraphReviewSummary.inactiveStatus
              }</span></>,
            },
            { id: "raw", label: <>技术详情 <span className="tab-count">{loadedMemoryGraphTotal}</span></> },
          ]}
          activeId={memoryGraphMode}
          onChange={(id) => {
            const nextMode = id as MemoryGraphMode;
            setMemoryGraphMode(nextMode);
            if ((nextMode === "explore" || nextMode === "raw") && memoryGraphTab === "preview") {
              setMemoryGraphTab("facts");
            }
          }}
          ariaLabel="记忆图谱视图"
          idPrefix={memoryGraphModeTabsId}
          className="tab-bar memory-graph-mode-tabs"
          triggerClassName={(_, selected) => `tab-btn${selected ? " active" : ""}`}
        />

        {memoryGraphMode === "overview" && (
          <div
            id={`${memoryGraphModeTabsId}-panel-overview`}
            className="memory-graph-overview"
            role="tabpanel"
            aria-labelledby={`${memoryGraphModeTabsId}-tab-overview`}
            tabIndex={0}
          >
            <div className="memory-graph-overview-start">
              {renderMemoryGraphGuide()}
              <div className="memory-graph-overview-actions">
                <button
                  className="button button-primary"
                  type="button"
                  onClick={() => {
                    setMemoryGraphMode("explore");
                    setMemoryGraphTab("facts");
                  }}
                >
                  探索关系
                </button>
                <button className="button button-secondary" type="button" onClick={() => setMemoryGraphMode("review")}>
                  复核问题
                </button>
                <button className="button button-secondary" type="button" onClick={() => setMemoryGraphMode("raw")}>
                  查看原始元数据
                </button>
              </div>
            </div>
            {renderMemoryGraphSummaryCards()}
            {loadedMemoryGraphTotal > 0 && visibleMemoryGraphTotal === 0 && hasActiveMemoryGraphFilters && renderMemoryGraphEmptyState("preview")}
            {hasNoMemoryGraphData && renderMemoryGraphEmptyState("preview")}
            {!hasNoMemoryGraphData && (
              <div className="memory-graph-overview-grid">
                {renderMemoryGraphSampleList(
                  "最近关系",
                  "facts",
                  memoryGraphSamples.facts.map((item) => (
                    <li className="memory-graph-sample-entry" key={item.id}>
                      <button className="memory-graph-clickable-item" type="button" onClick={() => selectMemoryGraphItem("fact", item)}>
                        <strong>{graphFactSentence(item)}</strong>
                        <span className={graphStatusPillClass(item.status)}>{graphStatusLabel(item.status)}</span>
                        {graphQualityBadges("fact", item)}
                        <span>{formatTimestamp(item.updated_at)}</span>
                        <small>点击进入探索视图</small>
                      </button>
                    </li>
                  )),
                  memoryGraphSamples.facts.length > 0,
                )}
                {renderMemoryGraphSampleList(
                  "最近实体",
                  "entities",
                  memoryGraphSamples.entities.map((item) => (
                    <li className="memory-graph-sample-entry" key={item.id}>
                      <button className="memory-graph-clickable-item" type="button" onClick={() => selectMemoryGraphItem("entity", item)}>
                        <strong>{graphEntityLabel(item)}</strong>
                        <span>{graphHumanLabel(item.entity_type, GRAPH_ENTITY_TYPE_LABELS)}</span>
                        <span className={graphStatusPillClass(item.status)}>{graphStatusLabel(item.status)}</span>
                        {graphQualityBadges("entity", item)}
                        <span>{formatTimestamp(item.updated_at)}</span>
                      </button>
                    </li>
                  )),
                  memoryGraphSamples.entities.length > 0,
                )}
                {renderMemoryGraphSampleList(
                  "最近事件片段",
                  "episodes",
                  memoryGraphSamples.episodes.map((item) => (
                    <li className="memory-graph-sample-entry" key={item.id}>
                      <button className="memory-graph-clickable-item" type="button" onClick={() => selectMemoryGraphItem("episode", item)}>
                        <strong>{graphEpisodeLabel(item)}</strong>
                        <span className={graphStatusPillClass(item.status)}>{graphStatusLabel(item.status)}</span>
                        {graphQualityBadges("episode", item)}
                        <span>{countDefinedIds(item.event_ids) + countDefinedIds(item.memory_item_ids)} 个来源</span>
                        <span>{formatTimestamp(item.updated_at)}</span>
                      </button>
                    </li>
                  )),
                  memoryGraphSamples.episodes.length > 0,
                )}
                <div className="memory-graph-review-panel memory-graph-overview-review">
                  <div className="memory-graph-review-header">
                    <div>
                      <p className="section-kicker">需复核</p>
                      <strong>质量摘要</strong>
                      <span className="memory-graph-review-note">详细队列请在复核视图中处理。</span>
                    </div>
                  </div>
                  <div className="memory-graph-review-grid">
                    {GRAPH_REVIEW_GROUPS.map((group) => (
                      <section className={`memory-graph-review-group${visibleGraphReviewGroups[group.key].length ? " is-warning" : ""}`} key={group.key}>
                        <div className="memory-graph-review-group-head">
                          <div>
                            <strong>{visibleGraphReviewGroups[group.key].length}</strong>
                            <span>{group.label}</span>
                          </div>
                          <small>{group.hint}</small>
                        </div>
                      </section>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {memoryGraphMode === "explore" && (
          <div
            id={`${memoryGraphModeTabsId}-panel-explore`}
            className="memory-graph-workbench memory-graph-workbench-explore"
            role="tabpanel"
            aria-labelledby={`${memoryGraphModeTabsId}-tab-explore`}
            tabIndex={0}
          >
            <aside className="memory-graph-sidebar">
              <MemoryGraphFilters controller={controller} limit={limit} onLimitChange={onLimitChange} />
            </aside>
            <div className="memory-graph-main">
              <TabList
                tabs={[
                  { id: "entities", label: <>实体 <span className="tab-count">{filteredMemoryGraphEntities.length}</span></> },
                  { id: "facts", label: <>关系 <span className="tab-count">{filteredMemoryGraphFacts.length}</span></> },
                  { id: "episodes", label: <>事件片段 <span className="tab-count">{filteredMemoryGraphEpisodes.length}</span></> },
                ]}
                activeId={memoryGraphTab}
                onChange={(id) => setMemoryGraphTab(id as MemoryGraphTab)}
                ariaLabel="探索图谱数据类型"
                idPrefix={memoryGraphDataTabsId}
                className="tab-bar memory-graph-tabs"
                triggerClassName={(_, selected) => `tab-btn${selected ? " active" : ""}`}
              />
              {memoryGraphTab === "entities" && (
                <div
                  id={`${memoryGraphDataTabsId}-panel-entities`}
                  className="table-scroll memory-graph-table-scroll"
                  role="tabpanel"
                  aria-labelledby={`${memoryGraphDataTabsId}-tab-entities`}
                  tabIndex={0}
                >
                  <table>
                    <caption className="sr-only">当前群成员记忆图谱实体</caption>
                    <thead><tr><th scope="col">实体</th><th scope="col">类型</th><th scope="col">标准化名称</th><th scope="col">别名</th><th scope="col">状态</th><th scope="col">置信度</th><th scope="col">更新时间</th></tr></thead>
                    <tbody>
                      {filteredMemoryGraphEntities.map((item) => (
                        <tr className={`memory-graph-clickable-row${memoryGraphSelection?.kind === "entity" && memoryGraphSelection.item.id === item.id ? " is-selected" : ""}`} key={item.id}>
                          <th scope="row"><button type="button" className="memory-graph-row-action" onClick={() => selectMemoryGraphItem("entity", item)}><span className="memory-graph-primary-cell"><strong>{graphEntityLabel(item)}</strong><span className="mono">#{item.id}</span>{graphQualityBadges("entity", item)}<small>查看详情</small></span></button></th>
                          <td>{graphHumanLabel(item.entity_type, GRAPH_ENTITY_TYPE_LABELS)}</td>
                          <td className="mono">{item.normalized_name || "-"}</td>
                          <td>{(item.aliases || []).slice(0, 3).join(", ") || "-"}</td>
                          <td><span className={graphStatusPillClass(item.status)}>{graphStatusLabel(item.status)}</span></td>
                          <td>{formatConfidence(item.confidence)}</td>
                          <td>{formatTimestamp(item.updated_at)}</td>
                        </tr>
                      ))}
                      {!filteredMemoryGraphEntities.length && <tr><td className="empty-cell" colSpan={7}>{renderMemoryGraphEmptyState("entities")}</td></tr>}
                    </tbody>
                  </table>
                </div>
              )}
              {memoryGraphTab === "facts" && (
                <div
                  id={`${memoryGraphDataTabsId}-panel-facts`}
                  className="table-scroll memory-graph-table-scroll"
                  role="tabpanel"
                  aria-labelledby={`${memoryGraphDataTabsId}-tab-facts`}
                  tabIndex={0}
                >
                  <table>
                    <caption className="sr-only">当前群成员记忆图谱关系事实</caption>
                    <thead><tr><th scope="col">关系</th><th scope="col">主体</th><th scope="col">对象</th><th scope="col">证据</th><th scope="col">状态</th><th scope="col">置信度</th><th scope="col">生效时间</th><th scope="col">失效时间</th><th scope="col">更新时间</th></tr></thead>
                    <tbody>
                      {filteredMemoryGraphFacts.map((item) => (
                        <tr className={`memory-graph-clickable-row${memoryGraphSelection?.kind === "fact" && memoryGraphSelection.item.id === item.id ? " is-selected" : ""}`} key={item.id}>
                          <th scope="row"><button type="button" className="memory-graph-row-action" onClick={() => selectMemoryGraphItem("fact", item)}><span className="memory-graph-primary-cell"><strong>{graphHumanLabel(item.predicate, GRAPH_PREDICATE_LABELS)}</strong><span className="mono">关系 #{item.id}</span>{graphQualityBadges("fact", item)}<small>查看详情</small></span></button></th>
                          <td>{graphFactSubject(item)}</td>
                          <td>{graphFactObject(item)}</td>
                          <td><div className="memory-graph-evidence-cell"><span>记忆 {item.memory_item_id ? `#${item.memory_item_id}` : "-"}</span><span>事件 {item.source_event_id ? `#${item.source_event_id}` : "-"}</span></div></td>
                          <td><span className={graphStatusPillClass(item.status)}>{graphStatusLabel(item.status)}</span></td>
                          <td>{formatConfidence(item.confidence)}</td>
                          <td>{formatTimestamp(item.valid_at)}</td>
                          <td>{formatTimestamp(item.invalid_at)}</td>
                          <td>{formatTimestamp(item.updated_at)}</td>
                        </tr>
                      ))}
                      {!filteredMemoryGraphFacts.length && <tr><td className="empty-cell" colSpan={9}>{renderMemoryGraphEmptyState("facts")}</td></tr>}
                    </tbody>
                  </table>
                </div>
              )}
              {memoryGraphTab === "episodes" && (
                <div
                  id={`${memoryGraphDataTabsId}-panel-episodes`}
                  className="table-scroll memory-graph-table-scroll"
                  role="tabpanel"
                  aria-labelledby={`${memoryGraphDataTabsId}-tab-episodes`}
                  tabIndex={0}
                >
                  <table>
                    <caption className="sr-only">当前群成员记忆图谱事件片段</caption>
                    <thead><tr><th scope="col">标题</th><th scope="col">安全摘要</th><th scope="col">证据</th><th scope="col">状态</th><th scope="col">重要性</th><th scope="col">会话</th><th scope="col">更新时间</th></tr></thead>
                    <tbody>
                      {filteredMemoryGraphEpisodes.map((item) => (
                        <tr className={`memory-graph-clickable-row${memoryGraphSelection?.kind === "episode" && memoryGraphSelection.item.id === item.id ? " is-selected" : ""}`} key={item.id}>
                          <th scope="row"><button type="button" className="memory-graph-row-action" onClick={() => selectMemoryGraphItem("episode", item)}><span className="memory-graph-primary-cell"><strong>{graphEpisodeLabel(item)}</strong><span className="mono">#{item.id}</span>{graphQualityBadges("episode", item)}<small>查看详情</small></span></button></th>
                          <td>{item.summary ? "已隐藏（不展示正文）" : "-"}</td>
                          <td><div className="memory-graph-evidence-cell"><span>{countDefinedIds(item.memory_item_ids)} 条记忆</span><span>{countDefinedIds(item.event_ids)} 个事件</span></div></td>
                          <td><span className={graphStatusPillClass(item.status)}>{graphStatusLabel(item.status)}</span></td>
                          <td>{item.importance ?? "-"}</td>
                          <td className="mono">{item.session_id || "-"}</td>
                          <td>{formatTimestamp(item.updated_at)}</td>
                        </tr>
                      ))}
                      {!filteredMemoryGraphEpisodes.length && <tr><td className="empty-cell" colSpan={7}>{renderMemoryGraphEmptyState("episodes")}</td></tr>}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <MemoryGraphInspection controller={controller} />
          </div>
        )}

        {memoryGraphMode === "review" && (
          <div
            id={`${memoryGraphModeTabsId}-panel-review`}
            className="memory-graph-workbench memory-graph-workbench-review"
            role="tabpanel"
            aria-labelledby={`${memoryGraphModeTabsId}-tab-review`}
            tabIndex={0}
          >
            <div className="memory-graph-main">
              <div className="memory-graph-review-panel">
                <div className="memory-graph-review-header">
                  <div>
                    <p className="section-kicker">图谱复核</p>
                    <strong>质量复核队列</strong>
                    <span className="memory-graph-review-note">技术复核视图；本地复核标记只影响当前浏览器视图，不会修改记忆数据，也不提供聊天正文。</span>
                  </div>
                  <div className="memory-graph-review-tools">
                    <span className="pill pill-muted">筛选隐藏 {hiddenMemoryGraphTotal} 项</span>
                    <span className="pill pill-muted">本地已复核 {visibleReviewedGraphTotal} 项</span>
                    <button className="button button-secondary button-compact" type="button" onClick={copyVisibleGraphQualityReport} disabled={!visibleMemoryGraphTotal}>
                      复制报告
                    </button>
                    <button className="button button-secondary button-compact" type="button" onClick={() => setMemoryGraphReviewedIds(new Set())} disabled={!memoryGraphReviewedIds.size}>
                      清空本地复核
                    </button>
                  </div>
                </div>
                <div className="form-grid memory-graph-review-controls">
                  <div className="field field-toggle memory-graph-evidence-toggle">
                    <span>本地复核</span>
                    <label className="toggle-chip">
                      <span>
                        <input type="checkbox" checked={memoryGraphHideReviewed} onChange={(event) => setMemoryGraphHideReviewed(event.target.checked)} />
                        隐藏已本地复核
                      </span>
                      <em>仅影响当前浏览器视图</em>
                    </label>
                  </div>
                </div>
                {visibleMemoryGraphTotal ? (
                  <>
                    {(!hasVisibleGraphReviewItems || hasHiddenLocalReviewedGraphItems) && renderMemoryGraphEmptyState("review")}
                    <div className="memory-graph-review-grid">
                      {GRAPH_REVIEW_GROUPS.map((group) => {
                        const entries = visibleGraphReviewGroups[group.key];
                        return (
                          <section className={`memory-graph-review-group${entries.length ? " is-warning" : ""}`} key={group.key}>
                            <div className="memory-graph-review-group-head">
                              <div><strong>{entries.length}</strong><span>{group.label}</span></div>
                              <small>{group.hint}</small>
                            </div>
                            {entries.length ? (
                              <div className="memory-graph-review-items">
                                {entries.map((entry) => (
                                  <div
                                    className={`memory-graph-review-item${memoryGraphSelection?.kind === entry.kind && memoryGraphSelection.item.id === entry.item.id ? " is-selected" : ""}`}
                                    key={`${group.key}:${entry.key}`}
                                  >
                                    <button
                                      className="memory-graph-review-select"
                                      type="button"
                                      onClick={() => selectMemoryGraphItem(entry.kind, entry.item, "review")}
                                    >
                                      <span className="memory-graph-review-item-main"><strong className="mono">{entry.key}</strong><span>{entry.label}</span></span>
                                    </button>
                                    <button className="button button-secondary button-compact" type="button" onClick={() => markMemoryGraphReviewed(entry.kind, entry.item.id)}>
                                      标记本地复核
                                    </button>
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <p className="memory-graph-review-empty">
                                {memoryGraphHideReviewed && visibleGraphQualitySummary.ids[group.key].length > 0 ? "本组待审查项已被本地复核隐藏。" : "无待审查项。"}
                              </p>
                            )}
                          </section>
                        );
                      })}
                    </div>
                  </>
                ) : (
                  renderMemoryGraphEmptyState("review")
                )}
              </div>
            </div>
            <MemoryGraphInspection controller={controller} reviewMode />
          </div>
        )}

        {memoryGraphMode === "raw" && (
          <div
            id={`${memoryGraphModeTabsId}-panel-raw`}
            className="memory-graph-raw"
            role="tabpanel"
            aria-labelledby={`${memoryGraphModeTabsId}-tab-raw`}
            tabIndex={0}
          >
            <div className="memory-graph-raw-note">
              <strong>原始元数据 / 开发排障视图</strong>
              <span>技术详情。这里只保留实体、关系、事件片段的元数据、ID 和安全摘要；不提供聊天正文导出。普通检查请优先使用概览、探索和复核视图。</span>
              <button className="button button-secondary button-compact" type="button" onClick={() => copyGraphScope(memoryGraphScopeFields)}>
                复制查询范围
              </button>
            </div>
            <TabList
              tabs={[
                { id: "entities", label: <>实体 <span className="tab-count">{memoryGraphEntities.length}</span></> },
                { id: "facts", label: <>关系 <span className="tab-count">{memoryGraphFacts.length}</span></> },
                { id: "episodes", label: <>事件片段 <span className="tab-count">{memoryGraphEpisodes.length}</span></> },
              ]}
              activeId={memoryGraphTab}
              onChange={(id) => setMemoryGraphTab(id as MemoryGraphTab)}
              ariaLabel="图谱原始元数据类型"
              idPrefix={memoryGraphDataTabsId}
              className="tab-bar memory-graph-tabs"
              triggerClassName={(_, selected) => `tab-btn${selected ? " active" : ""}`}
            />
            {memoryGraphTab === "entities" && (
              <div
                id={`${memoryGraphDataTabsId}-panel-entities`}
                className="table-scroll memory-graph-table-scroll memory-graph-raw-table"
                role="tabpanel"
                aria-labelledby={`${memoryGraphDataTabsId}-tab-entities`}
                tabIndex={0}
              >
                <table>
                  <caption className="sr-only">记忆图谱实体原始元数据</caption>
                  <thead><tr><th scope="col">ID</th><th scope="col">名称</th><th scope="col">类型</th><th scope="col">标准化名称</th><th scope="col">别名</th><th scope="col">状态</th><th scope="col">置信度</th><th scope="col">记忆 ID</th><th scope="col">事件 ID</th><th scope="col">更新时间</th></tr></thead>
                  <tbody>
                    {memoryGraphEntities.map((item) => (
                      <tr className="memory-graph-clickable-row" key={item.id}>
                        <th scope="row" className="mono"><button type="button" className="memory-graph-row-action" onClick={() => selectMemoryGraphItem("entity", item)}>#{item.id}</button></th><td>{graphEntityLabel(item)}</td><td>{item.entity_type || "-"}</td><td className="mono">{item.normalized_name || "-"}</td><td>{(item.aliases || []).join(", ") || "-"}</td><td>{item.status || "-"}</td><td>{formatConfidence(item.confidence)}</td><td className="mono">{[item.memory_item_id, ...(item.memory_item_ids || [])].filter(hasDefinedId).join(", ") || "-"}</td><td className="mono">{[item.source_event_id, ...(item.source_event_ids || []), ...(item.event_ids || [])].filter(hasDefinedId).join(", ") || "-"}</td><td>{formatTimestamp(item.updated_at)}</td>
                      </tr>
                    ))}
                    {!memoryGraphEntities.length && <tr><td className="empty-cell" colSpan={10}>{renderMemoryGraphEmptyState("entities")}</td></tr>}
                  </tbody>
                </table>
              </div>
            )}
            {memoryGraphTab === "facts" && (
              <div
                id={`${memoryGraphDataTabsId}-panel-facts`}
                className="table-scroll memory-graph-table-scroll memory-graph-raw-table"
                role="tabpanel"
                aria-labelledby={`${memoryGraphDataTabsId}-tab-facts`}
                tabIndex={0}
              >
                <table>
                  <caption className="sr-only">记忆图谱关系原始元数据</caption>
                  <thead><tr><th scope="col">ID</th><th scope="col">主体</th><th scope="col">主体 ID</th><th scope="col">关系类型</th><th scope="col">对象</th><th scope="col">对象 ID</th><th scope="col">对象值</th><th scope="col">状态</th><th scope="col">置信度</th><th scope="col">记忆</th><th scope="col">事件</th><th scope="col">更新时间</th></tr></thead>
                  <tbody>
                    {memoryGraphFacts.map((item) => (
                      <tr className="memory-graph-clickable-row" key={item.id}>
                        <th scope="row" className="mono"><button type="button" className="memory-graph-row-action" onClick={() => selectMemoryGraphItem("fact", item)}>#{item.id}</button></th><td>{item.subject_name || "-"}</td><td className="mono">{item.subject_entity_id || "-"}</td><td>{item.predicate || "-"}</td><td>{item.object_name || "-"}</td><td className="mono">{item.object_entity_id || "-"}</td><td>{item.object_value || "-"}</td><td>{item.status || "-"}</td><td>{formatConfidence(item.confidence)}</td><td className="mono">{item.memory_item_id || "-"}</td><td className="mono">{item.source_event_id || "-"}</td><td>{formatTimestamp(item.updated_at)}</td>
                      </tr>
                    ))}
                    {!memoryGraphFacts.length && <tr><td className="empty-cell" colSpan={12}>{renderMemoryGraphEmptyState("facts")}</td></tr>}
                  </tbody>
                </table>
              </div>
            )}
            {memoryGraphTab === "episodes" && (
              <div
                id={`${memoryGraphDataTabsId}-panel-episodes`}
                className="table-scroll memory-graph-table-scroll memory-graph-raw-table"
                role="tabpanel"
                aria-labelledby={`${memoryGraphDataTabsId}-tab-episodes`}
                tabIndex={0}
              >
                <table>
                  <caption className="sr-only">记忆图谱事件原始元数据</caption>
                  <thead><tr><th scope="col">ID</th><th scope="col">标题</th><th scope="col">安全摘要</th><th scope="col">状态</th><th scope="col">重要性</th><th scope="col">会话</th><th scope="col">事件 ID</th><th scope="col">记忆 ID</th><th scope="col">更新时间</th></tr></thead>
                  <tbody>
                    {memoryGraphEpisodes.map((item) => (
                      <tr className="memory-graph-clickable-row" key={item.id}>
                        <th scope="row" className="mono"><button type="button" className="memory-graph-row-action" onClick={() => selectMemoryGraphItem("episode", item)}>#{item.id}</button></th><td>{graphEpisodeLabel(item)}</td><td>{item.summary ? "已隐藏（不展示正文）" : "-"}</td><td>{item.status || "-"}</td><td>{item.importance ?? "-"}</td><td className="mono">{item.session_id || "-"}</td><td className="mono">{(item.event_ids || []).filter(hasDefinedId).join(", ") || "-"}</td><td className="mono">{(item.memory_item_ids || []).filter(hasDefinedId).join(", ") || "-"}</td><td>{formatTimestamp(item.updated_at)}</td>
                      </tr>
                    ))}
                    {!memoryGraphEpisodes.length && <tr><td className="empty-cell" colSpan={9}>{renderMemoryGraphEmptyState("episodes")}</td></tr>}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </section>
  );
}

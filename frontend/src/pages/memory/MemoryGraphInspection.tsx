import {
  GRAPH_ENTITY_TYPE_LABELS,
  GRAPH_PREDICATE_LABELS,
  copyTextLabelList,
  copyToClipboard,
  countDefinedIds,
  formatConfidence,
  formatTimestamp,
  graphEntityLabel,
  graphFactObject,
  graphFactSentence,
  graphFactSubject,
  graphHumanLabel,
  graphQualityBadges,
  graphStatusLabel,
  graphStatusPillClass,
} from "./model";
import { type MemoryGraphController } from "./useMemoryGraphController";

interface MemoryGraphInspectionProps {
  controller: MemoryGraphController;
  reviewMode?: boolean;
}

export function MemoryGraphInspection({
  controller,
  reviewMode = false,
}: MemoryGraphInspectionProps) {
  const {
    clearMemoryGraphFilters,
    copySelectedMemoryGraphJson,
    copySelectedMemoryGraphPath,
    markMemoryGraphReviewed,
    memoryGraphSelection,
    selectedGraphEntityNeighborhood,
    selectedGraphEntityRelatedFacts,
    selectedGraphFactObjectEntity,
    selectedGraphFactSubjectEntity,
    selectedGraphItemIsVisible,
    selectedMemoryGraphIsReviewed,
    setMemoryGraphSelection,
  } = controller;

  return (
    <aside className="memory-graph-detail-panel" aria-label="图谱项目检查详情" aria-live="polite">
      <div className="memory-graph-detail-header">
        <div>
          <p className="section-kicker">检查详情</p>
          <h4>
            {memoryGraphSelection
              ? `${memoryGraphSelection.kind === "entity" ? "实体" : memoryGraphSelection.kind === "fact" ? "关系" : "事件片段"} #${memoryGraphSelection.item.id}`
              : "未选择图谱项目"}
          </h4>
        </div>
        {memoryGraphSelection && (
          <div className="memory-graph-detail-actions">
            <button className="button button-secondary button-compact" type="button" onClick={() => copyToClipboard(memoryGraphSelection.item.id)}>
              复制 ID
            </button>
            <button className="button button-secondary button-compact" type="button" onClick={copySelectedMemoryGraphJson}>
              复制 JSON
            </button>
            <button className="button button-secondary button-compact" type="button" onClick={copySelectedMemoryGraphPath}>
              复制路径
            </button>
            {reviewMode && (
              <button
                className="button button-secondary button-compact"
                type="button"
                onClick={() => markMemoryGraphReviewed(memoryGraphSelection.kind, memoryGraphSelection.item.id)}
                disabled={selectedMemoryGraphIsReviewed}
                title="仅标记当前浏览器视图，不会修改记忆数据"
              >
                {selectedMemoryGraphIsReviewed ? "已本地复核" : "标记本地复核"}
              </button>
            )}
            <button className="button button-secondary button-compact" type="button" onClick={() => setMemoryGraphSelection(null)}>
              清除
            </button>
          </div>
        )}
      </div>
      {memoryGraphSelection && !selectedGraphItemIsVisible && (
        <div className="admin-notice admin-notice-warning memory-graph-hidden-selection">
          <span>当前选择已被筛选条件隐藏。</span>
          <button className="button button-secondary button-compact" type="button" onClick={clearMemoryGraphFilters}>
            清空筛选
          </button>
        </div>
      )}
      {!memoryGraphSelection && (
        <div className="memory-graph-detail-empty">
          <strong>下一步：选择一个图谱项目</strong>
          <span>在探索列表或复核队列中点击条目后，这里会显示邻域、证据和复制操作。</span>
        </div>
      )}
      {memoryGraphSelection?.kind === "entity" && (
        <div className="memory-graph-detail-body">
          {graphQualityBadges("entity", memoryGraphSelection.item)}
          <p className="memory-graph-local-note">
            查看实体邻域和证据路径。“复制路径”只复制 ID 路径，不包含私聊或群聊原文。
          </p>
          <dl className="memory-graph-detail-list">
            <div><dt>ID</dt><dd><span className="mono">#{memoryGraphSelection.item.id}</span></dd></div>
            <div><dt>类型</dt><dd>{graphHumanLabel(memoryGraphSelection.item.entity_type, GRAPH_ENTITY_TYPE_LABELS)}</dd></div>
            <div><dt>原始类型值</dt><dd className="mono">{memoryGraphSelection.item.entity_type || "-"}</dd></div>
            <div><dt>名称</dt><dd>{memoryGraphSelection.item.name || "-"}</dd></div>
            <div><dt>标准化名称</dt><dd className="mono">{memoryGraphSelection.item.normalized_name || "-"}</dd></div>
            <div><dt>别名</dt><dd>{(memoryGraphSelection.item.aliases || []).join(", ") || "-"}</dd></div>
            <div><dt>状态</dt><dd><span className={graphStatusPillClass(memoryGraphSelection.item.status)}>{graphStatusLabel(memoryGraphSelection.item.status)}</span></dd></div>
            <div><dt>置信度</dt><dd>{formatConfidence(memoryGraphSelection.item.confidence)}</dd></div>
            <div><dt>更新时间</dt><dd>{formatTimestamp(memoryGraphSelection.item.updated_at)}</dd></div>
          </dl>
          <div className="memory-graph-related">
            <strong>实体邻域</strong>
            <div className="memory-graph-mini-map" aria-label="所选实体的邻域关系图">
              <div className="memory-graph-mini-node is-center">
                <span>中心实体</span>
                <strong>{graphEntityLabel(memoryGraphSelection.item)}</strong>
                <small className="mono">entity:{memoryGraphSelection.item.id}</small>
              </div>
              <div className="memory-graph-mini-links">
                {selectedGraphEntityRelatedFacts.slice(0, 10).map((fact) => (
                  <button
                    className="memory-graph-mini-link"
                    type="button"
                    key={fact.id}
                    onClick={() => setMemoryGraphSelection({ kind: "fact", item: fact })}
                  >
                    <span className="mono">fact:{fact.id}</span>
                    <strong>{graphHumanLabel(fact.predicate, GRAPH_PREDICATE_LABELS)}</strong>
                    <span>{graphFactSentence(fact)}</span>
                  </button>
                ))}
                {!selectedGraphEntityRelatedFacts.length && <p>当前已加载关系中没有可绘制的邻接路径。</p>}
              </div>
              {selectedGraphEntityNeighborhood.relatedEntities.length > 0 && (
                <div className="memory-graph-mini-node-list">
                  {selectedGraphEntityNeighborhood.relatedEntities.map((entity) => (
                    <button className="memory-graph-mini-node" type="button" key={entity.id} onClick={() => setMemoryGraphSelection({ kind: "entity", item: entity })}>
                      <span>关联实体</span>
                      <strong>{graphEntityLabel(entity)}</strong>
                      <small className="mono">entity:{entity.id}</small>
                    </button>
                  ))}
                </div>
              )}
            </div>
            {[
              { key: "subject", label: "作为主体", facts: selectedGraphEntityNeighborhood.subject },
              { key: "object", label: "作为对象", facts: selectedGraphEntityNeighborhood.object },
              { key: "value", label: "文本提及", facts: selectedGraphEntityNeighborhood.valueMention },
            ].map((group) => (
              <div className="memory-graph-related-group" key={group.key}>
                <strong>{group.label} ({group.facts.length})</strong>
                {group.facts.length ? (
                  <ul>
                    {group.facts.slice(0, 8).map((fact) => (
                      <li key={fact.id}>
                        <button className="memory-graph-related-chip" type="button" onClick={() => setMemoryGraphSelection({ kind: "fact", item: fact })}>
                          <span>{graphFactSentence(fact)}</span>
                          {graphQualityBadges("fact", fact)}
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>当前已加载关系中没有此类关联。</p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      {memoryGraphSelection?.kind === "fact" && (
        <div className="memory-graph-detail-body">
          <div className="memory-graph-path">
            <button
              className="memory-graph-path-node"
              type="button"
              disabled={!selectedGraphFactSubjectEntity}
              onClick={() => selectedGraphFactSubjectEntity && setMemoryGraphSelection({ kind: "entity", item: selectedGraphFactSubjectEntity })}
            >
              <span>主体</span>
              <strong>{graphFactSubject(memoryGraphSelection.item)}</strong>
              <small className="mono">{memoryGraphSelection.item.subject_entity_id ? `entity:${memoryGraphSelection.item.subject_entity_id}` : "无实体 ID"}</small>
            </button>
            <span className="memory-graph-path-edge">{graphHumanLabel(memoryGraphSelection.item.predicate, GRAPH_PREDICATE_LABELS)}</span>
            <button
              className="memory-graph-path-node"
              type="button"
              disabled={!selectedGraphFactObjectEntity}
              onClick={() => selectedGraphFactObjectEntity && setMemoryGraphSelection({ kind: "entity", item: selectedGraphFactObjectEntity })}
            >
              <span>对象</span>
              <strong>{graphFactObject(memoryGraphSelection.item)}</strong>
              <small className="mono">{memoryGraphSelection.item.object_entity_id ? `entity:${memoryGraphSelection.item.object_entity_id}` : "文本值"}</small>
            </button>
          </div>
          <p className="memory-graph-sentence">{graphFactSentence(memoryGraphSelection.item)}</p>
          {graphQualityBadges("fact", memoryGraphSelection.item)}
          <dl className="memory-graph-detail-list">
            <div><dt>ID</dt><dd><span className="mono">#{memoryGraphSelection.item.id}</span></dd></div>
            <div><dt>主体</dt><dd>{graphFactSubject(memoryGraphSelection.item)} <span className="mono">{memoryGraphSelection.item.subject_entity_id ? `#${memoryGraphSelection.item.subject_entity_id}` : ""}</span></dd></div>
            <div><dt>对象</dt><dd>{graphFactObject(memoryGraphSelection.item)} <span className="mono">{memoryGraphSelection.item.object_entity_id ? `#${memoryGraphSelection.item.object_entity_id}` : ""}</span></dd></div>
            <div><dt>关系类型</dt><dd>{graphHumanLabel(memoryGraphSelection.item.predicate, GRAPH_PREDICATE_LABELS)}</dd></div>
            <div><dt>原始关系值</dt><dd className="mono">{memoryGraphSelection.item.predicate || "-"}</dd></div>
            <div><dt>置信度</dt><dd>{formatConfidence(memoryGraphSelection.item.confidence)}</dd></div>
            <div><dt>状态</dt><dd><span className={graphStatusPillClass(memoryGraphSelection.item.status)}>{graphStatusLabel(memoryGraphSelection.item.status)}</span></dd></div>
            <div>
              <dt>记忆项 ID</dt>
              <dd>
                <span className="mono">{memoryGraphSelection.item.memory_item_id ? `#${memoryGraphSelection.item.memory_item_id}` : "-"}</span>
                {memoryGraphSelection.item.memory_item_id && <button className="button button-secondary button-compact" type="button" onClick={() => copyToClipboard(memoryGraphSelection.item.memory_item_id)}>复制</button>}
              </dd>
            </div>
            <div>
              <dt>来源事件 ID</dt>
              <dd>
                <span className="mono">{memoryGraphSelection.item.source_event_id ? `#${memoryGraphSelection.item.source_event_id}` : "-"}</span>
                {memoryGraphSelection.item.source_event_id && <button className="button button-secondary button-compact" type="button" onClick={() => copyToClipboard(memoryGraphSelection.item.source_event_id)}>复制</button>}
              </dd>
            </div>
            <div><dt>生效时间</dt><dd>{formatTimestamp(memoryGraphSelection.item.valid_at)}</dd></div>
            <div><dt>失效时间</dt><dd>{formatTimestamp(memoryGraphSelection.item.invalid_at)}</dd></div>
            <div><dt>更新时间</dt><dd>{formatTimestamp(memoryGraphSelection.item.updated_at)}</dd></div>
          </dl>
        </div>
      )}
      {memoryGraphSelection?.kind === "episode" && (
        <div className="memory-graph-detail-body">
          {graphQualityBadges("episode", memoryGraphSelection.item)}
          <p className="memory-graph-local-note">
            这里展示当前群的事件片段摘要。请检查会话和证据 ID；无结果时可重置当前群筛选后刷新。
          </p>
          <dl className="memory-graph-detail-list">
            <div><dt>ID</dt><dd><span className="mono">#{memoryGraphSelection.item.id}</span></dd></div>
            <div><dt>标题</dt><dd>{memoryGraphSelection.item.title || "-"}</dd></div>
            <div><dt>摘要</dt><dd>{memoryGraphSelection.item.summary ? "已隐藏（不展示正文）" : "-"}</dd></div>
            <div><dt>状态</dt><dd><span className={graphStatusPillClass(memoryGraphSelection.item.status)}>{graphStatusLabel(memoryGraphSelection.item.status)}</span></dd></div>
            <div><dt>重要性</dt><dd>{memoryGraphSelection.item.importance ?? "-"}</dd></div>
            <div>
              <dt>会话 ID</dt>
              <dd>
                <span className="mono">{memoryGraphSelection.item.session_id || "-"}</span>
                {memoryGraphSelection.item.session_id && <button className="button button-secondary button-compact" type="button" onClick={() => copyToClipboard(memoryGraphSelection.item.session_id)}>复制</button>}
              </dd>
            </div>
            <div>
              <dt>事件 ID</dt>
              <dd>
                <span>{countDefinedIds(memoryGraphSelection.item.event_ids)} 个事件</span>
                {countDefinedIds(memoryGraphSelection.item.event_ids) > 0 && <button className="button button-secondary button-compact" type="button" onClick={() => copyToClipboard(copyTextLabelList(memoryGraphSelection.item.event_ids))}>全部复制</button>}
              </dd>
            </div>
            <div>
              <dt>记忆项 ID</dt>
              <dd>
                <span>{countDefinedIds(memoryGraphSelection.item.memory_item_ids)} 条记忆</span>
                {countDefinedIds(memoryGraphSelection.item.memory_item_ids) > 0 && <button className="button button-secondary button-compact" type="button" onClick={() => copyToClipboard(copyTextLabelList(memoryGraphSelection.item.memory_item_ids))}>全部复制</button>}
              </dd>
            </div>
            <div><dt>更新时间</dt><dd>{formatTimestamp(memoryGraphSelection.item.updated_at)}</dd></div>
          </dl>
        </div>
      )}
    </aside>
  );
}

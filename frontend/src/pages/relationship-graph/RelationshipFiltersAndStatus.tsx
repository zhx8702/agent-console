import { StatusTile } from "../../components/StatusTile";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import {
  ACCEPTANCE_OPTIONS,
  HISTORY_RECENT_DAYS,
  dateStatusClass,
  dateStatusLabel,
  acceptanceStatusLabel,
  fieldLabel,
  jobCount,
  nodeTypeLabel,
  readableRelationType,
} from "./graphModel";
import type { RelationshipGraphController } from "./useRelationshipGraphController";

export function RelationshipFiltersAndStatus(controller: RelationshipGraphController) {
  const {
    config,
    channel,
    setChannel,
    sourceKey,
    setSourceKey,
    acceptanceStatus,
    setAcceptanceStatus,
    nodeType,
    setNodeType,
    edgeType,
    setEdgeType,
    minConfidence,
    setMinConfidence,
    limit,
    setLimit,
    fromDate,
    setFromDate,
    toDate,
    setToDate,
    nodeSearch,
    setNodeSearch,
    edgeSearch,
    setEdgeSearch,
    nodeTypeOptions,
    edgeTypeOptions,
    targetDate,
    setTargetDate,
    enqueueLlmJobs,
    setEnqueueLlmJobs,
    missingHistorySyncFields,
    historySyncHint,
    optionalUserScopeLabel,
    loadGraphAndStatus,
    dateLoading,
    jobStatsLoading,
    dateRows,
    graph,
    modeFilteredNodes,
    graphEdges,
    nodes,
    edges,
    jobStatsStatus,
    selectedJobStats,
    scopeJobStats,
    windowStatsStatus,
    windowStatsTotals,
    windowStatsAcceptance,
  } = controller;

  return (
    <>
      <details className="panel relationship-filters relationship-disclosure">
        <summary>
          <span>
            <p className="section-kicker">筛选</p>
            <h3>群聊关系查询</h3>
          </span>
          <span className="relationship-disclosure-hint">展开</span>
        </summary>
        <div className="form-grid relationship-filter-grid">
          <label className="field">
            <span>消息渠道</span>
            <input value={channel} onChange={(event) => setChannel(event.target.value)} />
          </label>
          <label className="field">
            <span>数据来源</span>
            <input value={sourceKey} onChange={(event) => setSourceKey(event.target.value)} />
          </label>
          <label className="field">
            <span>审核状态</span>
            <select value={acceptanceStatus} onChange={(event) => setAcceptanceStatus(event.target.value)}>
              {ACCEPTANCE_OPTIONS.map((value) => (
                <option key={value || "default"} value={value}>{value ? acceptanceStatusLabel(value) : "默认仅显示已接受"}</option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>节点类型</span>
            <select value={nodeType} onChange={(event) => setNodeType(event.target.value)}>
              <option value="">全部类型</option>
              {nodeTypeOptions.map((value) => <option key={value} value={value}>{nodeTypeLabel(value)}</option>)}
            </select>
          </label>
          <label className="field">
            <span>关系类型</span>
            <select value={edgeType} onChange={(event) => setEdgeType(event.target.value)}>
              <option value="">全部关系</option>
              {edgeTypeOptions.map((value) => <option key={value} value={value}>{readableRelationType(value)}</option>)}
            </select>
          </label>
          <label className="field">
            <span>最低置信度</span>
            <input type="number" min="0" max="1" step="0.05" value={minConfidence} onChange={(event) => setMinConfidence(event.target.value)} placeholder="0.45" />
          </label>
          <label className="field">
            <span>返回上限</span>
            <input type="number" min="1" max="500" value={limit} onChange={(event) => setLimit(event.target.value)} />
          </label>
          <label className="field">
            <span>开始日期</span>
            <input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} />
          </label>
          <label className="field">
            <span>结束日期</span>
            <input type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} />
          </label>
          <label className="field">
            <span>节点搜索</span>
            <input value={nodeSearch} onChange={(event) => setNodeSearch(event.target.value)} placeholder="按名称或类型筛选" />
          </label>
          <label className="field">
            <span>关系搜索</span>
            <input value={edgeSearch} onChange={(event) => setEdgeSearch(event.target.value)} placeholder="按关系名称或两端对象筛选" />
          </label>
        </div>
        <p className="relationship-query-note">
          时间范围会发送给服务端；节点和关系搜索只在安全图谱摘要上本地过滤，不读取原始聊天内容。
        </p>
      </details>

      <details className="panel relationship-history-sync relationship-disclosure">
        <summary>
          <span>
            <p className="section-kicker">历史同步</p>
            <h3>按日期同步群聊历史</h3>
          </span>
          <span className="relationship-disclosure-hint">展开</span>
        </summary>
        <p className="relationship-section-description">
          导入某一天的历史消息，并可选择自动排队 AI 抽取任务；任务处理成功后关系图才会更新。
        </p>
        <p className={`relationship-sync-hint${missingHistorySyncFields.length ? " is-warning" : ""}`} role="status">
          {historySyncHint}
        </p>
        <TechnicalDetails
          summary="查看补算范围技术详情"
          value={{ tenant_id: config.tenantId, channel, source_key: sourceKey, session_id: config.sessionId, user_scope: optionalUserScopeLabel }}
        />
        <div className="form-grid relationship-sync-grid">
          <label className="field">
            {fieldLabel("同步日期", "target_date")}
            <input
              type="date"
              value={targetDate}
              onChange={(event) => setTargetDate(event.target.value)}
            />
          </label>
          <label className="toggle-chip relationship-sync-toggle">
            <span>
              <input
                type="checkbox"
                checked={enqueueLlmJobs}
                onChange={(event) => setEnqueueLlmJobs(event.target.checked)}
              />
              <strong className="relationship-toggle-label">
                自动智能抽取
              </strong>
            </span>
            <em>建议开启，否则关系图可能不会马上变化。</em>
          </label>
        </div>
        <div className="relationship-date-toolbar">
          <button
            className="button button-secondary"
            type="button"
            onClick={() => void loadGraphAndStatus()}
            disabled={dateLoading || jobStatsLoading}
          >
            {dateLoading || jobStatsLoading ? "正在加载状态" : "查看日期/任务状态"}
          </button>
          <span>最近 {HISTORY_RECENT_DAYS} 天，仅显示数量、导入状态和 AI 任务计数，不显示聊天内容。</span>
        </div>
        <div className="relationship-date-list" aria-label="近期历史日期状态">
          {dateRows.map((row) => (
            <button
              key={row.date}
              type="button"
              className={`relationship-date-row${row.date === targetDate ? " is-selected" : ""}`}
              onClick={() => setTargetDate(row.date)}
            >
              <strong>{row.date}</strong>
              <span className={dateStatusClass(row.status)}>{dateStatusLabel(row.status)}</span>
              <small>历史 {row.raw_message_count} / 已导入 {row.imported_count}</small>
              <small>
                AI 待/跑/成/败 {jobCount(row.job_counts, "pending")} / {jobCount(row.job_counts, "running")} / {jobCount(row.job_counts, "succeeded")} / {jobCount(row.job_counts, "failed") + jobCount(row.job_counts, "dead")}
              </small>
            </button>
          ))}
          {!dateRows.length && (
            <p className="muted-copy">
              选择授权群聊后，可以查看最近日期状态。
            </p>
          )}
        </div>
      </details>

      <section className="status-grid relationship-status-grid">
        <StatusTile label="数据版本" value={graph?.schema?.version || "-"} />
        <StatusTile label="节点" value={`${modeFilteredNodes.length} / ${graph?.counts?.nodes ?? nodes.length}`} />
        <StatusTile label="关系" value={`${graphEdges.length} / ${graph?.counts?.edges ?? edges.length}`} />
      </section>

      <details className="panel relationship-disclosure">
        <summary>
          <span>
            <p className="section-kicker">任务计数</p>
            <h3>抽取任务与窗口统计</h3>
          </span>
          <span className="relationship-disclosure-hint">展开</span>
        </summary>
      <section className="relationship-job-summary" aria-label="智能关系抽取任务状态">
        <div className="relationship-job-summary-header">
          <div>
            <p className="section-kicker">智能关系抽取任务</p>
            <h3>所选范围/日期任务计数</h3>
          </div>
          <span className={`relationship-evidence-status${jobStatsStatus.includes("失败") ? " is-warning" : ""}`}>
            {jobStatsStatus}
          </span>
        </div>
        <div className="relationship-job-grid">
          <div><span>待处理</span><strong>{selectedJobStats.pending}</strong></div>
          <div><span>运行中</span><strong>{selectedJobStats.running}</strong></div>
          <div><span>已成功</span><strong>{selectedJobStats.succeeded}</strong></div>
          <div><span>失败</span><strong>{selectedJobStats.failed}</strong></div>
          <div><span>终止</span><strong>{selectedJobStats.dead}</strong></div>
          <div><span>所选日期/范围总数</span><strong>{selectedJobStats.total} / {scopeJobStats.total}</strong></div>
          <div><span>范围可执行/延迟</span><strong>{scopeJobStats.ready} / {scopeJobStats.delayed}</strong></div>
        </div>
      </section>

      <section className="relationship-job-summary" aria-label="时间窗关系抽取统计">
        <div className="relationship-job-summary-header">
          <div>
            <p className="section-kicker">时间窗抽取</p>
            <h3>窗口关系统计</h3>
          </div>
          <span className={`relationship-evidence-status${windowStatsStatus.includes("失败") ? " is-warning" : ""}`}>
            {windowStatsStatus}
          </span>
        </div>
        <div className="relationship-job-grid">
          <div><span>关系项</span><strong>{windowStatsTotals.items ?? 0}</strong></div>
          <div><span>窗口</span><strong>{windowStatsTotals.windows ?? 0}</strong></div>
          <div><span>证据事件</span><strong>{windowStatsTotals.events ?? 0}</strong></div>
          <div><span>待审核</span><strong>{windowStatsAcceptance.needs_review ?? 0}</strong></div>
          <div><span>已接受</span><strong>{windowStatsAcceptance.accepted ?? 0}</strong></div>
          <div><span>已拒绝</span><strong>{windowStatsAcceptance.rejected ?? 0}</strong></div>
        </div>
      </section>
      </details>
    </>
  );
}

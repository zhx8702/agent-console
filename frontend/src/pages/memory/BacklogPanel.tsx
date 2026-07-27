import { DangerAction } from "../../components/DangerAction";
import {
  EXTRACTION_JOB_ACTIONS,
  EXTRACTION_JOB_ID_PREVIEW_LIMIT,
  EXTRACTION_JOB_STATUSES,
  extractionJobStatusLabel,
  formatTimestamp,
  hasSmokeOrTestScope,
  type ExtractionJobAction,
  type ExtractionJobMaintenanceResult,
  type ExtractionJobScopeCount,
} from "./model";

type ExtractionJobFilters = Partial<Record<string, string | number | boolean | undefined>>;

type BacklogPanelProps = {
  currentTenantId: string;
  currentUserId: string;
  currentSessionId: string;
  channel: string;
  sourceKey: string;
  status: string;
  errorType: string;
  createdAfter: string;
  createdBefore: string;
  updatedAfter: string;
  updatedBefore: string;
  statsLimit: number;
  action: ExtractionJobAction;
  dryRun: boolean;
  maintenanceLimit: number;
  maintenanceApiLimit: number;
  filters: ExtractionJobFilters;
  filterEntries: Array<[string, string]>;
  statusCounts: Record<string, number>;
  errorTypeCounts: Record<string, number>;
  scopeCounts: ExtractionJobScopeCount[];
  statsLoadedAt: string | null;
  maintenanceResult: ExtractionJobMaintenanceResult | null;
  onChannelChange: (value: string) => void;
  onSourceKeyChange: (value: string) => void;
  onStatusChange: (value: string) => void;
  onErrorTypeChange: (value: string) => void;
  onCreatedAfterChange: (value: string) => void;
  onCreatedBeforeChange: (value: string) => void;
  onUpdatedAfterChange: (value: string) => void;
  onUpdatedBeforeChange: (value: string) => void;
  onStatsLimitChange: (value: number) => void;
  onActionChange: (value: ExtractionJobAction) => void;
  onDryRunChange: (value: boolean) => void;
  onMaintenanceLimitChange: (value: number) => void;
  onLoadStats: () => void | Promise<void>;
  onRunMaintenance: (surfaceErrors?: boolean) => void | Promise<void>;
};

export function BacklogPanel({
  currentTenantId,
  currentUserId,
  currentSessionId,
  channel,
  sourceKey,
  status,
  errorType,
  createdAfter,
  createdBefore,
  updatedAfter,
  updatedBefore,
  statsLimit,
  action,
  dryRun,
  maintenanceLimit,
  maintenanceApiLimit,
  filters,
  filterEntries,
  statusCounts,
  errorTypeCounts,
  scopeCounts,
  statsLoadedAt,
  maintenanceResult,
  onChannelChange,
  onSourceKeyChange,
  onStatusChange,
  onErrorTypeChange,
  onCreatedAfterChange,
  onCreatedBeforeChange,
  onUpdatedAfterChange,
  onUpdatedBeforeChange,
  onStatsLimitChange,
  onActionChange,
  onDryRunChange,
  onMaintenanceLimitChange,
  onLoadStats,
  onRunMaintenance,
}: BacklogPanelProps) {
  const hasFilters = filterEntries.length > 0;
  const scopeReady = Boolean(currentTenantId && currentSessionId && currentUserId);

  return (
    <section className="panel span-3 memory-backlog-panel">
      <div className="panel-header">
        <div><p className="section-kicker">记忆抽取任务</p><h3>积压任务管理</h3></div>
        <span className={`pill ${dryRun ? "pill-ok" : "pill-danger"}`}>{dryRun ? "仅预演" : "写入模式"}</span>
      </div>
      <p className="muted-copy">
        积压任务固定在当前登录租户、已验证群聊和已验证成员范围内；默认仅预演并显示会影响哪些任务。
      </p>

      <div className="memory-backlog-stats-grid">
        {EXTRACTION_JOB_STATUSES.map((statusValue) => (
          <div className="summary-card" data-status={statusValue === "failed" || statusValue === "dead" ? "error" : statusValue === "running" ? "warning" : undefined} key={statusValue}>
            <span>{extractionJobStatusLabel(statusValue)}</span><strong>{statusCounts[statusValue] ?? 0}</strong>
          </div>
        ))}
      </div>

      <div className="memory-backlog-toolbar">
        <div className="memory-backlog-updated"><span>更新时间</span><strong>{formatTimestamp(statsLoadedAt)}</strong></div>
        <button className="button button-secondary" onClick={() => void onLoadStats()} disabled={!scopeReady}>刷新积压任务统计</button>
      </div>

      <div className="form-grid memory-backlog-filters">
        <div className="field"><span>已验证租户</span><strong>{currentTenantId}</strong></div>
        <label className="field"><span>渠道（channel）</span><input value={channel} onChange={(event) => onChannelChange(event.target.value)} /></label>
        <label className="field"><span>来源键（source_key）</span><input value={sourceKey} onChange={(event) => onSourceKeyChange(event.target.value)} /></label>
        <div className="field"><span>当前群</span><strong>{currentSessionId || "尚未选择"}</strong></div>
        <div className="field"><span>当前成员</span><strong>{currentUserId || "尚未选择"}</strong></div>
        <label className="field">
          <span>任务状态</span>
          <select value={status} onChange={(event) => onStatusChange(event.target.value)}>
            <option value="">全部</option>
            {EXTRACTION_JOB_STATUSES.map((statusValue) => <option value={statusValue} key={statusValue}>{extractionJobStatusLabel(statusValue)}</option>)}
          </select>
        </label>
        <label className="field"><span>错误类型（error_type）</span><input value={errorType} onChange={(event) => onErrorTypeChange(event.target.value)} placeholder="例如 TimeoutError" /></label>
        <label className="field"><span>统计条数上限</span><input type="number" min={1} max={100} value={statsLimit} onChange={(event) => onStatsLimitChange(Number(event.target.value) || 100)} /></label>
        <label className="field"><span>创建时间起点</span><input type="datetime-local" value={createdAfter} onChange={(event) => onCreatedAfterChange(event.target.value)} /></label>
        <label className="field"><span>创建时间终点</span><input type="datetime-local" value={createdBefore} onChange={(event) => onCreatedBeforeChange(event.target.value)} /></label>
        <label className="field"><span>更新时间起点</span><input type="datetime-local" value={updatedAfter} onChange={(event) => onUpdatedAfterChange(event.target.value)} /></label>
        <label className="field"><span>更新时间终点</span><input type="datetime-local" value={updatedBefore} onChange={(event) => onUpdatedBeforeChange(event.target.value)} /></label>
      </div>

      <div className="memory-backlog-maintenance">
        <div className="form-grid memory-backlog-maintenance-grid">
          <label className="field">
            <span>维护操作</span>
            <select value={action} onChange={(event) => onActionChange(event.target.value as ExtractionJobAction)}>
              {EXTRACTION_JOB_ACTIONS.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}
            </select>
          </label>
          <label className="field"><span>最多影响条数</span><input type="number" min={1} max={100} value={maintenanceLimit} onChange={(event) => onMaintenanceLimitChange(Number(event.target.value) || 100)} /></label>
          <label className={`toggle-chip memory-backlog-dry-run${dryRun ? " is-on" : " is-off"}`}>
            <span><input type="checkbox" checked={dryRun} onChange={(event) => onDryRunChange(event.target.checked)} />仅预演</span>
            <em>{dryRun ? "当前只预览预计影响数量，不写入。" : "已进入写入模式，执行前会二次确认。"}</em>
          </label>
        </div>

        <div className="memory-backlog-action-hint">
          <strong>{EXTRACTION_JOB_ACTIONS.find((item) => item.value === action)?.label}</strong>
          <span>{EXTRACTION_JOB_ACTIONS.find((item) => item.value === action)?.hint}</span>
        </div>

        <div className={`memory-backlog-filter-summary${hasFilters ? "" : " is-empty"}`}>
          <span>筛选摘要</span>
          {hasFilters ? <div>{filterEntries.map(([key, value]) => <code key={key}>{key}={value}</code>)}</div> : <strong>未设置过滤条件。除“重置超时任务”的预演外，请先限定范围、状态或时间。</strong>}
        </div>

        {action === "cleanup_smoke" && !hasSmokeOrTestScope(filters) && (
          <div className="admin-notice admin-notice-warning">清理冒烟或测试数据时，租户、渠道、来源、用户或会话中至少一项必须包含 <code>smoke</code> 或 <code>test</code>；写入模式不满足条件时会被拦截。</div>
        )}

        <div className="action-row">
          <button className="button button-secondary" onClick={() => void onLoadStats()} disabled={!scopeReady}>预览当前筛选统计</button>
          {dryRun ? (
            <button className="button button-primary" onClick={() => void onRunMaintenance()} disabled={!scopeReady}>运行预演</button>
          ) : (
            <DangerAction
              label="执行积压任务写操作"
              title={`确认执行“${EXTRACTION_JOB_ACTIONS.find((item) => item.value === action)?.label || action}”？`}
              confirmLabel="确认执行写操作"
              pendingLabel="正在处理积压任务…"
              disabled={!scopeReady}
              className="memory-backlog-run-danger"
              impact={(
                <ul>
                  <li>操作：{EXTRACTION_JOB_ACTIONS.find((item) => item.value === action)?.label || action}</li><li>最多影响：{maintenanceApiLimit} 个抽取任务</li>
                  <li>筛选范围：{filterEntries.map(([key, value]) => `${key}=${value}`).join(", ") || "未设置"}</li>
                  <li>{EXTRACTION_JOB_ACTIONS.find((item) => item.value === action)?.hint}</li>
                  <li>该操作会改变任务状态；失败重试会复用同一幂等键。</li>
                </ul>
              )}
              onConfirm={() => onRunMaintenance(true)}
            />
          )}
        </div>
      </div>

      <div className="memory-backlog-detail-grid">
        <div className="memory-backlog-card">
          <div className="memory-graph-sample-title">错误类型</div>
          {Object.entries(errorTypeCounts).length ? (
            <ul className="memory-backlog-count-list">
              {Object.entries(errorTypeCounts).map(([currentErrorType, count]) => <li key={currentErrorType}><span className="mono">{currentErrorType}</span><strong>{count}</strong></li>)}
            </ul>
          ) : <div className="memory-graph-sample-empty">暂无错误类型统计</div>}
        </div>
        <div className="memory-backlog-card">
          <div className="memory-graph-sample-title">近期范围统计</div>
          {scopeCounts.length ? (
            <div className="table-scroll compact-table-scroll memory-backlog-scope-scroll">
              <table>
                <caption className="sr-only">记忆抽取任务范围统计</caption>
                <thead><tr><th scope="col">范围</th><th scope="col">状态</th><th scope="col">错误</th><th scope="col">数量</th></tr></thead>
                <tbody>
                  {scopeCounts.map((item, index) => (
                    <tr key={`${item.tenant_id}-${item.channel}-${item.source_key}-${item.user_id}-${item.session_id}-${item.status}-${item.error_type}-${index}`}>
                      <th scope="row" className="mono">{[item.tenant_id, item.channel, item.source_key, item.user_id, item.session_id || "跨会话身份记忆"].filter(Boolean).join(" / ")}</th>
                      <td>{extractionJobStatusLabel(item.status || undefined)}</td><td className="mono">{item.error_type || "-"}</td><td>{item.count ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <div className="memory-graph-sample-empty">暂无范围统计</div>}
        </div>
      </div>

      <div className="memory-backlog-result">
        <div className="memory-graph-sample-title">维护结果</div>
        {maintenanceResult ? (
          <>
            <div className="memory-backlog-result-grid">
              <div><span>预计影响</span><strong>{maintenanceResult.would_affect ?? 0}</strong></div>
              <div><span>实际影响</span><strong>{maintenanceResult.affected ?? 0}</strong></div>
              <div><span>已显示 ID</span><strong>{Math.min((maintenanceResult.ids || []).length, EXTRACTION_JOB_ID_PREVIEW_LIMIT)}</strong></div>
            </div>
            <div className="memory-backlog-id-list">
              {(maintenanceResult.ids || []).slice(0, EXTRACTION_JOB_ID_PREVIEW_LIMIT).map((id) => <code key={id}>#{id}</code>)}
              {(maintenanceResult.ids || []).length > EXTRACTION_JOB_ID_PREVIEW_LIMIT && <span>另有 {(maintenanceResult.ids || []).length - EXTRACTION_JOB_ID_PREVIEW_LIMIT} 条</span>}
              {!(maintenanceResult.ids || []).length && <span>无匹配任务 ID</span>}
            </div>
          </>
        ) : <div className="memory-graph-sample-empty">尚未执行维护操作。</div>}
      </div>
    </section>
  );
}

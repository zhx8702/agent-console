import { DangerAction } from "../../components/DangerAction";
import {
  acceptanceStatusLabel,
  formatTimestamp,
  memoryItemStatusLabel,
  memoryScopeTypeLabel,
  memorySensitivityLabel,
  memorySourceTypeLabel,
  previewIds,
  type AcceptanceLegacyAudit,
  type AcceptanceLegacyAuditGroup,
  type AcceptanceLegacyBackfillResult,
} from "./model";

type AcceptanceAuditPanelProps = {
  countMap: Record<string, number>;
  sensitivityMap: Record<string, number>;
  audit: AcceptanceLegacyAudit | null;
  auditGroups: AcceptanceLegacyAuditGroup[];
  statsLoadedAt: string | null;
  backfillStatus: "needs_review" | "candidate";
  backfillLimit: number;
  backfillDryRun: boolean;
  backfillConfirm: boolean;
  backfillResult: AcceptanceLegacyBackfillResult | null;
  filters: Record<string, string | number | boolean | undefined>;
  onBackfillStatusChange: (value: "needs_review" | "candidate") => void;
  onBackfillLimitChange: (value: number) => void;
  onBackfillDryRunChange: (value: boolean) => void;
  onBackfillConfirmChange: (value: boolean) => void;
  onRunBackfill: (surfaceErrors?: boolean) => void | Promise<void>;
};

export function AcceptanceAuditPanel({
  countMap,
  sensitivityMap,
  audit,
  auditGroups,
  statsLoadedAt,
  backfillStatus,
  backfillLimit,
  backfillDryRun,
  backfillConfirm,
  backfillResult,
  filters,
  onBackfillStatusChange,
  onBackfillLimitChange,
  onBackfillDryRunChange,
  onBackfillConfirmChange,
  onRunBackfill,
}: AcceptanceAuditPanelProps) {
  return (
    <div className="memory-acceptance-audit-panel">
      <div className="memory-acceptance-audit-head">
        <div><p className="section-kicker">采纳状态统计</p><h3>旧版记录审计与补全</h3></div>
        <span className="pill pill-feature">仅显示 ID</span>
      </div>
      <div className="memory-acceptance-stats-grid">
        {[
          ["missing", countMap.missing_acceptance ?? 0],
          ["accepted", countMap.accepted ?? 0],
          ["candidate", countMap.candidate ?? 0],
          ["needs_review", countMap.needs_review ?? 0],
          ["rejected", countMap.rejected ?? 0],
          ["superseded", countMap.superseded ?? 0],
          ["expired", countMap.expired ?? 0],
        ].map(([label, count]) => (
          <div className="summary-card" data-status={label === "missing" ? "warning" : undefined} key={label}>
            <span>{acceptanceStatusLabel(String(label))}</span><strong>{count}</strong>
          </div>
        ))}
      </div>
      <div className="memory-acceptance-audit-meta">
        <span>更新于 {formatTimestamp(statsLoadedAt)}</span>
        <span>敏感级别：{memorySensitivityLabel("normal")} {sensitivityMap.normal ?? 0} / {memorySensitivityLabel("private")} {sensitivityMap.private ?? 0} / {memorySensitivityLabel("sensitive")} {sensitivityMap.sensitive ?? 0}</span>
        <span>缺失记录 ID：{previewIds(audit?.ids_preview, audit?.ids_truncated)}</span>
      </div>
      <div className="memory-acceptance-groups">
        {auditGroups.slice(0, 4).map((group, index) => (
          <div className="memory-acceptance-group" key={`${group.scope_type}-${group.status}-${group.memory_type}-${index}`}>
            <strong>{memoryScopeTypeLabel(group.scope_type)} / {memoryItemStatusLabel(group.status)} / {group.memory_type || "未分类"} / {memorySourceTypeLabel(group.source_type)}</strong>
            <span>缺失 {group.count || 0} 条 · 建议操作：{acceptanceStatusLabel(group.suggested_action || "needs_review")}</span>
            <code>{previewIds(group.ids_preview, group.ids_truncated)}</code>
          </div>
        ))}
        {!auditGroups.length && <div className="admin-notice">当前筛选范围内没有缺少采纳元数据的记录。</div>}
      </div>
      <div className="form-grid memory-acceptance-backfill-grid">
        <label className="field">
          <span>缺失记录标记为</span>
          <select value={backfillStatus} onChange={(event) => onBackfillStatusChange(event.target.value as "needs_review" | "candidate")}>
            <option value="needs_review">{acceptanceStatusLabel("needs_review")}</option><option value="candidate">{acceptanceStatusLabel("candidate")}</option>
          </select>
        </label>
        <label className="field">
          <span>最多处理条数</span>
          <input type="number" min={1} max={10000} value={backfillLimit} onChange={(event) => onBackfillLimitChange(Number(event.target.value) || 25)} />
        </label>
        <label className={`toggle-chip${backfillDryRun ? " is-on" : " is-off"}`}>
          <span>
            <input type="checkbox" checked={backfillDryRun} onChange={(event) => {
              onBackfillDryRunChange(event.target.checked);
              if (event.target.checked) onBackfillConfirmChange(false);
            }} />
            仅预演
          </span>
          <em>{backfillDryRun ? "只预览影响范围，不修改任何记忆元数据。" : "写入模式只补全缺失的采纳元数据。"}</em>
        </label>
        <label className="toggle-chip">
          <span><input type="checkbox" checked={backfillConfirm} onChange={(event) => onBackfillConfirmChange(event.target.checked)} disabled={backfillDryRun} />确认写入</span>
          <em>关闭预演后必须勾选。</em>
        </label>
      </div>
      {backfillResult && (
        <div className="memory-acceptance-audit-meta">
          <span>{backfillResult.dry_run ? "预计影响" : "已影响"} {backfillResult.dry_run ? backfillResult.would_affect || 0 : backfillResult.affected || 0} 条</span>
          <span>记录 ID：{previewIds(backfillResult.ids || backfillResult.ids_preview, backfillResult.ids_truncated)}</span>
        </div>
      )}
      <div className="action-row">
        {backfillDryRun ? (
          <button className="button button-secondary" onClick={() => void onRunBackfill()}>预演旧版记录补全</button>
        ) : (
          <DangerAction
            label="写入旧版记录补全"
            title="确认补全旧版记录的采纳元数据？"
            confirmLabel="确认写入补全结果"
            pendingLabel="正在写入补全结果…"
            disabled={!backfillConfirm}
            impact={(
              <ul>
                <li>最多写入：{Math.max(1, Math.min(10000, backfillLimit || 1))} 条缺失采纳元数据的记忆</li>
                <li>当前缺失数量：{audit?.missing_acceptance ?? "未知"}</li><li>目标状态：{acceptanceStatusLabel(backfillStatus)}</li>
                <li>筛选范围：{Object.entries(filters).map(([key, value]) => `${key}=${String(value)}`).join(", ") || "接口默认范围"}</li>
                <li>该写入会改变后续召回和人工复核队列。</li>
              </ul>
            )}
            onConfirm={() => onRunBackfill(true)}
          />
        )}
      </div>
    </div>
  );
}

import { DangerAction } from "../../components/DangerAction";
import {
  ACCEPTANCE_REVIEW_ACTIONS,
  acceptanceStatusOf,
  acceptanceStatusLabel,
  formatConfidence,
  formatTimestamp,
  memoryItemStatusLabel,
  memoryScopeTypeLabel,
  memorySensitivityLabel,
  memorySourceTypeLabel,
  supersededByItemIdOf,
  supersedesItemIdOf,
  type AcceptanceReviewAction,
  type AcceptanceReviewHistoryEntry,
  type MemoryItem,
} from "./model";

type NewMemoryDraft = {
  content: string;
  scopeType: string;
  memoryType: string;
  pinned: boolean;
  priority: number;
};

type EditMemoryDraft = {
  content: string;
  status: string;
  memoryType: string;
  sensitivity: string;
  confidence: string;
  pinned: boolean;
  priority: number;
};

type MemoryItemEditorProps = {
  newDraft: NewMemoryDraft;
  editDraft: EditMemoryDraft;
  selectedItem: MemoryItem | null;
  acceptanceSignalRows: Array<[string, number]>;
  acceptanceHistoryRows: AcceptanceReviewHistoryEntry[];
  acceptanceReviewReason: string;
  acceptanceReviewBusy: AcceptanceReviewAction | null;
  supersededByItemIdInput: string;
  supersedesItemIdInput: string;
  onNewContentChange: (value: string) => void;
  onNewMemoryTypeChange: (value: string) => void;
  onNewPinnedChange: (value: boolean) => void;
  onNewPriorityChange: (value: number) => void;
  onEditContentChange: (value: string) => void;
  onEditStatusChange: (value: string) => void;
  onEditMemoryTypeChange: (value: string) => void;
  onEditSensitivityChange: (value: string) => void;
  onEditConfidenceChange: (value: string) => void;
  onEditPinnedChange: (value: boolean) => void;
  onEditPriorityChange: (value: number) => void;
  onAcceptanceReviewReasonChange: (value: string) => void;
  onSupersededByItemIdInputChange: (value: string) => void;
  onSupersedesItemIdInputChange: (value: string) => void;
  onCreate: () => void | Promise<void>;
  onSave: () => void | Promise<void>;
  onDelete: () => void | Promise<void>;
  onReview: (action: AcceptanceReviewAction) => void | Promise<void>;
  onReviewSupersededBy: () => void | Promise<void>;
  onReviewSupersedes: () => void | Promise<void>;
};

export function MemoryItemEditor({
  newDraft,
  editDraft,
  selectedItem,
  acceptanceSignalRows,
  acceptanceHistoryRows,
  acceptanceReviewReason,
  acceptanceReviewBusy,
  supersededByItemIdInput,
  supersedesItemIdInput,
  onNewContentChange,
  onNewMemoryTypeChange,
  onNewPinnedChange,
  onNewPriorityChange,
  onEditContentChange,
  onEditStatusChange,
  onEditMemoryTypeChange,
  onEditSensitivityChange,
  onEditConfidenceChange,
  onEditPinnedChange,
  onEditPriorityChange,
  onAcceptanceReviewReasonChange,
  onSupersededByItemIdInputChange,
  onSupersedesItemIdInputChange,
  onCreate,
  onSave,
  onDelete,
  onReview,
  onReviewSupersededBy,
  onReviewSupersedes,
}: MemoryItemEditorProps) {
  return (
    <div className="memory-item-editor-stack">
      <div className="memory-item-editor">
        <div className="panel-header">
          <div><p className="section-kicker">新建记忆</p><h3>新建手工记忆</h3></div><span className="pill pill-feature">手工录入</span>
        </div>
        <div className="form-grid">
          <label className="field span-2"><span>内容</span><textarea rows={4} value={newDraft.content} onChange={(event) => onNewContentChange(event.target.value)} /></label>
          <div className="field">
            <span>记忆范围</span>
            <strong>当前群成员</strong>
            <small>固定写入 session 范围，不会扩散为跨群身份记忆。</small>
          </div>
          <label className="field"><span>记忆类型</span><input value={newDraft.memoryType} onChange={(event) => onNewMemoryTypeChange(event.target.value)} /></label>
          <label className="field"><span>优先级</span><input type="number" value={newDraft.priority} onChange={(event) => onNewPriorityChange(Number(event.target.value) || 0)} /></label>
          <label className="toggle-chip">
            <span><input type="checkbox" checked={newDraft.pinned} onChange={(event) => onNewPinnedChange(event.target.checked)} />置顶</span>
            <em>创建时固定标记为手工来源和生效状态。</em>
          </label>
        </div>
        <div className="action-row">
          <DangerAction
            label="创建单条记忆"
            title="确认创建群内成员记忆"
            confirmLabel="确认创建"
            pendingLabel="正在创建…"
            disabled={!newDraft.content.trim()}
            impact={<ul><li>范围：当前已验证群和成员</li><li>类型：{newDraft.memoryType || "备注"}</li><li>固定写入当前会话范围，并记录稳定幂等键。</li></ul>}
            onConfirm={onCreate}
          />
        </div>
      </div>

      <div className="memory-item-editor">
        <div className="panel-header"><div><p className="section-kicker">编辑记忆</p><h3>{selectedItem ? `编辑记忆 #${selectedItem.id}` : "编辑记忆"}</h3></div></div>
        <div className="form-grid">
          <label className="field span-2"><span>内容</span><textarea rows={5} value={editDraft.content} onChange={(event) => onEditContentChange(event.target.value)} disabled={!selectedItem} /></label>
          <label className="field">
            <span>状态</span>
            <select value={editDraft.status} onChange={(event) => onEditStatusChange(event.target.value)} disabled={!selectedItem}>
              {(["active", "pending", "archived", "invalidated", "deleted"] as const).map((status) => <option value={status} key={status}>{memoryItemStatusLabel(status)}</option>)}
            </select>
          </label>
          <label className="field"><span>记忆类型</span><input value={editDraft.memoryType} onChange={(event) => onEditMemoryTypeChange(event.target.value)} disabled={!selectedItem} /></label>
          <label className="field">
            <span>敏感级别</span>
            <select value={editDraft.sensitivity} onChange={(event) => onEditSensitivityChange(event.target.value)} disabled={!selectedItem}>
              {(["normal", "private", "sensitive"] as const).map((value) => <option value={value} key={value}>{memorySensitivityLabel(value)}</option>)}
            </select>
          </label>
          <label className="field"><span>置信度</span><input type="number" min={0} max={1} step={0.01} value={editDraft.confidence} onChange={(event) => onEditConfidenceChange(event.target.value)} disabled={!selectedItem} /></label>
          <label className="field"><span>优先级</span><input type="number" value={editDraft.priority} onChange={(event) => onEditPriorityChange(Number(event.target.value) || 0)} disabled={!selectedItem} /></label>
          <label className="toggle-chip">
            <span><input type="checkbox" checked={editDraft.pinned} onChange={(event) => onEditPinnedChange(event.target.checked)} disabled={!selectedItem} />置顶</span>
            <em>{selectedItem ? `${memoryScopeTypeLabel(selectedItem.scope_type)} / ${memorySourceTypeLabel(selectedItem.source_type)} / ${selectedItem.user_id}` : "请选择左侧一条记忆。"}</em>
          </label>
          {selectedItem && (
            <div className="memory-acceptance-detail span-2">
              <div className="memory-acceptance-detail-head">
                <div><span>采纳状态</span><strong>{acceptanceStatusLabel(acceptanceStatusOf(selectedItem))} {formatConfidence(selectedItem.acceptance_score)}</strong></div>
                <span className="pill pill-feature">持久化管理员复核</span>
              </div>
              <dl>
                <div><dt>采纳原因</dt><dd>{selectedItem.acceptance_reason || "-"}</dd></div>
                <div><dt>抽取置信度</dt><dd>{formatConfidence(selectedItem.extraction_confidence)}</dd></div>
                <div><dt>被哪条记忆取代</dt><dd>{supersededByItemIdOf(selectedItem) || "-"}</dd></div>
                <div><dt>取代哪条记忆</dt><dd>{supersedesItemIdOf(selectedItem) || "-"}</dd></div>
                <div><dt>判定信号</dt><dd>{acceptanceSignalRows.length ? acceptanceSignalRows.map(([key, value]) => `${key} ${formatConfidence(value)}`).join(", ") : "-"}</dd></div>
              </dl>
              {selectedItem.possible_conflicts && Number(selectedItem.possible_conflicts.count || 0) > 0 && (
                <div className="admin-notice">
                  发现疑似重复的规范化键（normalized_key）{selectedItem.possible_conflicts.normalized_key || selectedItem.duplicate_hint?.normalized_key || "-"}：{" "}
                  {(selectedItem.possible_conflicts.items || []).map((item) => `#${item.id} ${memoryItemStatusLabel(item.status)} ${acceptanceStatusLabel(item.acceptance_status || "missing")}`).join(", ")}
                </div>
              )}
              <label className="field"><span>复核原因</span><input value={acceptanceReviewReason} onChange={(event) => onAcceptanceReviewReasonChange(event.target.value)} placeholder="选填" /></label>
              <div className="form-grid">
                <label className="field"><span>取代当前记忆的条目 ID</span><input value={supersededByItemIdInput} onChange={(event) => onSupersededByItemIdInputChange(event.target.value)} placeholder="替代记忆 ID" /></label>
                <label className="field"><span>当前记忆要取代的条目 ID</span><input value={supersedesItemIdInput} onChange={(event) => onSupersedesItemIdInputChange(event.target.value)} placeholder="旧记忆 ID" /></label>
              </div>
              <div className="action-row memory-acceptance-actions">
                {ACCEPTANCE_REVIEW_ACTIONS.map(({ action, label, effect }) => (
                  <DangerAction
                    key={action}
                    label={label}
                    title={`确认${label}？`}
                    confirmLabel={label}
                    pendingLabel="正在保存复核结果…"
                    disabled={Boolean(acceptanceReviewBusy)}
                    impact={<ul><li>记忆 ID：#{selectedItem.id}</li><li>当前状态：{acceptanceStatusLabel(acceptanceStatusOf(selectedItem))}</li><li>范围：{memoryScopeTypeLabel(selectedItem.scope_type)} / {selectedItem.user_id} / {selectedItem.session_id || "无会话"}</li><li>复核原因：{acceptanceReviewReason.trim() || "未填写"}</li><li>{effect}</li></ul>}
                    onConfirm={() => onReview(action)}
                  />
                ))}
                <DangerAction
                  label="由目标记忆取代"
                  title="确认将当前记忆标记为已取代？"
                  confirmLabel="确认取代关系"
                  pendingLabel="正在保存取代关系…"
                  disabled={Boolean(acceptanceReviewBusy) || !Number.isInteger(Number(supersededByItemIdInput.trim())) || Number(supersededByItemIdInput.trim()) <= 0}
                  impact={<ul><li>当前记忆 ID：#{selectedItem.id}</li><li>替代记忆 ID：#{supersededByItemIdInput.trim() || "未填写"}</li><li>当前记忆会进入“已取代”状态，并退出正常召回。</li><li>复核原因：{acceptanceReviewReason.trim() || "未填写"}</li></ul>}
                  onConfirm={onReviewSupersededBy}
                />
                <DangerAction
                  label="接受并取代旧记忆"
                  title="确认接受当前记忆并取代旧记忆？"
                  confirmLabel="确认接受并取代"
                  pendingLabel="正在保存取代关系…"
                  disabled={Boolean(acceptanceReviewBusy) || !Number.isInteger(Number(supersedesItemIdInput.trim())) || Number(supersedesItemIdInput.trim()) <= 0}
                  impact={<ul><li>当前记忆 ID：#{selectedItem.id}</li><li>将被取代的旧记忆 ID：#{supersedesItemIdInput.trim() || "未填写"}</li><li>当前记忆会进入“已采纳”状态，旧记忆会退出正常召回。</li><li>复核原因：{acceptanceReviewReason.trim() || "未填写"}</li></ul>}
                  onConfirm={onReviewSupersedes}
                />
              </div>
              <div className="memory-acceptance-history">
                <strong>近期采纳复核记录</strong>
                {acceptanceHistoryRows.length ? (
                  <div className="memory-item-meta">
                    {acceptanceHistoryRows.map((entry, index) => (
                      <span key={`${entry.reviewed_at || "history"}-${index}`}>
                        {formatTimestamp(entry.reviewed_at)} {entry.action || "-"} {acceptanceStatusLabel(entry.previous_acceptance_status || entry.previous_status)} → {acceptanceStatusLabel(entry.status)}，复核人 {entry.reviewed_by || "-"}{entry.reason ? `：${entry.reason}` : ""}
                      </span>
                    ))}
                  </div>
                ) : <div className="memory-item-meta"><span>暂无复核记录</span></div>}
              </div>
            </div>
          )}
        </div>
        <div className="action-row">
          <DangerAction
            label="保存修改"
            title="确认保存群内记忆修改"
            confirmLabel="确认保存"
            pendingLabel="正在保存…"
            disabled={!selectedItem || !editDraft.content.trim()}
            impact={selectedItem ? <ul><li>记忆 ID：#{selectedItem.id}</li><li>范围：{selectedItem.user_id} / {selectedItem.session_id || "无会话"}</li><li>会更新内容、状态、敏感级别和召回优先级。</li></ul> : "请先选择记忆。"}
            onConfirm={onSave}
          />
          <DangerAction
            label="软删除记忆"
            title="确认软删除这条记忆？"
            confirmLabel="确认软删除"
            pendingLabel="正在软删除…"
            disabled={!selectedItem}
            impact={selectedItem ? <ul><li>记忆 ID：#{selectedItem.id}</li><li>当前状态：{memoryItemStatusLabel(selectedItem.status)} / {acceptanceStatusLabel(acceptanceStatusOf(selectedItem))}</li><li>范围：{memoryScopeTypeLabel(selectedItem.scope_type)} / {selectedItem.user_id} / {selectedItem.session_id || "无会话"}</li><li>软删除后会退出正常召回，但保留管理员审计元数据。</li><li>{selectedItem.pinned ? "该记忆已置顶，本次确认同时覆盖置顶保护。" : "本次操作允许覆盖置顶或人工记忆保护。"}</li></ul> : "请先选择一条记忆。"}
            onConfirm={onDelete}
          />
        </div>
      </div>
    </div>
  );
}

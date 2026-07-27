import { DangerAction } from "../../components/DangerAction";
import {
  PROFILE_ENRICHMENT_REVIEW_ACTIONS,
  formatConfidence,
  formatTimestamp,
  profileEnrichmentPillClass,
  profileEnrichmentStateLabel,
  profileEnrichmentStateOf,
  profileEnrichmentSummary,
  profileEnrichmentTitle,
  type ProfileEnrichmentCandidate,
  type ProfileEnrichmentReviewAction,
  type ProfileEnrichmentReviewState,
  type WxbotSession,
} from "./model";

type ProfileEnrichmentPanelProps = {
  candidates: ProfileEnrichmentCandidate[];
  selectedCandidateId: number | null;
  selectedCandidate: ProfileEnrichmentCandidate | null;
  selectedGroup: WxbotSession | null;
  scopeReady: boolean;
  scopeSummary: string;
  sessionId: string;
  userId: string;
  query: string;
  hours: number;
  limit: number;
  externalCandidatesJson: string;
  reviewState: ProfileEnrichmentReviewState;
  includeHidden: boolean;
  listLimit: number;
  notes: string;
  busy: string | null;
  onGenerate: () => void | Promise<void>;
  onQueryChange: (value: string) => void;
  onHoursChange: (value: number) => void;
  onLimitChange: (value: number) => void;
  onExternalCandidatesJsonChange: (value: string) => void;
  onReviewStateChange: (value: ProfileEnrichmentReviewState) => void;
  onIncludeHiddenChange: (value: boolean) => void;
  onListLimitChange: (value: number) => void;
  onNotesChange: (value: string) => void;
  onRefreshCurrent: () => void;
  onShowAllNeedsReview: () => void;
  onLoadDetail: (candidateId?: number | null) => void | Promise<void>;
  onReview: (action: ProfileEnrichmentReviewAction) => void | Promise<void>;
};

export function ProfileEnrichmentPanel({
  candidates,
  selectedCandidateId,
  selectedCandidate,
  selectedGroup,
  scopeReady,
  scopeSummary,
  sessionId,
  userId,
  query,
  hours,
  limit,
  externalCandidatesJson,
  reviewState,
  includeHidden,
  listLimit,
  notes,
  busy,
  onGenerate,
  onQueryChange,
  onHoursChange,
  onLimitChange,
  onExternalCandidatesJsonChange,
  onReviewStateChange,
  onIncludeHiddenChange,
  onListLimitChange,
  onNotesChange,
  onRefreshCurrent,
  onShowAllNeedsReview,
  onLoadDetail,
  onReview,
}: ProfileEnrichmentPanelProps) {
  return (
    <section className="panel span-3 profile-enrichment-panel">
      <div className="panel-header">
        <div>
          <p className="section-kicker">人物画像补全</p>
          <h3>群成员画像复核</h3>
        </div>
        <span className="pill pill-feature">{candidates.length} 条候选</span>
      </div>
      <div className="admin-notice admin-notice-warning">
        安全提示：生成的人物画像候选默认进入“待复核”；公开身份候选不会自动通过，必须由管理员明确通过、拒绝或隐藏后才会影响已接受的画像记忆。
      </div>
      <div className="profile-enrichment-workbench">
        <div className="profile-enrichment-column">
          <div className="profile-enrichment-card profile-enrichment-flow-card">
            <div className="panel-header">
              <div>
                <p className="section-kicker">人物画像候选</p>
                <h3>继承当前范围后生成</h3>
              </div>
              <span className={scopeReady ? "pill pill-ok" : "pill pill-muted"}>{scopeReady ? "已就绪" : "待选择"}</span>
            </div>
            <div className="profile-enrichment-steps">
              <section className="profile-enrichment-step">
                <div className="profile-enrichment-step-head">
                  <span>1</span>
                  <div><strong>继承当前群</strong><small>范围来自页面顶部的已验证群，不在画像工作区重复选择。</small></div>
                </div>
                <div className="profile-enrichment-scope-card">
                  <span>已验证群</span>
                  <strong>{selectedGroup?.session_name || "尚未选择微信群"}</strong>
                  <small className="mono">{sessionId || "请在页面顶部选择群聊"}</small>
                </div>
              </section>
              <section className="profile-enrichment-step">
                <div className="profile-enrichment-step-head">
                  <span>2</span>
                  <div><strong>继承当前成员</strong><small>成员来自当前群名册，切换对象请回到页面顶部。</small></div>
                </div>
                <div className="profile-enrichment-scope-card">
                  <span>当前对象</span>
                  <strong>{scopeSummary}</strong>
                  <small className="mono">{userId || "请在页面顶部应用群成员"}</small>
                </div>
                <p className="profile-enrichment-selection-note">公开查询名：<strong>{query || "应用成员后自动生成"}</strong></p>
              </section>
              <section className="profile-enrichment-step">
                <div className="profile-enrichment-step-head">
                  <span>3</span>
                  <div><strong>生成候选画像</strong><small>默认回看 168 小时、最多 8 条外部候选。</small></div>
                </div>
                <div className="profile-enrichment-scope-card">
                  <span>当前对象</span>
                  <strong>{scopeSummary}</strong>
                  <small>{selectedGroup?.session_name || sessionId || "尚未选择微信群"}</small>
                </div>
                <DangerAction
                  label={busy === "generate" ? "正在生成候选..." : "生成候选画像"}
                  title="确认生成群成员画像候选"
                  confirmLabel="确认生成"
                  pendingLabel="正在生成…"
                  disabled={!scopeReady || busy === "generate"}
                  className="profile-enrichment-generate-button"
                  impact={(
                    <ul>
                      <li>范围：{scopeSummary}</li>
                      <li>回看：{hours} 小时，候选上限 {limit} 条</li>
                      <li>只生成待复核候选，不会自动进入运行时画像。</li>
                      <li>请求使用稳定幂等键，重复确认不会重复创建。</li>
                    </ul>
                  )}
                  onConfirm={onGenerate}
                />
              </section>
            </div>
            <details className="profile-enrichment-advanced">
              <summary>高级选项与原始范围</summary>
              <div className="form-grid profile-enrichment-advanced-grid">
                <label className="field"><span>回看小时数</span><input type="number" min={1} max={720} value={hours} onChange={(event) => onHoursChange(Number(event.target.value) || 168)} /></label>
                <label className="field"><span>候选上限</span><input type="number" min={1} max={20} value={limit} onChange={(event) => onLimitChange(Number(event.target.value) || 8)} /></label>
                <div className="field span-2"><span>已验证范围</span><strong>{scopeSummary}</strong><small>租户、群聊与成员来自当前登录身份和后端名册，不能手工覆盖。</small></div>
                <label className="field"><span>公开查询名称（query）</span><input value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder="公开名称 / 用户名 / WXID" /></label>
                <label className="field span-2"><span>外部候选数据（external_candidates JSON）</span><textarea rows={7} value={externalCandidatesJson} onChange={(event) => onExternalCandidatesJsonChange(event.target.value)} spellCheck={false} /></label>
              </div>
            </details>
          </div>
        </div>
        <div className="profile-enrichment-column">
          <div className="profile-enrichment-card">
            <div className="panel-header">
              <div><p className="section-kicker">候选列表</p><h3>待复核画像</h3></div>
              <span className="pill pill-muted">最多 {listLimit} 条</span>
            </div>
            <div className="profile-enrichment-list-summary">
              <strong>{scopeSummary}</strong>
              <span>默认筛选当前群成员的待复核记录；也可以查看全部待复核候选。</span>
            </div>
            <div className="action-row profile-enrichment-list-actions">
              <button className="button button-secondary" type="button" onClick={onRefreshCurrent}>刷新当前候选</button>
              <button className="button button-secondary" type="button" onClick={onShowAllNeedsReview} disabled={!scopeReady}>查看当前成员全部待复核</button>
            </div>
            <details className="profile-enrichment-advanced profile-enrichment-list-options">
              <summary>列表筛选选项</summary>
              <div className="form-grid profile-enrichment-filters">
                <label className="field">
                  <span>复核状态</span>
                  <select value={reviewState} onChange={(event) => onReviewStateChange(event.target.value as ProfileEnrichmentReviewState)}>
                    <option value="">全部</option>{(["needs_review", "candidate", "accepted", "rejected", "hidden"] as const).map((state) => <option value={state} key={state}>{profileEnrichmentStateLabel(state)}</option>)}
                  </select>
                </label>
                <label className="field"><span>列表条数</span><input type="number" min={1} max={500} value={listLimit} onChange={(event) => onListLimitChange(Number(event.target.value) || 100)} /></label>
                <label className="toggle-chip span-2">
                  <span><input type="checkbox" checked={includeHidden} onChange={(event) => onIncludeHiddenChange(event.target.checked)} />包含已隐藏候选</span>
                  <em>默认不显示已隐藏候选。</em>
                </label>
              </div>
            </details>
            <div className="profile-enrichment-list">
              {candidates.map((item) => {
                const state = profileEnrichmentStateOf(item);
                return (
                  <button type="button" className={`profile-enrichment-row${selectedCandidateId === item.id ? " is-active" : ""}`} key={item.id} onClick={() => void onLoadDetail(item.id)}>
                    <div><strong>{profileEnrichmentTitle(item)}</strong><span>{profileEnrichmentSummary(item) || "候选内容暂无摘要，打开详情查看 JSON 响应。"}</span></div>
                    <span className={profileEnrichmentPillClass(state)}>{profileEnrichmentStateLabel(state)}</span>
                    <small><span className="mono">#{item.id}</span> · 用户 <span className="mono">{item.user_id}</span> · 群 <span className="mono">{item.session_id || "-"}</span></small>
                    <em>{formatTimestamp(item.updated_at || item.created_at)}</em>
                  </button>
                );
              })}
              {!candidates.length && <div className="admin-notice">当前筛选下没有待复核画像候选。先选择群成员生成，或查看全部待复核。</div>}
            </div>
          </div>
        </div>
        <div className="profile-enrichment-column">
          <div className="profile-enrichment-card profile-enrichment-detail">
            <div className="panel-header">
              <div><p className="section-kicker">复核详情</p><h3>{selectedCandidateId ? `候选 #${selectedCandidateId}` : "选择一条候选"}</h3></div>
              <span className={profileEnrichmentPillClass(profileEnrichmentStateOf(selectedCandidate))}>
                {selectedCandidate ? profileEnrichmentStateLabel(profileEnrichmentStateOf(selectedCandidate)) : "未选择"}
              </span>
            </div>
            {selectedCandidate ? (
              <>
                <dl className="profile-enrichment-detail-list">
                  <div><dt>名称</dt><dd>{profileEnrichmentTitle(selectedCandidate)}</dd></div>
                  <div><dt>状态</dt><dd>{profileEnrichmentStateLabel(profileEnrichmentStateOf(selectedCandidate))}</dd></div>
                  <div><dt>用户</dt><dd className="mono">{selectedCandidate.user_id}</dd></div>
                  <div><dt>微信群</dt><dd className="mono">{selectedCandidate.session_id || "-"}</dd></div>
                  <div><dt>置信度</dt><dd>{formatConfidence(selectedCandidate.confidence)}</dd></div>
                  <div><dt>更新时间</dt><dd>{formatTimestamp(selectedCandidate.updated_at)}</dd></div>
                  <div className="profile-enrichment-detail-wide"><dt>画像摘要</dt><dd>{profileEnrichmentSummary(selectedCandidate) || "-"}</dd></div>
                  <div className="profile-enrichment-detail-wide"><dt>复核备注</dt><dd>{selectedCandidate.value?.review?.notes || selectedCandidate.value?.acceptance?.review_reason || "-"}</dd></div>
                </dl>
                <label className="field"><span>复核备注</span><textarea rows={4} value={notes} onChange={(event) => onNotesChange(event.target.value)} placeholder="可填写接受或拒绝原因" /></label>
                <div className="action-row">
                  {PROFILE_ENRICHMENT_REVIEW_ACTIONS.map(({ action, label, effect }) => (
                    <DangerAction
                      key={action}
                      label={label}
                      title={`确认${label}？`}
                      confirmLabel={label}
                      pendingLabel="正在保存复核结果…"
                      disabled={busy !== null}
                      impact={(
                        <ul>
                          <li>候选 ID：#{selectedCandidate.id}</li><li>名称：{profileEnrichmentTitle(selectedCandidate)}</li>
                          <li>当前状态：{profileEnrichmentStateLabel(profileEnrichmentStateOf(selectedCandidate))}</li>
                          <li>范围：{selectedCandidate.user_id} / {selectedCandidate.session_id || "无会话"}</li>
                          <li>复核备注：{notes.trim() || "未填写"}</li><li>{effect}</li>
                        </ul>
                      )}
                      onConfirm={() => onReview(action)}
                    />
                  ))}
                  <button className="button button-secondary" type="button" onClick={() => void onLoadDetail(selectedCandidateId)}>重新读取详情</button>
                </div>
              </>
            ) : (
              <div className="admin-notice">从左侧候选列表选择一条记录后，可查看画像摘要并执行通过、拒绝或隐藏。</div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

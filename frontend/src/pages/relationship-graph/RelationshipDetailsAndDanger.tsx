import { DangerAction } from "../../components/DangerAction";
import { OutputPanel } from "../../components/OutputPanel";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import {
  acceptanceClass,
  acceptanceStatusLabel,
  displayEdgeSource,
  displayEdgeTarget,
  evidenceCountsLabel,
  evidenceObservedRange,
  evidenceSourceLabel,
  formatConfidence,
  formatTimestamp,
  graphNodeLabel,
  readableRelationType,
  nodeTypeLabel,
  dateStatusClass,
  edgeCanAccept,
  edgeCanExpire,
  edgeCanReject,
  edgeCanReturnToReview,
  evidenceQuality,
  extractionMethodLabel,
  judgementSummary,
  counterpartEdges,
  firstMemoryItemId,
  formatAcceptanceScore,
  safeNodeAliases,
} from "./graphModel";
import type { RelationshipGraphController } from "./useRelationshipGraphController";

export type RelationshipMutationController = Pick<
  RelationshipGraphController,
  | "syncing"
  | "missingHistorySyncFields"
  | "selectedGroupId"
  | "targetDate"
  | "enqueueLlmJobs"
  | "runHistorySync"
  | "extracting"
  | "extractionMaxJobCount"
  | "runDailyExtraction"
  | "windowExtractionCursor"
  | "windowExtractionMaxWindowsValue"
  | "windowExtractionDryRun"
  | "runWindowExtraction"
  | "windowCatchupMaxWindowsValue"
  | "runWindowCatchup"
  | "runRecentCoverage"
  | "loadGraphAndStatus"
  | "dateLoading"
  | "jobStatsLoading"
  | "loading"
>;

export function RelationshipMutationActions({
  controller,
}: {
  controller: RelationshipMutationController;
}) {
  const {
    syncing,
    missingHistorySyncFields,
    selectedGroupId,
    targetDate,
    enqueueLlmJobs,
    runHistorySync,
    extracting,
    extractionMaxJobCount,
    runDailyExtraction,
    windowExtractionMaxWindowsValue,
    windowExtractionDryRun,
    runWindowExtraction,
    windowCatchupMaxWindowsValue,
    runWindowCatchup,
    runRecentCoverage,
    loadGraphAndStatus,
    dateLoading,
    jobStatsLoading,
    loading,
  } = controller;

  return (
          <div className="relationship-action-group">
            <span>历史与抽取</span>
            <DangerAction
              label={syncing || extracting ? "覆盖中" : "同步并抽取近7天"}
              title="确认同步并抽取近7天"
              confirmLabel="确认覆盖"
              pendingLabel="正在覆盖…"
              disabled={syncing || extracting}
              impact={(
                <dl>
                  <div><dt>目标群</dt><dd>{selectedGroupId ? "当前已验证群聊" : "未选择"}</dd></div>
                  <div><dt>范围</dt><dd>最近 7 天历史导入，并按日追平窗口抽取。</dd></div>
                  <div><dt>影响</dt><dd>会调用历史同步和语义抽取；新关系进入待审核，不展示聊天原文。</dd></div>
                </dl>
              )}
              onConfirm={runRecentCoverage}
            />
            <DangerAction
              label={syncing ? "同步中" : "同步日期并排队抽取"}
              title="确认同步群聊历史"
              confirmLabel="确认同步"
              pendingLabel="正在同步…"
              disabled={syncing || missingHistorySyncFields.length > 0}
              impact={(
                <dl>
                  <div><dt>目标群</dt><dd>{selectedGroupId ? "当前已验证群聊" : "未选择"}</dd></div>
                  <div><dt>日期</dt><dd>{targetDate || "未选择"}</dd></div>
                  <div><dt>范围</dt><dd>仅当前群获授权成员；不会接受用户 ID 覆盖。</dd></div>
                  <div><dt>影响</dt><dd>导入历史消息并{enqueueLlmJobs ? "排队 AI 抽取" : "仅写入记忆事件"}，重复提交由稳定幂等键保护。</dd></div>
                </dl>
              )}
              onConfirm={runHistorySync}
            />
            <DangerAction
              label={extracting ? "抽取中" : "运行所选日期 AI 抽取"}
              title="确认运行所选日期抽取"
              confirmLabel="确认运行"
              pendingLabel="正在抽取…"
              disabled={extracting || missingHistorySyncFields.length > 0}
              impact={<p>只处理当前已验证群聊在 {targetDate || "未选择日期"} 的待处理任务，最多 {extractionMaxJobCount} 个。</p>}
              onConfirm={runDailyExtraction}
            />
            <DangerAction
              label={extracting ? "抽取中" : "运行窗口关系抽取"}
              title="确认运行窗口关系抽取"
              confirmLabel="确认运行"
              pendingLabel="正在抽取…"
              disabled={extracting || missingHistorySyncFields.length > 0}
              impact={<p>从当前进度开始处理最多 {windowExtractionMaxWindowsValue} 个窗口；{windowExtractionDryRun ? "当前为演练，不写入关系。" : "结果将写入关系图。"}</p>}
              onConfirm={runWindowExtraction}
            />
            <DangerAction
              label={extracting ? "追平中" : "连续窗口追平"}
              title="确认连续追平窗口"
              confirmLabel="确认追平"
              pendingLabel="正在追平…"
              disabled={extracting || missingHistorySyncFields.length > 0}
              impact={<p>从当前进度连续处理最多 {windowCatchupMaxWindowsValue} 个窗口，受 60 秒时间预算限制。</p>}
              onConfirm={runWindowCatchup}
            />
            <button
              className="button button-secondary"
              type="button"
              onClick={() => void loadGraphAndStatus()}
              disabled={dateLoading || jobStatsLoading || loading || extracting}
            >
              {dateLoading || jobStatsLoading ? "刷新中" : "刷新日期/任务状态"}
            </button>
          </div>
  );
}

export function RelationshipUnavailableReset() {
  return (
    <div className="relationship-action-group relationship-action-group-danger">
      <span>清理</span>
      <button
        className="button button-secondary"
        type="button"
        disabled
        title="后端没有安全的关系图清理端点；避免误删生产数据。"
      >
        清空/重置不可用
      </button>
    </div>
  );
}

export function RelationshipDetailPanel(controller: RelationshipGraphController) {
  const {
    selection,
    setSelection,
    selectedNode,
    selectedEdge,
    nodesById,
    evidence,
    evidenceLoading,
    evidenceStatus,
    loadEdgeEvidence,
    reviewing,
    reviewEdge,
    graphEdges,
    pendingEdges,
    anonymousGraphLabels,
  } = controller;
  const replaceableEdges = selectedEdge
    ? counterpartEdges(selectedEdge, [...(graphEdges || []), ...(pendingEdges || [])])
    : [];
  const judgement = judgementSummary(evidence?.judgement);

  return (
        <section className="panel relationship-detail-panel">
          <div className="panel-header">
            <div>
              <p className="section-kicker">详情</p>
              <h3>{selection ? (selection.kind === "node" ? "这个人" : "这条互动") : "点一个人或一条线"}</h3>
            </div>
            {selection && (
              <button
                type="button"
                className="button button-secondary button-compact"
                onClick={() => setSelection(null)}
                title="回到整张图（也可以点空白处或按 Esc）"
              >
                取消选中
              </button>
            )}
          </div>
          {!selection && (
            <div className="relationship-empty is-compact">
              <strong>还没选中谁</strong>
              <span>在图上点人，或点一条线。名字优先用群里的称呼；wxid 只留在技术详情里。</span>
            </div>
          )}
          {selectedNode && (
            <>
              <dl className="relationship-detail-list">
                <div><dt>群里怎么叫</dt><dd>{graphNodeLabel(selectedNode, anonymousGraphLabels)}</dd></div>
                <div><dt>也叫</dt><dd>{safeNodeAliases(selectedNode)}</dd></div>
                <div><dt>类型</dt><dd>{nodeTypeLabel(selectedNode.type)}</dd></div>
                <div><dt>审核状态</dt><dd><span className={acceptanceClass(selectedNode.acceptance_status)}>{acceptanceStatusLabel(selectedNode.acceptance_status)}</span></dd></div>
                <div><dt>置信度</dt><dd>{formatConfidence(selectedNode.confidence)}</dd></div>
                <div><dt>证据数</dt><dd>{selectedNode.evidence_count ?? 0}</dd></div>
                <div><dt>首次出现</dt><dd>{formatTimestamp(selectedNode.first_seen)}</dd></div>
                <div><dt>最近出现</dt><dd>{formatTimestamp(selectedNode.last_seen)}</dd></div>
              </dl>
              <TechnicalDetails summary="查看节点技术详情" value={selectedNode} />
            </>
          )}
          {selectedEdge && (
            <>
              <dl className="relationship-detail-list">
                <div><dt>互动</dt><dd>{readableRelationType(selectedEdge.label || selectedEdge.type)}</dd></div>
                <div><dt>谁</dt><dd>{nodesById.get(displayEdgeSource(selectedEdge)) ? graphNodeLabel(nodesById.get(displayEdgeSource(selectedEdge))!, anonymousGraphLabels) : "未知成员"}</dd></div>
                <div><dt>和谁</dt><dd>{nodesById.get(displayEdgeTarget(selectedEdge)) ? graphNodeLabel(nodesById.get(displayEdgeTarget(selectedEdge))!, anonymousGraphLabels) : "未知成员"}</dd></div>
                <div><dt>审核状态</dt><dd><span className={acceptanceClass(selectedEdge.acceptance_status)}>{acceptanceStatusLabel(selectedEdge.acceptance_status)}</span></dd></div>
                <div><dt>抽取方式</dt><dd>{extractionMethodLabel(selectedEdge.extraction_method)}</dd></div>
                <div><dt>验收分</dt><dd>{formatAcceptanceScore(selectedEdge.acceptance_score ?? evidenceQuality(evidence).score)}</dd></div>
                <div><dt>验收原因</dt><dd>{selectedEdge.acceptance_reason || evidenceQuality(evidence).reason || "-"}</dd></div>
                <div><dt>可能冲突</dt><dd>{evidenceQuality(evidence).conflicts || 0}</dd></div>
                <div><dt>置信度</dt><dd>{formatConfidence(selectedEdge.confidence)}</dd></div>
                <div><dt>证据数</dt><dd>{selectedEdge.evidence_count ?? 0}</dd></div>
                <div><dt>消息数</dt><dd>{selectedEdge.source_message_count ?? "-"}</dd></div>
                <div><dt>首次出现</dt><dd>{formatTimestamp(selectedEdge.first_seen)}</dd></div>
                <div><dt>最近出现</dt><dd>{formatTimestamp(selectedEdge.last_seen)}</dd></div>
              </dl>
              {(
                edgeCanAccept(selectedEdge.acceptance_status)
                || edgeCanReject(selectedEdge.acceptance_status)
                || edgeCanExpire(selectedEdge.acceptance_status)
                || edgeCanReturnToReview(selectedEdge.acceptance_status)
                || replaceableEdges.some((item) => firstMemoryItemId(item) > 0)
              ) && (
                <div className="relationship-review-actions">
                  <p className="relationship-review-note">审核只改这条关系的验收状态，不展示也不改写聊天原文。</p>
                  {edgeCanAccept(selectedEdge.acceptance_status) && (
                    <DangerAction
                      label={reviewing ? "审核中" : "接受关系"}
                      title="确认接受该关系"
                      confirmLabel="确认接受"
                      pendingLabel="正在接受…"
                      disabled={reviewing}
                      impact={(
                        <dl>
                          <div><dt>关系</dt><dd>{readableRelationType(selectedEdge.label || selectedEdge.type)}</dd></div>
                          <div><dt>当前状态</dt><dd>{acceptanceStatusLabel(selectedEdge.acceptance_status)}</dd></div>
                          <div><dt>影响</dt><dd>允许这条关系进入机器人召回。看图不需要接受。</dd></div>
                        </dl>
                      )}
                      onConfirm={() => reviewEdge("accept")}
                    />
                  )}
                  {edgeCanReject(selectedEdge.acceptance_status) && (
                    <DangerAction
                      label={reviewing ? "审核中" : "拒绝关系"}
                      title="确认拒绝该关系"
                      confirmLabel="确认拒绝"
                      pendingLabel="正在拒绝…"
                      disabled={reviewing}
                      impact={(
                        <dl>
                          <div><dt>关系</dt><dd>{readableRelationType(selectedEdge.label || selectedEdge.type)}</dd></div>
                          <div><dt>当前状态</dt><dd>{acceptanceStatusLabel(selectedEdge.acceptance_status)}</dd></div>
                          <div><dt>影响</dt><dd>标记为已拒绝；默认关系图不再显示该边。</dd></div>
                        </dl>
                      )}
                      onConfirm={() => reviewEdge("reject")}
                    />
                  )}
                  {edgeCanReturnToReview(selectedEdge.acceptance_status) && (
                    <DangerAction
                      label={reviewing ? "审核中" : "退回待审"}
                      title="确认退回待审核"
                      confirmLabel="确认退回"
                      pendingLabel="正在退回…"
                      disabled={reviewing}
                      impact={(
                        <dl>
                          <div><dt>关系</dt><dd>{readableRelationType(selectedEdge.label || selectedEdge.type)}</dd></div>
                          <div><dt>当前状态</dt><dd>{acceptanceStatusLabel(selectedEdge.acceptance_status)}</dd></div>
                          <div><dt>影响</dt><dd>该关系会离开默认图，重新进入待审核队列。</dd></div>
                        </dl>
                      )}
                      onConfirm={() => reviewEdge("needs_review")}
                    />
                  )}
                  {edgeCanExpire(selectedEdge.acceptance_status) && (
                    <DangerAction
                      label={reviewing ? "审核中" : "标记过期"}
                      title="确认将该关系标记为过期"
                      confirmLabel="确认过期"
                      pendingLabel="正在过期…"
                      disabled={reviewing}
                      impact={(
                        <dl>
                          <div><dt>关系</dt><dd>{readableRelationType(selectedEdge.label || selectedEdge.type)}</dd></div>
                          <div><dt>当前状态</dt><dd>{acceptanceStatusLabel(selectedEdge.acceptance_status)}</dd></div>
                          <div><dt>影响</dt><dd>标记为已过期；默认关系图不再显示该边。</dd></div>
                        </dl>
                      )}
                      onConfirm={() => reviewEdge("expire")}
                    />
                  )}
                  {replaceableEdges.map((other) => {
                    const otherItemId = firstMemoryItemId(other);
                    if (!otherItemId) return null;
                    return (
                      <DangerAction
                        key={other.id}
                        label={reviewing ? "审核中" : `替代 ${readableRelationType(other.label || other.type)}`}
                        title="确认用当前关系替代另一条"
                        confirmLabel="确认替代"
                        pendingLabel="正在替代…"
                        disabled={reviewing}
                        impact={(
                          <dl>
                            <div><dt>当前关系</dt><dd>{readableRelationType(selectedEdge.label || selectedEdge.type)}</dd></div>
                            <div><dt>被替代</dt><dd>{readableRelationType(other.label || other.type)} · {acceptanceStatusLabel(other.acceptance_status)}</dd></div>
                            <div><dt>影响</dt><dd>当前关系保持有效，被替代关系标记为已被替代，不再进入默认图。</dd></div>
                          </dl>
                        )}
                        onConfirm={() => reviewEdge("supersede", selectedEdge, { supersedes_item_id: otherItemId })}
                      />
                    );
                  })}
                </div>
              )}
              <TechnicalDetails summary="查看关系技术详情" value={selectedEdge} />

              <div className="relationship-evidence-panel" aria-live="polite">
                <div className="relationship-evidence-header">
                  <div>
                    <p className="section-kicker">证据来源</p>
                    <h4>安全证据元数据</h4>
                  </div>
                  <button
                    className="button button-secondary button-compact"
                    type="button"
                    onClick={() => void loadEdgeEvidence(selectedEdge)}
                    disabled={evidenceLoading}
                  >
                    {evidenceLoading ? "加载中" : "刷新证据"}
                  </button>
                </div>
                <p className={`relationship-evidence-status${evidenceStatus.includes("失败") || evidenceStatus.includes("缺少") ? " is-warning" : ""}`}>
                  {evidenceStatus}
                </p>
                <p className="relationship-evidence-note">不展示原始聊天内容</p>

                {evidence && (
                  <>
                    <dl className="relationship-detail-list">
                      <div><dt>证据来源</dt><dd>{evidenceCountsLabel(evidence)}</dd></div>
                      {evidenceSourceLabel(evidence.evidence_source) && (
                        <div><dt>证据类型</dt><dd>{evidenceSourceLabel(evidence.evidence_source)}</dd></div>
                      )}
                      <div><dt>审核状态</dt><dd><span className={acceptanceClass(evidence.edge?.acceptance_status)}>{acceptanceStatusLabel(evidence.edge?.acceptance_status)}</span></dd></div>
                      <div><dt>首次出现</dt><dd>{formatTimestamp(evidence.edge?.first_seen)}</dd></div>
                      <div><dt>最近出现</dt><dd>{formatTimestamp(evidence.edge?.last_seen)}</dd></div>
                      {evidenceObservedRange(evidence) && (
                        <div><dt>群消息时间</dt><dd>{evidenceObservedRange(evidence)}</dd></div>
                      )}
                      {(evidence.evidence_counts?.evidence_days ?? 0) > 0 && (
                        <div><dt>覆盖天数</dt><dd>{evidence.evidence_counts?.evidence_days}</dd></div>
                      )}
                    </dl>
                    {judgement && (
                      <div className="relationship-judgement" data-testid="relationship-judgement">
                        <h4>怎么判定的</h4>
                        <dl className="relationship-detail-list">
                          <div><dt>抽取方式</dt><dd>{judgement.method}</dd></div>
                          {judgement.signals.length > 0 && (
                            <div><dt>信号</dt><dd>{judgement.signals.join("，")}</dd></div>
                          )}
                          {judgement.policyText && (
                            <div><dt>通过依据</dt><dd>{judgement.policyText}</dd></div>
                          )}
                          {judgement.reviewerText && (
                            <div><dt>判定者</dt><dd>{judgement.reviewerText}</dd></div>
                          )}
                          {judgement.modelReason && (
                            <div><dt>模型理由</dt><dd>{judgement.modelReason}<small className="muted-copy"> 模型自己的转述，不是聊天原文；主题词可能是模型归纳的（如把成员名归为组合名），按"群消息时间"去原聊天里核对。</small></dd></div>
                          )}
                        </dl>
                      </div>
                    )}
                    <TechnicalDetails summary="查看证据技术详情" value={evidence} />
                  </>
                )}
              </div>
            </>
          )}
        </section>
  );
}

export function RelationshipHistorySummary(controller: RelationshipGraphController) {
  const {
    selectedDateStatus,
    selectedDateStatusText,
    targetDate,
    selectedDateRawCount,
    selectedDateImportedCount,
    selectedJobStats,
    historyNextStep,
    syncOutput,
    output,
  } = controller;

  return (
    <>
      <section className="panel relationship-history-summary" aria-label="历史同步摘要">
        <div>
          <span>历史导入状态</span>
          <strong className={dateStatusClass(selectedDateStatus?.status)}>{selectedDateStatusText}</strong>
        </div>
        <div>
          <span>选择日期</span>
          <strong>{targetDate || "-"}</strong>
        </div>
        <div>
          <span>历史消息数</span>
          <strong>{selectedDateRawCount}</strong>
        </div>
        <div>
          <span>已导入</span>
          <strong>{selectedDateImportedCount}</strong>
        </div>
        <div>
          <span>AI待处理</span>
          <strong>{selectedJobStats.pending}</strong>
        </div>
        <div>
          <span>AI运行中</span>
          <strong>{selectedJobStats.running}</strong>
        </div>
        <div>
          <span>AI成功/失败</span>
          <strong>{selectedJobStats.succeeded} / {selectedJobStats.failed + selectedJobStats.dead}</strong>
        </div>
        <div className="relationship-history-next">
          <span>下一步</span>
          <strong>{historyNextStep}</strong>
        </div>
      </section>

      <OutputPanel flush title="历史同步调试 JSON" value={syncOutput} />
      <OutputPanel flush title="关系图响应摘要 JSON" value={output} />
    </>
  );
}

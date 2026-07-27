import type { RelationshipGraphController } from "./useRelationshipGraphController";
import {
  RelationshipMutationActions,
  RelationshipUnavailableReset,
} from "./RelationshipDetailsAndDanger";

export function RelationshipActionPanel(controller: RelationshipGraphController) {
  const {
    actionPanelOpen,
    setActionPanelOpen,
    loadGraph,
    loading,
    showAllGraph,
    extracting,
    extractionBatchLimit,
    setExtractionBatchLimit,
    extractionContinuous,
    setExtractionContinuous,
    extractionMaxJobs,
    setExtractionMaxJobs,
    selectedJobStats,
    estimatedExtractionClicks,
    windowExtractionSize,
    setWindowExtractionSize,
    windowExtractionMaxWindows,
    setWindowExtractionMaxWindows,
    windowCatchupMaxWindows,
    setWindowCatchupMaxWindows,
    windowExtractionDryRun,
    setWindowExtractionDryRun,
    windowExtractionCursor,
  } = controller;

  return (
      <section className={`panel relationship-action-panel${actionPanelOpen ? " is-open" : ""}`} aria-label="关系图操作">
        <div className="relationship-action-bar">
          <div className="relationship-action-copy">
            <p className="section-kicker">操作</p>
            <h3>关系图控制台</h3>
            <span>历史导入、AI 抽取任务和图谱读取分开执行；此页不显示原始聊天内容。</span>
          </div>
          <div className="relationship-action-group">
            <span>图谱</span>
            <button className="button button-primary" type="button" onClick={() => void loadGraph()} disabled={loading}>
              {loading ? "加载中" : "刷新关系图"}
            </button>
            <button className="button button-secondary" type="button" onClick={() => void showAllGraph()} disabled={loading}>
              显示全部关系
            </button>
          </div>
          <button
            className="button button-secondary relationship-action-toggle"
            type="button"
            onClick={() => setActionPanelOpen((open) => !open)}
            aria-expanded={actionPanelOpen}
          >
            {actionPanelOpen ? "收起抽取控制" : "抽取控制"}
          </button>
        </div>
        <div className="relationship-action-groups" hidden={!actionPanelOpen}>
          <RelationshipMutationActions controller={controller} />
          <div className="relationship-action-group relationship-extraction-controls">
            <span>AI 批处理</span>
            <label className="relationship-inline-control">
              <small>每批上限</small>
              <select
                value={extractionBatchLimit}
                onChange={(event) => setExtractionBatchLimit(event.target.value)}
                disabled={extracting}
              >
                <option value="20">20</option>
                <option value="50">50</option>
                <option value="100">100</option>
              </select>
            </label>
            <label className="relationship-inline-check">
              <input
                type="checkbox"
                checked={extractionContinuous}
                onChange={(event) => setExtractionContinuous(event.target.checked)}
                disabled={extracting}
              />
              <small>连续</small>
            </label>
            <label className="relationship-inline-control">
              <small>任务上限</small>
              <input
                type="number"
                min="1"
                max="500"
                value={extractionMaxJobs}
                onChange={(event) => setExtractionMaxJobs(event.target.value)}
                disabled={extracting || !extractionContinuous}
              />
            </label>
            <em>待处理 {selectedJobStats.pending}，约 {estimatedExtractionClicks || 0} 次</em>
          </div>
          <div className="relationship-action-group relationship-extraction-controls">
            <span>窗口抽取</span>
            <label className="relationship-inline-control">
              <small>窗口大小</small>
              <select
                value={windowExtractionSize}
                onChange={(event) => setWindowExtractionSize(event.target.value)}
                disabled={extracting}
              >
                <option value="30">30</option>
                <option value="50">50</option>
                <option value="80">80</option>
              </select>
            </label>
            <label className="relationship-inline-control">
              <small>窗口上限</small>
              <select
                value={windowExtractionMaxWindows}
                onChange={(event) => setWindowExtractionMaxWindows(event.target.value)}
                disabled={extracting}
              >
                <option value="1">1</option>
                <option value="3">3</option>
                <option value="5">5</option>
              </select>
            </label>
            <label className="relationship-inline-control">
              <small>追平上限</small>
              <input
                type="number"
                min="1"
                max="100"
                value={windowCatchupMaxWindows}
                onChange={(event) => setWindowCatchupMaxWindows(event.target.value)}
                disabled={extracting}
              />
            </label>
            <label className="relationship-inline-check">
              <input
                type="checkbox"
                checked={windowExtractionDryRun}
                onChange={(event) => setWindowExtractionDryRun(event.target.checked)}
                disabled={extracting}
              />
              <small>仅演练</small>
            </label>
            <em>当前游标 {windowExtractionCursor}</em>
          </div>
          <RelationshipUnavailableReset />
        </div>
      </section>
  );
}

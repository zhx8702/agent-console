import type { RelationshipGraphController } from "./useRelationshipGraphController";
import {
  RelationshipMutationActions,
  RelationshipUnavailableReset,
} from "./RelationshipDetailsAndDanger";

export function RelationshipActionPanel(controller: RelationshipGraphController) {
  const {
    actionPanelOpen,
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

  if (!actionPanelOpen) return null;

  return (
    <section className="panel relationship-action-panel is-open" aria-label="关系图操作">
      <div className="relationship-action-groups">
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

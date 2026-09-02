import { GroupScopeEmpty } from "../../components/GroupScopeEmpty";
import { PageHeader } from "../../components/PageHeader";
import { RelationshipActionPanel } from "./RelationshipActionPanel";
import {
  RelationshipDetailPanel,
  RelationshipHistorySummary,
} from "./RelationshipDetailsAndDanger";
import { RelationshipFiltersAndStatus } from "./RelationshipFiltersAndStatus";
import { RelationshipGraphPresentation } from "./RelationshipGraphPresentation";
import { useRelationshipGraphController } from "./useRelationshipGraphController";

export function RelationshipGraphWorkspace() {
  const controller = useRelationshipGraphController();

  if (!controller.selectedGroupIsVerified) {
    return (
      <GroupScopeEmpty
        eyebrow="关系记忆可视化"
        title="群聊关系图"
        description="展示当前群的关系记忆与审核状态，可接受或拒绝待审核关系，不展示聊天原文。"
      />
    );
  }

  return (
    <div className="relationship-graph-page">
      <PageHeader
        eyebrow="关系记忆可视化"
        title="群聊关系图"
        description="默认看最近 7 天已接受关系。可按日回放、点选画布、审核或替代关系；调度会补同步已知群并抽取。不展示聊天原文。"
        actions={
          <div className="action-row">
            <button className="button button-primary" type="button" onClick={() => void controller.loadGraph()} disabled={controller.loading}>
              {controller.loading ? "加载中" : "刷新关系图"}
            </button>
            <button className="button button-secondary" type="button" onClick={() => void controller.showPendingReview()} disabled={controller.loading || controller.reviewing}>
              {controller.pendingReviewCount > 0 ? `查看待审核（${controller.pendingReviewCount}）` : "查看待审核"}
            </button>
            <button className="button button-secondary" type="button" onClick={() => void controller.showAllGraph()} disabled={controller.loading}>
              显示全部关系
            </button>
            <button
              className="button button-secondary"
              type="button"
              onClick={() => controller.setActionPanelOpen((open) => !open)}
              aria-expanded={controller.actionPanelOpen}
            >
              {controller.actionPanelOpen ? "收起抽取控制" : "抽取控制"}
            </button>
          </div>
        }
      />

      <RelationshipActionPanel {...controller} />

      <div className="relationship-workbench">
        <RelationshipGraphPresentation {...controller} />
        <RelationshipDetailPanel {...controller} />
      </div>

      <RelationshipFiltersAndStatus {...controller} />
      <RelationshipHistorySummary {...controller} />
    </div>
  );
}

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
        description="以只读方式展示当前群的关系记忆，包括审核状态与证据引用，不展示聊天原文。"
      />
    );
  }

  return (
    <div className="relationship-graph-page">
      <PageHeader
        eyebrow="关系记忆可视化"
        title="群聊关系图"
        description="只读展示当前群的关系记忆与审核状态，不展示聊天原文。"
        actions={
          <div className="action-row">
            <button className="button button-primary" type="button" onClick={() => void controller.loadGraph()} disabled={controller.loading}>
              {controller.loading ? "加载中" : "刷新关系图"}
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

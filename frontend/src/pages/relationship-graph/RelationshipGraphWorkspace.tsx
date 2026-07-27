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

  return (
    <div className="relationship-graph-page">
      <PageHeader
        eyebrow="关系记忆可视化"
        title="Relationship Graph / 群聊关系图"
        description="以只读方式展示当前群的关系记忆，包括审核状态与证据引用，不展示聊天原文。"
      />

      <RelationshipActionPanel {...controller} />
      <RelationshipFiltersAndStatus {...controller} />

      <div className="relationship-workbench">
        <RelationshipGraphPresentation {...controller} />
        <RelationshipDetailPanel {...controller} />
      </div>

      <RelationshipHistorySummary {...controller} />
    </div>
  );
}

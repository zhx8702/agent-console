import { PageHeader } from "../../components/PageHeader";
import { SearchableSelect } from "../../components/SearchableSelect";
import type { KnowledgeScopeController } from "./useKnowledgeScope";

type KnowledgeHeaderProps = {
  scope: KnowledgeScopeController;
  faqCount: number;
  documentCount: number;
};

export function KnowledgeHeader({ scope, faqCount, documentCount }: KnowledgeHeaderProps) {
  return (
    <section className="panel span-3 panel-hero">
      <PageHeader
        eyebrow="知识运营"
        title="FAQ / 知识库运营"
        description="FAQ 做短问短答，知识库做长文本沉淀。按作用域管理，并支持 FAQ 命中测试。"
      />
      <div className="knowledge-scope-bar">
        <div className="knowledge-scope-controls">
          <label className="field knowledge-scope-field knowledge-scope-mode">
            <span>作用域</span>
            <select value={scope.scopeMode} onChange={(event) => scope.setScopeMode(event.target.value as typeof scope.scopeMode)}>
              <option value="global">全局</option>
              <option value="session">从群/会话列表选择</option>
            </select>
          </label>
          <label className="field knowledge-scope-field knowledge-scope-target">
            <span>目标群 / 会话</span>
            {scope.scopeMode === "session" ? (
              <SearchableSelect
                value={scope.targetSessionId}
                options={scope.sessionOptions}
                onChange={(value) => {
                  scope.setTargetSessionId(value);
                  scope.selectVerifiedGroup(value);
                }}
                placeholder="选择群聊或会话"
                searchPlaceholder="输入群名、备注或 session_id 搜索"
                emptyText="暂无可选群或会话"
              />
            ) : <input value="" disabled placeholder="全局作用域无需填写" />}
          </label>
        </div>
        <div className="status-grid">
          <div className="status-tile"><span>当前范围</span><strong>{scope.currentScopeText}</strong></div>
          <div className="status-tile" data-status="enabled"><span>FAQ 总数</span><strong>{faqCount}</strong></div>
          <div className="status-tile"><span>知识文档</span><strong>{documentCount}</strong></div>
        </div>
      </div>
      <div className="knowledge-tabbar">
        <button className={`knowledge-tab${scope.activeTab === "faq" ? " is-active" : ""}`} onClick={() => scope.setActiveTab("faq")}>FAQ 管理</button>
        <button className={`knowledge-tab${scope.activeTab === "docs" ? " is-active" : ""}`} onClick={() => scope.setActiveTab("docs")}>知识文档</button>
        <button className={`knowledge-tab${scope.activeTab === "import" ? " is-active" : ""}`} onClick={() => scope.setActiveTab("import")}>聊天转 FAQ</button>
      </div>
    </section>
  );
}

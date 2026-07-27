import { DangerAction } from "../../components/DangerAction";
import { splitMultivalue } from "./model";
import type { FaqController } from "./useFaqController";

type FaqWorkspaceProps = {
  faq: FaqController;
  currentScopeText: string;
};

export function FaqWorkspace({ faq, currentScopeText }: FaqWorkspaceProps) {
  return (
    <>
      <section className="panel span-2">
        <div className="knowledge-workbench">
          <aside className="knowledge-sidebar">
            <div className="panel-header"><div><p className="section-kicker">问答列表</p><h3>常见问答列表</h3></div></div>
            <div className="form-grid knowledge-filter-grid">
              <label className="field">
                <span>搜索</span>
                <input value={faq.search} onChange={(event) => faq.setSearch(event.target.value)} placeholder="搜问题、答案、标签、变体" />
              </label>
              <label className="field">
                <span>状态</span>
                <select value={faq.statusFilter} onChange={(event) => faq.setStatusFilter(event.target.value)}>
                  <option value="">全部</option><option value="published">已发布</option><option value="disabled">已停用</option>
                </select>
              </label>
            </div>
            <div className="knowledge-stats-inline">
              <span className="pill pill-ok">已发布 {faq.publishedCount}</span>
              <span className="pill pill-muted">已停用 {faq.disabledCount}</span>
              <span className="pill pill-feature">当前筛选 {faq.filteredItems.length}</span>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void faq.load(faq.selectedId)} disabled={faq.loading}>刷新</button>
              <button className="button button-primary" onClick={() => faq.hydrateEditor(null)}>新建 FAQ</button>
            </div>
            <div className="knowledge-list">
              {faq.filteredItems.map((item) => (
                <button
                  key={item.id}
                  className={`knowledge-list-item${faq.selectedId === item.id ? " is-active" : ""}`}
                  onClick={() => faq.hydrateEditor(item)}
                >
                  <div className="knowledge-list-head">
                    <strong>{item.question}</strong>
                    <span className={`pill ${(item.status || "published") === "published" ? "pill-ok" : "pill-muted"}`}>{(item.status || "published") === "published" ? "已发布" : "已停用"}</span>
                  </div>
                  <div className="knowledge-list-copy">{item.answer}</div>
                  <div className="knowledge-meta-row">
                    <span className="mono">#{item.id}</span>
                    <span>{item.scope === "global" ? "全局" : item.session_id || "会话"}</span>
                    <span>变体 {item.variants?.length || 0}</span>
                  </div>
                  {!!item.tags?.length && <div className="knowledge-chip-row">
                    {item.tags.slice(0, 4).map((tag) => <span key={tag} className="pill pill-feature">{tag}</span>)}
                  </div>}
                </button>
              ))}
              {!faq.filteredItems.length && <div className="admin-notice">{faq.loading ? "正在加载 FAQ..." : "当前作用域下还没有 FAQ。"}</div>}
            </div>
          </aside>

          <div className="knowledge-editor">
            <div className="panel-header"><div><p className="section-kicker">问答编辑</p><h3>{faq.selected ? `编辑问答 #${faq.selected.id}` : "新建常见问答"}</h3></div></div>
            <div className="form-grid">
              <label className="field span-2"><span>问题</span><input value={faq.question} onChange={(event) => faq.setQuestion(event.target.value)} /></label>
              <label className="field span-2"><span>标准答案</span><textarea rows={6} value={faq.answer} onChange={(event) => faq.setAnswer(event.target.value)} /></label>
              <label className="field">
                <span>状态</span>
                <select value={faq.status} onChange={(event) => faq.setStatus(event.target.value)}>
                  <option value="published">已发布</option><option value="disabled">已停用</option>
                </select>
              </label>
              <label className="field"><span>范围</span><input value={currentScopeText} readOnly /></label>
              <label className="field span-2">
                <span>变体句式</span>
                <textarea rows={4} value={faq.variantsText} onChange={(event) => faq.setVariantsText(event.target.value)} placeholder={"每行一个变体，也支持逗号分隔\n例如：退款流程\n怎么退款"} />
              </label>
              <label className="field span-2">
                <span>标签</span>
                <textarea rows={3} value={faq.tagsText} onChange={(event) => faq.setTagsText(event.target.value)} placeholder={"每行一个标签，也支持逗号分隔\n例如：售后\n退款"} />
              </label>
            </div>
            <div className="knowledge-chip-row">
              {splitMultivalue(faq.variantsText).map((item) => <span key={`variant-${item}`} className="pill pill-muted">{item}</span>)}
              {splitMultivalue(faq.tagsText).map((item) => <span key={`tag-${item}`} className="pill pill-feature">{item}</span>)}
            </div>
            <div className="action-row">
              <button className="button button-primary" onClick={() => void faq.save()}>{faq.selected ? "保存 FAQ" : "创建 FAQ"}</button>
              <button className="button button-secondary" onClick={() => faq.hydrateEditor(null)}>清空编辑器</button>
              <button className="button button-secondary" onClick={() => void faq.toggleStatus()} disabled={!faq.selected}>{faq.status === "published" ? "停用 FAQ" : "启用 FAQ"}</button>
              <DangerAction
                label="删除 FAQ"
                title="删除 FAQ"
                impact={<p>FAQ #{faq.selectedId ?? "—"} 将从当前知识范围中永久删除，后续问答不会再召回它。</p>}
                onConfirm={faq.remove}
                disabled={!faq.selected}
              />
            </div>
          </div>
        </div>
      </section>
      <FaqPreviewPanel faq={faq} />
    </>
  );
}

function FaqPreviewPanel({ faq }: { faq: FaqController }) {
  return (
    <section className="panel">
      <div className="panel-header"><div><p className="section-kicker">命中预览</p><h3>问答命中测试台</h3></div></div>
      <label className="field">
        <span>测试问题</span>
        <textarea rows={5} value={faq.testQuery} onChange={(event) => faq.setTestQuery(event.target.value)} placeholder="输入一条真实用户问题，检查会命中哪条 FAQ" />
      </label>
      <div className="action-row"><button className="button button-primary" onClick={() => void faq.runPreview()}>执行测试</button></div>
      {faq.preview ? faq.preview.matched ? (
        <div className="knowledge-preview-card">
          <div className="knowledge-preview-row"><span>命中结果</span><strong>已命中</strong></div>
          <div className="knowledge-preview-row"><span>命中问题</span><strong>{faq.preview.citation?.snippet || "-"}</strong></div>
          <div className="knowledge-preview-row"><span>命中范围</span><strong>{faq.preview.resolved_scope || "-"}</strong></div>
          <div className="knowledge-preview-row"><span>作用域会话</span><strong className="mono">{faq.preview.resolved_session_id || "-"}</strong></div>
          <div className="knowledge-preview-row"><span>相似度</span><strong>{typeof faq.preview.score === "number" ? faq.preview.score.toFixed(4) : "-"}</strong></div>
          <div className="knowledge-preview-row"><span>最终回复</span><strong>{faq.preview.reply_text || "-"}</strong></div>
        </div>
      ) : <div className="admin-notice">当前问题没有命中 FAQ，会继续走后续 FAQ miss 流程。</div>
        : <div className="admin-notice">这里会展示 FAQ 命中测试结果，包括命中的问题、范围和最终回复。</div>}
    </section>
  );
}

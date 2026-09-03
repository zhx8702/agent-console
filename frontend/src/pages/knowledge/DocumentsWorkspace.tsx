import { DangerAction } from "../../components/DangerAction";
import type { DocumentsController } from "./useDocumentsController";

export function DocumentsWorkspace({ documents }: { documents: DocumentsController }) {
  return (
    <>
      <section className="panel span-3">
        <div className="knowledge-workbench">
          <aside className="knowledge-sidebar">
            <div className="panel-header"><div><p className="section-kicker">知识文档</p><h3>知识文档列表</h3></div></div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void documents.load(documents.selectedId)} disabled={documents.loading}>刷新</button>
              <button className="button button-primary" onClick={() => documents.hydrateEditor(null)}>新建文档</button>
            </div>
            <div className="knowledge-list">
              {documents.items.map((item) => (
                <button
                  key={item.id}
                  className={`knowledge-list-item${documents.selectedId === item.id ? " is-active" : ""}`}
                  onClick={() => void documents.open(item)}
                >
                  <div className="knowledge-list-head"><strong>{item.title}</strong><span className="pill pill-feature">{item.source || "manual"}</span></div>
                  <div className="knowledge-meta-row"><span className="mono">#{item.id}</span><span>{item.scope === "global" ? "全局" : item.session_id || "会话"}</span></div>
                  {item.url && <div className="knowledge-list-copy">{item.url}</div>}
                </button>
              ))}
              {!documents.items.length && <div className="admin-notice">{documents.loading ? "正在加载知识文档..." : "当前作用域下还没有知识文档。"}</div>}
            </div>
          </aside>

          <div className="knowledge-editor">
            <div className="panel-header"><div><p className="section-kicker">文档编辑</p><h3>{documents.selected ? `查看文档 #${documents.selected.id}` : "新建知识文档"}</h3></div></div>
            <div className="form-grid">
              <label className="field"><span>标题</span><input value={documents.title} onChange={(event) => documents.setTitle(event.target.value)} /></label>
              <label className="field"><span>来源</span><input value={documents.source} onChange={(event) => documents.setSource(event.target.value)} /></label>
              <label className="field span-2"><span>URL</span><input value={documents.url} onChange={(event) => documents.setUrl(event.target.value)} /></label>
              <label className="field span-2"><span>Metadata JSON</span><textarea rows={4} value={documents.meta} onChange={(event) => documents.setMeta(event.target.value)} /></label>
              <label className="field span-2">
                <span>文档内容</span>
                <textarea rows={10} value={documents.content} onChange={(event) => documents.setContent(event.target.value)} placeholder="输入长文本知识内容；选择已有文档后会回填正文，可直接编辑后保存。" />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-primary" onClick={() => void documents.save()}>{documents.selected ? "保存修改" : "新增文档"}</button>
              <button className="button button-secondary" onClick={() => documents.hydrateEditor(null)}>清空编辑器</button>
              <DangerAction
                label="删除文档"
                title="删除知识文档"
                impact={<p>文档 #{documents.selectedId ?? "—"} 及其召回索引将被永久删除，相关问答可能不再命中。</p>}
                onConfirm={documents.remove}
                disabled={!documents.selected}
              />
            </div>
            {documents.selected && <div className="knowledge-preview-card">
              <div className="knowledge-preview-row"><span>当前文档 ID</span><strong className="mono">#{documents.selected.id}</strong></div>
              <div className="knowledge-preview-row"><span>Hash</span><strong className="mono">{documents.selected.content_hash || "-"}</strong></div>
            </div>}
          </div>
        </div>
      </section>
      <DocumentSearchPanel documents={documents} />
    </>
  );
}

function DocumentSearchPanel({ documents }: { documents: DocumentsController }) {
  return (
    <section className="panel">
      <div className="panel-header"><div><p className="section-kicker">召回测试</p><h3>文档召回测试台</h3></div></div>
      <label className="field">
        <span>召回测试问题</span>
        <textarea rows={5} value={documents.searchQuery} onChange={(event) => documents.setSearchQuery(event.target.value)} placeholder="输入一条真实用户问题，检查会召回哪些文档片段" />
      </label>
      <div className="action-row"><button className="button button-primary" onClick={() => void documents.runSearch()} disabled={documents.searchLoading}>执行文档召回</button></div>
      {documents.searchResult ? documents.searchResult.items.length ? (
        <div className="knowledge-draft-list">
          {documents.searchResult.items.map((hit) => (
            <article key={`${hit.doc_id}-${hit.chunk_id}`} className="knowledge-draft-card">
              <div className="knowledge-draft-head"><strong>{hit.title || `文档 #${hit.doc_id}`}</strong><span className="pill pill-feature">{hit.score.toFixed(4)}</span></div>
              <div className="knowledge-meta-row"><span className="mono">doc #{hit.doc_id}</span><span className="mono">chunk #{hit.chunk_id}</span></div>
              <div className="knowledge-source-preview is-compact"><pre>{hit.content}</pre></div>
            </article>
          ))}
        </div>
      ) : <div className="admin-notice">当前问题没有召回任何文档片段。</div>
        : <div className="admin-notice">这里会展示知识文档向量召回结果，包含文档 ID、片段 ID 和相似度。</div>}
    </section>
  );
}

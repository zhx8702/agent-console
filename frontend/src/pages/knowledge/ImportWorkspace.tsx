import { DangerAction } from "../../components/DangerAction";
import type { ImportController } from "./useImportController";

type ImportWorkspaceProps = {
  importer: ImportController;
  currentScopeText: string;
};

export function ImportWorkspace({ importer, currentScopeText }: ImportWorkspaceProps) {
  const validSelectedCount = importer.drafts.filter(
    (item) => item.selected && item.question.trim() && item.answer.trim(),
  ).length;
  return (
    <>
      <section className="panel span-2">
        <div className="knowledge-workbench">
          <ImportSource importer={importer} currentScopeText={currentScopeText} />
          <div className="knowledge-editor">
            <div className="panel-header"><div><p className="section-kicker">问答草稿</p><h3>常见问答草稿确认区</h3></div></div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => importer.setAllSelection(true)} disabled={!importer.drafts.length}>全选</button>
              <button className="button button-secondary" onClick={() => importer.setAllSelection(false)} disabled={!importer.drafts.length}>全不选</button>
              <DangerAction
                label="确认导入选中项"
                title="批量导入 FAQ 草稿"
                impact={<p>将创建 {validSelectedCount} 条 FAQ。请先确认问题、答案和标签均已完成复核。</p>}
                onConfirm={importer.importSelected}
                confirmLabel="确认导入"
                pendingLabel="正在导入…"
                disabled={importer.loading || validSelectedCount === 0}
              />
            </div>
            <div className="knowledge-draft-list">
              {importer.drafts.map((draft) => (
                <article key={draft.draftId} className="knowledge-draft-card">
                  <div className="knowledge-draft-head">
                    <label className="knowledge-draft-toggle">
                      <input type="checkbox" checked={draft.selected} onChange={(event) => importer.updateDraft(draft.draftId, { selected: event.target.checked })} />
                      <span>导入</span>
                    </label>
                    <span className="pill pill-feature">{draft.draftId}</span>
                  </div>
                  <div className="form-grid">
                    <label className="field span-2"><span>问题</span><input value={draft.question} onChange={(event) => importer.updateDraft(draft.draftId, { question: event.target.value })} /></label>
                    <label className="field span-2"><span>答案</span><textarea rows={5} value={draft.answer} onChange={(event) => importer.updateDraft(draft.draftId, { answer: event.target.value })} /></label>
                    <label className="field"><span>变体句式</span><textarea rows={4} value={draft.variantsText} onChange={(event) => importer.updateDraft(draft.draftId, { variantsText: event.target.value })} /></label>
                    <label className="field"><span>标签</span><textarea rows={4} value={draft.tagsText} onChange={(event) => importer.updateDraft(draft.draftId, { tagsText: event.target.value })} /></label>
                  </div>
                  <div className="knowledge-source-preview is-compact"><strong>抽取依据</strong><pre>{draft.sourceExcerpt}</pre></div>
                </article>
              ))}
              {!importer.drafts.length && <div className="admin-notice">先读取聊天记录，再确认草稿。当前只会生成草稿，不会自动导入 FAQ。</div>}
            </div>
          </div>
        </div>
      </section>
      <section className="panel">
        <div className="panel-header"><div><p className="section-kicker">导入说明</p><h3>导入规则说明</h3></div></div>
        <ul className="route-list">
          <li>当前版本只做轻量抽取：优先识别问句，再把后面连续 1 到 3 条回复合成候选答案。</li>
          <li>抽取结果一定先进草稿区，你可以手动改问题、答案、标签和变体，再决定是否导入。</li>
          <li>如果聊天很杂，建议先用“粘贴聊天记录”手动整理一小段，再导入，命中质量会更稳定。</li>
        </ul>
      </section>
    </>
  );
}

function ImportSource({ importer, currentScopeText }: ImportWorkspaceProps) {
  return (
    <aside className="knowledge-sidebar">
      <div className="panel-header"><div><p className="section-kicker">内容来源</p><h3>聊天记录来源</h3></div></div>
      <div className="form-grid">
        <label className="field">
          <span>导入方式</span>
          <select value={importer.mode} onChange={(event) => importer.setMode(event.target.value as typeof importer.mode)}>
            <option value="paste">粘贴聊天记录</option><option value="session">读取当前群 / 会话</option>
          </select>
        </label>
        {importer.mode === "session" && <>
          <label className="field"><span>来源会话</span><input value={importer.sourceSession?.session_name || importer.sourceSessionId || ""} readOnly placeholder="请先在顶部或当前页选择目标群 / 会话" /></label>
          <label className="field">
            <span>周期类型</span>
            <select value={importer.reportType} onChange={(event) => importer.setReportType(event.target.value as typeof importer.reportType)}>
              <option value="daily">按日读取</option><option value="monthly">按月读取</option>
            </select>
          </label>
          {importer.reportType === "daily" ? (
            <label className="field"><span>日期</span><input value={importer.date} onChange={(event) => importer.setDate(event.target.value)} placeholder="YYYY-MM-DD" /></label>
          ) : (
            <label className="field"><span>月份</span><input value={importer.yearMonth} onChange={(event) => importer.setYearMonth(event.target.value)} placeholder="YYYY-MM" /></label>
          )}
        </>}
        {importer.mode === "paste" && <label className="field span-2">
          <span>原始聊天记录</span>
          <textarea rows={14} value={importer.text} onChange={(event) => importer.setText(event.target.value)} placeholder={"支持直接粘贴聊天内容，按行解析。\n例如：\n张三：sub2api 本地怎么跑？\n李四：先配 .env，再执行 docker compose up -d"} />
        </label>}
      </div>
      <div className="action-row">
        {importer.mode === "paste" ? (
          <button className="button button-primary" onClick={() => void importer.generateFromPaste()}>生成 FAQ 草稿</button>
        ) : (
          <button className="button button-primary" onClick={() => void importer.generateFromSession()} disabled={importer.loading}>读取并生成草稿</button>
        )}
        <button className="button button-secondary" onClick={importer.clear}>清空</button>
      </div>
      <div className="knowledge-preview-card">
        <div className="knowledge-preview-row"><span>当前导入目标</span><strong>{currentScopeText}</strong></div>
        <div className="knowledge-preview-row"><span>草稿数量</span><strong>{importer.drafts.length}</strong></div>
        <div className="knowledge-preview-row"><span>已勾选</span><strong>{importer.drafts.filter((item) => item.selected).length}</strong></div>
      </div>
      <div className="knowledge-source-preview">
        <strong>来源预览</strong>
        {importer.mode === "session" ? importer.messages?.messages?.length ? (
          <pre>{importer.messages.messages.slice(0, 20).map((item) => `${item.timestamp ? `[${item.timestamp}] ` : ""}${item.sender_name || "用户"}：${item.text || ""}`).join("\n")}</pre>
        ) : <div className="admin-notice">读取当前群/会话后，这里会展示原始消息片段。</div>
          : importer.text.trim() ? <pre>{importer.text.trim()}</pre>
            : <div className="admin-notice">粘贴聊天记录后，这里会展示导入前的原始文本。</div>}
      </div>
    </aside>
  );
}

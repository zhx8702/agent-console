import { useEffect, useRef, useState } from "react";
import { apiRequest } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";

export type Revision = { id: string; target_doc_id: number; base_hash: string; title: string; content: string; status: string; before: { title: string; content: string }; evaluation?: { answers?: { decision?: { confidence?: number } } }; result_hash?: string };
export function JevRevisionPanel({ candidateId, version, sessionId, revision, disabled, onSaved }: { candidateId: string; version: number; sessionId: string; revision: Revision; disabled: boolean; onSaved: () => Promise<void> }) {
  const { config } = useConsoleConfig();
  const [title, setTitle] = useState(revision.title);
  const [content, setContent] = useState(revision.content);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState<{ id: string; revision: Revision }[] | null>(null);
  const generation = useRef(0);
  const savedKey = useRef<{ body: string; key: string } | null>(null);
  useEffect(() => {
    generation.current += 1; setTitle(revision.title); setContent(revision.content); setReason(""); setError(""); setHistory(null); setBusy(false); savedKey.current = null;
    return () => { generation.current += 1; };
  }, [config, candidateId, version, revision.id]);
  async function save() {
    const current = generation.current;
    const body = JSON.stringify({ version, target_doc_id: revision.target_doc_id, base_hash: revision.base_hash, title, content, reason });
    if (savedKey.current?.body !== body) savedKey.current = { body, key: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      await apiRequest(config, `/v1/admin/jev/knowledge/candidates/${candidateId}/revision`, { auth: true, query: { tenant_id: config.tenantId }, init: { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": savedKey.current.key }, body } });
      if (current === generation.current) await onSaved();
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function loadHistory() {
    const current = generation.current;
    setBusy(true); setError("");
    try {
      const result = await apiRequest<{ items: { id: string; revision: Revision }[] }>(config, `/v1/admin/jev/knowledge/documents/${revision.target_doc_id}/history`, { auth: true, query: { tenant_id: config.tenantId, session_id: sessionId } });
      if (current === generation.current) setHistory(result.items);
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  return <section aria-label="知识修订草案">
    <h5>知识 #{revision.target_doc_id} 的修订草案</h5>
    <p>基于版本 <code>{revision.base_hash}</code> · Jev 置信度 {revision.evaluation?.answers?.decision?.confidence?.toFixed(2) ?? "待审核"}</p>
    <details><summary>查看修订前正文</summary><h6>{revision.before.title}</h6><pre style={{ whiteSpace: "pre-wrap" }}>{revision.before.content}</pre></details>
    <label>修订标题<input aria-label={`修订标题 ${candidateId}`} value={title} disabled={disabled || busy || revision.status === "published"} onChange={e => setTitle(e.target.value)} /></label>
    <label>修订正文<textarea aria-label={`修订正文 ${candidateId}`} rows={12} value={content} disabled={disabled || busy || revision.status === "published"} onChange={e => setContent(e.target.value)} /></label>
    {revision.status !== "published" && <>
      <label>修改说明<input aria-label={`修改说明 ${candidateId}`} value={reason} disabled={disabled || busy} onChange={e => setReason(e.target.value)} /></label>
      <button disabled={disabled || busy || !reason.trim() || !title.trim() || !content.trim()} onClick={() => void save()}>保存草案并重新审核</button>
      <p>修改后需要重新通过 Jev 审核，再由管理员批准更新。旧正文会保留在修订记录中。</p>
    </>}
    <button disabled={busy || disabled} onClick={() => void loadHistory()}>查看该知识的修订记录</button>
    {history?.map(entry => <details key={entry.id}><summary>{entry.revision.before.title} · {entry.revision.base_hash} → {entry.revision.result_hash}</summary><pre style={{ whiteSpace: "pre-wrap" }}>{entry.revision.before.content}</pre></details>)}
    {history?.length === 0 && <p>尚无已批准的 Jev 修订记录。</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

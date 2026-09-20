import { useEffect, useRef, useState } from "react";
import { apiRequest } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";
import { DangerAction } from "../../components/DangerAction";

type Job = { id: string; session_id: string; period: string; status: string; scanned: number; candidate_count: number; error_type: string };
type Candidate = { id: string; session_id: string; status: string; version: number; reason: string; error_type: string; kb_doc_id: number | null; draft: { title: string; question: string; environment: string; solution: string; outcome: string; evidence_ids: number[]; resolution_ids: number[] }; review: { answers?: { decision?: { confidence?: number } } }; comparisons: { doc_id: number; relation: string; confidence: number }[] };
type Evidence = { id: number; speaker: string; text: string; occurred_ts: number };
type Data = { jobs: Job[]; candidates: Candidate[] };
const labels: Record<string, string> = { pending: "等待处理", running: "处理中", completed: "已完成", failed: "失败", skipped: "已跳过", ready: "可发布", needs_review: "待核验", unresolved: "尚未解决", duplicate: "已有相同知识", rejected: "已排除", published: "已发布", resolved: "已由后续反馈解决" };
const relations: Record<string, string> = { duplicate: "重复", conflict: "冲突", supplement: "补充", unrelated: "不同主题", review: "待核验" };
const reasons: Record<string, string> = { low_confidence: "置信度不足", sensitive_evidence: "可能包含敏感信息", awaiting_resolution: "等待解决结果", missing_resolution_evidence: "缺少成员确认结果", insufficient_support: "原文支持不足", resolution_not_confirmed: "不能确认问题已解决", supported_resolved_experience: "原文支持且有成员确认", existing_knowledge_requires_review: "与旧知识的关系需要核验", existing_knowledge_duplicate: "已有相同知识", not_reusable_or_unsupported: "无复用价值或缺乏依据" };

export function JevKnowledgePanel({ sessionId, disabled }: { sessionId: string; disabled: boolean }) {
  const { config } = useConsoleConfig();
  const [data, setData] = useState<Data | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [evidence, setEvidence] = useState<Record<string, Evidence[]>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const generation = useRef(0);
  const keys = useRef<Record<string, string>>({});
  useEffect(() => { generation.current += 1; setData(null); setEvidence({}); setNotes({}); setError(""); setBusy(false); keys.current = {}; }, [config, sessionId]);
  async function load() {
    const current = generation.current;
    setBusy(true); setError("");
    try {
      const result = await apiRequest<Data>(config, "/v1/admin/jev/knowledge", { auth: true, query: { tenant_id: config.tenantId, session_id: sessionId } });
      if (current === generation.current) setData(result);
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function retryJob(job: Job) {
    const current = generation.current;
    const key = `job:${job.id}`;
    keys.current[key] ||= crypto.randomUUID();
    setBusy(true); setError("");
    try {
      await apiRequest(config, `/v1/admin/jev/knowledge/jobs/${job.id}/retry`, { auth: true, query: { tenant_id: config.tenantId }, init: { method: "POST", headers: { "Idempotency-Key": keys.current[key] } } });
      if (current === generation.current) { delete keys.current[key]; await load(); }
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function showEvidence(item: Candidate) {
    const current = generation.current;
    setBusy(true); setError("");
    try {
      const result = await apiRequest<{ messages: Evidence[] }>(config, `/v1/admin/jev/knowledge/candidates/${item.id}/evidence`, { auth: true, query: { tenant_id: config.tenantId } });
      if (current === generation.current) setEvidence(previous => ({ ...previous, [item.id]: result.messages }));
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function act(item: Candidate, action: string) {
    const current = generation.current;
    const reason = (notes[item.id] || "").trim();
    if (!reason) throw new Error("请填写审核说明");
    const body = JSON.stringify({ version: item.version, action, reason });
    const key = `${item.id}:${body}`;
    keys.current[key] ||= crypto.randomUUID();
    setBusy(true);
    try {
      await apiRequest(config, `/v1/admin/jev/knowledge/candidates/${item.id}`, { auth: true, query: { tenant_id: config.tenantId }, init: { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": keys.current[key] }, body } });
      if (current === generation.current) await load();
    } finally { if (current === generation.current) setBusy(false); }
  }
  return <section aria-label="每日知识候选">
    <h3>每日知识候选</h3>
    <p>查看当前会话筛选范围内最近 50 个任务、100 条候选。未发布的候选不会用于回答。置信度表示模型判断，不代表独立事实核验。</p>
    <button disabled={disabled || busy} onClick={() => void load()}>加载知识任务与候选</button>
    {error && <p role="alert">{error}</p>}
    {data && <>
      <div className="table-wrap"><table><thead><tr><th>日期 / 群</th><th>状态</th><th>扫描消息 / 候选</th></tr></thead><tbody>{data.jobs.map(job => <tr key={job.id}><td>{job.period}<div className="mono">{job.session_id}</div></td><td>{labels[job.status] || job.status} {job.error_type}{["failed", "skipped"].includes(job.status) && <button disabled={busy || disabled} onClick={() => void retryJob(job)}>继续整理</button>}</td><td>{job.scanned} / {job.candidate_count}</td></tr>)}</tbody></table></div>
      {data.jobs.length === 0 && <p>当前范围尚无知识整理任务。</p>}
      {data.candidates.map(item => <article key={item.id} className="panel">
        <h4>{item.draft.title} · {labels[item.status] || item.status}</h4>
        <p className="mono">{item.session_id}</p>
        <p>{reasons[item.reason] || item.reason} {item.error_type} · Jev 置信度 {item.review?.answers?.decision?.confidence?.toFixed(2) ?? "—"}</p>
        <dl><dt>问题</dt><dd>{item.draft.question}</dd><dt>环境</dt><dd>{item.draft.environment || "未注明"}</dd><dt>处理方法</dt><dd>{item.draft.solution || "暂无"}</dd><dt>结果与限制</dt><dd>{item.draft.outcome || "暂无确认结果"}</dd></dl>
        <p>证据消息：{item.draft.evidence_ids.join("、")}；确认结果：{item.draft.resolution_ids.join("、") || "无"}</p>
        {item.comparisons.map(c => <p key={c.doc_id}>知识 #{c.doc_id}：{relations[c.relation] || c.relation}（{c.confidence.toFixed(2)}）</p>)}
        {item.kb_doc_id && <p>已发布为知识文档 #{item.kb_doc_id}</p>}
        <button disabled={busy || disabled} onClick={() => void showEvidence(item)}>查看原始证据（脱敏）</button>
        {evidence[item.id]?.map(message => <blockquote key={message.id}>#{message.id} · {message.speaker} · {new Date(message.occurred_ts * 1000).toLocaleString()}<p>{message.text}</p></blockquote>)}
        {!["running", "published", "pending", "resolved"].includes(item.status) && <>
          <label>审核说明<input aria-label={`审核说明 ${item.id}`} value={notes[item.id] || ""} disabled={busy || disabled} onChange={e => setNotes({ ...notes, [item.id]: e.target.value })} /></label>
          {item.status === "ready" && <DangerAction label="发布到本群知识库" title="发布知识候选" impact={`将“${item.draft.title}”加入本群知识库，后续答疑可以引用。`} disabled={busy || disabled || !notes[item.id]?.trim()} onConfirm={() => act(item, "publish")} />}
          <DangerAction label="排除候选" title="排除知识候选" impact="该候选不会进入知识库。" disabled={busy || disabled || !notes[item.id]?.trim()} onConfirm={() => act(item, "reject")} />
          {item.status !== "rejected" && <button disabled={busy || disabled || !notes[item.id]?.trim()} onClick={() => void act(item, "retry").catch(e => setError(String(e)))}>重新审核候选</button>}
        </>}
      </article>)}
    </>}
  </section>;
}

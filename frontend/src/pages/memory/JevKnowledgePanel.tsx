import { useEffect, useRef, useState } from "react";
import { apiRequest } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";
import { JevRevisionPanel, type Revision } from "./JevRevisionPanel";
import { DangerAction } from "../../components/DangerAction";

type Job = { id: string; session_id: string; period: string; status: string; scanned: number; candidate_count: number; quality_count?: number; error_type: string };
type Candidate = { id: string; session_id: string; status: string; version: number; reason: string; error_type: string; kb_doc_id: number | null; revision?: Revision; draft: { title: string; question: string; environment: string; solution: string; outcome: string; evidence_ids: number[]; resolution_ids: number[] }; review: { answers?: { decision?: { confidence?: number } } }; comparisons: { doc_id: number; relation: string; confidence: number }[] };
type Evidence = { truncated?: boolean; id: number; speaker: string; text: string; occurred_ts: number };
type RuntimeEvidence = { message_id: number; processing_status: string; processing_reason: string; decisions: { status: string; reasons: string[] }[]; deliveries: { status: string; reply_text: string; error: string }[] };
type Finding = { review?: { _operator?: { actor: string; reason: string; at: string } }; id: string; status: string; reason: string; finding: { kind: string; title: string; explanation: string; evidence_ids: number[]; trace_ids: string[] } };
type Data = { pagination?: Record<"jobs" | "candidates" | "findings", string>; jobs: Job[]; candidates: Candidate[]; findings?: Finding[]; quality_summary?: { kind: string; status: string; count: number }[] };
const labels: Record<string, string> = { confirmed: "人工确认", dismissed: "人工排除", pending: "等待处理", running: "处理中", completed: "已完成", failed: "失败", skipped: "已跳过", ready: "可发布", revision_ready: "修订可批准", supported: "证据支持", needs_review: "待核验", unresolved: "尚未解决", duplicate: "已有相同知识", rejected: "已排除", published: "已发布", resolved: "已由后续反馈解决" };
const relations: Record<string, string> = { duplicate: "重复", conflict: "冲突", supplement: "补充", unrelated: "不同主题", review: "待核验" };
const reasons: Record<string, string> = { other_knowledge_requires_review: "与其他知识的冲突尚未解决", expected_policy_behavior: "符合已配置的参与策略", delivered_answer_missing: "缺少实际发送的回复证据", low_confidence: "置信度不足", sensitive_evidence: "可能包含敏感信息", awaiting_resolution: "等待解决结果", missing_resolution_evidence: "缺少成员确认结果", insufficient_support: "原文支持不足", resolution_not_confirmed: "不能确认问题已解决", supported_resolved_experience: "原文支持且有成员确认", existing_knowledge_requires_review: "与旧知识的关系需要核验", existing_knowledge_duplicate: "已有相同知识", not_reusable_or_unsupported: "无复用价值或缺乏依据", revision_supported: "修订内容有证据支持", revision_requires_review: "修订仍需核验", runtime_evidence_missing: "缺少处理或发送证据", dialogue_and_runtime_supported: "对话及运行记录支持此判断", unsupported_finding: "原文不支持此问题", insufficient_evidence: "证据不足" };

export function JevKnowledgePanel({ sessionId, disabled }: { sessionId: string; disabled: boolean }) {
  const { config } = useConsoleConfig();
  const [candidateStatus, setCandidateStatus] = useState("");
  const [findingStatus, setFindingStatus] = useState("");
  const [data, setData] = useState<Data | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [evidence, setEvidence] = useState<Record<string, Evidence[]>>({});
  const [findingEvidence, setFindingEvidence] = useState<Record<string, { hash: string; runtime: RuntimeEvidence[] }>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const generation = useRef(0);
  const keys = useRef<Record<string, string>>({});
  useEffect(() => { generation.current += 1; setData(null); setEvidence({}); setFindingEvidence({}); setNotes({}); setError(""); setBusy(false); keys.current = {}; }, [config, sessionId, candidateStatus, findingStatus]);
  function query() {
    return { tenant_id: config.tenantId, session_id: sessionId,
      ...(candidateStatus ? { candidate_status: candidateStatus } : {}),
      ...(findingStatus ? { finding_status: findingStatus } : {}) };
  }
  async function loadMore(section: "jobs" | "candidates" | "findings") {
    const cursor = data?.pagination?.[section];
    if (!cursor) return;
    const current = generation.current;
    setBusy(true); setError("");
    try {
      const cursorName = { jobs: "job_cursor", candidates: "candidate_cursor", findings: "finding_cursor" }[section];
      const result = await apiRequest<Data>(config, "/v1/admin/jev/knowledge", { auth: true, query: { ...query(), [cursorName]: cursor } });
      if (current === generation.current) setData(previous => {
        if (!previous) return previous;
        const seen = new Set(previous[section]?.map(item => item.id));
        return { ...previous, [section]: [...(previous[section] || []), ...(result[section] || []).filter(item => !seen.has(item.id))],
          pagination: { jobs: "", candidates: "", findings: "", ...previous.pagination, [section]: result.pagination?.[section] || "" } };
      });
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function load() {
    const current = generation.current;
    setBusy(true); setError("");
    try {
      const result = await apiRequest<Data>(config, "/v1/admin/jev/knowledge", { auth: true, query: query() });
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
  async function showEvidence(item: { id: string }, kind: "candidates" | "findings" = "candidates") {
    const current = generation.current;
    setBusy(true); setError("");
    try {
      const result = await apiRequest<{ messages: Evidence[]; evidence_hash?: string; runtime?: RuntimeEvidence[] }>(config, `/v1/admin/jev/knowledge/${kind}/${item.id}/evidence`, { auth: true, query: { tenant_id: config.tenantId } });
      if (current === generation.current) {
        setEvidence(previous => ({ ...previous, [`${kind}:${item.id}`]: result.messages }));
        if (kind === "findings") setFindingEvidence(previous => ({ ...previous, [item.id]: { hash: result.evidence_hash || "", runtime: result.runtime || [] } }));
      }
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function reviewFinding(item: Finding, action: "confirm" | "dismiss") {
    const current = generation.current;
    const reason = (notes[`finding:${item.id}`] || "").trim();
    if (!reason) throw new Error("请填写复盘处理说明");
    const body = JSON.stringify({ expected_status: item.status, action, reason, evidence_hash: findingEvidence[item.id]?.hash || "" });
    const key = `finding:${item.id}:${body}`;
    keys.current[key] ||= crypto.randomUUID();
    setBusy(true);
    try {
      await apiRequest(config, `/v1/admin/jev/knowledge/findings/${item.id}`, { auth: true, query: { tenant_id: config.tenantId }, init: { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": keys.current[key] }, body } });
      if (current === generation.current) await load();
    } finally { if (current === generation.current) setBusy(false); }
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
    <p>按群和状态查看知识任务与候选，可继续加载历史记录。未发布的候选不会用于回答。置信度表示模型判断，不代表独立事实核验。</p>
    <label>知识候选状态<select aria-label="知识候选状态" value={candidateStatus} disabled={disabled || busy} onChange={e => setCandidateStatus(e.target.value)}>
      <option value="">全部状态</option>{["pending", "running", "needs_review", "ready", "revision_ready", "unresolved", "resolved", "duplicate", "published", "rejected", "failed", "skipped"].map(status => <option key={status} value={status}>{labels[status]}</option>)}
    </select></label>
    <label>复盘状态<select aria-label="复盘状态" value={findingStatus} disabled={disabled || busy} onChange={e => setFindingStatus(e.target.value)}>
      <option value="">全部状态</option>{["needs_review", "supported", "rejected", "confirmed", "dismissed"].map(status => <option key={status} value={status}>{labels[status] || status}</option>)}
    </select></label>
    <button disabled={disabled || busy} onClick={() => void load()}>加载知识任务与候选</button>
    {error && <p role="alert">{error}</p>}
    {data && <>
      <div className="table-wrap"><table><thead><tr><th>日期 / 群</th><th>状态</th><th>扫描消息 / 知识候选 / 质量发现</th></tr></thead><tbody>{data.jobs.map(job => <tr key={job.id}><td>{job.period}<div className="mono">{job.session_id}</div></td><td>{labels[job.status] || job.status} {job.error_type}{["failed", "skipped"].includes(job.status) && <button disabled={busy || disabled} onClick={() => void retryJob(job)}>继续整理</button>}</td><td>{job.scanned} / {job.candidate_count} / {job.quality_count ?? 0}</td></tr>)}</tbody></table></div>
      {data.pagination?.jobs && <button disabled={busy || disabled} onClick={() => void loadMore("jobs")}>加载更早的整理任务</button>}
      {data.jobs.length === 0 && <p>当前范围尚无知识整理任务。</p>}
      <section aria-label="每日回答质量复盘">
        <h3>每日回答质量复盘</h3>
        <p>与知识候选独立展示，不进入答疑知识库。“证据支持”是 Jev 的复核结果；待核验项不作为已确认故障统计。</p>
        {(data.quality_summary || []).map(row => <p key={`${row.kind}:${row.status}`}>{({ missed_help: "疑似漏答", unhelpful_answer: "回答无帮助", unnecessary_reply: "多余回复", good_resolution: "有效解决" } as Record<string,string>)[row.kind] || row.kind} · {labels[row.status] || row.status}：{row.count}</p>)}
        {(data.findings || []).map(item => <article key={item.id} className="panel">
          <h4>{item.finding.title} · {labels[item.status] || item.status}</h4><p>{item.finding.explanation}</p><p>{reasons[item.reason] || item.reason}</p>
          <p>消息：{item.finding.evidence_ids.join("、")}</p><p className="mono">追踪：{item.finding.trace_ids.join("、") || "无处理记录"}</p>
          <button disabled={busy || disabled} onClick={() => void showEvidence(item, "findings")}>查看复盘证据</button>
          {evidence[`findings:${item.id}`]?.map(message => <blockquote key={message.id}>#{message.id} · {message.speaker}<p>{message.text}</p>{message.truncated && <p>此处只展示长消息的部分内容，请结合原始消息核对完整上下文。</p>}</blockquote>)}
          {findingEvidence[item.id]?.runtime.map(record => <div key={record.message_id}>
            <p>消息 #{record.message_id} · 处理：{record.processing_status || "无记录"} · {record.processing_reason}</p>
            {record.decisions.map((decision, index) => <p key={index}>参与判断：{decision.status} · {(decision.reasons || []).join("、")}</p>)}
            {record.deliveries.map((delivery, index) => <blockquote key={index}>发送：{delivery.status}<p>{delivery.reply_text}</p>{delivery.error && <p>{delivery.error}</p>}</blockquote>)}
          </div>)}
          {item.review?._operator && <p>人工处理：{item.review._operator.actor} · {item.review._operator.reason}</p>}
          {!["confirmed", "dismissed"].includes(item.status) && <>
            <label>复盘处理说明<input aria-label={`复盘处理说明 ${item.id}`} value={notes[`finding:${item.id}`] || ""} disabled={busy || disabled} onChange={e => setNotes({ ...notes, [`finding:${item.id}`]: e.target.value })} /></label>
            <DangerAction label="确认复盘结论" title="确认复盘结论" impact="记录人工核验结果；不会自动修改群参与策略或发布知识。" disabled={busy || disabled || !notes[`finding:${item.id}`]?.trim() || !findingEvidence[item.id]?.hash} onConfirm={() => reviewFinding(item, "confirm")} />
            <DangerAction label="排除复盘误报" title="排除复盘误报" impact="保留原始模型判断和排除原因，标记为人工排除。" disabled={busy || disabled || !notes[`finding:${item.id}`]?.trim()} onConfirm={() => reviewFinding(item, "dismiss")} />
            {!findingEvidence[item.id]?.hash && <p>确认前请先查看复盘证据和实际发送记录。</p>}
          </>}
        </article>)}
        {data.pagination?.findings && <button disabled={busy || disabled} onClick={() => void loadMore("findings")}>加载更多复盘记录</button>}
        {data.findings?.length === 0 && <p>当前范围没有质量复盘发现。</p>}
      </section>
      {data.candidates.map(item => <article key={item.id} className="panel">
        <h4>{item.draft.title} · {labels[item.status] || item.status}</h4>
        <p className="mono">{item.session_id}</p>
        <p>{reasons[item.reason] || item.reason} {item.error_type} · Jev 置信度 {item.review?.answers?.decision?.confidence?.toFixed(2) ?? "—"}</p>
        <dl><dt>问题</dt><dd>{item.draft.question}</dd><dt>环境</dt><dd>{item.draft.environment || "未注明"}</dd><dt>处理方法</dt><dd>{item.draft.solution || "暂无"}</dd><dt>结果与限制</dt><dd>{item.draft.outcome || "暂无确认结果"}</dd></dl>
        <p>证据消息：{item.draft.evidence_ids.join("、")}；确认结果：{item.draft.resolution_ids.join("、") || "无"}</p>
        {item.comparisons.map(c => <p key={c.doc_id}>知识 #{c.doc_id}：{relations[c.relation] || c.relation}（{c.confidence.toFixed(2)}）</p>)}
        {item.kb_doc_id && <p>已发布为知识文档 #{item.kb_doc_id}</p>}
        {item.revision?.id && <JevRevisionPanel candidateId={item.id} version={item.version} sessionId={item.session_id} revision={item.revision} disabled={busy || disabled || ["running", "pending"].includes(item.status)} onSaved={load} />}
        <button disabled={busy || disabled} onClick={() => void showEvidence(item)}>查看原始证据（脱敏）</button>
        {evidence[`candidates:${item.id}`]?.map(message => <blockquote key={message.id}>#{message.id} · {message.speaker} · {new Date(message.occurred_ts * 1000).toLocaleString()}<p>{message.text}</p>{message.truncated && <p>此处只展示长消息的部分内容，请结合原始消息核对完整上下文。</p>}</blockquote>)}
        {!["running", "published", "pending", "resolved"].includes(item.status) && <>
          <label>审核说明<input aria-label={`审核说明 ${item.id}`} value={notes[item.id] || ""} disabled={busy || disabled} onChange={e => setNotes({ ...notes, [item.id]: e.target.value })} /></label>
          {item.status === "ready" && <DangerAction label="发布到本群知识库" title="发布知识候选" impact={`将“${item.draft.title}”加入本群知识库，后续答疑可以引用。`} disabled={busy || disabled || !notes[item.id]?.trim()} onConfirm={() => act(item, "publish")} />}
          {item.status === "revision_ready" && <DangerAction label="批准修订现有知识" title="批准知识修订" impact={`将修订内容写入知识 #${item.revision?.target_doc_id}，保留旧版本记录。`} disabled={busy || disabled || !notes[item.id]?.trim()} onConfirm={() => act(item, "apply_revision")} />}
          <DangerAction label="排除候选" title="排除知识候选" impact="该候选不会进入知识库。" disabled={busy || disabled || !notes[item.id]?.trim()} onConfirm={() => act(item, "reject")} />
          {item.status !== "rejected" && <button disabled={busy || disabled || !notes[item.id]?.trim()} onClick={() => void act(item, "retry").catch(e => setError(String(e)))}>重新审核候选</button>}
        </>}
      </article>)}
      {data.pagination?.candidates && <button disabled={busy || disabled} onClick={() => void loadMore("candidates")}>加载更多知识候选</button>}
    </>}
  </section>;
}

import { useCallback, useEffect, useRef, useState } from "react";
import { apiRequest } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";
import { UnsavedChangesGuard } from "../../components/UnsavedChangesGuard";

const domains = { relationship: "群关系", memory: "长期记忆", intent: "意图判断", moderation: "内容审核", participation: "群内求助" };
type Domain = keyof typeof domains;
type Policy = Record<Domain, boolean> & { enabled: boolean; shadow_only: boolean; min_confidence: number; sample_rate: number; participation_shadow_only: boolean; help_sessions: string[] };
type Evaluation = { id: string; session_id?: string; domain: Domain; status: string; attempts: number; applied: boolean; error_type: string; duration_ms: number; target_id: number | null; retryable: boolean; result: { model?: string; _audit?: { reason?: string; trace_id?: string; min_confidence?: number; effective_decision?: string }; answers?: Record<string, { choice?: string; confidence?: number; score?: number }> } | null };
type Dashboard = { policy: Policy; version: number; runtime: { enabled: boolean; key_configured: boolean; model: string }; summary: { domain: Domain; status: string; count: number; input_tokens: number; output_tokens: number; applied: number }[]; items: Evaluation[] };
const statuses: Record<string, string> = { pending: "等待评估", running: "评估中", completed: "已完成", failed: "失败", skipped: "已跳过" };
const choices: Record<string, string> = { accepted: "接受", needs_review: "待人工审核", rejected: "拒绝", allow: "正常", review: "需复核", flag: "标记风险", keep: "保留原判断", abstain: "撤回原判断", reply: "建议回复", observe: "继续旁观" };
const reasons: Record<string, string> = { applied: "达到阈值，已交给业务策略", no_change: "保留原流程，无需介入", low_confidence: "置信度未达到阈值", shadow_only: "观察模式，只记录建议", group_removed: "该群已移出答疑白名单", owner_disabled: "所属插件已停用", policy_disabled: "评估策略已停用", evaluation_failed: "评估失败，沿用原流程", online_timeout: "超过在线时限，沿用原流程", human_protected: "保留人工审核或置顶状态", invalid_decision: "判断无效，未应用", evidence_review_required: "证据或敏感性检查要求人工审核" };

export function JevPanel() {
  const { config } = useConsoleConfig();
  const [data, setData] = useState<Dashboard | null>(null);
  const [draft, setDraft] = useState<Policy | null>(null);
  const [domain, setDomain] = useState("");
  const [status, setStatus] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const savedKey = useRef<{ payload: string; key: string } | null>(null);
  const dirty = Boolean(data && draft && JSON.stringify(data.policy) !== JSON.stringify(draft));
  const load = useCallback(async () => {
    const current = ++generation.current;
    setBusy(true); setError("");
    try {
      const result = await apiRequest<Dashboard>(config, "/v1/admin/jev", { auth: true, query: { tenant_id: config.tenantId, domain, status, session_id: sessionId.trim() } });
      if (current === generation.current) { setData(result); setDraft(result.policy); }
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }, [config, domain, status, sessionId]);
  useEffect(() => {
    setData(null); setDraft(null); setNotice(""); savedKey.current = null;
    if (config.tenantId) void load();
    return () => { generation.current += 1; };
  }, [load, config.tenantId]);
  async function save() {
    if (!data || !draft) return;
    const current = generation.current;
    const payload = JSON.stringify({ version: data.version, policy: draft });
    if (savedKey.current?.payload !== payload) savedKey.current = { payload, key: crypto.randomUUID() };
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await apiRequest<{ version: number; policy: Policy }>(config, "/v1/admin/jev/policy", { auth: true, query: { tenant_id: config.tenantId }, init: { method: "PUT", headers: { "Content-Type": "application/json", "Idempotency-Key": savedKey.current.key }, body: payload } });
      if (current !== generation.current) return;
      setData({ ...data, ...result }); setDraft(result.policy); savedKey.current = null;
      setNotice("已保存，各处理进程将在 5 秒内读取新策略。");
    } catch (e) { if (current === generation.current) setError(`${String(e)}。如发生版本冲突，请先放弃修改，再重新加载。`); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function retry(id: string) {
    const current = generation.current;
    setBusy(true); setError("");
    try {
      await apiRequest(config, `/v1/admin/jev/jobs/${id}/retry`, { auth: true, query: { tenant_id: config.tenantId }, init: { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() } } });
      if (current === generation.current) await load();
    } catch (e) { if (current === generation.current) setError(String(e)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  if (!config.tenantId) return <p>请先选择租户。</p>;
  const completed = data?.summary.filter(x => x.status === "completed").reduce((n, x) => n + x.count, 0) || 0;
  const pending = data?.summary.filter(x => ["pending", "running"].includes(x.status)).reduce((n, x) => n + x.count, 0) || 0;
  const tokens = data?.summary.reduce((n, x) => n + Number(x.input_tokens) + Number(x.output_tokens), 0) || 0;
  return <section className="panel" aria-label="Jev 评估">
    <UnsavedChangesGuard when={dirty} />
    <h2>Jev 评估</h2>
    <p>群关系和记忆在保存后排队评估。意图与内容审核在观察模式下排队，参与决策模式下限时判断，超时沿用原流程。</p>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    <button onClick={() => void load()} disabled={busy || dirty}>重新加载</button>
    {data && draft && <>
      <p>服务器开关：{data.runtime.enabled ? "已启用" : "未启用"} · 密钥：{data.runtime.key_configured ? "已配置" : "未配置"} · 模型：{data.runtime.model}</p>
      <fieldset disabled={busy}>
        <legend>当前租户策略</legend>
        <label><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />启用评估</label>
        <label>评估模式<select aria-label="评估模式" value={draft.shadow_only ? "shadow" : "active"} onChange={e => setDraft({ ...draft, shadow_only: e.target.value === "shadow" })}><option value="shadow">观察模式：只记录建议</option><option value="active">参与决策：使用达到阈值的判断</option></select></label>
        {!draft.shadow_only && <p>达到阈值的结果可调整自动生成记忆的接受状态、撤回不支持的意图或增加审核标记。人工审核、置顶记忆和已有敏感词拦截会保留。</p>}
        <label>最低置信度<input aria-label="最低置信度" type="number" min="0" max="1" step="0.01" value={draft.min_confidence} onChange={e => setDraft({ ...draft, min_confidence: Number(e.target.value) })} /></label>
        <label>采样比例<input aria-label="采样比例" type="number" min="0" max="1" step="0.05" value={draft.sample_rate} onChange={e => setDraft({ ...draft, sample_rate: Number(e.target.value) })} /></label>
        {Object.entries(domains).map(([key, label]) => <label key={key}><input type="checkbox" checked={draft[key as Domain]} onChange={e => setDraft({ ...draft, [key]: e.target.checked })} />{label}</label>)}
        <label><input type="checkbox" checked={draft.participation_shadow_only} onChange={e => setDraft({ ...draft, participation_shadow_only: e.target.checked })} />群内求助仅记录建议</label>
        <label>主动答疑群（每行一个会话 ID）<textarea aria-label="主动答疑群" value={draft.help_sessions.join("\n")} onChange={e => setDraft({ ...draft, help_sessions: e.target.value.split("\n") })} /></label>
        <p>求助判断单独选择观察或参与决策，仅对以上群生效，还需开启该群主动参与。明确求助时按问题回答，保留安静时段、频率限制和成员退出设置。</p>
        <button disabled={!dirty} onClick={() => void save()}>保存策略</button><button disabled={!dirty} onClick={() => setDraft(data.policy)}>放弃修改</button>
      </fieldset>
      <p>已完成 {completed} · 排队及运行中 {pending} · 已记录 tokens {tokens.toLocaleString()}</p>
    </>}
    <label>场景<select aria-label="评估场景" value={domain} disabled={busy || dirty} onChange={e => setDomain(e.target.value)}><option value="">全部</option>{Object.entries(domains).map(([key,label]) => <option value={key} key={key}>{label}</option>)}</select></label>
    <label>状态<select aria-label="评估状态" value={status} disabled={busy || dirty} onChange={e => setStatus(e.target.value)}><option value="">全部</option>{Object.entries(statuses).map(([key,label]) => <option value={key} key={key}>{label}</option>)}</select></label>
    <label>会话筛选<input aria-label="评估会话" value={sessionId} disabled={dirty} placeholder="会话 ID，留空显示全部" onChange={e => setSessionId(e.target.value)} /></label>
    <p>最近记录按审核优先级排序。用量来自上游成功响应，不代表完整账单。建议回复仍须通过群参与限制，已参与决策不代表微信已送达。</p>
    <div className="table-wrap"><table><thead><tr><th>场景 / 记忆</th><th>状态</th><th>判断</th><th>置信度</th><th>模型 / 质量 / 优先级</th><th>用时 / 尝试</th><th>处理及原因</th><th>会话 / 追踪</th></tr></thead><tbody>{(data?.items || []).map(item => <tr key={item.id}>
      <td>{domains[item.domain]}{item.target_id ? ` #${item.target_id}` : ""}</td><td>{statuses[item.status] || item.status}{item.error_type && ` · ${item.error_type}`}</td>
      <td>{choices[item.result?.answers?.decision?.choice || ""] || item.result?.answers?.decision?.choice || "—"}{item.result?._audit?.effective_decision && <div>实际处理：{choices[item.result._audit.effective_decision] || item.result._audit.effective_decision}</div>}</td><td>{item.result?.answers?.decision?.confidence?.toFixed(2) ?? "—"}{item.result?._audit?.min_confidence != null && <div>阈值 {item.result._audit.min_confidence.toFixed(2)}</div>}</td><td>{item.result?.model || "—"} / {item.result?.answers?.quality?.score ?? "—"} / {item.result?.answers?.priority?.score ?? "—"}</td><td>{item.duration_ms} ms / {item.attempts}</td>
      <td>{item.applied ? "已参与决策" : "仅记录"}{item.result?._audit?.reason && <div>{reasons[item.result._audit.reason] || item.result._audit.reason}</div>}{item.retryable && <button disabled={busy || dirty} onClick={() => void retry(item.id)}>重新评估</button>}</td>
      <td className="mono">{item.session_id || "—"}{item.result?._audit?.trace_id && <div>{item.result._audit.trace_id}</div>}</td>
    </tr>)}</tbody></table></div>
    {data && data.items.length === 0 && <p>当前筛选条件下没有评估记录。</p>}
  </section>;
}

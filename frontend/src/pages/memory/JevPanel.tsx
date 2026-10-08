import { useCallback, useEffect, useRef, useState } from "react";
import { Alert } from "../../components";
import { StatusTile } from "../../components/StatusTile";
import { apiRequest } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";
import { JevKnowledgePanel } from "./JevKnowledgePanel";
import { UnsavedChangesGuard } from "../../components/UnsavedChangesGuard";

const domains = { relationship: "群关系", memory: "长期记忆", intent: "意图判断", moderation: "内容审核", participation: "群内求助", knowledge: "群知识迭代" };
type Domain = keyof typeof domains;
type Policy = Record<Domain, boolean> & { enabled: boolean; shadow_only: boolean; min_confidence: number; sample_rate: number; participation_shadow_only: boolean; help_sessions: string[]; knowledge_sessions?: string[]; knowledge_daily_hour?: number; knowledge_timezone?: string; knowledge_min_confidence?: number };
type Evaluation = { id: string; session_id?: string; domain: Domain; status: string; attempts: number; applied: boolean; error_type: string; duration_ms: number; target_id: number | null; retryable: boolean; result: { model?: string; _audit?: { reason?: string; trace_id?: string; min_confidence?: number; effective_decision?: string }; answers?: Record<string, { choice?: string; confidence?: number; score?: number }> } | null; turn?: Turn };
type Turn = { lane?: string; message?: string | null; keyword_hit?: boolean | null; keyword_reason?: string | null; processing_status?: string | null; processing_reason?: string | null; participation_status?: string | null; participation_reasons?: string[]; delivery_status?: string | null; outcome?: string };
type Funnel = { evaluated: number; lanes: Record<string, number>; blocked_reasons: Record<string, number> };
type Desk = { session_id: string; in_help_sessions: boolean; participation_enabled: boolean; participation_shadow_only: boolean; min_confidence: number; social?: { effective_enabled: boolean; group_enabled: boolean; proactive_enabled: boolean } | null; channel: { reply_mode: string; configured_reply_mode: string; inherits_global_keywords: boolean; keyword_count: number; mention_sender: boolean; has_session_row: boolean } | null; conflicts: string[] };
type Dashboard = { policy: Policy; version: number; runtime: { enabled: boolean; key_configured: boolean; model: string }; summary: { domain: Domain; status: string; count: number; input_tokens: number; output_tokens: number; applied: number }[]; items: Evaluation[]; funnel?: Funnel; desk?: Desk };
const statuses: Record<string, string> = { pending: "等待评估", running: "评估中", completed: "已完成", failed: "失败", skipped: "已跳过" };
const choices: Record<string, string> = { accepted: "接受", needs_review: "待人工审核", rejected: "拒绝", allow: "正常", review: "需复核", flag: "标记风险", keep: "保留原判断", abstain: "撤回原判断", reply: "建议回复", observe: "继续旁观", retain: "保留知识候选", reject: "不建议入库", unresolved: "问题未解决", duplicate: "重复知识", supplement: "补充知识", conflict: "知识冲突", unrelated: "不同主题" };
const reasons: Record<string, string> = { applied: "达到阈值，已交给业务策略", no_change: "保留原流程，无需介入", low_confidence: "置信度未达到阈值", shadow_only: "观察模式，只记录建议", group_removed: "该群已移出答疑白名单", owner_disabled: "所属插件已停用", policy_disabled: "评估策略已停用", evaluation_failed: "评估失败，沿用原流程", online_timeout: "超过在线时限，沿用原流程", human_protected: "保留人工审核或置顶状态", invalid_decision: "判断无效，未应用", evidence_review_required: "证据或敏感性检查要求人工审核" };
const lanes: Record<string, string> = { observe: "继续旁观", reply_blocked: "建议回复未应用", reply_applied: "已参与决策", failed: "评估失败", pending: "排队中", other: "其他" };
const LANE_ORDER = ["observe", "reply_blocked", "reply_applied", "failed", "pending", "other"] as const;
const conflicts: Record<string, string> = { participation_disabled: "群内求助评估已关闭", not_in_help_sessions: "该群不在主动答疑白名单，Jev 不会判断求助", jev_observe_only: "求助判断仍是观察模式，只记录建议，不会主动开口", social_disabled: "群参与总开关未打开，Jev 判断了也不会发出去", social_proactive_disabled: "该群主动参与未打开，Jev 不会判断求助", channel_off: "微信回复策略是关闭，关键词和 Jev 都不会发出去", contains_without_keywords: "回复模式是包含关键词，但当前没有关键词", no_session_policy: "该群没有独立回复策略，继承全局默认" };
const outcomes: Record<string, string> = { sent: "微信已发出", queued: "已进入发送队列", send_failed: "发送失败", suppressed: "参与策略拦截", jev_blocked: "Jev 建议回复但未应用", jev_observe: "Jev 判断继续旁观", applied_not_sent: "Jev 已参与决策，未见发送记录", no_delivery: "有处理记录，未见发送", no_runtime: "没有对应的处理记录" };
const replyModes: Record<string, string> = { off: "关闭", contains: "包含关键词", all: "全部回复", inherit: "继承全局" };

function runtimePill(runtime: Dashboard["runtime"] | undefined, enabled: boolean | undefined) {
  if (!runtime?.enabled || !runtime.key_configured || !enabled) return { className: "pill-warning", label: "未就绪" };
  return { className: "pill-ok", label: "已就绪" };
}

export function JevPanel() {
  const { config } = useConsoleConfig();
  const [data, setData] = useState<Dashboard | null>(null);
  const [draft, setDraft] = useState<Policy | null>(null);
  const [domain, setDomain] = useState("");
  const [status, setStatus] = useState("");
  const [sessionId, setSessionId] = useState(config.sessionId || "");
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
  useEffect(() => { setSessionId(config.sessionId || ""); }, [config.sessionId]);
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
  if (!config.tenantId) return <p className="muted-copy">请先选择租户。</p>;
  const completed = data?.summary.filter(x => x.status === "completed").reduce((n, x) => n + x.count, 0) || 0;
  const pending = data?.summary.filter(x => ["pending", "running"].includes(x.status)).reduce((n, x) => n + x.count, 0) || 0;
  const tokens = data?.summary.reduce((n, x) => n + Number(x.input_tokens) + Number(x.output_tokens), 0) || 0;
  const blockedItems = (data?.items || []).filter(item => item.domain === "participation" && (item.turn?.lane === "reply_blocked" || (!item.turn && item.result?.answers?.decision?.choice === "reply" && !item.applied)));
  const pill = runtimePill(data?.runtime, draft?.enabled);
  return (
    <div className="jev-workspace">
      <UnsavedChangesGuard when={dirty} />
      <section className="panel panel-hero" aria-label="Jev 评估">
        <div className="panel-header">
          <div>
            <p className="section-kicker">结构化评判</p>
            <h2>Jev 评估</h2>
          </div>
          <span className={`pill ${pill.className}`}>{pill.label}</span>
        </div>
        <p className="muted-copy">
          群关系和记忆在保存后排队评估。意图与内容审核在观察模式下排队，参与决策模式下限时判断，超时沿用原流程。
        </p>
        {error ? <Alert variant="danger" title="操作未完成">{error}</Alert> : null}
        {notice ? <Alert variant="success">{notice}</Alert> : null}
        <div className="action-row">
          <button className="button button-secondary" type="button" onClick={() => void load()} disabled={busy || dirty}>重新加载</button>
        </div>
        {data && draft ? (
          <>
            <div className="status-grid">
              <StatusTile label="服务器开关" value={data.runtime.enabled ? "已启用" : "未启用"} />
              <StatusTile label="密钥" value={data.runtime.key_configured ? "已配置" : "未配置"} />
              <StatusTile label="模型" value={data.runtime.model} />
              <StatusTile label="已完成" value={String(completed)} />
              <StatusTile label="排队及运行中" value={String(pending)} />
              <StatusTile label="已记录 tokens" value={tokens.toLocaleString()} />
            </div>
            <fieldset className="jev-policy-fieldset" disabled={busy}>
              <p className="section-kicker">当前租户策略</p>
              <div className="form-grid">
                <label className="toggle-chip">
                  <strong>
                    <input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })} />
                    启用评估
                  </strong>
                  <em>关闭后各场景沿用原流程，不调用 Jev。</em>
                </label>
                <label className="toggle-chip">
                  <strong>
                    <input type="checkbox" checked={draft.participation_shadow_only} onChange={e => setDraft({ ...draft, participation_shadow_only: e.target.checked })} />
                    群内求助仅记录建议
                  </strong>
                  <em>打开后求助判断只写审计，不改变是否开口。</em>
                </label>
                <label className="field">
                  <span>评估模式</span>
                  <select aria-label="评估模式" value={draft.shadow_only ? "shadow" : "active"} onChange={e => setDraft({ ...draft, shadow_only: e.target.value === "shadow" })}>
                    <option value="shadow">观察模式：只记录建议</option>
                    <option value="active">参与决策：使用达到阈值的判断</option>
                  </select>
                </label>
                <label className="field">
                  <span>最低置信度</span>
                  <input aria-label="最低置信度" type="number" min="0" max="1" step="0.01" value={draft.min_confidence} onChange={e => setDraft({ ...draft, min_confidence: Number(e.target.value) })} />
                </label>
                <label className="field">
                  <span>采样比例</span>
                  <input aria-label="采样比例" type="number" min="0" max="1" step="0.05" value={draft.sample_rate} onChange={e => setDraft({ ...draft, sample_rate: Number(e.target.value) })} />
                </label>
              </div>
              {!draft.shadow_only ? (
                <p className="muted-copy">达到阈值的结果可调整自动生成记忆的接受状态、撤回不支持的意图或增加审核标记。人工审核、置顶记忆和已有敏感词拦截会保留。</p>
              ) : null}
              <div className="form-grid">
                {Object.entries(domains).map(([key, label]) => (
                  <label className="toggle-chip" key={key}>
                    <strong>
                      <input type="checkbox" checked={Boolean(draft[key as Domain])} onChange={e => setDraft({ ...draft, [key]: e.target.checked })} />
                      {label}
                    </strong>
                  </label>
                ))}
              </div>
              <div className="form-grid">
                <label className="field span-2">
                  <span>主动答疑群（每行一个会话 ID）</span>
                  <textarea aria-label="主动答疑群" value={draft.help_sessions.join("\n")} onChange={e => setDraft({ ...draft, help_sessions: e.target.value.split("\n") })} />
                </label>
                <label className="field span-2">
                  <span>每日知识整理群（每行一个会话 ID）</span>
                  <textarea aria-label="每日知识整理群" value={(draft.knowledge_sessions || []).join("\n")} onChange={e => setDraft({ ...draft, knowledge_sessions: e.target.value.split("\n") })} />
                </label>
                <label className="field">
                  <span>每日整理时间（小时）</span>
                  <input aria-label="每日知识整理时间" type="number" min="0" max="23" value={draft.knowledge_daily_hour ?? 3} onChange={e => setDraft({ ...draft, knowledge_daily_hour: Number(e.target.value) })} />
                </label>
                <label className="field">
                  <span>整理时区</span>
                  <input aria-label="知识整理时区" value={draft.knowledge_timezone || "Asia/Shanghai"} onChange={e => setDraft({ ...draft, knowledge_timezone: e.target.value })} />
                </label>
                <label className="field">
                  <span>知识审核最低置信度</span>
                  <input aria-label="知识审核最低置信度" type="number" min="0.8" max="1" step="0.01" value={draft.knowledge_min_confidence ?? .9} onChange={e => setDraft({ ...draft, knowledge_min_confidence: Number(e.target.value) })} />
                </label>
              </div>
              <p className="muted-copy">每日整理前一天的群聊，补查最近 7 天。知识候选独立审核，发布后才进入答疑检索；不会自动覆盖已有知识。</p>
              <p className="muted-copy">求助判断单独选择观察或参与决策，仅对以上群生效。群参与页的「允许主动参与」也必须打开，否则 Jev 不会判断求助。明确求助时按问题回答，保留安静时段、频率限制和成员退出设置。</p>
              <div className="action-row">
                <button className="button button-primary" type="button" disabled={!dirty} onClick={() => void save()}>保存策略</button>
                <button className="button button-secondary" type="button" disabled={!dirty} onClick={() => setDraft(data.policy)}>放弃修改</button>
              </div>
            </fieldset>
            <p className="page-meta-line">已完成 {completed} · 排队及运行中 {pending} · 已记录 tokens {tokens.toLocaleString()}</p>
          </>
        ) : null}
      </section>

      {data?.desk ? (
        <section className="panel" aria-label="当前群对照">
          <div className="panel-header">
            <div>
              <p className="section-kicker">当前群</p>
              <h3>当前群对照</h3>
            </div>
          </div>
          {data.desk.session_id ? (
            <>
              <p className="muted-copy">
                {data.desk.in_help_sessions ? "已在主动答疑白名单" : "不在主动答疑白名单"} · 求助评估{data.desk.participation_enabled ? "已开启" : "已关闭"} · {data.desk.participation_shadow_only ? "观察模式" : "参与决策"} · 主动参与{data.desk.social ? (data.desk.social.proactive_enabled ? "已开启" : "未开启") : "未读取"} · 阈值 {data.desk.min_confidence.toFixed(2)}
              </p>
              {data.desk.channel ? (
                <p className="muted-copy">
                  微信策略：{replyModes[data.desk.channel.reply_mode] || data.desk.channel.reply_mode}{data.desk.channel.configured_reply_mode === "inherit" ? "（继承）" : ""} · 关键词 {data.desk.channel.keyword_count} 个{data.desk.channel.inherits_global_keywords ? "（继承全局）" : ""} · @发送者 {data.desk.channel.mention_sender ? "开启" : "关闭"}
                </p>
              ) : null}
            </>
          ) : (
            <p className="muted-copy">填入群会话 ID，或从控制台顶部选中目标群，才能对照微信策略和漏答。</p>
          )}
          {data.desk.conflicts.map(code => (
            <Alert key={code} variant="warning" title="与当前参与策略不一致">{conflicts[code] || code}</Alert>
          ))}
        </section>
      ) : null}

      {data?.funnel ? (
        <section className="panel" aria-label="求助漏斗">
          <div className="panel-header">
            <div>
              <p className="section-kicker">求助判断</p>
              <h3>求助漏斗</h3>
            </div>
          </div>
          <p className="muted-copy">按当前会话筛选统计全部求助评估，不只是下面这一页。</p>
          <div className="status-grid">
            <StatusTile label="评估" value={String(data.funnel.evaluated)} />
            {LANE_ORDER.map(key => (
              <StatusTile key={key} label={lanes[key]} value={String(data.funnel?.lanes[key] || 0)} />
            ))}
          </div>
          <p className="page-meta-line">评估 {data.funnel.evaluated} · {LANE_ORDER.map(key => `${lanes[key]} ${data.funnel?.lanes[key] || 0}`).join(" · ")}</p>
          {Object.keys(data.funnel.blocked_reasons).length > 0 ? (
            <p className="muted-copy">未应用原因：{Object.entries(data.funnel.blocked_reasons).map(([key, count]) => `${reasons[key] || key} ${count}`).join(" · ")}</p>
          ) : null}
        </section>
      ) : null}

      <section className="panel" aria-label="最近评估">
        <div className="panel-header">
          <div>
            <p className="section-kicker">审计记录</p>
            <h3>最近评估</h3>
          </div>
        </div>
        <p className="muted-copy">最近记录按审核优先级排序。用量来自上游成功响应，不代表完整账单。建议回复仍须通过群参与限制，已参与决策不代表微信已送达。</p>
        <div className="page-ops-bar">
          <label className="field">
            <span>场景</span>
            <select aria-label="评估场景" value={domain} disabled={busy || dirty} onChange={e => setDomain(e.target.value)}>
              <option value="">全部</option>
              {Object.entries(domains).map(([key, label]) => <option value={key} key={key}>{label}</option>)}
            </select>
          </label>
          <label className="field">
            <span>状态</span>
            <select aria-label="评估状态" value={status} disabled={busy || dirty} onChange={e => setStatus(e.target.value)}>
              <option value="">全部</option>
              {Object.entries(statuses).map(([key, label]) => <option value={key} key={key}>{label}</option>)}
            </select>
          </label>
          <label className="field">
            <span>会话筛选</span>
            <input aria-label="评估会话" value={sessionId} disabled={dirty} placeholder="会话 ID，留空显示全部" onChange={e => setSessionId(e.target.value)} />
          </label>
        </div>
        {blockedItems.length > 0 ? (
          <section className="jev-blocked-list" aria-label="建议回复但未应用">
            <div className="panel-header">
              <div>
                <p className="section-kicker">漏答对照</p>
                <h3>建议回复但未应用</h3>
              </div>
            </div>
            <p className="muted-copy">Jev 认为该问需要回答，但置信度、观察模式或超时把它挡下了。关键词命中表示按微信策略本来也可以回。</p>
            {blockedItems.map(item => (
              <article key={item.id} className="jev-blocked-card">
                <p>{item.turn?.message || "没有对应原文"}</p>
                <p className="muted-copy">{choices[item.result?.answers?.decision?.choice || ""] || "—"} · 置信度 {item.result?.answers?.decision?.confidence?.toFixed(2) ?? "—"}{item.result?._audit?.min_confidence != null && ` / 阈值 ${item.result._audit.min_confidence.toFixed(2)}`} · {reasons[item.result?._audit?.reason || ""] || item.result?._audit?.reason || "未应用"}</p>
                <p className="muted-copy">{item.turn?.keyword_hit == null ? "无法核对照关键词" : item.turn.keyword_hit ? "关键词会命中" : "关键词未命中"} · {outcomes[item.turn?.outcome || ""] || item.turn?.outcome || "未见发送记录"}</p>
              </article>
            ))}
          </section>
        ) : null}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>场景 / 记忆</th>
                <th>状态</th>
                <th>判断</th>
                <th>置信度</th>
                <th>原文 / 关键词</th>
                <th>发送</th>
                <th>处理及原因</th>
                <th>会话 / 追踪</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items || []).map(item => (
                <tr key={item.id}>
                  <td>{domains[item.domain]}{item.target_id ? ` #${item.target_id}` : ""}</td>
                  <td>{statuses[item.status] || item.status}{item.error_type && ` · ${item.error_type}`}</td>
                  <td>
                    {choices[item.result?.answers?.decision?.choice || ""] || item.result?.answers?.decision?.choice || "—"}
                    {item.result?._audit?.effective_decision ? <div className="jev-table-note">实际处理：{choices[item.result._audit.effective_decision] || item.result._audit.effective_decision}</div> : null}
                  </td>
                  <td>
                    {item.result?.answers?.decision?.confidence?.toFixed(2) ?? "—"}
                    {item.result?._audit?.min_confidence != null ? <div className="jev-table-note">阈值 {item.result._audit.min_confidence.toFixed(2)}</div> : null}
                  </td>
                  <td>
                    {item.turn?.message || "—"}
                    {item.domain === "participation" ? <div className="jev-table-note">{item.turn?.keyword_hit == null ? "关键词未核验" : item.turn.keyword_hit ? "关键词会命中" : "关键词未命中"}</div> : null}
                  </td>
                  <td>
                    {item.turn ? (outcomes[item.turn.outcome || ""] || item.turn.outcome || "—") : "—"}
                    {item.turn?.participation_status ? <div className="jev-table-note">{item.turn.participation_status}</div> : null}
                  </td>
                  <td>
                    {item.applied ? "已参与决策" : "仅记录"}
                    {item.result?._audit?.reason ? <div className="jev-table-note">{reasons[item.result._audit.reason] || item.result._audit.reason}</div> : null}
                    {item.retryable ? <button className="button button-secondary button-compact" type="button" disabled={busy || dirty} onClick={() => void retry(item.id)}>重新评估</button> : null}
                  </td>
                  <td className="mono">
                    {item.session_id || "—"}
                    {item.result?._audit?.trace_id ? <div className="jev-table-note">{item.result._audit.trace_id}</div> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data && data.items.length === 0 ? <p className="muted-copy">当前筛选条件下没有评估记录。</p> : null}
      </section>

      <JevKnowledgePanel sessionId={sessionId.trim()} disabled={dirty || busy} />
    </div>
  );
}

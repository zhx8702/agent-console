import { OutputPanel } from "../../components/OutputPanel";
import { DangerAction } from "../../components/DangerAction";
import { formatJson } from "../../lib/api";
import type { WxbotPageController } from "./useWxbotPageController";
import { AgentAuditResultCell, formatAgentToolScope, formatDateValue, sessionDisplayName, wxbotBooleanLabel } from "./model";

export function WxbotAgentTab({ controller }: { controller: WxbotPageController }) {
  const {
    agentAllowedTools,
    agentAuditLimit,
    agentAuditSessionFilter,
    agentAuditToolName,
    agentAuditTraceId,
    agentEnabled,
    agentOutput,
    agentScope,
    agentScopeLabel,
    agentScopes,
    agentToolAuditItems,
    agentToolCatalog,
    agentToolOwners,
    agentToolPolicyDirty,
    agentToolPolicyEtag,
    agentToolPolicySnapshot,
    agentToolPolicyStatus,
    config,
    effectiveGroupSession,
    effectiveGroupSessionId,
    effectiveGroupSessionName,
    groupSessions,
    loadAgentToolAudit,
    loadAgentToolPolicy,
    saveAgentToolPolicy,
    setAgentAllowedTools,
    setAgentAuditLimit,
    setAgentAuditSessionFilter,
    setAgentAuditToolName,
    setAgentAuditTraceId,
    setAgentEnabled,
    setAgentScope,
  } = controller;

  return (
        <>
          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">智能体概览</p>
                <h3>{agentScopeLabel}</h3>
              </div>
            </div>
            <div className="summary-grid">
              <div className="summary-card" data-status={
                !agentToolPolicySnapshot
                  ? undefined
                  : agentToolPolicySnapshot.enabled ? "ok" : "warning"
              }>
                <span>当前状态</span>
                <strong>{
                  !agentToolPolicySnapshot
                    ? "未读取"
                    : agentToolPolicySnapshot.enabled ? "已启用" : "已停用"
                }</strong>
              </div>
              <div className="summary-card">
                <span>当前群</span>
                <strong>{effectiveGroupSessionId ? sessionDisplayName(effectiveGroupSession) : "未选择"}</strong>
              </div>
              <div className="summary-card">
                <span>有效工具</span>
                <strong>{agentToolPolicySnapshot ? agentToolPolicySnapshot.effective_tools.length : "-"}</strong>
              </div>
              <div className="summary-card">
                <span>来源插件</span>
                <strong>{agentToolOwners.length || "-"}</strong>
              </div>
            </div>
            <div className="route-list">
              <div>当前智能体支持多个作用域；会根据群聊意图切到资料查询、插件状态或绘图生成，不替代普通回复策略。</div>
              <div>工具白名单按群生效，沿用顶部“全局目标群 / 会话”或左侧群列表的当前选中群。</div>
              <div>未配置过的群默认启用并继承当前作用域的全部工具；只有明确停用或保存手动白名单才会覆盖默认值。</div>
              <div>文件处理与消息导出还受“群行为 → 允许群文件发送”总开关约束；总开关关闭时，管理员也不能绕过。</div>
              <div>如果当前群关闭智能体，后端会直接返回“该群智能体能力已关闭”，不会再回落到普通模型工具调用。</div>
              <div>工具目录会标明插件归属，后续继续拆分作用域和插件能力时，可直接看出工具来源。</div>
            </div>
          </section>

          <section className="panel span-2">
            <div className="panel-header">
              <div>
                <p className="section-kicker">工具策略</p>
                <h3>当前群智能体工具白名单</h3>
              </div>
            </div>
            {!effectiveGroupSessionId ? (
              <div className="admin-notice admin-notice-error">
                请先在顶部“全局目标群 / 会话”或左侧群列表里选择一个群，再配置智能体。
              </div>
            ) : (
              <>
                <div className="agent-state-note">
                  <strong>{effectiveGroupSessionName}</strong>
                  <span className="mono">{effectiveGroupSessionId}</span>
                </div>
                <div className="form-grid">
                  <label className="field">
                    <span>工具作用域</span>
                    <select value={agentScope} onChange={(event) => setAgentScope(event.target.value)}>
                      {agentScopes.map((item) => (
                        <option key={item} value={item}>
                          {item}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    <span>智能体开关</span>
                    <select value={agentEnabled} onChange={(event) => setAgentEnabled(event.target.value)}>
                      <option value="true">启用</option>
                      <option value="false">停用</option>
                    </select>
                  </label>
                  <label className="field">
                    <span>策略更新时间</span>
                    <input value={formatDateValue(agentToolPolicySnapshot?.updated_at || null)} readOnly />
                  </label>
                  <label className="field">
                    <span>作用域</span>
                    <input value={agentToolPolicySnapshot?.scope || agentScope} readOnly />
                  </label>
                  <label className="field">
                    <span>默认模式</span>
                    <input value={agentToolPolicySnapshot?.inherits_default_tools ? "全部工具" : "手动白名单"} readOnly />
                  </label>
                </div>
                <div className="agent-tool-grid">
                  {agentToolCatalog.map((item) => {
                    const checked = agentAllowedTools.includes(item.name);
                    return (
                      <label
                        key={item.name}
                        className={`toggle-chip agent-tool-card${checked ? " is-active" : ""}${agentEnabled !== "true" ? " is-disabled" : ""}`}
                      >
                        <div className="agent-tool-head">
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={agentEnabled !== "true"}
                            onChange={(event) => {
                              const nextChecked = event.target.checked;
                              setAgentAllowedTools((current) => {
                                const set = new Set(current);
                                if (nextChecked) {
                                  set.add(item.name);
                                } else {
                                  set.delete(item.name);
                                }
                                return agentToolCatalog
                                  .map((tool) => tool.name)
                                  .filter((name) => set.has(name));
                              });
                            }}
                          />
                          <strong>{item.name}</strong>
                        </div>
                        <div className="agent-tool-meta">
                          <span className="agent-tool-badge">{item.owner || "旧版内置"}</span>
                          <span className="agent-tool-badge agent-tool-badge-muted">
                            {formatAgentToolScope(item.channels)}
                          </span>
                          <span className="agent-tool-badge agent-tool-badge-muted">
                            {formatAgentToolScope(item.session_kinds)}
                          </span>
                          <span className="mono">{item.scope || agentScope}</span>
                        </div>
                        <em>{item.description || "未提供说明"}</em>
                      </label>
                    );
                  })}
                  {!agentToolCatalog.length && (
                    <div className="admin-notice admin-notice-error">
                      当前还没有可用的智能体工具目录，请先检查后端插件挂载。
                    </div>
                  )}
                </div>
                <div className="action-row">
                  <button className="button button-secondary" onClick={() => void loadAgentToolPolicy()} disabled={!config.adminToken}>
                    读取当前群策略
                  </button>
                  <button className="button button-secondary" onClick={() => setAgentAllowedTools(agentToolCatalog.map((item) => item.name))} disabled={!config.adminToken || !agentToolCatalog.length}>
                    勾选全部工具
                  </button>
                  <button className="button button-secondary" onClick={() => {
                    setAgentEnabled("true");
                    setAgentAllowedTools(agentToolCatalog.map((item) => item.name));
                  }} disabled={!config.adminToken || !agentToolCatalog.length}>
                    恢复默认全部工具
                  </button>
                  <DangerAction
                    label="保存智能体策略"
                    title="确认更新当前群智能体工具权限"
                    impact={<p>将立即改变当前已验证群可调用的工具范围；关闭智能体会阻止该群后续工具调用。</p>}
                    confirmLabel="确认保存"
                    pendingLabel="正在保存…"
                    disabled={!config.adminToken || !effectiveGroupSessionId || !agentToolPolicyEtag || !agentToolPolicyDirty || agentToolPolicyStatus === "saving"}
                    onConfirm={saveAgentToolPolicy}
                  />
                </div>
                <p className="muted-copy">
                  保存时如果你勾选了全部工具，前端会写回“默认全部工具”模式，不会强行落成一份冗长白名单。
                </p>
              </>
            )}
          </section>

          <section className="panel">
            <div className="panel-header">
              <div>
                <p className="section-kicker">策略快照</p>
                <h3>当前生效结果</h3>
              </div>
            </div>
            <ul className="route-list">
              <li>群会话：<span className="mono">{effectiveGroupSessionId || "未选择群会话"}</span></li>
              <li>群名称：<span>{effectiveGroupSessionId ? sessionDisplayName(effectiveGroupSession) : "-"}</span></li>
              <li>智能体是否启用：<span>{agentToolPolicySnapshot ? wxbotBooleanLabel(agentToolPolicySnapshot.enabled, "已启用", "已停用") : "未读取"}</span></li>
              <li>是否继承默认工具集：<span>{agentToolPolicySnapshot ? wxbotBooleanLabel(agentToolPolicySnapshot.inherits_default_tools) : "未读取"}</span></li>
              <li>策略来源：<span>{agentToolPolicySnapshot ? (agentToolPolicySnapshot.policy_configured ? "当前群明确配置" : "系统默认") : "未读取"}</span></li>
              <li>当前目录来源插件：<span className="mono">{agentToolOwners.join(", ") || "-"}</span></li>
              <li>可用工具：<span className="mono">{(agentToolPolicySnapshot?.available_tools || agentToolCatalog.map((item) => item.name)).join(", ") || "-"}</span></li>
              <li>当前有效工具：<span className="mono">{(agentToolPolicySnapshot?.effective_tools || []).join(", ") || "-"}</span></li>
              <li>当前手动白名单：<span className="mono">{
                !agentToolPolicySnapshot
                  ? "未读取"
                  : agentToolPolicySnapshot.inherits_default_tools
                    ? "继承默认全部工具"
                    : agentToolPolicySnapshot.allowed_tools.join(", ") || "未选择工具"
              }</span></li>
            </ul>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">调用审计</p>
                <h3>最近工具调用审计</h3>
              </div>
            </div>
            <div className="agent-audit-filters">
              <label className="field">
                <span>会话筛选</span>
                <select value={agentAuditSessionFilter} onChange={(event) => setAgentAuditSessionFilter(event.target.value)}>
                  <option value="__current__">
                    {effectiveGroupSessionId ? `当前目标群 · ${sessionDisplayName(effectiveGroupSession)}` : "当前目标群 / 未选择时不过滤"}
                  </option>
                  <option value="">全租户全部群</option>
                  {groupSessions.map((item) => (
                    <option key={item.session_id} value={item.session_id}>
                      {sessionDisplayName(item)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>工具名</span>
                <select value={agentAuditToolName} onChange={(event) => setAgentAuditToolName(event.target.value)}>
                  <option value="">全部工具</option>
                  {agentToolCatalog.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>追踪 ID</span>
                <input
                  value={agentAuditTraceId}
                  onChange={(event) => setAgentAuditTraceId(event.target.value)}
                  placeholder="可选，定位单次调用"
                />
              </label>
              <label className="field">
                <span>条数</span>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={agentAuditLimit}
                  onChange={(event) => setAgentAuditLimit(Number(event.target.value) || 20)}
                />
              </label>
              <div className="action-row">
                <button className="button button-secondary" onClick={() => void loadAgentToolAudit()} disabled={!config.adminToken}>
                  刷新审计
                </button>
              </div>
            </div>
            <div className="table-scroll">
              <table>
                <caption className="sr-only">智能体工具调用审计</caption>
                <thead>
                  <tr>
                    <th scope="col">时间</th>
                    <th scope="col">范围</th>
                    <th scope="col">工具</th>
                    <th scope="col">群 / 会话</th>
                    <th scope="col">耗时</th>
                    <th scope="col">追踪 ID</th>
                    <th scope="col">结果</th>
                    <th scope="col">最终回复</th>
                  </tr>
                </thead>
                <tbody>
                  {agentToolAuditItems.map((item) => (
                    <tr key={item.id}>
                      <td>{formatDateValue(item.created_at || null)}</td>
                      <td>
                        <span className="mono">{item.scope || "-"}</span>
                      </td>
                      <td>
                        <div className="agent-audit-meta">
                          <strong>{item.tool_name}</strong>
                          <span className="mono">{formatJson(item.tool_args || {})}</span>
                        </div>
                      </td>
                      <td>
                        <div className="agent-audit-meta">
                          <strong>{groupSessions.find((session) => session.session_id === item.session_id)?.session_name || item.session_id}</strong>
                          <span className="mono">{item.session_id}</span>
                        </div>
                      </td>
                      <td>{item.latency_ms ?? 0} 毫秒</td>
                      <td className="mono">{item.trace_id || "-"}</td>
                      <td>
                        <AgentAuditResultCell item={item} />
                      </td>
                      <td>
                        <div className="agent-reply-preview">
                          {item.final_reply_text?.trim() || "-"}
                        </div>
                      </td>
                    </tr>
                  ))}
                  {!agentToolAuditItems.length && (
                    <tr>
                      <td colSpan={8}>当前条件下暂无智能体工具调用审计</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <OutputPanel title="智能体策略 / 审计响应" value={agentOutput} />
        </>
      );
}

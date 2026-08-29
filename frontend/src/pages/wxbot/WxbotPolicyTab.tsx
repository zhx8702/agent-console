import { Alert } from "../../components/Alert";
import { OutputPanel } from "../../components/OutputPanel";
import { Link } from "react-router-dom";
import { ToggleCard } from "../group-behavior/presentation";
import type { WxbotPageController } from "./useWxbotPageController";
import {
  formatDateValue,
  groupActivityReasonLabel,
  groupActivityValidationError,
  wxbotBooleanLabel,
  wxbotModelTierLabel,
  wxbotOperationStatusLabel,
  wxbotReplyModeLabel,
  wxbotSessionStateLabel,
} from "./model";

export function WxbotPolicyTab({ controller }: { controller: WxbotPageController }) {
  const {
    agentScopes,
    applySimpleReplyPreset,
    config,
    effectiveActivitySessionId,
    effectiveActivitySessionName,
    effectiveSessionId,
    effectiveSessionIsGroup,
    globalGroupReplyMentionSender,
    globalGroupReplyMode,
    globalPolicyDirty,
    globalPolicyEtag,
    globalPolicySnapshot,
    globalPrivateReplyMode,
    globalTriggerKeywordsText,
    groupActivityBusy,
    groupActivityConfig,
    groupActivityDecision,
    groupActivityDirty,
    groupActivityEtag,
    groupActivityEvents,
    groupActivityFeedback,
    groupActivityFormDisabled,
    groupActivityLoadedForScope,
    groupActivityServerEtag,
    groupActivityStatus,
    groupParticipationDirty,
    groupParticipationError,
    groupParticipationEtag,
    groupParticipationPolicy,
    groupParticipationStatus,
    discardGroupActivityDraft,
    loadGlobalReplyPolicy,
    loadGroupActivity,
    loadGroupParticipationPolicy,
    loadReplyPolicy,
    loadSdkTriggerDebug,
    loadSessionState,
    mentionSenderMode,
    policyConflict,
    policyEtag,
    policyOutput,
    policySnapshot,
    replyMode,
    replyPolicyDirty,
    runGroupActivityDryRun,
    saveGlobalReplyPolicy,
    saveGroupActivity,
    saveGroupParticipationPolicy,
    saveReplyPolicy,
    saveSdkTriggerDebug,
    sdkGroupRequireAtMe,
    sdkGateDirty,
    sdkTriggerDebug,
    sessionPolicyDirty,
    sessionId,
    sessionStateSnapshot,
    sessions,
    setGlobalGroupReplyMentionSender,
    setGlobalGroupReplyMode,
    setGlobalPrivateReplyMode,
    setGlobalTriggerKeywordsText,
    setGroupActivityConfig,
    setGroupParticipationEnabled,
    setMentionSenderMode,
    setReplyMode,
    setSdkGroupRequireAtMe,
    setSessionAutoReplyEnabled,
    setSessionId,
    setTriggerKeywordsText,
    triggerKeywordsText,
    updateConfig,
  } = controller;

  return (
        <>
          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">快速配置</p>
                <h3>简化回复配置</h3>
              </div>
            </div>
            <p className="muted-copy">
              目标效果：私聊直接回复，群里只有 @机器人 才回复。系统会在一个事务中校验并写入全局策略、当前群会话策略、复读策略与 SDK 入站门禁；任一版本冲突都不会部分生效。当前群参与总开关是独立的最终闸门：新群默认开启，明确关闭过的群需在下方重新开启。群聊侧是否回复，主要由 SDK 的“必须 @我才入站”开关决定；只要没有 @机器人，消息不会进入控制台。
            </p>
            {policyConflict ? (
              <Alert variant="warning" title="策略版本冲突">
                本地草稿仍在。请使用对应区域的“放弃草稿并重新读取”，核对服务端新版本后再保存。
              </Alert>
            ) : null}
            <div className="action-row">
              <button className="button button-primary" onClick={() => void applySimpleReplyPreset()} disabled={!config.adminToken || !effectiveActivitySessionId || replyPolicyDirty || groupActivityDirty}>
                一键设为 私聊直接回复 / 群里@回复
              </button>
              <button className="button button-secondary" onClick={() => {
                void loadGlobalReplyPolicy();
                void loadSdkTriggerDebug();
                if (effectiveSessionId) {
                  void loadReplyPolicy();
                  void loadSessionState();
                }
                if (effectiveActivitySessionId) {
                  void loadGroupActivity();
                  void loadGroupParticipationPolicy();
                }
              }} disabled={!config.adminToken || groupActivityDirty || replyPolicyDirty || groupParticipationDirty}>
                刷新当前策略
              </button>
            </div>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">全局策略</p>
                <h3>租户全局默认回复策略</h3>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>私聊默认回复模式</span>
                <select value={globalPrivateReplyMode} onChange={(event) => setGlobalPrivateReplyMode(event.target.value)}>
                  <option value="all">{wxbotReplyModeLabel("all")}</option>
                  <option value="off">{wxbotReplyModeLabel("off")}</option>
                  <option value="contains">{wxbotReplyModeLabel("contains")}</option>
                </select>
              </label>
              <label className="field">
                <span>群聊默认回复模式</span>
                <select value={globalGroupReplyMode} onChange={(event) => setGlobalGroupReplyMode(event.target.value)}>
                  <option value="off">{wxbotReplyModeLabel("off")}</option>
                  <option value="contains">{wxbotReplyModeLabel("contains")}</option>
                </select>
              </label>
              <label className="field span-2">
                <span>群回复时默认 @发送者</span>
                <select value={globalGroupReplyMentionSender} onChange={(event) => setGlobalGroupReplyMentionSender(event.target.value)}>
                  <option value="false">不提及（推荐，更自然）</option>
                  <option value="true">提及（每次明确指向发送者）</option>
                </select>
              </label>
              <label className="field span-2">
                <span>全局触发关键词</span>
                <textarea
                  rows={6}
                  value={globalTriggerKeywordsText}
                  onChange={(event) => setGlobalTriggerKeywordsText(event.target.value)}
                  placeholder="每行一个关键词。全局或会话使用“包含触发词”模式时可作为默认关键词集。"
                />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadGlobalReplyPolicy()} disabled={!config.adminToken}>
                {globalPolicyDirty ? "放弃草稿并重新读取" : "读取全局策略"}
              </button>
              <button className="button button-primary" onClick={() => void saveGlobalReplyPolicy()} disabled={!config.adminToken || !globalPolicyEtag || !globalPolicyDirty || policyConflict === "global"}>
                保存全局策略
              </button>
              <span className="pill pill-muted">{globalPolicyDirty ? "有未保存修改" : "已同步"}</span>
              <span className="pill pill-muted">版本 {globalPolicyEtag || "-"}</span>
            </div>
            <p className="muted-copy">
              群回复默认不 @发送者，减少机械感；确实需要每条回复都明确指向成员时，可切换为“提及”。
            </p>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">会话覆盖</p>
                <h3>会话单独覆盖</h3>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>会话</span>
                <select
                  value={sessionId}
                  disabled={groupActivityDirty || replyPolicyDirty}
                  onChange={(event) => {
                    const nextSessionId = event.target.value;
                    setSessionId(nextSessionId);
                    updateConfig({ sessionId: nextSessionId.trim() });
                  }}
                >
                  <option value="">请选择会话</option>
                  {sessions.map((item) => (
                    <option key={item.session_id} value={item.session_id}>
                      {item.session_name || item.session_id}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>单会话回复模式</span>
                <select value={replyMode} onChange={(event) => setReplyMode(event.target.value)}>
                  <option value="inherit">{wxbotReplyModeLabel("inherit")}</option>
                  {!effectiveSessionIsGroup ? <option value="all">{wxbotReplyModeLabel("all")}</option> : null}
                  <option value="off">{wxbotReplyModeLabel("off")}</option>
                  <option value="contains">{wxbotReplyModeLabel("contains")}</option>
                </select>
              </label>
              <label className="field">
                <span>回复时是否 @发送者</span>
                <select value={mentionSenderMode} onChange={(event) => setMentionSenderMode(event.target.value)}>
                  <option value="inherit">{wxbotReplyModeLabel("inherit")}</option>
                  <option value="on">提及</option>
                  <option value="off">不提及</option>
                </select>
              </label>
              <label className="field span-2">
                <span>单会话关键词</span>
                <textarea
                  rows={6}
                  value={triggerKeywordsText}
                  onChange={(event) => setTriggerKeywordsText(event.target.value)}
                  placeholder="每行一个关键词，仅“包含触发词”模式生效"
                />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadReplyPolicy()} disabled={!config.adminToken}>
                {sessionPolicyDirty ? "放弃草稿并重新读取" : "读取策略"}
              </button>
              <button className="button button-primary" onClick={() => void saveReplyPolicy()} disabled={!config.adminToken || !policyEtag || !sessionPolicyDirty || policyConflict === "session"}>
                保存策略
              </button>
              <span className="pill pill-muted">{sessionPolicyDirty ? "有未保存修改" : "已同步"}</span>
              <span className="pill pill-muted">版本 {policyEtag || "-"}</span>
            </div>
            <p className="muted-copy">
              大多数场景保持“继承全局策略”即可，也就是沿用当前租户的私聊或群聊默认策略。只有某个群或某个私聊要单独关闭、单独开启关键词触发时，才需要修改。
            </p>
            {effectiveSessionIsGroup ? (
              <p className="muted-copy">
                群成员明确请求“转人工 / 人工客服 / 真人”时，智能体会如实回复当前无法直接转接，并提示联系群管理员；不会假装已有人接管，也不会自动把整个群切到人工模式。
              </p>
            ) : null}
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">最终回复总闸</p>
                <h3>当前群参与总开关</h3>
              </div>
              <span className={`pill ${
                groupParticipationStatus === "loading" || !groupParticipationPolicy
                  ? "pill-muted"
                  : groupParticipationPolicy.effective_enabled ? "pill-ok" : "pill-danger"
              }`}>
                {groupParticipationStatus === "loading"
                  ? "读取中"
                  : !groupParticipationPolicy
                    ? "未读取"
                    : groupParticipationPolicy.effective_enabled ? "允许参与" : "停止参与"}
              </span>
            </div>
            {effectiveActivitySessionId ? (
              <>
                <p className="muted-copy">
                  当前群：<span className="mono">{effectiveActivitySessionName || effectiveActivitySessionId}</span>。
                  未单独配置的群默认开启；只有在这里明确关闭后，@机器人、关键词和普通回复都会停止。
                </p>
                {groupParticipationError ? (
                  <Alert variant="warning" title="群参与开关不可用">
                    {groupParticipationError}
                  </Alert>
                ) : null}
                {groupParticipationPolicy && !groupParticipationPolicy.effective_enabled ? (
                  <Alert variant="warning" title="当前群不会触发回复">
                    {!groupParticipationPolicy.kill_switches.global_enabled ? "全局发布控制已关闭。" : null}
                    {!groupParticipationPolicy.kill_switches.tenant_enabled ? "租户发布控制已关闭。" : null}
                    {!groupParticipationPolicy.kill_switches.group_enabled ? "当前群参与总开关已关闭。" : null}
                  </Alert>
                ) : null}
                {groupParticipationPolicy ? (
                  <div className="form-grid">
                    <ToggleCard
                      checked={groupParticipationPolicy.kill_switches.group_enabled}
                      label="允许当前群参与回复"
                      description="默认开启；关闭后本群任何回复条件都不会生效"
                      disabled={groupParticipationStatus === "loading" || groupParticipationStatus === "saving"}
                      onChange={setGroupParticipationEnabled}
                    />
                    <div className="status-grid span-2">
                      <article className="status-tile">
                        <span>全局控制</span>
                        <strong>{groupParticipationPolicy.kill_switches.global_enabled ? "开启" : "关闭"}</strong>
                      </article>
                      <article className="status-tile">
                        <span>租户控制</span>
                        <strong>{groupParticipationPolicy.kill_switches.tenant_enabled ? "开启" : "关闭"}</strong>
                      </article>
                      <article className="status-tile">
                        <span>当前群</span>
                        <strong>{groupParticipationPolicy.kill_switches.group_enabled ? "开启" : "关闭"}</strong>
                      </article>
                    </div>
                  </div>
                ) : null}
                <div className="action-row">
                  <button className="button button-secondary" onClick={() => void loadGroupParticipationPolicy()} disabled={!config.adminToken || groupParticipationStatus === "loading" || groupParticipationStatus === "saving"}>
                    重新读取总开关
                  </button>
                  <button className="button button-primary" onClick={() => void saveGroupParticipationPolicy()} disabled={!config.adminToken || !groupParticipationEtag || !groupParticipationDirty || groupParticipationStatus === "saving"}>
                    保存群参与总开关
                  </button>
                  <span className="pill pill-muted">版本 {groupParticipationEtag || "-"}</span>
                  <Link className="button button-secondary" to="/group-behavior">高级参与策略</Link>
                </div>
              </>
            ) : (
              <p className="muted-copy">请先选择一个群会话；总开关按群隔离，不会应用到私聊，也不会跨群共享。</p>
            )}
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">智能体主动参与</p>
                <h3>智能体自动暖场</h3>
              </div>
            </div>
            {effectiveActivitySessionId ? (
              <>
                <div className="summary-grid">
                  <div className="summary-card" data-status={groupActivityConfig.enabled ? "warning" : "ok"}>
                    <span>自动暖场</span>
                    <strong>{groupActivityConfig.enabled ? "已启用" : "已关闭"}</strong>
                  </div>
                  <div className="summary-card" data-status={groupActivityConfig.max_per_day === 1 ? "ok" : "warning"}>
                    <span>每日上限</span>
                    <strong>{groupActivityConfig.max_per_day}</strong>
                  </div>
                  <div
                    className="summary-card"
                    data-status={groupActivityDecision?.status === "failed" ? "warning" : "ok"}
                  >
                    <span>最近安全检查</span>
                    <strong>{groupActivityDecision ? wxbotOperationStatusLabel(groupActivityDecision.status) : "未检查"}</strong>
                  </div>
                </div>
                <p className="muted-copy">
                  当前群：<span className="mono">{effectiveActivitySessionName || effectiveActivitySessionId}</span>。默认关闭、每天最多 1 次；仅在群里询问身份时明确说明智能体身份，不主动添加固定前缀。
                </p>
                <fieldset
                  className="form-grid fieldset-reset"
                  disabled={groupActivityFormDisabled}
                >
                  <label className="field">
                    <span>启用自动暖场</span>
                    <select
                      value={String(groupActivityConfig.enabled)}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        enabled: event.target.value === "true",
                      }))}
                    >
                      <option value="false">关闭（默认）</option>
                      <option value="true">启用</option>
                    </select>
                  </label>
                  <label className="field">
                    <span>时区（IANA）</span>
                    <input
                      value={groupActivityConfig.timezone}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        timezone: event.target.value,
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>允许开始</span>
                    <input
                      type="time"
                      value={groupActivityConfig.active_start}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        active_start: event.target.value,
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>允许结束</span>
                    <input
                      type="time"
                      value={groupActivityConfig.active_end}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        active_end: event.target.value,
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>静默开始</span>
                    <input
                      type="time"
                      value={groupActivityConfig.quiet_start}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        quiet_start: event.target.value,
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>静默结束</span>
                    <input
                      type="time"
                      value={groupActivityConfig.quiet_end}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        quiet_end: event.target.value,
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>群空闲多久后检查（分钟）</span>
                    <input
                      type="number"
                      min={180}
                      value={groupActivityConfig.idle_minutes}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        idle_minutes: Number(event.target.value),
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>两次暖场最短间隔（分钟）</span>
                    <input
                      type="number"
                      min={60}
                      value={groupActivityConfig.min_send_interval_minutes}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        min_send_interval_minutes: Number(event.target.value),
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>每天最多暖场（建议 1）</span>
                    <input
                      type="number"
                      min={1}
                      max={3}
                      value={groupActivityConfig.max_per_day}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        max_per_day: Number(event.target.value),
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>话题去重窗口（分钟）</span>
                    <input
                      type="number"
                      min={60}
                      max={10080}
                      value={groupActivityConfig.topic_repeat_window_minutes}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        topic_repeat_window_minutes: Number(event.target.value),
                      }))}
                    />
                  </label>
                  <label className="field">
                    <span>模型档位</span>
                    <select
                      value={groupActivityConfig.llm_model_tier}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        llm_model_tier: event.target.value,
                      }))}
                    >
                      {(["tier-1", "tier-2", "tier-3"] as const).map((tier) => <option value={tier} key={tier}>{wxbotModelTierLabel(tier)}</option>)}
                    </select>
                  </label>
                  <label className="field">
                    <span>生成温度（0–2）</span>
                    <input
                      type="number"
                      min={0}
                      max={2}
                      step={0.1}
                      value={groupActivityConfig.temperature}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        temperature: Number(event.target.value),
                      }))}
                    />
                  </label>
                  <label className="field span-2">
                    <span>可用智能体工具范围</span>
                    <select
                      value={groupActivityConfig.agent_tool_scope}
                      onChange={(event) => setGroupActivityConfig((current) => ({
                        ...current,
                        agent_tool_scope: event.target.value,
                      }))}
                    >
                      {Array.from(new Set([
                        "group_info",
                        "group_plugin_status",
                        "group_draw_generation",
                        "group_personal_map",
                        ...agentScopes,
                        groupActivityConfig.agent_tool_scope,
                      ])).filter(Boolean).map((scope) => (
                        <option key={scope} value={scope}>{scope}</option>
                      ))}
                    </select>
                  </label>
                </fieldset>
                <div className="action-row">
                  <button
                    className="button button-secondary"
                    onClick={() => void loadGroupActivity()}
                    disabled={!config.adminToken || Boolean(groupActivityBusy) || groupActivityDirty}
                  >
                    {groupActivityBusy === "load" ? "读取中…" : "读取暖场配置"}
                  </button>
                  {groupActivityDirty ? (
                    <button className="button button-secondary" onClick={discardGroupActivityDraft}>
                      放弃未保存修改
                    </button>
                  ) : null}
                  <button
                    className="button button-primary"
                    onClick={() => void saveGroupActivity()}
                    disabled={
                      !config.adminToken
                      || Boolean(groupActivityBusy)
                      || !groupActivityLoadedForScope
                      || !groupActivityDirty
                      || Boolean(groupActivityValidationError(groupActivityConfig))
                    }
                  >
                    {groupActivityBusy === "save" ? "保存中…" : "保存暖场配置"}
                  </button>
                  <button
                    className="button button-secondary"
                    onClick={() => void runGroupActivityDryRun()}
                    disabled={!config.adminToken || Boolean(groupActivityBusy) || groupActivityDirty}
                  >
                    {groupActivityBusy === "dry-run" ? "检查中…" : "安全检查（不发送）"}
                  </button>
                </div>
                {groupActivityValidationError(groupActivityConfig) ? (
                  <p className="muted-copy" role="alert">{groupActivityValidationError(groupActivityConfig)}</p>
                ) : null}
                {groupActivityFeedback ? <p className="muted-copy" role="status">{groupActivityFeedback}</p> : null}
                <div className="route-list" aria-live="polite">
                  <div>
                    加载状态：<strong>{wxbotOperationStatusLabel(groupActivityStatus)}</strong>
                    {" · "}草稿：<strong>{groupActivityDirty ? "有未保存修改" : "已同步"}</strong>
                  </div>
                  <div>
                    配置版本：<span className="mono">{groupActivityLoadedForScope ? groupActivityConfig.version : "-"}</span>
                    {" · "}ETag：<span className="mono">{groupActivityLoadedForScope ? groupActivityEtag || "-" : "-"}</span>
                  </div>
                </div>
                {groupActivityStatus === "conflict" ? (
                  <div className="alert alert-warning" role="alert">
                    <span className="alert-icon" aria-hidden="true">!</span>
                    <div className="alert-content">
                      <strong>暖场配置已被其他操作者更新</strong>
                      <div>
                        本地草稿仍保留。服务器 ETag：<span className="mono">{groupActivityServerEtag || "未知"}</span>。
                        重新加载会用服务器版本覆盖当前暖场表单。
                      </div>
                      <button className="button button-secondary" onClick={() => void loadGroupActivity()}>
                        加载服务器版本（覆盖草稿）
                      </button>
                    </div>
                  </div>
                ) : null}
                <div className="route-list">
                  <div>
                    安全检查结论：<strong>{groupActivityReasonLabel(groupActivityDecision?.reason_code || groupActivityDecision?.reason)}</strong>
                  </div>
                  <div>
                    原因码：<span className="mono">{groupActivityDecision?.reason_code || groupActivityDecision?.reason || "-"}</span>
                    {" · "}
                    上下文消息：<span className="mono">{groupActivityDecision?.message_count ?? "-"}</span>
                  </div>
                  <div>安全检查只检查已保存配置和实时群状态，绝不会绕过限制或发送消息。</div>
                </div>
                <div className="table-scroll compact-table-scroll">
                  <table>
                    <caption className="sr-only">最近群参与决策</caption>
                    <thead>
                      <tr>
                        <th scope="col">时间</th>
                        <th scope="col">状态</th>
                        <th scope="col">决策原因</th>
                        <th scope="col">上下文</th>
                        <th scope="col">内容</th>
                      </tr>
                    </thead>
                    <tbody>
                      {groupActivityEvents.map((item) => (
                        <tr key={item.id}>
                          <td>{formatDateValue(item.created_at)}</td>
                          <td>{wxbotOperationStatusLabel(item.status)}</td>
                          <td title={item.reason_code || ""}>
                            {groupActivityReasonLabel(item.reason_code)}
                          </td>
                          <td>{item.message_count ?? 0}</td>
                          <td>{item.generated_text || item.error || "-"}</td>
                        </tr>
                      ))}
                      {!groupActivityEvents.length ? (
                        <tr>
                          <td colSpan={5}>当前群还没有暖场执行记录</td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <p className="muted-copy">请先选择一个群会话。自动暖场默认关闭，且不会对私聊生效。</p>
            )}
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">会话状态</p>
                <h3>智能体自动回复状态</h3>
              </div>
            </div>
            <div className="summary-grid">
              <div className="summary-card" data-status={sessionStateSnapshot?.auto_reply_enabled ? "ok" : "warning"}>
                <span>自动回复</span>
                <strong>{sessionStateSnapshot?.auto_reply_enabled ? "已启用" : "已暂停"}</strong>
              </div>
              <div className="summary-card" data-status={sessionStateSnapshot?.suppress_ai_reply ? "warning" : "ok"}>
                <span>会话状态</span>
                <strong>{wxbotSessionStateLabel(sessionStateSnapshot?.state)}</strong>
              </div>
            </div>
            <div className="route-list">
              <div>当前会话: <span className="mono">{effectiveSessionId || "-"}</span></div>
              <div>
                状态说明:
                {" "}
                {sessionStateSnapshot?.explanation || "请选择会话后读取状态"}
              </div>
              <div>
                为什么会转人工:
                {" "}
                {effectiveSessionIsGroup ? (
                  <>
                    群聊里这些关键词
                    {" "}
                    <span className="mono">{(sessionStateSnapshot?.handoff_hint_keywords || []).join(" / ") || "转人工 / 人工协助 / 真人"}</span>
                    {" "}
                    会触发明确的能力边界回复：“目前无法直接转接人工，如需帮助请联系群管理员”；不会自动暂停群聊。后台只能手动暂停智能体，不会通知、分配或确认任何人工受理人。
                  </>
                ) : (
                  <>
                    命中预处理里的人工接管关键词
                    {" "}
                    <span className="mono">{(sessionStateSnapshot?.handoff_hint_keywords || []).join(" / ") || "转人工 / 人工协助 / 真人"}</span>
                    {" "}
                    后，会把会话设为“已暂停，等待人工接管”，后续消息不再进入模型；这只代表暂停智能体，不代表已有人工接单。
                  </>
                )}
              </div>
              <div>
                最近一次疑似转人工命中:
                {" "}
                <span className="mono">
                  {sessionStateSnapshot?.latest_handoff_turn?.content || "-"}
                </span>
                {" "}
                ({formatDateValue(sessionStateSnapshot?.latest_handoff_turn?.created_at || null)})
              </div>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadSessionState()} disabled={!config.adminToken || !effectiveSessionId}>
                读取会话状态
              </button>
              <button
                className="button button-primary"
                onClick={() => void setSessionAutoReplyEnabled(true)}
                disabled={!config.adminToken || !effectiveSessionId}
              >
                切回自动回复
              </button>
              <button
                className="button button-secondary"
                onClick={() => void setSessionAutoReplyEnabled(false)}
                disabled={!config.adminToken || !effectiveSessionId}
              >
                暂停智能体（等待人工接管）
              </button>
            </div>
            <p className="muted-copy">
              “切回自动回复”会恢复会话的自动回复状态；“暂停智能体”只会停止机器人回复，不会通知、分配或确认任何人工受理人。
            </p>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">SDK 入站门禁</p>
                <h3>群消息入站门禁</h3>
              </div>
            </div>
            <div className="form-grid">
              <label className="field">
                <span>群消息必须 @我 才入站</span>
                <select value={sdkGroupRequireAtMe} onChange={(event) => setSdkGroupRequireAtMe(event.target.value)}>
                  <option value="true">必须 @我</option>
                  <option value="false">无需 @我</option>
                </select>
              </label>
              <label className="field">
                <span>当前捕获模式</span>
                <input value={sdkTriggerDebug?.group_capture_mode || "-"} readOnly />
              </label>
              <label className="field span-2">
                <span>机器人别名</span>
                <input value={(sdkTriggerDebug?.my_names || []).join(", ")} readOnly />
              </label>
            </div>
            <div className="action-row">
              <button className="button button-secondary" onClick={() => void loadSdkTriggerDebug()} disabled={!config.adminToken}>
                {sdkGateDirty ? "放弃草稿并重新读取" : "读取 SDK 门禁"}
              </button>
              <button className="button button-primary" onClick={() => void saveSdkTriggerDebug()} disabled={!config.adminToken || !effectiveActivitySessionId || !sdkGateDirty || globalPolicyDirty || sessionPolicyDirty}>
                通过聚合策略保存
              </button>
            </div>
            <p className="muted-copy">
              “必须 @我”表示群消息必须先 @机器人别名才会进入系统；“无需 @我”表示群消息全部入站，便于定位误触发。
            </p>
          </section>

          <section className="panel span-3">
            <div className="panel-header">
              <div>
                <p className="section-kicker">策略说明</p>
                <h3>当前生效结果</h3>
              </div>
            </div>
            <ul className="route-list">
              <li>当前会话: <span className="mono">{effectiveSessionId || "-"}</span></li>
              <li>全局私聊默认：<span>{wxbotReplyModeLabel(globalPolicySnapshot?.private_reply_mode || globalPrivateReplyMode)}</span></li>
              <li>全局群聊默认：<span>{wxbotReplyModeLabel(globalPolicySnapshot?.group_reply_mode || globalGroupReplyMode)}</span></li>
              <li>全局群回复默认 @发送者：<span>{wxbotBooleanLabel(globalPolicySnapshot?.group_reply_mention_sender ?? (globalGroupReplyMentionSender === "true"))}</span></li>
              <li>当前会话有效模式：<span>{wxbotReplyModeLabel(policySnapshot?.effective_mode)}</span></li>
              <li>当前群参与总开关：<span>{groupParticipationPolicy ? (groupParticipationPolicy.kill_switches.group_enabled ? "开启" : "关闭") : "未读取"}</span></li>
              <li>群参与最终状态：<span>{groupParticipationPolicy ? (groupParticipationPolicy.effective_enabled ? "允许参与" : "停止参与") : "未读取"}</span></li>
              <li>当前会话智能体状态：<span>{wxbotSessionStateLabel(sessionStateSnapshot?.state)}</span></li>
              <li>当前会话是否允许自动回复：<span>{wxbotBooleanLabel(sessionStateSnapshot?.auto_reply_enabled ?? false)}</span></li>
              <li>当前会话 @发送者模式：<span>{policySnapshot?.mention_sender_mode === "on" ? "提及" : policySnapshot?.mention_sender_mode === "off" ? "不提及" : wxbotReplyModeLabel(policySnapshot?.mention_sender_mode)}</span></li>
              <li>当前会话是否最终 @发送者：<span>{wxbotBooleanLabel(Boolean(policySnapshot?.effective_mention_sender))}</span></li>
              <li>当前会话是否继承全局关键词：<span>{wxbotBooleanLabel(Boolean(policySnapshot?.inherits_global_keywords))}</span></li>
              <li>当前有效关键词: <span className="mono">{policySnapshot?.effective_trigger_keywords_text || "-"}</span></li>
              <li>SDK 当前是否要求群消息先 @我：<span>{wxbotBooleanLabel(sdkTriggerDebug?.group_require_at_me ?? true)}</span></li>
              <li>最简配置建议：全局私聊设为“全部消息”、全局群聊设为“包含触发词”、SDK 群消息设为“必须 @我”。</li>
              <li>“继承全局策略”只在会话层可用，表示沿用当前租户的私聊或群聊默认值。</li>
            </ul>
          </section>

          <OutputPanel flush title="回复策略响应" value={policyOutput} />
        </>
      );
}

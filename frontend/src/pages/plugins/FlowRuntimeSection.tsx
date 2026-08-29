import { useId, useState } from "react";

import type {
  EffectAuditFilters,
  FlowEffectLogResponse,
  FlowEffectSummaryResponse,
  FlowEffectSummaryRow,
  FlowEffectTraceRecord,
  FlowRunResult,
  MessageFlowRuntimeStatus,
  ReadyzFlowChecks,
  TraceAggregate,
  TraceEventCard,
} from "./models";
import {
  compactList,
  formatTraceTime,
  payloadKeys,
  replyQueueSummary,
  traceMessageSummary,
} from "./models";

type FlowRuntimeSectionProps = {
  flowStatus: MessageFlowRuntimeStatus | null;
  readyzFlow: ReadyzFlowChecks | null;
  effectLog: FlowEffectLogResponse | null;
  effectSummary: FlowEffectSummaryResponse | null;
  effectTraceFilter: string;
  effectAuditFilters: EffectAuditFilters;
  traceAggregate: TraceAggregate | null;
  traceAggregateLoading: boolean;
  traceAggregateError: string;
  flowLoading: boolean;
  flowError: string;
  onRefresh: () => void;
  onSelectTrace: (traceId: string | undefined) => void;
  onClearTraceFilter: () => void;
  onSelectAuditFilters: (item: FlowEffectSummaryRow) => void;
  onClearAuditFilters: () => void;
  onClearAllFilters: () => void;
};

const formatBool = (value: boolean | undefined) => {
  if (value === undefined) {
    return "-";
  }
  return value ? "true" : "false";
};

const formatList = (value: string[] | undefined) => {
  if (!value?.length) {
    return "-";
  }
  return value.join(", ");
};

export function FlowRuntimeSection({
  flowStatus,
  readyzFlow,
  effectLog,
  effectSummary,
  effectTraceFilter,
  effectAuditFilters,
  traceAggregate,
  traceAggregateLoading,
  traceAggregateError,
  flowLoading,
  flowError,
  onRefresh,
  onSelectTrace,
  onClearTraceFilter,
  onSelectAuditFilters,
  onClearAuditFilters,
  onClearAllFilters,
}: FlowRuntimeSectionProps) {
  const traceFlowTitleId = useId();
  const [traceFlowModalOpen, setTraceFlowModalOpen] = useState(false);
  const loadFlowRuntimeStatus = onRefresh;
  const selectEffectTrace = onSelectTrace;
  const clearEffectTraceFilter = onClearTraceFilter;
  const selectEffectAuditFilters = onSelectAuditFilters;
  const clearEffectAuditFilters = onClearAuditFilters;
  const clearAllEffectFilters = onClearAllFilters;
  const hasEffectAuditFilters = Boolean(
    effectAuditFilters.owner
    || effectAuditFilters.type
    || effectAuditFilters.status
    || effectAuditFilters.dry_run !== undefined,
  );
  const effectAuditFilterLabel = [
    effectAuditFilters.owner ? `owner=${effectAuditFilters.owner}` : "",
    effectAuditFilters.type ? `type=${effectAuditFilters.type}` : "",
    effectAuditFilters.status ? `status=${effectAuditFilters.status}` : "",
    effectAuditFilters.dry_run !== undefined ? `dry_run=${formatBool(effectAuditFilters.dry_run)}` : "",
  ].filter(Boolean).join(" / ") || "all effects";
  const runtimeConfig = flowStatus?.runtime || readyzFlow?.checks?.flow_runtime;
  const shadowConfig = flowStatus?.shadow || readyzFlow?.checks?.flow_shadow;
  const effectCommitConfig = flowStatus?.effect_commit || readyzFlow?.checks?.flow_effect_commit;
  const effectHandlers = flowStatus?.effect_handlers || readyzFlow?.checks?.flow_effect_handlers;
  const effectHandlerItems = effectHandlers?.items || [];
  const effectHandlerFallbacks = effectHandlers?.fallbacks || [];
  const effectLogItems = effectLog?.items || [];
  const effectSummaryData = effectSummary?.summary;
  const effectStatusRows = effectSummaryData?.by_status || [];
  const effectDryRunRows = effectSummaryData?.by_dry_run || [];
  const effectMatrixRows = effectSummaryData?.matrix || [];
  const runtimeResult = flowStatus?.last_runtime_result || null;
  const shadowResult = flowStatus?.last_shadow_result || null;
  const recentDispatches = [
    ...(runtimeResult?.effect_dispatches || []),
    ...(shadowResult?.effect_dispatches || []),
  ];
  const recentCommits = [
    ...(runtimeResult?.effect_commits || []),
    ...(shadowResult?.effect_commits || []),
  ];
  const countTraceStatus = (items: FlowEffectTraceRecord[], status: string) =>
    items.filter((item) => item.status === status || item.commit_status === status).length;
  const countSummaryStatus = (status: string) =>
    effectStatusRows.find((item) => item.status === status)?.count || 0;
  const effectRiskItems = [
    {
      label: "Handler 失败",
      count: countTraceStatus(recentDispatches, "handler_error"),
      level: "danger",
      detail: "副作用已提交，但实际执行失败；优先看同 trace 的 handler/error。",
    },
    {
      label: "缺少 Handler",
      count: countTraceStatus(recentDispatches, "no_handler"),
      level: "warning",
      detail: "有 effect 没有执行器，通常是新 effect 未注册或 allowlist 未放行。",
    },
    {
      label: "Commit 失败",
      count: recentCommits.filter((item) => Boolean(item.error)).length,
      level: "danger",
      detail: "幂等门闩或审计写入失败，副作用可能没有进入执行阶段。",
    },
    {
      label: "重复提交",
      count: countTraceStatus(recentDispatches, "duplicate") + countSummaryStatus("duplicate"),
      level: "info",
      detail: "同一个 effect 被幂等拦住；偶发正常，持续出现再查重试。",
    },
    {
      label: "Audit 异常",
      count: effectLog?.backend === "error" || effectSummary?.backend === "error" ? 1 : 0,
      level: "danger",
      detail: effectLog?.error || effectSummary?.error || "flow_effect_log 查询或写入异常，先看 Postgres/audit 配置。",
    },
  ].filter((item) => item.count > 0);
  const flowRiskLevel = effectRiskItems.some((item) => item.level === "danger")
    ? "danger"
    : effectRiskItems.some((item) => item.level === "warning")
      ? "warning"
      : "ok";
  const runtimeStatus = runtimeConfig?.enabled
    ? runtimeConfig.allowed === false ? "error" : "enabled"
    : "off";
  const shadowStatus = shadowConfig?.enabled ? "enabled" : "off";
  const effectCommitEnabled = Boolean(effectCommitConfig?.backend && effectCommitConfig.backend !== "none");
  const effectCommitStatus = effectCommitConfig?.allowed === false ? "error" : effectCommitEnabled ? "enabled" : "off";
  const handlerMode = effectCommitConfig?.handler_mode || (effectCommitConfig?.handlers_enabled ? "all" : "off");
  const handlerAllowlist = effectCommitConfig?.handler_allowlist || [];
  const handlerAllowlistText = formatList(handlerAllowlist);
  const handlerScopeText = !effectCommitConfig?.handlers_enabled
    ? "off"
    : handlerMode === "selective"
      ? `selective: ${handlerAllowlistText}`
      : "all";
  const handlerStatus = effectCommitConfig?.handlers_enabled ? "enabled" : "off";
  const handlerExecutionDetail = !effectCommitConfig?.handlers_enabled
    ? `${effectHandlers?.count ?? 0} 个 handler 已注册但未执行`
    : !runtimeConfig?.enabled && shadowConfig?.effect_dry_run_enabled
      ? `${handlerScopeText}，当前 shadow 只记录 dry-run，不执行真实 handler`
      : `${handlerScopeText}，commit 成功后执行匹配 handler`;
  const handlerSelectorsFor = (owner?: string, effectType?: string) => {
    const normalizedOwner = String(owner || "").trim();
    const normalizedType = String(effectType || "").trim();
    const selectors = new Set<string>(["*"]);
    if (normalizedOwner) selectors.add(normalizedOwner);
    if (normalizedType) selectors.add(normalizedType);
    if (normalizedOwner && normalizedType) {
      selectors.add(`${normalizedOwner}:${normalizedType}`);
      selectors.add(`${normalizedOwner}.${normalizedType}`);
      selectors.add(`${normalizedOwner}/${normalizedType}`);
    }
    return selectors;
  };
  const handlerIsAllowed = (owner?: string, effectType?: string) => {
    if (!effectCommitConfig?.handlers_enabled) {
      return false;
    }
    if (handlerMode !== "selective") {
      return true;
    }
    const selectors = handlerSelectorsFor(owner, effectType);
    return handlerAllowlist.some((item) => selectors.has(item));
  };
  const auditStatus = effectCommitConfig?.log_backend && effectCommitConfig.log_backend !== "none" ? "enabled" : "off";
  const flowResultSummary = (result: FlowRunResult | null) => ({
    flowName: result?.flow_name || "-",
    traceId: result?.trace_id || "-",
    sessionId: result?.session_id || "-",
    status: result?.status || "-",
    stepCount: result?.steps?.length ?? 0,
    commitCount: result?.effect_commits?.length ?? 0,
    dispatchCount: result?.effect_dispatches?.length ?? 0,
    stopReason: result?.stop_reason || "-",
    error: result?.error || "-",
  });
  const renderFlowTraceTables = (result: FlowRunResult | null) => {
    const steps = result?.steps || [];
    const commits = result?.effect_commits || [];
    const dispatches = result?.effect_dispatches || [];
    if (!steps.length && !commits.length && !dispatches.length) {
      return null;
    }
    return (
      <details className="flow-runtime-advanced flow-trace-stack">
        <summary>
          <span>
            <strong>执行步骤和 Effect 明细</strong>
            <small>查看最近一次 run 的 step、commit 和 dispatch 原始记录。</small>
          </span>
          <em>{steps.length + commits.length + dispatches.length} rows</em>
        </summary>
        <div className="table-scroll compact-table-scroll flow-runtime-handler-table">
          <table>
            <caption className="sr-only">Flow 步骤运行结果</caption>
            <thead>
              <tr>
                <th scope="col">步骤</th>
                <th scope="col">所有者</th>
                <th scope="col">状态</th>
                <th scope="col">动作</th>
                <th scope="col">耗时</th>
                <th scope="col">原因</th>
              </tr>
            </thead>
            <tbody>
              {steps.length ? steps.map((step, index) => (
                <tr key={`${step.id || "step"}:${index}`}>
                  <td className="mono">{step.id || step.kind || "-"}</td>
                  <td className="mono">{step.owner || "-"}</td>
                  <td>{step.status || "-"}</td>
                  <td>{step.action || "-"}</td>
                  <td>{step.elapsed_ms === undefined ? "-" : step.elapsed_ms.toFixed(1)}</td>
                  <td>{step.error || step.reason || "-"}</td>
                </tr>
              )) : (
                <tr><td colSpan={6}>-</td></tr>
              )}
            </tbody>
          </table>
        </div>

        {(commits.length || dispatches.length) ? (
          <div className="table-scroll compact-table-scroll flow-runtime-handler-table">
            <table>
              <caption className="sr-only">Flow Effect 运行结果</caption>
              <thead>
                <tr>
                  <th scope="col">阶段</th>
                  <th scope="col">所有者</th>
                  <th scope="col">Effect</th>
                  <th scope="col">状态</th>
                  <th scope="col">提交</th>
                  <th scope="col">演练</th>
                  <th scope="col">错误</th>
                </tr>
              </thead>
              <tbody>
                {[...commits.map((item) => ({ ...item, stage: "commit" })), ...dispatches.map((item) => ({ ...item, stage: "dispatch" }))].map((item, index) => (
                  <tr key={`${item.stage}:${item.idempotency_key || index}`}>
                    <td>{item.stage}</td>
                    <td className="mono">{item.owner || "-"}</td>
                    <td className="mono">{item.type || "-"}</td>
                    <td>{item.status || "-"}</td>
                    <td>{item.commit_status || "-"}</td>
                    <td>{formatBool(item.dry_run)}</td>
                    <td>{item.error || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </details>
    );
  };
  const lastRuntimeSummary = flowResultSummary(runtimeResult);
  const lastShadowSummary = flowResultSummary(shadowResult);
  const selectedTraceId = effectTraceFilter.trim();
  const activeTraceAggregate = traceAggregate?.traceId === selectedTraceId ? traceAggregate : null;
  const selectedTraceRuntime = activeTraceAggregate?.runtimeResult
    || (selectedTraceId && runtimeResult?.trace_id === selectedTraceId ? runtimeResult : null);
  const selectedTraceShadow = activeTraceAggregate?.shadowResult
    || (selectedTraceId && shadowResult?.trace_id === selectedTraceId ? shadowResult : null);
  const selectedTraceRuntimeSource = activeTraceAggregate?.runtimeResult
    ? "Redis snapshot"
    : selectedTraceRuntime
      ? "最近 runtime"
      : "未命中";
  const selectedTraceShadowSource = activeTraceAggregate?.shadowResult
    ? "Redis snapshot"
    : selectedTraceShadow
      ? "最近 shadow"
      : "未命中";
  const selectedTraceSteps = [
    ...(selectedTraceRuntime?.steps || []).map((step) => ({ ...step, source: "runtime" })),
    ...(selectedTraceShadow?.steps || []).map((step) => ({ ...step, source: "shadow" })),
  ];
  const selectedTraceDispatches = [
    ...(selectedTraceRuntime?.effect_dispatches || []).map((item) => ({ ...item, source: "runtime" })),
    ...(selectedTraceShadow?.effect_dispatches || []).map((item) => ({ ...item, source: "shadow" })),
  ];
  const traceUsesWxbotReplyQueue = Boolean(activeTraceAggregate?.replyQueue.length)
    || activeTraceAggregate?.inbound.some((item) => item.channel === "wechat")
    || activeTraceAggregate?.outbound.some((item) => item.channel === "wechat");
  const traceAggregateStats = activeTraceAggregate
    ? [
        { label: "入站", value: activeTraceAggregate.inbound.length, detail: "inbound stream" },
        { label: "Flow Step", value: selectedTraceSteps.length, detail: selectedTraceSteps.length ? "snapshot/最近 trace" : "暂无 snapshot" },
        { label: "Effect", value: activeTraceAggregate.effects.length, detail: "audit log" },
        { label: "Handler", value: selectedTraceDispatches.length, detail: "dispatch trace" },
        { label: "回复队列", value: activeTraceAggregate.replyQueue.length, detail: "wxbot queue" },
        { label: "通用出站流", value: activeTraceAggregate.outbound.length, detail: "非 wxbot 回复队列" },
      ]
    : [];
  const traceFlowNodes = activeTraceAggregate
    ? [
        {
          label: "入站消息",
          status: activeTraceAggregate.inbound.length ? "hit" : "miss",
          count: activeTraceAggregate.inbound.length,
          detail: activeTraceAggregate.inbound[0]
            ? `${activeTraceAggregate.inbound[0].channel || "unknown"} · ${activeTraceAggregate.inbound[0].session_id || "no session"}`
            : "没有查到 inbound stream",
          meta: activeTraceAggregate.inbound[0] ? traceMessageSummary(activeTraceAggregate.inbound[0].payload) : "-",
        },
        {
          label: "Flow / Step",
          status: selectedTraceSteps.some((step) => step.status === "error" || step.error) ? "error" : selectedTraceSteps.length ? "hit" : "miss",
          count: selectedTraceSteps.length,
          detail: `${selectedTraceRuntimeSource} / ${selectedTraceShadowSource}`,
          meta: selectedTraceSteps.length
            ? compactList(selectedTraceSteps.map((step) => `${step.source}:${step.id || step.kind || step.status || "step"}`), 4)
            : "当前后端只保留最近 runtime/shadow trace",
        },
        {
          label: "Effect Commit",
          status: activeTraceAggregate.effects.some((item) => item.status === "error" || item.status === "handler_error") ? "error" : activeTraceAggregate.effects.length ? "hit" : "miss",
          count: activeTraceAggregate.effects.length,
          detail: activeTraceAggregate.effects.length ? compactList(activeTraceAggregate.effects.map((item) => `${item.owner || "-"}.${item.type || "-"}`), 3) : "没有 effect audit",
          meta: activeTraceAggregate.effects.length ? compactList(activeTraceAggregate.effects.map((item) => item.status || "unknown"), 4) : "-",
        },
        {
          label: "Handler Dispatch",
          status: selectedTraceDispatches.some((item) => item.status === "handler_error" || item.error) ? "error" : selectedTraceDispatches.length ? "hit" : "miss",
          count: selectedTraceDispatches.length,
          detail: selectedTraceDispatches.length ? compactList(selectedTraceDispatches.map((item) => `${item.source}:${item.owner || "-"}.${item.type || "-"}`), 3) : "没有 handler dispatch",
          meta: selectedTraceDispatches.length ? compactList(selectedTraceDispatches.map((item) => item.error || item.status || item.commit_status || "unknown"), 4) : "-",
        },
        {
          label: "回复队列",
          status: activeTraceAggregate.replyQueue.some((item) => item.status === "failed" || item.error) ? "error" : activeTraceAggregate.replyQueue.length ? "hit" : "miss",
          count: activeTraceAggregate.replyQueue.length,
          detail: activeTraceAggregate.replyQueue[0]?.status || "没有 wxbot reply queue",
          meta: activeTraceAggregate.replyQueue[0] ? replyQueueSummary(activeTraceAggregate.replyQueue[0]) : "-",
        },
        {
          label: "通用出站流",
          status: activeTraceAggregate.outbound.length ? "hit" : "miss",
          count: activeTraceAggregate.outbound.length,
          detail: activeTraceAggregate.outbound[0]
            ? `${activeTraceAggregate.outbound[0].reason || "outbound"} · attempts=${activeTraceAggregate.outbound[0].attempts ?? "-"}`
            : traceUsesWxbotReplyQueue
              ? "wxbot 回复走回复队列，不写通用出站流"
              : "没有查到通用出站流",
          meta: activeTraceAggregate.outbound[0]
            ? traceMessageSummary(activeTraceAggregate.outbound[0].payload)
            : traceUsesWxbotReplyQueue
              ? "已由 wxbot reply queue/SDK bridge 处理"
              : "-",
        },
      ]
    : [];
  const traceFlowHasError = traceFlowNodes.some((node) => node.status === "error");
  const traceEventState = (status?: string | null, hasError = false, dryRun = false): TraceEventCard["state"] => {
    const normalized = String(status || "").toLowerCase();
    if (hasError || normalized.includes("error") || normalized.includes("fail")) {
      return "error";
    }
    if (dryRun) {
      return "dry";
    }
    return "hit";
  };
  const traceIdentityRows = activeTraceAggregate
    ? [
        { label: "tenant", value: activeTraceAggregate.inbound[0]?.tenant_id || activeTraceAggregate.outbound[0]?.tenant_id || "-" },
        { label: "session", value: activeTraceAggregate.inbound[0]?.session_id || activeTraceAggregate.replyQueue[0]?.session_id || activeTraceAggregate.outbound[0]?.session_id || "-" },
        { label: "user", value: activeTraceAggregate.inbound[0]?.user_id || "-" },
        { label: "channel", value: activeTraceAggregate.inbound[0]?.channel || activeTraceAggregate.outbound[0]?.channel || "-" },
      ]
    : [];
  const traceRuntimeCards = activeTraceAggregate
    ? [
        {
          label: "Runtime",
          value: selectedTraceRuntime?.status || "未命中",
          detail: selectedTraceRuntimeSource,
          state: selectedTraceRuntime?.error ? "error" : selectedTraceRuntime ? "hit" : "miss",
        },
        {
          label: "Shadow",
          value: selectedTraceShadow?.status || "未命中",
          detail: selectedTraceShadowSource,
          state: selectedTraceShadow?.error ? "error" : selectedTraceShadow ? "hit" : "miss",
        },
      ]
    : [];
  const traceInboundEvents: TraceEventCard[] = activeTraceAggregate
    ? activeTraceAggregate.inbound.map((item, index) => ({
        key: `in:${item.stream_key || item.stream || "stream"}:${item.id}:${index}`,
        eyebrow: formatTraceTime(item.created_ts_ms),
        title: `${item.channel || "unknown"} 入站`,
        status: item.stream_key || item.stream || "stream",
        state: "hit",
        detail: traceMessageSummary(item.payload),
        meta: [
          `session ${item.session_id || "-"}`,
          `user ${item.user_id || "-"}`,
          `keys ${compactList(payloadKeys(item.payload), 6)}`,
        ],
        chips: [item.source || "inbound", item.tenant_id || "tenant -"],
      }))
    : [];
  const traceStepEvents: TraceEventCard[] = selectedTraceSteps.map((step, index) => ({
    key: `step:${step.source}:${step.id || step.kind || "step"}:${index}`,
    eyebrow: step.source,
    title: step.id || step.kind || "Flow step",
    status: step.status || "-",
    state: traceEventState(step.status, Boolean(step.error)),
    detail: step.error || step.reason || step.action || "无错误原因",
    meta: [
      `owner ${step.owner || "-"}`,
      step.elapsed_ms === undefined ? "elapsed -" : `elapsed ${step.elapsed_ms.toFixed(1)}ms`,
      step.attempts === undefined ? "attempts -" : `attempts ${step.attempts}`,
    ],
    chips: [step.kind || "step", step.action || "action -"],
  }));
  const traceEffectEvents: TraceEventCard[] = activeTraceAggregate
    ? [
        ...activeTraceAggregate.effects.map((item, index) => ({
          key: `effect:audit:${item.idempotency_key || item.id || index}`,
          eyebrow: item.created_at || "audit log",
          title: `${item.owner || "-"}.${item.type || "-"}`,
          status: item.status || "-",
          state: traceEventState(item.status, false, item.dry_run),
          detail: `${item.payload_size ?? 0} bytes payload`,
          meta: [
            `keys ${compactList(item.payload_keys || [], 6)}`,
            item.idempotency_key ? `key ${item.idempotency_key}` : "key -",
          ],
          chips: ["audit", item.dry_run ? "dry-run" : "commit"],
        })),
        ...selectedTraceDispatches.map((item, index) => ({
          key: `effect:handler:${item.source}:${item.idempotency_key || index}`,
          eyebrow: `${item.source} handler`,
          title: `${item.owner || "-"}.${item.type || "-"}`,
          status: item.status || item.commit_status || "-",
          state: traceEventState(item.status || item.commit_status, Boolean(item.error), item.dry_run),
          detail: item.error || item.idempotency_key || "handler dispatch trace",
          meta: [
            `commit ${item.commit_status || "-"}`,
            `dry ${formatBool(item.dry_run)}`,
          ],
          chips: ["handler", item.source],
        })),
      ]
    : [];
  const traceDeliveryEvents: TraceEventCard[] = activeTraceAggregate
    ? [
        ...activeTraceAggregate.replyQueue.map((item, index) => ({
          key: `reply:${item.id || index}`,
          eyebrow: item.sent_at || item.queued_at || item.created_at || "reply queue",
          title: "wxbot 回复队列",
          status: item.status || "-",
          state: traceEventState(item.status, Boolean(item.error)),
          detail: item.error || replyQueueSummary(item),
          meta: [
            `session ${item.session_id || "-"}`,
            `attempt ${item.attempt_count ?? "-"}`,
            item.sdk_outbound_id ? `sdk ${item.sdk_outbound_id}` : "sdk -",
          ],
          chips: ["reply_queue", item.command_id || "command -"],
        })),
        ...activeTraceAggregate.outbound.map((item, index) => ({
          key: `out:${item.stream_key || item.stream || "stream"}:${item.id}:${index}`,
          eyebrow: formatTraceTime(item.created_ts_ms),
          title: "通用出站流",
          status: item.reason || "outbound",
          state: "hit" as const,
          detail: traceMessageSummary(item.payload),
          meta: [
            `session ${item.session_id || "-"}`,
            `attempt ${item.attempts ?? "-"}`,
            `keys ${compactList(payloadKeys(item.payload), 6)}`,
          ],
          chips: [item.channel || "channel -", item.stream_key || item.stream || "stream"],
        })),
      ]
    : [];
  const traceEmptyDeliveryText = traceUsesWxbotReplyQueue
    ? "wxbot 回复已经进入回复队列；没有额外写通用出站流。"
    : "没有查到回复队列或通用出站流记录。";
  const traceAggregateErrors = activeTraceAggregate?.errors || [];
  const renderTraceEventCards = (events: TraceEventCard[]) => events.map((event) => (
    <article className={`flow-trace-event-card is-${event.state}`} key={event.key}>
      <div className="flow-trace-event-main">
        <span>{event.eyebrow}</span>
        <strong>{event.title}</strong>
        <small>{event.detail}</small>
      </div>
      <div className="flow-trace-event-side">
        <span className={`plugin-badge ${event.state === "error" ? "is-danger" : event.state === "miss" ? "is-muted" : ""}`}>
          {event.status}
        </span>
        <div className="flow-trace-event-chips">
          {event.chips.map((chip) => <span key={chip}>{chip}</span>)}
        </div>
      </div>
      <div className="flow-trace-event-meta">
        {event.meta.map((item) => <span key={item}>{item}</span>)}
      </div>
    </article>
  ));
  const renderTraceEventSection = (title: string, subtitle: string, events: TraceEventCard[], emptyText: string, previewLimit?: number) => {
    const eventLimit = previewLimit ?? 4;
    const visibleEvents = events.slice(0, eventLimit);
    const overflowEvents = events.slice(eventLimit);
    const hasError = events.some((event) => event.state === "error");
    const firstEvent = events[0];
    const summaryText = firstEvent ? `${firstEvent.title} · ${firstEvent.status} · ${firstEvent.detail}` : emptyText;

    return (
      <details key={`${selectedTraceId}:${subtitle}:${hasError ? "error" : "ok"}`} className={`flow-trace-lane ${hasError ? "has-error" : ""}`} open={hasError || undefined}>
        <summary className="flow-trace-lane-header">
          <div className="flow-trace-lane-title">
            <span>{subtitle}</span>
            <h5>{title}</h5>
            <small>{summaryText}</small>
          </div>
          <div className="flow-trace-lane-status">
            {hasError && <span className="plugin-badge is-danger">error</span>}
            <strong>{events.length}</strong>
            <span className="flow-trace-lane-toggle" aria-hidden="true" />
          </div>
        </summary>
        {events.length ? (
          <div className="flow-trace-lane-body">
            <div className="flow-trace-event-list">
              {renderTraceEventCards(visibleEvents)}
            </div>
            {overflowEvents.length ? (
              <details className="flow-trace-overflow-drawer">
                <summary>展开剩余 {overflowEvents.length} 条</summary>
                <div className="flow-trace-event-list">
                  {renderTraceEventCards(overflowEvents)}
                </div>
              </details>
            ) : null}
          </div>
        ) : (
          <div className="flow-trace-lane-body">
            <div className="flow-trace-empty">{emptyText}</div>
          </div>
        )}
      </details>
    );
  };
  const runtimeRows = [
    { label: "Runtime", value: runtimeConfig?.name || "-", state: runtimeStatus, detail: runtimeConfig?.enabled ? "接管主链路" : "未接管" },
    { label: "Shadow", value: shadowConfig?.name || "-", state: shadowStatus, detail: shadowConfig?.enabled ? shadowConfig.mode || "shadow" : "未运行" },
    { label: "Commit", value: effectCommitConfig?.backend || "none", state: effectCommitStatus, detail: effectCommitEnabled ? "幂等门闩启用" : "未启用" },
    { label: "Handlers", value: handlerMode, state: handlerStatus, detail: handlerExecutionDetail },
    { label: "Audit", value: effectCommitConfig?.log_backend || "none", state: auditStatus, detail: auditStatus === "enabled" ? effectCommitConfig?.log_failure_policy || "fail_closed" : "未写入" },
  ];
  const pipelineNodes = [
    { label: "入站消息", state: "enabled", detail: readyzFlow?.status === "ready" ? "ready" : readyzFlow?.status || "-" },
    { label: "FlowRunner", state: runtimeStatus === "error" ? "error" : runtimeStatus, detail: runtimeConfig?.enabled ? runtimeConfig.name || "-" : "未接管" },
    { label: shadowConfig?.enabled ? "Shadow 对照" : "Shadow 关闭", state: shadowStatus, detail: shadowConfig?.mode || "noop" },
    { label: "Effect Commit", state: effectCommitStatus, detail: effectCommitConfig?.backend || "none" },
    { label: "Effect Handlers", state: handlerStatus, detail: effectCommitConfig?.handlers_enabled ? handlerScopeText : "no dispatch" },
    { label: "Audit Log", state: auditStatus, detail: effectCommitConfig?.log_backend || "none" },
  ];
  const rolloutStage = runtimeConfig?.enabled
    ? "FlowRunner 已接管主链路"
    : shadowConfig?.enabled
      ? "Shadow 验证中"
      : "Legacy 主链路运行中";
  const rolloutStatusText = runtimeStatus === "error" || effectCommitStatus === "error"
    ? "配置需要修正"
    : runtimeConfig?.enabled
      ? "真实流量已切到 FlowRunner"
      : shadowConfig?.enabled
        ? "正在旁路对照，不影响真实回复"
        : "生产消息仍由旧 DialogOrchestrator 处理";
  const rolloutFocus = runtimeConfig?.enabled
    ? "重点看最近 Runtime、Effect Audit 和风险提示；danger 需要马上处理，duplicate 通常是幂等命中。"
    : shadowConfig?.enabled
      ? "当前仍未接管真实流量，重点对照 shadow trace 与真实回复是否一致。"
      : "当前页面只显示旧链路状态；开启 shadow/runtime 后才会出现 FlowRunner trace。";
  const readinessChecks = [
    {
      label: "主链路",
      value: runtimeConfig?.enabled ? "FlowRunner" : "Legacy",
      state: runtimeStatus,
      detail: runtimeConfig?.enabled ? runtimeConfig?.name || "-" : "旧链路仍在处理真实消息",
    },
    {
      label: "旁路验证",
      value: shadowConfig?.enabled ? "开启" : "关闭",
      state: shadowStatus,
      detail: shadowConfig?.enabled ? shadowConfig?.mode || "shadow" : "未对照 FlowRunner",
    },
    {
      label: "副作用幂等",
      value: effectCommitEnabled ? effectCommitConfig?.backend || "-" : "未启用",
      state: effectCommitStatus,
      detail: effectCommitEnabled ? "effect 会先经过 commit gate" : "副作用主要仍由 executor/hook 直接执行",
    },
    {
      label: "Handler 执行",
      value: effectCommitConfig?.handlers_enabled ? handlerMode : "关闭",
      state: handlerStatus,
      detail: handlerExecutionDetail,
    },
    {
      label: "Audit",
      value: auditStatus === "enabled" ? effectCommitConfig?.log_backend || "-" : "未启用",
      state: auditStatus,
      detail: auditStatus === "enabled" ? "写入 flow_effect_log" : "不会持久记录 effect audit",
    },
  ];
  const stateLabel = (state: string) => {
    if (state === "enabled") return "启用";
    if (state === "error") return "异常";
    if (state === "ok") return "正常";
    return "关闭";
  };


  return (
    <>
      <section className="panel panel-scroll plugins-flow-panel span-3">
        <details className="plugins-flow-details">
        <summary className="panel-header">
          <div>
            <p className="section-kicker">消息流运行状态</p>
            <h3>Flow / Effect 运行视图</h3>
          </div>
          <div className="action-row">
            <button
              className="button button-secondary"
              type="button"
              onClick={(event) => {
                event.preventDefault();
                void loadFlowRuntimeStatus();
              }}
            >
              {flowLoading ? "刷新中..." : "刷新 Runtime"}
            </button>
          </div>
        </summary>
        {flowError && <p className="muted-copy">{flowError}</p>}
        <div className="flow-runtime-board">
          <div className="flow-runtime-command-center">
            <div className={`flow-runtime-overview is-${runtimeStatus === "error" || effectCommitStatus === "error" ? "danger" : runtimeConfig?.enabled || shadowConfig?.enabled ? "active" : "muted"}`}>
            <div>
              <span>当前阶段</span>
              <strong>{rolloutStage}</strong>
              <small>{rolloutStatusText}</small>
            </div>
            <div>
              <span>观察重点</span>
              <strong>{runtimeStatus === "error" || effectCommitStatus === "error" ? "先修配置" : runtimeConfig?.enabled ? "运行中" : shadowConfig?.enabled ? "对照中" : "未接管"}</strong>
              <small>{rolloutFocus}</small>
            </div>
          </div>

          <div className={`flow-runtime-risk is-${flowRiskLevel}`}>
            <div>
              <strong>{flowRiskLevel === "ok" ? "Effect 链路未发现近期风险" : "Effect 链路近期风险"}</strong>
              <span>{flowRiskLevel === "ok" ? "最近 runtime/shadow dispatch 和 audit summary 没有异常状态" : "优先处理 danger，再确认 warning 是否符合预期"}</span>
            </div>
            <div className="flow-runtime-risk-list">
              {effectRiskItems.length ? effectRiskItems.map((item) => (
                <span className={`flow-runtime-risk-chip is-${item.level}`} key={item.label}>
                  <strong>{item.count}</strong>
                  {item.label}
                  <small>{item.detail}</small>
                </span>
              )) : (
                <span className="flow-runtime-risk-chip is-ok">
                  <strong>0</strong>
                  risk
                  <small>无异常状态</small>
                </span>
              )}
            </div>
          </div>
          </div>

          <div className="flow-runtime-diagnostics">
            <div className="flow-readiness-grid">
              {readinessChecks.map((item) => (
              <article className={`flow-readiness-card is-${item.state}`} key={item.label}>
                <span>{item.label}</span>
                <strong>{item.value}</strong>
                <small>{item.detail}</small>
              </article>
            ))}
          </div>

            <div className="flow-runtime-filter">
              <div>
                <strong>Effect Filters</strong>
                <span className="mono">trace={effectTraceFilter || "all"} · {effectAuditFilterLabel}</span>
              </div>
              <div className="flow-runtime-filter-actions">
                <button className="button button-secondary" onClick={clearEffectTraceFilter} disabled={!effectTraceFilter || flowLoading}>
                  清除 Trace
                </button>
                <button className="button button-secondary" onClick={clearEffectAuditFilters} disabled={!hasEffectAuditFilters || flowLoading}>
                  清除 Effect
                </button>
                <button className="button button-secondary" onClick={clearAllEffectFilters} disabled={(!effectTraceFilter && !hasEffectAuditFilters) || flowLoading}>
                  清除全部
                </button>
              </div>
            </div>
          </div>

          <div className="flow-runtime-results">
            <article className={`flow-run-card ${runtimeResult?.error ? "has-error" : ""}`}>
              <div className="plugin-card-header">
                <div>
                  <strong>最近 Runtime</strong>
                  <span>{lastRuntimeSummary.flowName} · {lastRuntimeSummary.sessionId}</span>
                </div>
                <span className={`plugin-badge ${runtimeResult?.error ? "is-danger" : runtimeResult?.status ? "" : "is-muted"}`}>
                  {lastRuntimeSummary.status}
                </span>
              </div>
              <div className="flow-run-metrics">
                <span><strong>{lastRuntimeSummary.stepCount}</strong> steps</span>
                <span><strong>{lastRuntimeSummary.commitCount}</strong> commits</span>
                <span><strong>{lastRuntimeSummary.dispatchCount}</strong> dispatches</span>
              </div>
              <div className="flow-run-trace-row">
                <span>trace</span>
                {runtimeResult?.trace_id ? <button className="link-button mono" onClick={() => selectEffectTrace(runtimeResult.trace_id)}>{lastRuntimeSummary.traceId}</button> : <strong>-</strong>}
              </div>
              {(lastRuntimeSummary.stopReason !== "-" || lastRuntimeSummary.error !== "-") && (
                <p className="flow-run-note">{lastRuntimeSummary.error !== "-" ? lastRuntimeSummary.error : lastRuntimeSummary.stopReason}</p>
              )}
              {renderFlowTraceTables(runtimeResult)}
            </article>

            <article className={`flow-run-card ${shadowResult?.error ? "has-error" : ""}`}>
              <div className="plugin-card-header">
                <div>
                  <strong>最近 Shadow</strong>
                  <span>{lastShadowSummary.flowName} · {lastShadowSummary.sessionId}</span>
                </div>
                <span className={`plugin-badge ${shadowResult?.error ? "is-danger" : shadowResult?.status ? "" : "is-muted"}`}>
                  {lastShadowSummary.status}
                </span>
              </div>
              <div className="flow-run-metrics">
                <span><strong>{lastShadowSummary.stepCount}</strong> steps</span>
                <span><strong>{lastShadowSummary.commitCount}</strong> commits</span>
                <span><strong>{lastShadowSummary.dispatchCount}</strong> dispatches</span>
              </div>
              <div className="flow-run-trace-row">
                <span>trace</span>
                {shadowResult?.trace_id ? <button className="link-button mono" onClick={() => selectEffectTrace(shadowResult.trace_id)}>{lastShadowSummary.traceId}</button> : <strong>-</strong>}
              </div>
              {(lastShadowSummary.stopReason !== "-" || lastShadowSummary.error !== "-") && (
                <p className="flow-run-note">{lastShadowSummary.error !== "-" ? lastShadowSummary.error : lastShadowSummary.stopReason}</p>
              )}
              {renderFlowTraceTables(shadowResult)}
            </article>
          </div>

          {selectedTraceId && (
            <article className="flow-trace-aggregate">
              <div className="flow-runtime-detail-header">
                <div>
                  <h4>单条 Trace 聚合</h4>
                  <p className="muted-copy">
                    按 trace 汇总入站、Flow step、Effect、Handler、回复队列和通用出站流记录；payload 只显示 keys 和脱敏摘要。
                  </p>
                </div>
                <div className="flow-trace-header-actions">
                  <button className="button button-secondary" onClick={() => setTraceFlowModalOpen(true)} disabled={!activeTraceAggregate}>
                    查看流转图
                  </button>
                  <span className={`plugin-badge ${traceAggregateLoading ? "is-muted" : ""}`}>
                    {traceAggregateLoading ? "loading" : selectedTraceId}
                  </span>
                </div>
              </div>
              {traceAggregateError && <p className="muted-copy">{traceAggregateError}</p>}
              {activeTraceAggregate ? (
                <>
                  <div className="flow-trace-stat-grid">
                    {traceAggregateStats.map((item) => (
                      <div className="flow-trace-stat" key={item.label}>
                        <span>{item.label}</span>
                        <strong>{item.value}</strong>
                        <small>{item.detail}</small>
                      </div>
                    ))}
                  </div>

                  <div className="flow-trace-path" aria-label="追踪聚合流转阶段">
                    {traceFlowNodes.map((node, index) => (
                      <div className="flow-trace-path-item" key={node.label}>
                        <div className={`flow-trace-path-card is-${node.status}`}>
                          <div>
                            <span>{node.label}</span>
                            <strong>{node.count}</strong>
                          </div>
                          <small>{node.detail}</small>
                          <em>{node.meta}</em>
                        </div>
                        {index < traceFlowNodes.length - 1 && <span className="flow-trace-path-arrow">→</span>}
                      </div>
                    ))}
                  </div>

                  <div className="flow-trace-dossier">
                    <section className="flow-trace-identity-card">
                      <div>
                        <span>消息身份</span>
                        <h5>Trace Context</h5>
                      </div>
                      <dl>
                        {traceIdentityRows.map((item) => (
                          <div key={item.label}>
                            <dt>{item.label}</dt>
                            <dd className="mono">{item.value}</dd>
                          </div>
                        ))}
                      </dl>
                    </section>

                    <section className="flow-trace-runtime-card">
                      <div>
                        <span>运行快照</span>
                        <h5>Runtime / Shadow</h5>
                      </div>
                      <div className="flow-trace-runtime-grid">
                        {traceRuntimeCards.map((item) => (
                          <article className={`is-${item.state}`} key={item.label}>
                            <span>{item.label}</span>
                            <strong>{item.value}</strong>
                            <small>{item.detail}</small>
                          </article>
                        ))}
                      </div>
                    </section>

                    <section className={`flow-trace-health-card is-${traceFlowHasError || traceAggregateErrors.length ? "error" : "ok"}`}>
                      <span>排查焦点</span>
                      <h5>{traceFlowHasError || traceAggregateErrors.length ? "这条链路需要处理" : "链路状态稳定"}</h5>
                      <p>{traceFlowHasError ? "优先看红色事件卡片里的 error/status 字段。" : "默认视图只展示关键摘要；字段级核对放在下方原始明细。"}</p>
                      {traceAggregateErrors.length ? (
                        <div className="flow-trace-error-list">
                          {traceAggregateErrors.map((item) => <span key={item}>{item}</span>)}
                        </div>
                      ) : null}
                    </section>
                  </div>

                  <div className="flow-trace-lane-grid">
                    {renderTraceEventSection("入站消息", "Inbound", traceInboundEvents, "没有查到该 trace 的入站 stream 记录。")}
                    {renderTraceEventSection("Flow / Step", "Runtime + Shadow", traceStepEvents, "当前后端只保留最近 runtime/shadow trace；这条 trace 可以继续看 Effect Audit 和回复队列。", 6)}
                    {renderTraceEventSection("Effect / Handler", "Audit + Dispatch", traceEffectEvents, "没有查到 effect audit 或 handler dispatch。")}
                    {renderTraceEventSection("Reply / Outbound", "Delivery", traceDeliveryEvents, traceEmptyDeliveryText)}
                  </div>

                  <details className="flow-runtime-advanced flow-trace-raw-drawer">
                    <summary>
                      <span>
                        <strong>原始聚合明细</strong>
                        <small>用于字段级核对；默认先看上方事件卡片定位问题。</small>
                      </span>
                      <em>{traceInboundEvents.length + traceStepEvents.length + traceEffectEvents.length + traceDeliveryEvents.length} rows</em>
                    </summary>
                    <div className="flow-trace-raw-grid">
                      <section className="flow-trace-detail-card">
                        <h5>入站消息</h5>
                        <dl className="plugin-meta-list">
                          {activeTraceAggregate.inbound.length ? activeTraceAggregate.inbound.map((item) => (
                            <div key={`in:${item.stream_key || item.stream}:${item.id}`}>
                              <dt>{formatTraceTime(item.created_ts_ms)}</dt>
                              <dd>{item.channel || "-"} · {item.session_id || "-"} · keys {compactList(payloadKeys(item.payload), 6)}</dd>
                            </div>
                          )) : <div><dt>-</dt><dd>没有查到该 trace 的入站 stream 记录</dd></div>}
                        </dl>
                      </section>
                      <section className="flow-trace-detail-card is-tall">
                        <h5>Flow / Step</h5>
                        <dl className="plugin-meta-list">
                          {selectedTraceSteps.length ? selectedTraceSteps.map((step, index) => (
                            <div key={`step:${step.source}:${step.id || step.kind}:${index}`}>
                              <dt>{step.source}</dt>
                              <dd>{step.id || step.kind || "-"} · {step.status || "-"} · {step.error || step.reason || step.action || "-"}</dd>
                            </div>
                          )) : <div><dt>-</dt><dd>当前后端只保留最近 runtime/shadow trace</dd></div>}
                        </dl>
                      </section>
                      <section className="flow-trace-detail-card is-tall">
                        <h5>Effect / Handler</h5>
                        <dl className="plugin-meta-list">
                          {traceEffectEvents.length ? traceEffectEvents.map((item) => (
                            <div key={item.key}>
                              <dt>{item.status}</dt>
                              <dd>{item.title} · {item.detail}</dd>
                            </div>
                          )) : <div><dt>-</dt><dd>没有查到 effect audit 或 handler dispatch</dd></div>}
                        </dl>
                      </section>
                      <section className="flow-trace-detail-card">
                        <h5>Reply Queue / 通用出站流</h5>
                        <dl className="plugin-meta-list">
                          {traceDeliveryEvents.length ? traceDeliveryEvents.map((item) => (
                            <div key={item.key}>
                              <dt>{item.status}</dt>
                              <dd>{item.title} · {item.detail}</dd>
                            </div>
                          )) : <div><dt>-</dt><dd>{traceEmptyDeliveryText}</dd></div>}
                        </dl>
                      </section>
                    </div>
                  </details>
                </>
              ) : (
                <p className="muted-copy">{traceAggregateLoading ? "正在加载该 trace..." : "暂无该 trace 的聚合数据。"}</p>
              )}
            </article>
          )}

          <details className="flow-runtime-advanced flow-runtime-map">
            <summary>
              <span>
                <strong>底层 Pipeline 和运行组件状态</strong>
              </span>
              <span className="flow-runtime-summary-action">
                <em>{runtimeConfig?.enabled ? "FlowRunner" : shadowConfig?.enabled ? "Shadow" : "Legacy"}</em>
                <b aria-hidden="true" />
              </span>
            </summary>
            <div className="flow-runtime-map-body">
              <section className="flow-runtime-map-section">
                <div className="flow-runtime-map-section-header">
                  <h4>Pipeline 接线</h4>
                  <span>{pipelineNodes.length} nodes</span>
                </div>
                <div className="flow-runtime-pipeline" aria-label="消息流运行管线">
                  {pipelineNodes.map((node, index) => (
                    <div className="flow-runtime-node-wrap" key={`${node.label}-${index}`}>
                      <div className={`flow-runtime-node is-${node.state}`}>
                        <strong>{node.label}</strong>
                        <span>{node.detail}</span>
                      </div>
                      {index < pipelineNodes.length - 1 && <span className="flow-runtime-arrow">→</span>}
                    </div>
                  ))}
                </div>
              </section>

              <section className="flow-runtime-map-section">
                <div className="flow-runtime-map-section-header">
                  <h4>运行组件状态</h4>
                  <span>{runtimeRows.length} items</span>
                </div>
                <div className="flow-runtime-summary-grid">
                  {runtimeRows.map((row) => (
                    <article className={`flow-runtime-summary is-${row.state}`} key={row.label}>
                      <span>{row.label}</span>
                      <strong>{row.value}</strong>
                      <small>{stateLabel(row.state)} · {row.detail}</small>
                    </article>
                  ))}
                </div>
              </section>
            </div>
          </details>

          <details className="flow-runtime-advanced flow-runtime-detail-drawer">
            <summary>
              <span>
                <strong>Runtime / Effect 配置和审计明细</strong>
                <small>默认收起，用于字段级配置核对、Effect summary 和 audit log 原始表。</small>
              </span>
              <span className="flow-runtime-summary-action">
                <em>{effectSummaryData?.total ?? 0} audit</em>
                <b aria-hidden="true" />
              </span>
            </summary>
            <div className="flow-runtime-detail-grid">
            <article className="flow-runtime-detail">
              <h4>Runtime 配置</h4>
              <dl className="plugin-meta-list">
                <div><dt>enabled</dt><dd>{formatBool(runtimeConfig?.enabled)}</dd></div>
                <div><dt>flow</dt><dd>{runtimeConfig?.name || "-"}</dd></div>
                <div><dt>allowed</dt><dd>{formatBool(runtimeConfig?.allowed)}</dd></div>
                <div><dt>reason</dt><dd>{runtimeConfig?.reason || "-"}</dd></div>
                <div><dt>allowed_names</dt><dd>{formatList(runtimeConfig?.allowed_names)}</dd></div>
                <div><dt>允许目标 Flow</dt><dd>{formatBool(runtimeConfig?.allow_target_flows)}</dd></div>
                <div><dt>允许兼容回退</dt><dd>{formatBool(runtimeConfig?.allow_compatible_fallback)}</dd></div>
              </dl>
            </article>

            <article className="flow-runtime-detail">
              <h4>Shadow 配置</h4>
              <dl className="plugin-meta-list">
                <div><dt>enabled</dt><dd>{formatBool(shadowConfig?.enabled)}</dd></div>
                <div><dt>flow</dt><dd>{shadowConfig?.name || "-"}</dd></div>
                <div><dt>mode</dt><dd>{shadowConfig?.mode || "-"}</dd></div>
                <div><dt>core_preview</dt><dd>{formatBool(shadowConfig?.core_preview_enabled)}</dd></div>
                <div><dt>plugin_dry_run</dt><dd>{formatBool(shadowConfig?.plugin_dry_run_enabled)}</dd></div>
                <div><dt>effect_dry_run</dt><dd>{formatBool(shadowConfig?.effect_dry_run_enabled)}</dd></div>
              </dl>
            </article>

            <article className="flow-runtime-detail">
              <h4>Effect 后端</h4>
              <dl className="plugin-meta-list">
                <div><dt>commit_backend</dt><dd>{effectCommitConfig?.backend || "-"}</dd></div>
                <div><dt>handlers</dt><dd>{formatBool(effectCommitConfig?.handlers_enabled)}</dd></div>
                <div><dt>handler_mode</dt><dd>{handlerMode}</dd></div>
                <div><dt>allowlist</dt><dd>{handlerAllowlistText}</dd></div>
                <div><dt>handler_safe</dt><dd>{formatBool(effectCommitConfig?.handlers_commit_backend_safe)}</dd></div>
                <div><dt>handler_count</dt><dd>{effectHandlers?.count ?? 0}</dd></div>
                <div><dt>audit_log</dt><dd>{effectCommitConfig?.log_backend || "-"}</dd></div>
                <div><dt>log_policy</dt><dd>{effectCommitConfig?.log_failure_policy || "-"}</dd></div>
                <div><dt>ttl_seconds</dt><dd>{effectCommitConfig?.ttl_seconds ?? "-"}</dd></div>
                <div><dt>stream</dt><dd>{effectCommitConfig?.stream || "-"}</dd></div>
              </dl>
            </article>

            <article className="flow-runtime-detail is-wide">
              <div className="flow-runtime-detail-header">
                <h4>Effect Handlers</h4>
                <span className={`plugin-badge ${effectCommitConfig?.handlers_enabled ? "" : "is-muted"}`}>
                  {effectCommitConfig?.handlers_enabled ? handlerScopeText : "dispatch off"}
                </span>
              </div>
              <div className="table-scroll compact-table-scroll flow-runtime-handler-table">
                <table>
                  <caption className="sr-only">Effect 处理器注册表</caption>
                  <thead>
                    <tr>
                      <th scope="col">所有者</th>
                      <th scope="col">Effect</th>
                      <th scope="col">处理器</th>
                      <th scope="col">启用</th>
                      <th scope="col">回退</th>
                    </tr>
                  </thead>
                  <tbody>
                    {effectHandlerItems.length ? effectHandlerItems.map((item) => {
                      const fallback = effectHandlerFallbacks.find(
                        (entry) => entry.owner === item.owner && entry.type === item.type,
                      );
                      return (
                        <tr key={`${item.owner || "-"}:${item.type || "-"}`}>
                          <td className="mono">{item.owner || "-"}</td>
                          <td className="mono">{item.type || "-"}</td>
                          <td>{item.handler || "-"}</td>
                          <td>{handlerIsAllowed(item.owner, item.type) ? "yes" : "no"}</td>
                          <td>{fallback?.fallback_for || "-"}</td>
                        </tr>
                      );
                    }) : (
                      <tr>
                        <td colSpan={5}>-</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </article>

            <article className="flow-runtime-detail is-wide">
              <div className="flow-runtime-detail-header">
                <h4>Effect Summary</h4>
                <span className={`plugin-badge ${effectSummary?.enabled ? "" : "is-muted"}`}>
                  {effectSummaryData?.total ?? 0} total
                </span>
              </div>
              <div className="flow-effect-summary-grid">
                <div>
                  <strong>Status</strong>
                  <dl className="plugin-meta-list">
                    {effectStatusRows.length ? effectStatusRows.map((item) => (
                      <div key={item.status || "unknown"}><dt>{item.status || "-"}</dt><dd>{item.count ?? 0}</dd></div>
                    )) : <div><dt>-</dt><dd>0</dd></div>}
                  </dl>
                </div>
                <div>
                  <strong>Dry-run</strong>
                  <dl className="plugin-meta-list">
                    {effectDryRunRows.length ? effectDryRunRows.map((item) => (
                      <div key={String(item.dry_run)}><dt>{formatBool(item.dry_run)}</dt><dd>{item.count ?? 0}</dd></div>
                    )) : <div><dt>-</dt><dd>0</dd></div>}
                  </dl>
                </div>
              </div>
              <div className="table-scroll compact-table-scroll flow-runtime-handler-table">
                <table>
                  <caption className="sr-only">Effect 运行摘要</caption>
                  <thead>
                    <tr>
                      <th scope="col">所有者</th>
                      <th scope="col">Effect</th>
                      <th scope="col">状态</th>
                      <th scope="col">演练</th>
                      <th scope="col">数量</th>
                      <th scope="col">筛选</th>
                    </tr>
                  </thead>
                  <tbody>
                    {effectMatrixRows.length ? effectMatrixRows.map((item, index) => (
                      <tr key={`${item.owner || "-"}:${item.type || "-"}:${item.status || "-"}:${String(item.dry_run)}:${index}`}>
                        <td className="mono">{item.owner || "-"}</td>
                        <td className="mono">{item.type || "-"}</td>
                        <td>{item.status || "-"}</td>
                        <td>{formatBool(item.dry_run)}</td>
                        <td>{item.count ?? 0}</td>
                        <td>
                          <button className="link-button" onClick={() => selectEffectAuditFilters(item)}>
                            应用
                          </button>
                        </td>
                      </tr>
                    )) : (
                      <tr>
                        <td colSpan={6}>-</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </article>

            <article className="flow-runtime-detail is-wide">
              <div className="flow-runtime-detail-header">
                <h4>Effect Audit</h4>
                <span className={`plugin-badge ${effectLog?.enabled ? "" : "is-muted"}`}>
                  {effectLog?.backend || "none"}
                </span>
              </div>
              <div className="table-scroll compact-table-scroll flow-runtime-handler-table">
                <table>
                  <caption className="sr-only">Effect 审计日志</caption>
                  <thead>
                    <tr>
                      <th scope="col">时间</th>
                      <th scope="col">所有者</th>
                      <th scope="col">Effect</th>
                      <th scope="col">状态</th>
                      <th scope="col">演练</th>
                      <th scope="col">载荷字段</th>
                      <th scope="col">Trace</th>
                    </tr>
                  </thead>
                  <tbody>
                    {effectLogItems.length ? effectLogItems.map((item) => (
                      <tr key={item.idempotency_key || item.id || `${item.owner}:${item.type}`}>
                        <td className="mono">{item.created_at || "-"}</td>
                        <td className="mono">{item.owner || "-"}</td>
                        <td className="mono">{item.type || "-"}</td>
                        <td>{item.status || "-"}</td>
                        <td>{formatBool(item.dry_run)}</td>
                        <td>{formatList(item.payload_keys)}</td>
                        <td>
                          {item.trace_id ? (
                            <button className="link-button mono" onClick={() => selectEffectTrace(item.trace_id)}>
                              {item.trace_id}
                            </button>
                          ) : "-"}
                        </td>
                      </tr>
                    )) : (
                      <tr>
                        <td colSpan={7}>-</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </article>
            </div>
          </details>

        </div>
        </details>
      </section>

      {traceFlowModalOpen && activeTraceAggregate && (
        <div className="trace-flow-modal-backdrop" role="presentation" onClick={() => setTraceFlowModalOpen(false)}>
          <div className="trace-flow-modal" role="dialog" aria-modal="true" aria-labelledby={traceFlowTitleId} onClick={(event) => event.stopPropagation()}>
            <div className="trace-flow-modal-header">
              <div>
                <p className="section-kicker">单次消息流转</p>
                <h3 id={traceFlowTitleId}>消息路径流转</h3>
                <p className="muted-copy mono">{activeTraceAggregate.traceId}</p>
              </div>
              <button className="button button-secondary" onClick={() => setTraceFlowModalOpen(false)}>
                关闭
              </button>
            </div>
            <div className={`trace-flow-status is-${traceFlowHasError ? "error" : "ok"}`}>
              <strong>{traceFlowHasError ? "链路存在异常节点" : "链路节点未发现错误状态"}</strong>
              <span>从入站流到 wxbot 回复队列 / 通用出站流的聚合视图，节点为 0 表示该阶段未命中或后端未保留对应快照。</span>
            </div>
            <div className="trace-flow-diagram" aria-label="单次消息追踪路径">
              {traceFlowNodes.map((node, index) => (
                <div className="trace-flow-node-wrap" key={node.label}>
                  <article className={`trace-flow-node is-${node.status}`}>
                    <span className="trace-flow-node-dot" aria-hidden="true" />
                    <span className="trace-flow-node-label">{node.label}</span>
                    <strong>{node.count}</strong>
                    <small>{node.status === "error" ? "异常" : node.status === "miss" ? "未命中" : "已命中"}</small>
                  </article>
                  {index < traceFlowNodes.length - 1 && <span className="trace-flow-arrow">→</span>}
                </div>
              ))}
            </div>
            <div className="trace-flow-detail-list">
              {traceFlowNodes.map((node) => (
                <article className={`trace-flow-detail-item is-${node.status}`} key={`${node.label}:detail`}>
                  <div>
                    <strong>{node.label}</strong>
                    <span>{node.count} 条</span>
                  </div>
                  <p>{node.detail}</p>
                  <small>{node.meta}</small>
                </article>
              ))}
            </div>
            <div className="trace-flow-modal-grid">
              <section>
                <h4>消息身份</h4>
                <dl className="plugin-meta-list">
                  <div><dt>tenant</dt><dd>{activeTraceAggregate.inbound[0]?.tenant_id || activeTraceAggregate.outbound[0]?.tenant_id || "-"}</dd></div>
                  <div><dt>session</dt><dd>{activeTraceAggregate.inbound[0]?.session_id || activeTraceAggregate.replyQueue[0]?.session_id || activeTraceAggregate.outbound[0]?.session_id || "-"}</dd></div>
                  <div><dt>user</dt><dd>{activeTraceAggregate.inbound[0]?.user_id || "-"}</dd></div>
                  <div><dt>channel</dt><dd>{activeTraceAggregate.inbound[0]?.channel || activeTraceAggregate.outbound[0]?.channel || "-"}</dd></div>
                </dl>
              </section>
              <section>
                <h4>关键时间</h4>
                <dl className="plugin-meta-list">
                  <div><dt>inbound</dt><dd>{formatTraceTime(activeTraceAggregate.inbound[0]?.created_ts_ms)}</dd></div>
                  <div><dt>effect</dt><dd>{activeTraceAggregate.effects[0]?.created_at || "-"}</dd></div>
                  <div><dt>reply_queue</dt><dd>{activeTraceAggregate.replyQueue[0]?.sent_at || activeTraceAggregate.replyQueue[0]?.queued_at || activeTraceAggregate.replyQueue[0]?.created_at || "-"}</dd></div>
                  <div><dt>通用出站流</dt><dd>{formatTraceTime(activeTraceAggregate.outbound[0]?.created_ts_ms)}</dd></div>
                </dl>
              </section>
            </div>
          </div>
        </div>
      )}

    </>
  );
}

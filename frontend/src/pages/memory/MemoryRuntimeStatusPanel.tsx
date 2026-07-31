import { useCallback, useEffect, useMemo, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";
import { extractionJobStatusLabel, formatTimestamp, friendlyApiError } from "./model";

type RuntimeDiagnostic = {
  area: string;
  status: string;
  code: string;
  message: string;
};

type RuntimeJob = {
  id?: number;
  status?: string;
  error_type?: string;
  attempts?: number;
  max_attempts?: number;
  created_at?: string | null;
  updated_at?: string | null;
};

type MemoryManagementStatus = {
  config: {
    values: Record<string, boolean | number | string | null>;
    source: string;
    note?: string;
  };
  runtime_scope: {
    required?: boolean;
    status?: string;
    tenant_id?: string;
    session_id?: string;
  };
  jobs: {
    stats?: {
      counts?: Record<string, number>;
      status_counts?: Record<string, number>;
    };
    recent?: RuntimeJob[];
  };
  review?: {
    total?: number;
    counts?: Record<string, number>;
  };
  governance?: {
    auto_cleanup_enabled?: boolean | null;
    needs_review_retention_days?: number | null;
    rejected_retention_days?: number | null;
    auto_expire_days?: number | null;
    batch_size?: number | null;
    expired_items?: number;
    expiring_within_7_days?: number;
  };
  visibility?: {
    scanned_items?: number;
    group_session_visible_items?: number;
  };
  diagnostics?: RuntimeDiagnostic[];
};

type GovernanceCleanupPreview = {
  dry_run?: boolean;
  skipped_locked?: boolean;
  needs_review_expired?: number;
  rejected_purged?: number;
  stale_auto_expired?: number;
  selected?: number;
  physical_expiry?: {
    selected?: number;
    items_selected?: number;
    events_selected?: number;
    jobs_selected?: number;
  };
};

type MemoryRuntimeStatusPanelProps = {
  sessionId: string;
  channel: string;
  sourceKey: string;
  userId: string;
  selectedSessionIsGroup: boolean;
  selectedMemberIsVerified: boolean;
  onOutput: (value: string) => void;
};

const CONFIG_LABELS: Record<string, string> = {
  memory_llm_extraction_enabled: "LLM 自动抽取",
  memory_llm_extraction_job_enabled: "抽取任务入队",
  memory_llm_extraction_job_drain_enabled: "抽取任务消费",
  memory_retrieval_enabled: "记忆召回",
  memory_group_identity_memory_enabled: "群内身份记忆",
  memory_hybrid_retrieval_enabled: "混合检索",
  memory_vector_index_enabled: "向量索引",
  memory_graph_retrieval_enabled: "图谱召回",
  memory_graph_llm_extraction_enabled: "图谱 LLM 抽取",
  memory_governance_auto_cleanup_enabled: "自动治理清理",
};

function numericCount(values: Record<string, number> | undefined, ...keys: string[]) {
  return keys.reduce((total, key) => total + Number(values?.[key] || 0), 0);
}

function settingText(value: boolean | number | string | null) {
  if (value === true) return "已开启";
  if (value === false) return "已关闭";
  if (value === null || value === undefined) return "未提供";
  return String(value);
}

export function MemoryRuntimeStatusPanel({
  sessionId,
  channel,
  sourceKey,
  userId,
  selectedSessionIsGroup,
  selectedMemberIsVerified,
  onOutput,
}: MemoryRuntimeStatusPanelProps) {
  const { config } = useConsoleConfig();
  const [status, setStatus] = useState<MemoryManagementStatus | null>(null);
  const [cleanupPreview, setCleanupPreview] = useState<GovernanceCleanupPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [error, setError] = useState("");

  const scopeReady = selectedSessionIsGroup && selectedMemberIsVerified;
  const loadStatus = useCallback(async () => {
    if (!scopeReady) {
      setStatus(null);
      setError("请先选择已验证群聊和群成员，运行状态会严格限定到该范围。");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest<MemoryManagementStatus>(
        config,
        "/plugins/memory/management-status",
        {
          auth: true,
          query: {
            tenant_id: config.tenantId,
            channel,
            source_key: sourceKey,
            session_id: sessionId,
            user_id: userId,
            recent_job_limit: 10,
          },
        },
      );
      setStatus(result);
      onOutput(formatJson({
        status: "memory_runtime_status_loaded",
        runtime_scope: result.runtime_scope,
        config: result.config,
        job_counts: result.jobs.stats?.status_counts || result.jobs.stats?.counts || {},
        review: result.review,
        governance: result.governance,
        visibility: result.visibility,
        diagnostics: result.diagnostics,
        omitted_fields: ["recent_jobs"],
      }));
    } catch (err) {
      const message = friendlyApiError(err, "记忆运行状态读取失败");
      setStatus(null);
      setError(message);
      onOutput(formatJson({ error: message }));
    } finally {
      setLoading(false);
    }
  }, [channel, config, onOutput, scopeReady, sessionId, sourceKey, userId]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const jobCounts = status?.jobs.stats?.status_counts || status?.jobs.stats?.counts || {};
  const reviewCounts = status?.review?.counts || {};
  const pendingReview = numericCount(reviewCounts, "candidate", "needs_review", "missing_acceptance");
  const recentJobs = status?.jobs.recent || [];
  const configEntries = useMemo(
    () => Object.entries(status?.config.values || {}),
    [status],
  );
  const runtimeScopeStatus = status?.runtime_scope.status || "";
  const runtimeScopePillClass = runtimeScopeStatus === "disabled"
    ? "pill-danger"
    : runtimeScopeStatus === "enabled" || runtimeScopeStatus === "not_required"
      ? "pill-ok"
      : "pill-muted";

  const runCleanupPreview = async () => {
    if (!scopeReady) return;
    setPreviewLoading(true);
    setError("");
    try {
      const result = await apiRequest<GovernanceCleanupPreview>(
        config,
        "/plugins/memory/governance/cleanup",
        {
          auth: true,
          init: {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              dry_run: true,
              needs_review_days: status?.governance?.needs_review_retention_days || undefined,
              rejected_days: status?.governance?.rejected_retention_days || undefined,
              auto_expire_days: status?.governance?.auto_expire_days || undefined,
              limit: Math.min(5000, Math.max(1, status?.governance?.batch_size || 500)),
            }),
          },
        },
      );
      setCleanupPreview(result);
      onOutput(formatJson({
        status: "memory_governance_cleanup_preview",
        dry_run: result.dry_run,
        selected: result.selected || 0,
        needs_review_expired: result.needs_review_expired || 0,
        rejected_purged: result.rejected_purged || 0,
        stale_auto_expired: result.stale_auto_expired || 0,
        physical_expiry: result.physical_expiry,
        note: "仅预演，没有写入或删除。",
      }));
    } catch (err) {
      const message = friendlyApiError(err, "记忆治理清理预演失败");
      setCleanupPreview(null);
      setError(message);
      onOutput(formatJson({ error: message }));
    } finally {
      setPreviewLoading(false);
    }
  };

  return (
    <section className="panel span-3" aria-labelledby="memory-runtime-status-title">
      <div className="panel-header">
        <div>
          <p className="section-kicker">生效配置与可观测性</p>
          <h3 id="memory-runtime-status-title">记忆运行状态</h3>
        </div>
        <span className={`pill ${runtimeScopePillClass}`}>
          {runtimeScopeStatus || "未读取"}
        </span>
      </div>
      <p className="muted-copy">
        显示当前服务进程的生效配置，以及当前租户、群和成员范围的运行时开关。两者不同层级，必须同时满足才会写入或召回。
      </p>

      {error && <div className="admin-notice admin-notice-warning" role="alert">{error}</div>}

      <div className="action-row">
        <button className="button button-secondary" type="button" onClick={() => void loadStatus()} disabled={!scopeReady || loading}>
          {loading ? "正在刷新…" : "刷新运行状态"}
        </button>
        <button className="button button-secondary" type="button" onClick={() => void runCleanupPreview()} disabled={!scopeReady || previewLoading}>
          {previewLoading ? "正在预演…" : "预演到期清理"}
        </button>
      </div>

      <div className="memory-backlog-stats-grid">
        <div className="summary-card"><span>待处理任务</span><strong>{jobCounts.pending || 0}</strong></div>
        <div className="summary-card" data-status="error"><span>失败 / 终止</span><strong>{numericCount(jobCounts, "failed", "dead")}</strong></div>
        <div className="summary-card" data-status={pendingReview ? "warning" : undefined}><span>待人工复核</span><strong>{pendingReview}</strong></div>
        <div className="summary-card" data-status={status?.governance?.expired_items ? "warning" : undefined}><span>已到期记录</span><strong>{status?.governance?.expired_items || 0}</strong></div>
        <div className="summary-card"><span>7 天内到期</span><strong>{status?.governance?.expiring_within_7_days || 0}</strong></div>
        <div className="summary-card"><span>当前群可见</span><strong>{status?.visibility?.group_session_visible_items || 0}</strong></div>
      </div>

      <div className="form-grid">
        <div className="field">
          <span>运行时范围开关</span>
          <strong>{status?.runtime_scope.status || "未读取"}</strong>
          <small>{status?.runtime_scope.required ? "当前部署要求租户/会话开关" : "当前部署不要求额外范围开关"}</small>
        </div>
        <div className="field">
          <span>治理保留策略</span>
          <strong>待复核 {status?.governance?.needs_review_retention_days ?? "-"} 天 / 已拒绝 {status?.governance?.rejected_retention_days ?? "-"} 天</strong>
          <small>自动记忆默认 {status?.governance?.auto_expire_days ?? "-"} 天；批次 {status?.governance?.batch_size ?? "-"} 条</small>
        </div>
        {configEntries.map(([key, value]) => (
          <div className="field" key={key}>
            <span>{CONFIG_LABELS[key] || key}</span>
            <strong>{settingText(value)}</strong>
            <small className="mono">{key}</small>
          </div>
        ))}
      </div>

      <div className="memory-backlog-detail-grid">
        <div className="memory-backlog-card">
          <div className="memory-graph-sample-title">为什么没有写入 / 召回</div>
          {(status?.diagnostics || []).length ? (
            <ul className="memory-backlog-count-list">
              {(status?.diagnostics || []).map((item) => (
                <li key={item.code}>
                  <span><strong>{item.area}</strong>：{item.message}</span>
                  <code>{item.code}</code>
                </li>
              ))}
            </ul>
          ) : <div className="memory-graph-sample-empty">刷新后会显示配置、任务、复核和群可见性诊断。</div>}
        </div>

        <div className="memory-backlog-card">
          <div className="memory-graph-sample-title">到期清理预演</div>
          <div className="admin-notice">
            治理 cleanup 是服务级维护操作，不只限定当前群；本面板固定 dry-run 且只展示汇总计数。
          </div>
          {cleanupPreview ? (
            <div className="memory-backlog-result-grid">
              <div><span>预计总影响</span><strong>{cleanupPreview.selected || 0}</strong></div>
              <div><span>待复核过期</span><strong>{cleanupPreview.needs_review_expired || 0}</strong></div>
              <div><span>拒绝项清理</span><strong>{cleanupPreview.rejected_purged || 0}</strong></div>
              <div><span>自动记忆过期</span><strong>{cleanupPreview.stale_auto_expired || 0}</strong></div>
              <div><span>物理到期数据</span><strong>{cleanupPreview.physical_expiry?.selected || 0}</strong></div>
            </div>
          ) : <div className="memory-graph-sample-empty">仅提供 dry-run 预演；不会从此面板直接执行清理。</div>}
        </div>
      </div>

      <div className="memory-backlog-card">
        <div className="memory-graph-sample-title">近期抽取任务</div>
        {recentJobs.length ? (
          <div className="table-scroll compact-table-scroll">
            <table>
              <caption className="sr-only">近期记忆抽取任务</caption>
              <thead><tr><th scope="col">任务</th><th scope="col">状态</th><th scope="col">尝试</th><th scope="col">错误类型</th><th scope="col">更新时间</th></tr></thead>
              <tbody>
                {recentJobs.map((job, index) => (
                  <tr key={`${job.id || "job"}-${index}`}>
                    <th scope="row" className="mono">#{job.id || "-"}</th>
                    <td>{extractionJobStatusLabel(job.status)}</td>
                    <td>{job.attempts || 0} / {job.max_attempts || 0}</td>
                    <td className="mono">{job.error_type || "-"}</td>
                    <td>{formatTimestamp(job.updated_at || job.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <div className="memory-graph-sample-empty">当前群成员范围没有近期抽取任务。</div>}
      </div>
    </section>
  );
}

export type { MemoryManagementStatus };

import { useState } from "react";

import { Alert, DangerAction, EmptyState } from "../../components";
import {
  friendlyErrorMessage,
  formatTime,
  TechnicalDetails,
} from "./presentation";

export type PolicyVersionMetadata = {
  version: number;
  parent_version: number;
  rollback_from_version: number | null;
  actor: string;
  change_summary: string[];
  reason_present: boolean;
  created_at: string;
};

type VersionRollbackPanelProps = {
  headingId: string;
  title: string;
  kicker?: string;
  subjectLabel: string;
  currentVersion: number;
  updatedBy: string;
  updatedAt: string | null;
  history: PolicyVersionMetadata[];
  historyLoading: boolean;
  historyLoadingMore?: boolean;
  historyError: string;
  nextCursor: string | null;
  disabled?: boolean;
  onRefreshHistory: () => void;
  onLoadMoreHistory: () => void;
  onRollback: (version: number, reason: string) => Promise<void>;
};

const summaryLabel = (value: string) => {
  const [category, detail] = value.split(":", 2);
  const categoryLabels: Record<string, string> = {
    rollback: "回滚",
    kill_switch: "开关",
    policy: "参与策略",
    privacy: "隐私策略",
    voice_profile: "表达风格",
    metadata: "元数据",
  };
  const detailLabels: Record<string, string> = {
    updated: "已更新",
    enabled: "已开启",
    disabled: "已关闭",
    created: "已创建",
    deleted: "已删除",
    restored: "已恢复",
  };
  const categoryLabel = categoryLabels[category] || "配置";
  const detailLabel = detail ? detailLabels[detail] || "已变更" : "";
  return `${categoryLabel}${detailLabel ? ` · ${detailLabel}` : ""}`;
};

const actorLabel = (value: string) => {
  const labels: Record<string, string> = {
    operator: "管理员",
    "platform-operator": "平台管理员",
    "tenant-operator": "租户管理员",
    system: "系统",
  };
  return labels[value] || (value ? "已认证管理员" : "系统默认");
};

export function VersionRollbackPanel({
  headingId,
  title,
  kicker = "版本控制",
  subjectLabel,
  currentVersion,
  updatedBy,
  updatedAt,
  history,
  historyLoading,
  historyLoadingMore = false,
  historyError,
  nextCursor,
  disabled = false,
  onRefreshHistory,
  onLoadMoreHistory,
  onRollback,
}: VersionRollbackPanelProps) {
  const [targetVersion, setTargetVersion] = useState("");
  const [reason, setReason] = useState("");
  const parsedVersion = Number(targetVersion);
  const selectableVersions = history.filter((item) => item.version < currentVersion);
  const targetValid = selectableVersions.some((item) => item.version === parsedVersion);

  const rollback = async () => {
    if (!targetValid) {
      throw new Error("请选择服务器返回的历史版本");
    }
    await onRollback(parsedVersion, reason.trim());
    setTargetVersion("");
    setReason("");
    onRefreshHistory();
  };

  return (
    <section className="panel span-3" aria-labelledby={headingId}>
      <div className="panel-header">
        <div>
          <p className="section-kicker">{kicker}</p>
          <h2 id={headingId}>{title}</h2>
        </div>
        <button
          className="button button-secondary"
          type="button"
          onClick={onRefreshHistory}
          disabled={historyLoading || historyLoadingMore}
        >
          {historyLoading ? "读取中…" : "刷新历史"}
        </button>
      </div>
      <div className="status-grid">
        <article className="status-tile"><span>当前版本</span><strong>v{currentVersion}</strong></article>
        <article className="status-tile"><span>最近操作者</span><strong>{actorLabel(updatedBy)}</strong></article>
        <article className="status-tile"><span>更新时间</span><strong>{formatTime(updatedAt)}</strong></article>
      </div>
      {historyError ? (
        <>
          <Alert variant="danger" title="版本历史读取失败">
            {friendlyErrorMessage(historyError, "版本历史读取未完成，请稍后重试。")}
          </Alert>
          <TechnicalDetails
            data={{ error: historyError }}
            summary="查看版本历史错误详情"
            label={`${subjectLabel}历史错误 JSON`}
          />
        </>
      ) : null}
      {!historyLoading && !historyError && !history.length ? (
        <EmptyState compact title="暂无历史版本" description="保存首个版本后，服务器会在这里返回不可变版本元数据。" />
      ) : null}
      {history.length ? (
        <div className="stack-list" aria-label={`${subjectLabel}版本历史`}>
          {history.map((item) => (
            <article className="status-tile" key={item.version}>
              <span>v{item.version} · {formatTime(item.created_at)}</span>
              <strong>{item.change_summary.map(summaryLabel).join("；")}</strong>
              <small>{actorLabel(item.actor)}{item.rollback_from_version ? ` · 来自 v${item.rollback_from_version}` : ""}</small>
              <TechnicalDetails
                data={item}
                summary={`查看 v${item.version} 技术详情`}
                label={`${subjectLabel} v${item.version} 完整 JSON`}
              />
            </article>
          ))}
        </div>
      ) : null}
      {nextCursor ? (
        <div className="action-row">
          <button className="button button-secondary" type="button" onClick={onLoadMoreHistory} disabled={historyLoadingMore}>
            {historyLoadingMore ? "加载中…" : "加载更早版本"}
          </button>
        </div>
      ) : null}
      <div className="form-grid">
        <label className="field">
          <span>回滚到{subjectLabel}版本</span>
          <select value={targetVersion} onChange={(event) => setTargetVersion(event.target.value)}>
            <option value="">选择历史版本</option>
            {selectableVersions.map((item) => <option key={item.version} value={item.version}>v{item.version}</option>)}
          </select>
        </label>
        <label className="field span-2">
          <span>回滚原因（进入审计记录）</span>
          <input maxLength={240} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="例如：恢复到已验证的稳定策略" />
        </label>
      </div>
      <div className="action-row">
        <DangerAction
          label={`回滚${subjectLabel}`}
          title={`确认回滚${subjectLabel}`}
          impact={<p>{subjectLabel}将从 v{currentVersion} 创建一个以 v{targetValid ? parsedVersion : "?"} 为内容的新版本；历史版本保持不可变。</p>}
          confirmLabel="确认回滚"
          pendingLabel="正在回滚…"
          onConfirm={rollback}
          disabled={disabled || !targetValid}
        />
      </div>
    </section>
  );
}

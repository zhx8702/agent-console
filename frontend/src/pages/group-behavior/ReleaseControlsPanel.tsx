import { useState } from "react";

import { Alert, DangerAction, EmptyState } from "../../components";
import type { VersionedResourceState } from "../../lib/api";
import {
  friendlyErrorMessage,
  resourceStatus,
  TechnicalDetails,
  ToggleCard,
} from "./presentation";

export type RolloutStage =
  | "shadow"
  | "privacy_5"
  | "style_10"
  | "contextual"
  | "proactive";

export type ScopedParticipationControlDocument = {
  scope: "global" | "tenant";
  tenant_id: string;
  version: number;
  control: {
    enabled: boolean;
    rollout_stage: RolloutStage;
  };
  updated_by: string;
  updated_at: string | null;
};

type ScopeName = "global" | "tenant";

type ReleaseControlsPanelProps = {
  globalState: VersionedResourceState<ScopedParticipationControlDocument>;
  tenantState: VersionedResourceState<ScopedParticipationControlDocument>;
  tenantId: string;
  onUpdate: (
    scope: ScopeName,
    update: (value: ScopedParticipationControlDocument) => ScopedParticipationControlDocument,
  ) => void;
  onSave: (scope: ScopeName, reason: string) => Promise<void>;
  onRefresh: (scope: ScopeName) => void;
};

const STAGE_OPTIONS: Array<{ value: RolloutStage; label: string }> = [
  { value: "shadow", label: "全量影子评估" },
  { value: "privacy_5", label: "5% 隐私灰度" },
  { value: "style_10", label: "10% 节奏与语气" },
  { value: "contextual", label: "上下文柔性参与" },
  { value: "proactive", label: "主动暖场灰度" },
];

function ScopeCard({
  title,
  description,
  scope,
  state,
  onUpdate,
  onSave,
  onRefresh,
}: {
  title: string;
  description: string;
  scope: ScopeName;
  state: VersionedResourceState<ScopedParticipationControlDocument>;
  onUpdate: ReleaseControlsPanelProps["onUpdate"];
  onSave: ReleaseControlsPanelProps["onSave"];
  onRefresh: ReleaseControlsPanelProps["onRefresh"];
}) {
  const draft = state.draft;
  const enabled = Boolean(draft?.control?.enabled);
  const [reason, setReason] = useState("");
  const save = async () => {
    await onSave(scope, reason.trim());
    setReason("");
  };
  return (
        <article className="release-control-card" aria-label={title}>
      <div className="panel-header">
        <div>
          <p className="section-kicker">{scope === "global" ? "全局发布" : "租户发布"}</p>
          <h3>{title}</h3>
        </div>
        <span className={`pill ${enabled ? "pill-ok" : "pill-danger"}`}>
          {enabled ? "已开启" : "已停止"}
        </span>
      </div>
      <p className="muted-copy">{description}</p>
      {state.error ? (
        <Alert
          variant={state.status === "conflict" ? "warning" : "danger"}
          title={state.status === "conflict" ? "发布控制版本冲突" : "发布控制不可用"}
        >
          {state.status === "conflict"
            ? "服务器已有更新。当前草稿保留，请重新读取后核对。"
            : friendlyErrorMessage(
                state.error,
                "发布控制读取或保存未完成，请稍后重试。",
              )}
        </Alert>
      ) : null}
      {state.error ? (
        <TechnicalDetails
          data={{ error: state.error }}
          summary="查看发布控制错误详情"
          label={`${title}错误 JSON`}
        />
      ) : null}
      {draft?.control ? (
        <>
          <div className="form-grid release-control-fields">
            <ToggleCard
              checked={draft.control.enabled}
              label={scope === "global" ? "允许全局发布" : "允许本租户发布"}
              description="关闭后立即阻断该层级以下所有实际群参与"
              onChange={(enabled) =>
                onUpdate(scope, (value) => ({
                  ...value,
                  control: { ...value.control, enabled },
                }))
              }
            />
            <label className="field">
              <span>最高发布阶段</span>
              <select
                value={draft.control.rollout_stage}
                onChange={(event) =>
                  onUpdate(scope, (value) => ({
                    ...value,
                    control: {
                      ...value.control,
                      rollout_stage: event.target.value as RolloutStage,
                    },
                  }))
                }
              >
                {STAGE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="action-row release-control-actions">
            <label className="field release-control-reason">
              <span>变更原因（进入审计）</span>
              <input maxLength={240} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="例如：开启隐私灰度" />
            </label>
            {scope === "global" ? (
              <DangerAction
                label="保存全局发布控制"
                title="确认修改全局发布控制"
                impact={<p>该操作会影响所有租户。关闭开关会立即停止全局实际参与，策略历史和影子事件仍保留。</p>}
                confirmLabel="确认保存"
                pendingLabel="保存中…"
                onConfirm={save}
                disabled={!state.dirty || state.status === "saving"}
                className="release-control-save release-control-save-danger"
              />
            ) : (
              <button
                className="button button-primary release-control-save"
                type="button"
                onClick={() => void save()}
                disabled={!state.dirty || state.status === "saving"}
              >
                {state.status === "saving" ? "保存中…" : "保存租户发布控制"}
              </button>
            )}
            <button
              className="button button-secondary release-control-refresh"
              type="button"
              onClick={() => onRefresh(scope)}
              disabled={state.status === "loading" || state.status === "saving"}
            >
              重新读取
            </button>
            <span className={`pill release-control-sync ${state.dirty ? "pill-feature" : "pill-muted"}`}>
              {resourceStatus(state)} · v{draft.version}
            </span>
          </div>
          <TechnicalDetails
            data={{ etag: state.etag, control: draft }}
            summary="查看发布控制技术详情"
            label={`${title}完整 JSON`}
          />
        </>
      ) : state.status !== "loading" ? (
        <EmptyState
          compact
          title={scope === "global" ? "没有全局控制权限或读取失败" : "租户控制尚不可用"}
          description="此处不会用前端默认值覆盖服务端控制。"
        />
      ) : null}
    </article>
  );
}

export function ReleaseControlsPanel({
  globalState,
  tenantState,
  tenantId,
  onUpdate,
  onSave,
  onRefresh,
}: ReleaseControlsPanelProps) {
  return (
    <section className="panel span-3 release-controls-panel" aria-labelledby="release-controls-heading">
      <div className="panel-header">
        <div>
          <p className="section-kicker">独立发布控制</p>
          <h2 id="release-controls-heading">全局与租户发布控制</h2>
        </div>
        <span className="pill pill-muted">分层发布</span>
      </div>
      <p className="muted-copy">
        全局、租户与单群是三个独立资源，各自使用版本令牌与审计记录；最终阶段取最保守值。
      </p>
      <div className="release-control-grid">
        <ScopeCard
          title="全局发布控制"
          description="平台级紧急停止与最高灰度阶段。没有平台全局权限时保持只读不可用。"
          scope="global"
          state={globalState}
          onUpdate={onUpdate}
          onSave={onSave}
          onRefresh={onRefresh}
        />
        <ScopeCard
          title={`租户 ${tenantId}`}
          description="只影响当前租户，无法越过更保守的全局发布阶段。"
          scope="tenant"
          state={tenantState}
          onUpdate={onUpdate}
          onSave={onSave}
          onRefresh={onRefresh}
        />
      </div>
    </section>
  );
}

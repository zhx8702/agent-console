import { useState } from "react";

import { Alert, DangerAction, EmptyState } from "../../components";
import type { VersionedResourceState } from "../../lib/api";
import {
  friendlyErrorMessage,
  resourceStatus,
  TechnicalDetails,
  ToggleCard,
} from "./presentation";

export type TenantMemberControlDocument = {
  tenant_id: string;
  user_id: string;
  version: number;
  control: {
    memory_opt_out: boolean;
    participation_opt_out: boolean;
    no_group_mentions: boolean;
  };
  deletion_state: "none" | "requested" | "completed" | "failed";
  deletion_intent_key: string;
  updated_by: string;
  updated_at: string | null;
};

type TenantMemberControlPanelProps = {
  state: VersionedResourceState<TenantMemberControlDocument>;
  memberId: string;
  onUpdate: (
    update: (value: TenantMemberControlDocument) => TenantMemberControlDocument,
  ) => void;
  onSave: (reason: string) => Promise<void>;
  onRequestErasure: (reason: string) => Promise<void>;
  onRefresh: () => void;
};

const DELETION_LABELS: Record<TenantMemberControlDocument["deletion_state"], string> = {
  none: "未请求",
  requested: "已进入持久删除队列",
  completed: "删除已完成",
  failed: "删除重试中",
};

export function TenantMemberControlPanel({
  state,
  memberId,
  onUpdate,
  onSave,
  onRequestErasure,
  onRefresh,
}: TenantMemberControlPanelProps) {
  const draft = state.draft;
  const [reason, setReason] = useState("");
  if (!draft) {
    return (
      <section className="panel span-3">
        <EmptyState compact title="租户级成员控制尚未读取" description="读取成员后才能管理跨群退出与删除请求。" />
      </section>
    );
  }
  const erasureInFlight = draft.deletion_state === "requested" || draft.deletion_state === "failed";
  return (
    <section className="panel span-3" aria-labelledby="tenant-member-control-heading">
      <div className="panel-header">
        <div>
          <p className="section-kicker">跨群成员权利</p>
          <h2 id="tenant-member-control-heading">跨群退出与数据删除</h2>
        </div>
        <span className={`pill ${draft.control.memory_opt_out ? "pill-danger" : "pill-muted"}`}>
          {DELETION_LABELS[draft.deletion_state]}
        </span>
      </div>
      <p className="muted-copy">
        这里的退出选择覆盖该成员在当前租户下的所有群；群级策略只能更严格，不能重新开启已退出的能力。
      </p>
      {state.error ? (
        <Alert variant={state.status === "conflict" ? "warning" : "danger"} title="租户成员控制操作失败">
          {state.status === "conflict"
            ? "服务器已有更新，请重新读取后核对。"
            : friendlyErrorMessage(
                state.error,
                "跨群成员控制读取或保存未完成，请稍后重试。",
              )}
        </Alert>
      ) : null}
      <div className="form-grid">
        <ToggleCard
          checked={draft.control.memory_opt_out}
          label="退出全部成员记忆"
          description="立即禁止捕获和召回；物理删除需使用下方删除请求"
          onChange={(memoryOptOut) => onUpdate((value) => ({
            ...value,
            control: { ...value.control, memory_opt_out: memoryOptOut },
          }))}
          disabled={erasureInFlight}
        />
        <ToggleCard
          checked={draft.control.participation_opt_out}
          label="退出柔性参与"
          description="不因该成员相关的软信号主动插话"
          onChange={(participationOptOut) => onUpdate((value) => ({
            ...value,
            control: { ...value.control, participation_opt_out: participationOptOut },
          }))}
        />
        <ToggleCard
          checked={draft.control.no_group_mentions}
          label="所有群内不提及"
          description="跨群禁止机器人主动 @ 该成员"
          onChange={(noGroupMentions) => onUpdate((value) => ({
            ...value,
            control: { ...value.control, no_group_mentions: noGroupMentions },
          }))}
        />
      </div>
      <div className="action-row">
        <label className="field span-2">
          <span>变更原因（进入审计）</span>
          <input
            maxLength={240}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="例如：成员要求跨群退出记忆"
          />
        </label>
        <button
          className="button button-primary"
          type="button"
          onClick={() => void onSave(reason.trim()).then(() => setReason(""))}
          disabled={!state.dirty || state.status === "saving" || erasureInFlight}
        >
          {state.status === "saving" ? "保存中…" : "保存跨群退出设置"}
        </button>
        <button className="button button-secondary" type="button" onClick={onRefresh} disabled={state.status === "loading" || state.status === "saving"}>
          刷新删除状态
        </button>
        <DangerAction
          label="删除该成员全部记忆"
          title="确认提交跨群记忆删除"
          impact={<p>将先保持成员全局退出并写入可恢复的持久删除任务，再按租户与成员范围删除所有受众版本；删除期间不会恢复召回。</p>}
          confirmLabel="提交删除请求"
          pendingLabel="提交中…"
          onConfirm={() => onRequestErasure(reason.trim()).then(() => setReason(""))}
          disabled={state.status === "saving" || erasureInFlight || !memberId}
        />
        <span className={`pill ${state.dirty ? "pill-feature" : "pill-muted"}`}>
          {resourceStatus(state)} · v{draft.version}
        </span>
      </div>
      <TechnicalDetails
        data={{ etag: state.etag, error: state.error || undefined, control: draft }}
        summary="查看跨群控制技术详情"
        label="跨群成员控制完整 JSON"
      />
    </section>
  );
}

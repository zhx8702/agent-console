import { Alert, EmptyState } from "../../components";
import type {
  MemberPrivacyPolicyDocument,
  MemberPrivacyValues,
  VersionedResourceState,
} from "../../lib/api";
import {
  friendlyErrorMessage,
  resourceStatus,
  TechnicalDetails,
  ToggleCard,
} from "./presentation";
import { MemberMemoryPanel, type MemberMemoryItem } from "./MemberMemoryPanel";
import {
  VersionRollbackPanel,
  type PolicyVersionMetadata,
} from "./VersionRollbackPanel";

function listValue(value: string) {
  return Array.from(
    new Set(
      value
        .split(/[\n,，]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

function numberValue(value: string, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

type MemberPrivacyPanelProps = {
  memberIdInput: string;
  onMemberIdInputChange: (value: string) => void;
  activeMemberId: string;
  state: VersionedResourceState<MemberPrivacyPolicyDocument>;
  reason: string;
  onReasonChange: (value: string) => void;
  onLoad: () => void;
  onUpdate: (updater: (policy: MemberPrivacyValues) => MemberPrivacyValues) => void;
  onSave: () => void;
  onRollback: (version: number, reason: string) => Promise<void>;
  history: PolicyVersionMetadata[];
  historyLoading: boolean;
  historyLoadingMore: boolean;
  historyError: string;
  historyNextCursor: string | null;
  onRefreshHistory: () => void;
  onLoadMoreHistory: () => void;
  memoryItems: MemberMemoryItem[];
  memoryLoading: boolean;
  memoryLoadingMore: boolean;
  memoryMutatingId: number | null;
  memoryError: string;
  memoryNextCursor: string | null;
  onRefreshMemory: () => void;
  onLoadMoreMemory: () => void;
  onCorrectMemory: (item: MemberMemoryItem, content: string, reason: string) => Promise<void>;
  onDeleteMemory: (item: MemberMemoryItem) => Promise<void>;
};

export function MemberPrivacyPanel({
  memberIdInput,
  onMemberIdInputChange,
  activeMemberId,
  state,
  reason,
  onReasonChange,
  onLoad,
  onUpdate,
  onSave,
  onRollback,
  history,
  historyLoading,
  historyLoadingMore,
  historyError,
  historyNextCursor,
  onRefreshHistory,
  onLoadMoreHistory,
  memoryItems,
  memoryLoading,
  memoryLoadingMore,
  memoryMutatingId,
  memoryError,
  memoryNextCursor,
  onRefreshMemory,
  onLoadMoreMemory,
  onCorrectMemory,
  onDeleteMemory,
}: MemberPrivacyPanelProps) {
  const memberDraft = state.draft;
  const tenantControlOverridesLocal = Boolean(
    memberDraft?.effective_policy &&
      JSON.stringify(memberDraft.effective_policy) !== JSON.stringify(memberDraft.policy),
  );

  return (
    <div className="page-grid span-3">
      <section className="panel span-3" aria-labelledby="member-privacy-heading">
        <div className="panel-header">
          <div>
            <p className="section-kicker">成员级设置</p>
            <h2 id="member-privacy-heading">成员隐私、记忆与纠错</h2>
          </div>
          {activeMemberId ? <span className="pill pill-feature">{activeMemberId}</span> : null}
        </div>
        <p className="muted-copy">成员策略默认保守：未明确开启时，不建立持久成员记忆，也不允许群内召回。</p>
        <div className="form-grid">
          <label className="field span-2">
            <span>成员微信标识</span>
            <input
              value={memberIdInput}
              onChange={(event) => onMemberIdInputChange(event.target.value)}
              placeholder="粘贴从成员目录获得的标识"
            />
          </label>
          <div className="action-row">
            <button
              className="button button-secondary"
              type="button"
              onClick={onLoad}
              disabled={state.status === "loading"}
            >
              {state.status === "loading" ? "读取中…" : "读取成员策略"}
            </button>
          </div>
        </div>
        {state.error ? (
          <Alert
            variant={state.status === "conflict" ? "warning" : "danger"}
            title={state.status === "conflict" ? "成员策略版本冲突" : "成员策略读取失败"}
          >
            {state.status === "conflict"
              ? "服务器上已有更新。当前草稿仍保留，请重新读取后核对再保存。"
              : friendlyErrorMessage(
                  state.error,
                  "成员策略读取或保存未完成，请稍后重试。",
                )}
          </Alert>
        ) : null}
        {memberDraft || state.error ? (
          <TechnicalDetails
            data={{ etag: state.etag, error: state.error || undefined, policy: memberDraft }}
            summary="查看成员策略技术详情"
            label="成员策略完整 JSON"
          />
        ) : null}
      </section>

      {memberDraft ? (
        <>
          {tenantControlOverridesLocal ? (
            <Alert variant="info" title="租户级退出正在覆盖当前群配置" className="span-3">
              下方编辑的是可独立恢复的群级配置；机器人运行时继续采用更严格的租户级有效策略，直到跨群退出被解除。
            </Alert>
          ) : null}
          <section className="panel span-2" aria-labelledby="member-memory-heading">
            <div className="panel-header">
              <div>
                <p className="section-kicker">记忆边界</p>
                <h2 id="member-memory-heading">记忆与召回范围</h2>
              </div>
            </div>
            <div className="form-grid">
              {(
                [
                  ["memory_enabled", "允许持久记忆", "关闭时不为该成员建立持久记忆"],
                  ["allow_group_recall", "允许群内召回", "允许在当前群使用该成员记忆"],
                  ["allow_private_recall", "允许私聊召回", "允许在该成员私聊中使用记忆"],
                  ["sensitive_memory_enabled", "允许敏感记忆", "默认关闭；仅在明确授权后开启"],
                ] as const
              ).map(([key, label, description]) => (
                <ToggleCard
                  key={key}
                  checked={memberDraft.policy[key]}
                  label={label}
                  description={description}
                  onChange={(checked) => onUpdate((policy) => ({ ...policy, [key]: checked }))}
                />
              ))}
              <label className="field">
                <span>保留天数（1–3650）</span>
                <input
                  type="number"
                  min={1}
                  max={3650}
                  value={memberDraft.policy.retention_days}
                  onChange={(event) =>
                    onUpdate((policy) => ({
                      ...policy,
                      retention_days: numberValue(event.target.value, 1),
                    }))
                  }
                />
              </label>
              <label className="field">
                <span>受众范围</span>
                <select
                  value={memberDraft.policy.audience_scope}
                  onChange={(event) =>
                    onUpdate((policy) => ({
                      ...policy,
                      audience_scope: event.target.value as MemberPrivacyValues["audience_scope"],
                    }))
                  }
                >
                  <option value="private">仅私聊</option>
                  <option value="session">仅当前会话</option>
                  <option value="explicit">仅指定会话</option>
                </select>
              </label>
              <label className="field span-2">
                <span>允许会话标识（仅指定会话时必填）</span>
                <textarea
                  rows={3}
                  value={memberDraft.policy.allowed_session_ids.join("\n")}
                  onChange={(event) =>
                    onUpdate((policy) => ({
                      ...policy,
                      allowed_session_ids: listValue(event.target.value),
                    }))
                  }
                />
              </label>
            </div>
          </section>
          <section className="panel" aria-labelledby="member-participation-heading">
            <div className="panel-header">
              <div>
                <p className="section-kicker">成员控制</p>
                <h2 id="member-participation-heading">参与、提及与数据权利</h2>
              </div>
            </div>
            <div className="form-grid">
              {(
                [
                  ["proactive_participation_enabled", "允许主动参与", "机器人可主动回应该成员相关话题"],
                  ["soft_reply_opt_out", "退出柔性回复", "不因软性信号主动插话"],
                  ["no_group_mentions", "群内不提及", "不在群消息中 @ 该成员"],
                  ["correction_enabled", "允许自然纠错", "支持“你记错了”等纠正指令"],
                  ["deletion_enabled", "允许删除记忆", "支持“别记我”等删除或退出指令"],
                ] as const
              ).map(([key, label, description]) => (
                <ToggleCard
                  key={key}
                  checked={memberDraft.policy[key]}
                  label={label}
                  description={description}
                  onChange={(checked) => onUpdate((policy) => ({ ...policy, [key]: checked }))}
                />
              ))}
            </div>
            <label className="field u-mt-4">
              <span>本次成员策略变更原因</span>
              <input
                maxLength={240}
                value={reason}
                onChange={(event) => onReasonChange(event.target.value)}
                placeholder="例如：成员要求群内不提及"
              />
            </label>
            <div className="action-row">
              <button
                className="button button-primary"
                type="button"
                onClick={onSave}
                disabled={!state.dirty || state.status === "saving"}
              >
                {state.status === "saving" ? "保存中…" : "保存成员策略"}
              </button>
              <span className={`pill ${state.dirty ? "pill-feature" : "pill-muted"}`}>
                {resourceStatus(state)} · v{memberDraft.version}
              </span>
            </div>
          </section>

          <MemberMemoryPanel
            items={memoryItems}
            loading={memoryLoading}
            loadingMore={memoryLoadingMore}
            mutatingId={memoryMutatingId}
            error={memoryError}
            nextCursor={memoryNextCursor}
            onRefresh={onRefreshMemory}
            onLoadMore={onLoadMoreMemory}
            onCorrect={onCorrectMemory}
            onDelete={onDeleteMemory}
          />

          <VersionRollbackPanel
            headingId="member-policy-version-heading"
            title="成员策略版本与回滚"
            subjectLabel="成员策略"
            currentVersion={memberDraft.version}
            updatedBy={memberDraft.updated_by}
            updatedAt={memberDraft.updated_at}
            history={history}
            historyLoading={historyLoading}
            historyLoadingMore={historyLoadingMore}
            historyError={historyError}
            nextCursor={historyNextCursor}
            disabled={state.status === "saving" || state.dirty}
            onRefreshHistory={onRefreshHistory}
            onLoadMoreHistory={onLoadMoreHistory}
            onRollback={onRollback}
          />
        </>
      ) : (
        <div className="span-3">
          <EmptyState
            compact
            title="选择一位成员"
            description={friendlyErrorMessage(
              state.error,
              "输入成员微信标识并读取策略后，才会显示记忆、召回、保留期与纠错删除设置。",
            )}
          />
        </div>
      )}
    </div>
  );
}

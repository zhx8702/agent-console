import { Alert, EmptyState } from "../../components";
import type { ParticipationPolicyValues } from "../../lib/api";
import {
  ParticipationBudgetSection,
  ProactiveParticipationSection,
} from "./ParticipationPolicySections";
import { policyEffectiveEnabled } from "./policyModel";
import { ReleaseControlsPanel } from "./ReleaseControlsPanel";
import { friendlyErrorMessage, TechnicalDetails, ToggleCard } from "./presentation";
import type { GroupBehaviorController } from "./useGroupBehaviorController";
import { VersionRollbackPanel } from "./VersionRollbackPanel";
import { VoiceProfileSection } from "./VoiceProfileSection";

type GroupPolicyPanelProps = {
  controller: GroupBehaviorController;
};

export function GroupPolicyPanel({ controller }: GroupPolicyPanelProps) {
  const {
    tenantId,
    groupId,
    policyState,
    policyReason,
    setPolicyReason,
    globalControlState,
    tenantControlState,
    policyHistory,
    policyHistoryLoading,
    policyHistoryLoadingMore,
    policyHistoryError,
    policyHistoryNextCursor,
    voiceHistory,
    voiceHistoryLoading,
    voiceHistoryLoadingMore,
    voiceHistoryError,
    voiceHistoryNextCursor,
    updatePolicyDraft,
    previewVoiceProfile,
    updateReleaseControl,
    saveReleaseControl,
    loadGlobalControl,
    loadTenantControl,
    loadPolicyHistory,
    loadVoiceHistory,
    rollbackPolicy,
    reloadPolicy,
  } = controller;
  const policyDraft = policyState.draft;

  if (!policyDraft) {
    return (
      <div>
        <EmptyState
          title={policyState.status === "loading" ? "正在读取群策略" : "群策略尚不可用"}
          description={friendlyErrorMessage(
            policyState.error,
            "读取成功后才会显示编辑控件，不会用前端默认值覆盖后端配置。",
          )}
          action={
            policyState.status !== "loading" ? (
              <button className="button button-secondary" type="button" onClick={reloadPolicy}>
                重新读取
              </button>
            ) : null
          }
        />
        {policyState.error ? (
          <TechnicalDetails
            data={{ error: policyState.error }}
            summary="查看读取错误技术详情"
            label="群策略读取错误 JSON"
          />
        ) : null}
      </div>
    );
  }

  const effectiveEnabled = policyEffectiveEnabled(policyDraft);
  const participationBlockers = [
    !policyDraft.kill_switches.group_enabled ? "当前群开关" : "",
    !policyDraft.kill_switches.tenant_enabled ? `租户 ${tenantId} 发布控制` : "",
    !policyDraft.kill_switches.global_enabled ? "全局发布控制" : "",
  ].filter(Boolean);
  const updateParticipationValue = <Key extends keyof ParticipationPolicyValues>(
    key: Key,
    value: ParticipationPolicyValues[Key],
  ) => {
    updatePolicyDraft((draft) => ({
      ...draft,
      policy: { ...draft.policy, [key]: value },
    }));
  };

  return (
    <div className="page-grid">
      <ReleaseControlsPanel
        globalState={globalControlState}
        tenantState={tenantControlState}
        tenantId={tenantId}
        onUpdate={updateReleaseControl}
        onSave={saveReleaseControl}
        onRefresh={(scope) => {
          if (scope === "global") {
            void loadGlobalControl();
          } else {
            void loadTenantControl();
          }
        }}
      />
      <section className="panel span-3 participation-switch-panel" aria-labelledby="kill-switch-heading">
        <div className="panel-header">
          <div>
            <p className="section-kicker">逐层收紧</p>
            <h2 id="kill-switch-heading">当前有效链路与单群开关</h2>
          </div>
          <span className={`pill ${effectiveEnabled ? "pill-ok" : "pill-danger"}`}>
            {effectiveEnabled ? "当前允许参与" : "当前停止参与"}
          </span>
        </div>
        <p className="muted-copy">
          全局与租户状态由上方独立资源管理；群策略只写入当前群开关。三层全部开启后才允许实际参与。
        </p>
        {!effectiveEnabled ? (
          <Alert variant="warning" title="当前群尚未实际参与">
            仍被以下开关阻断：{participationBlockers.join("、")}。
            如需只启用当前群，请先开启并保存当前群开关，再开启租户发布控制，最后开启全局发布控制；
            未配置的其他群默认保持关闭，不会随上级开关一起启动。
          </Alert>
        ) : null}
        <div className="status-grid">
          <article className="status-tile">
            <span>全局层（只读快照）</span>
            <strong>{policyDraft.kill_switches.global_enabled ? "开启" : "停止"}</strong>
          </article>
          <article className="status-tile">
            <span>租户层（只读快照）</span>
            <strong>{policyDraft.kill_switches.tenant_enabled ? "开启" : "停止"}</strong>
          </article>
          <article className="status-tile">
            <span>当前群有效结果</span>
            <strong>{effectiveEnabled ? "允许参与" : "停止参与"}</strong>
          </article>
        </div>
        <div className="form-grid">
          <ToggleCard
            checked={policyDraft.kill_switches.group_enabled}
            label="当前群开关"
            description="仅控制当前已验证群聊，无法越过更保守的全局或租户层"
            onChange={(checked) =>
              updatePolicyDraft((draft) => ({
                ...draft,
                kill_switches: { ...draft.kill_switches, group_enabled: checked },
              }))
            }
          />
          <ToggleCard
            checked={policyDraft.policy.file_send_enabled}
            label="允许群文件发送"
            description="默认关闭；关闭时群管理员、智能体工具和后台直发都不能向当前群发送文件"
            onChange={(checked) => updateParticipationValue("file_send_enabled", checked)}
          />
        </div>
      </section>

      <ParticipationBudgetSection
        policy={policyDraft.policy}
        onUpdate={updateParticipationValue}
      />
      <ProactiveParticipationSection
        policy={policyDraft.policy}
        onUpdate={updateParticipationValue}
      />
      <VoiceProfileSection
        profile={policyDraft.voice_profile}
        groupId={groupId}
        reason={policyReason}
        onReasonChange={setPolicyReason}
        onUpdateDraft={updatePolicyDraft}
        onPreview={previewVoiceProfile}
      />
      <VersionRollbackPanel
        headingId="group-policy-version-heading"
        title="群策略版本与回滚"
        subjectLabel="群策略"
        currentVersion={policyDraft.version}
        updatedBy={policyDraft.updated_by}
        updatedAt={policyDraft.updated_at}
        history={policyHistory}
        historyLoading={policyHistoryLoading}
        historyLoadingMore={policyHistoryLoadingMore}
        historyError={policyHistoryError}
        nextCursor={policyHistoryNextCursor}
        disabled={policyState.status === "saving" || policyState.dirty}
        onRefreshHistory={() => void loadPolicyHistory(groupId)}
        onLoadMoreHistory={() => {
          if (policyHistoryNextCursor) {
            void loadPolicyHistory(groupId, {
              cursor: policyHistoryNextCursor,
              append: true,
            });
          }
        }}
        onRollback={rollbackPolicy}
      />
      <VersionRollbackPanel
        headingId="voice-profile-version-heading"
        title="表达风格版本与回滚"
        subjectLabel="包含该表达风格的整组策略"
        currentVersion={policyDraft.voice_profile?.version || policyDraft.version}
        updatedBy={policyDraft.updated_by}
        updatedAt={policyDraft.updated_at}
        history={voiceHistory}
        historyLoading={voiceHistoryLoading}
        historyLoadingMore={voiceHistoryLoadingMore}
        historyError={voiceHistoryError}
        nextCursor={voiceHistoryNextCursor}
        disabled={policyState.status === "saving" || policyState.dirty}
        onRefreshHistory={() => void loadVoiceHistory(groupId)}
        onLoadMoreHistory={() => {
          if (voiceHistoryNextCursor) {
            void loadVoiceHistory(groupId, {
              cursor: voiceHistoryNextCursor,
              append: true,
            });
          }
        }}
        onRollback={rollbackPolicy}
      />
    </div>
  );
}

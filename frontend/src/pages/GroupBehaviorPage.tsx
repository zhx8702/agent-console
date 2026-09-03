import {
  Alert,
  GroupScopeEmpty,
  PageHeader,
  Tabs,
  UnsavedChangesGuard,
} from "../components";
import { DiscardChangesDialog } from "./group-behavior/DiscardChangesDialog";
import { GroupPolicyPanel } from "./group-behavior/GroupPolicyPanel";
import { MemberPrivacyPanel } from "./group-behavior/MemberPrivacyPanel";
import { ParticipationEventsPanel } from "./group-behavior/ParticipationEventsPanel";
import { ParticipationSimulatorPanel } from "./group-behavior/ParticipationSimulatorPanel";
import {
  policyEffectiveEnabled,
  voiceProfileValidationError,
} from "./group-behavior/policyModel";
import {
  friendlyErrorMessage,
  resourceStatus,
  TechnicalDetails,
} from "./group-behavior/presentation";
import { TenantMemberControlPanel } from "./group-behavior/TenantMemberControlPanel";
import { useGroupBehaviorController } from "./group-behavior/useGroupBehaviorController";

export function GroupBehaviorPage() {
  const controller = useGroupBehaviorController();
  const {
    tenantId,
    groupId,
    activeTab,
    setActiveTab,
    policyState,
    notice,
    events,
    eventsLoading,
    eventsLoadingMore,
    eventsError,
    eventsNextCursor,
    eventFilters,
    memberIdInput,
    setMemberIdInput,
    activeMemberId,
    memberState,
    memberReason,
    setMemberReason,
    tenantMemberState,
    memberHistory,
    memberHistoryLoading,
    memberHistoryLoadingMore,
    memberHistoryError,
    memberHistoryNextCursor,
    memberMemoryItems,
    memberMemoryLoading,
    memberMemoryLoadingMore,
    memberMemoryMutatingId,
    memberMemoryError,
    memberMemoryNextCursor,
    discardPrompt,
    cancelDiscard,
    confirmDiscard,
    hasUnsavedChanges,
    savePolicy,
    reloadPolicy,
    runPreview,
    loadEvents,
    setEventFilters,
    loadMember,
    updateMemberDraft,
    saveMember,
    rollbackMember,
    loadMemberHistory,
    loadMemberMemory,
    correctMemberMemory,
    deleteMemberMemory,
    updateTenantMemberDraft,
    saveTenantMember,
    requestTenantMemberErasure,
    loadTenantMemberControl,
  } = controller;

  if (!groupId) {
    return (
      <GroupScopeEmpty
        eyebrow="群聊行为控制"
        title="群参与与行为"
        description="把机器人何时开口、说多少、用什么风格以及成员隐私边界，集中在一个可审计的群级控制面。"
      />
    );
  }

  const policyDraft = policyState.draft;
  const voiceValidationError = voiceProfileValidationError(
    policyDraft?.voice_profile || null,
    groupId,
  );
  const effectiveEnabled = policyEffectiveEnabled(policyDraft);

  const policyPanel = <GroupPolicyPanel controller={controller} />;

  const simulatorPanel = (
    <ParticipationSimulatorPanel key={groupId} onPreview={runPreview} />
  );

  const eventsPanel = (
    <ParticipationEventsPanel
      key={groupId}
      events={events}
      loading={eventsLoading}
      loadingMore={eventsLoadingMore}
      error={eventsError}
      nextCursor={eventsNextCursor}
      filters={eventFilters}
      onFiltersChange={(filters) => {
        setEventFilters(filters);
        void loadEvents(groupId, { filters });
      }}
      onRefresh={() => void loadEvents(groupId)}
      onLoadMore={() => {
        if (eventsNextCursor) {
          void loadEvents(groupId, { cursor: eventsNextCursor, append: true });
        }
      }}
    />
  );

  const privacyPanel = (
    <div className="page-grid">
      <MemberPrivacyPanel
        key={`${groupId}:${activeMemberId || "unselected"}`}
        memberIdInput={memberIdInput}
        onMemberIdInputChange={setMemberIdInput}
        activeMemberId={activeMemberId}
        state={memberState}
        reason={memberReason}
        onReasonChange={setMemberReason}
        onLoad={() => void loadMember()}
        onUpdate={updateMemberDraft}
        onSave={() => void saveMember()}
        onRollback={rollbackMember}
        history={memberHistory}
        historyLoading={memberHistoryLoading}
        historyLoadingMore={memberHistoryLoadingMore}
        historyError={memberHistoryError}
        historyNextCursor={memberHistoryNextCursor}
        onRefreshHistory={() => {
          if (activeMemberId) void loadMemberHistory(groupId, activeMemberId);
        }}
        onLoadMoreHistory={() => {
          if (activeMemberId && memberHistoryNextCursor) {
            void loadMemberHistory(groupId, activeMemberId, {
              cursor: memberHistoryNextCursor,
              append: true,
            });
          }
        }}
        memoryItems={memberMemoryItems}
        memoryLoading={memberMemoryLoading}
        memoryLoadingMore={memberMemoryLoadingMore}
        memoryMutatingId={memberMemoryMutatingId}
        memoryError={memberMemoryError}
        memoryNextCursor={memberMemoryNextCursor}
        onRefreshMemory={() => {
          if (activeMemberId) void loadMemberMemory(groupId, activeMemberId);
        }}
        onLoadMoreMemory={() => {
          if (activeMemberId && memberMemoryNextCursor) {
            void loadMemberMemory(groupId, activeMemberId, {
              cursor: memberMemoryNextCursor,
              append: true,
            });
          }
        }}
        onCorrectMemory={correctMemberMemory}
        onDeleteMemory={deleteMemberMemory}
      />
      {activeMemberId ? (
        <TenantMemberControlPanel
          key={`${tenantId}:${activeMemberId}`}
          state={tenantMemberState}
          memberId={activeMemberId}
          onUpdate={updateTenantMemberDraft}
          onSave={saveTenantMember}
          onRequestErasure={requestTenantMemberErasure}
          onRefresh={() => void loadTenantMemberControl(activeMemberId)}
        />
      ) : null}
    </div>
  );

  return (
    <div className="page-grid group-behavior-page">
      <UnsavedChangesGuard when={hasUnsavedChanges} />
      <DiscardChangesDialog
        prompt={discardPrompt}
        onCancel={cancelDiscard}
        onConfirm={confirmDiscard}
      />
      <section className="panel panel-hero span-3">
        <PageHeader
          eyebrow="群聊行为控制"
          title="群参与与行为"
          description="在当前群统一控制何时开口、参与预算、表达风格与成员隐私；所有写入都带版本保护、稳定重试标识和审计原因。"
          actions={
            <div className="action-row">
              <button
                className="button button-primary"
                type="button"
                onClick={() => void savePolicy()}
                disabled={
                  !policyState.dirty
                  || policyState.status === "saving"
                  || Boolean(voiceValidationError)
                }
              >
                {policyState.status === "saving" ? "保存中…" : "保存群策略"}
              </button>
              <button
                className="button button-secondary"
                type="button"
                onClick={reloadPolicy}
                disabled={policyState.status === "loading"}
              >
                重新读取
              </button>
              <span className={`pill ${effectiveEnabled ? "pill-ok" : "pill-danger"}`}>
                {effectiveEnabled ? "有效参与开启" : "有效参与关闭"}
              </span>
            </div>
          }
        />
        <div className="status-grid page-hero-metrics">
          <article className="status-tile"><span>策略版本</span><strong>{policyDraft ? `v${policyDraft.version}` : "—"}</strong></article>
          <article className="status-tile"><span>同步状态</span><strong>{resourceStatus(policyState)}</strong></article>
        </div>
        {notice ? <Alert variant="success" title="已完成">{notice}</Alert> : null}
        {policyState.error ? (
          <Alert
            variant={policyState.status === "conflict" ? "warning" : "danger"}
            title={policyState.status === "conflict" ? "策略版本冲突" : "策略操作失败"}
          >
            {policyState.status === "conflict"
              ? "服务器上已有更新。当前草稿仍保留，请重新读取后核对再保存。"
              : friendlyErrorMessage(
                  policyState.error,
                  "策略操作未完成，请稍后重试；未保存的草稿仍会保留。",
                )}
          </Alert>
        ) : null}
        <TechnicalDetails
          summary="查看策略技术详情"
          label="群策略技术详情 JSON"
          data={{
            tenant_id: tenantId,
            session_id: groupId,
            etag: policyState.etag,
            resource_status: policyState.status,
            error: policyState.error || undefined,
            policy: policyDraft,
          }}
        />
      </section>

      <section className="panel span-3">
        <Tabs
          className="group-behavior-tabs"
          activeId={activeTab}
          onChange={setActiveTab}
          ariaLabel="群参与控制"
          tabs={[
            {
              id: "policy",
              label: (
                <span className="group-behavior-tab-label">
                  <span className="group-behavior-tab-index" aria-hidden="true">01</span>
                  <span>
                    <strong>参与策略</strong>
                    <small aria-hidden="true">发布、预算与表达边界</small>
                  </span>
                </span>
              ),
              content: policyPanel,
            },
            {
              id: "simulator",
              label: (
                <span className="group-behavior-tab-label">
                  <span className="group-behavior-tab-index" aria-hidden="true">02</span>
                  <span>
                    <strong>历史与决策模拟</strong>
                    <small aria-hidden="true">回放结构化参与场景</small>
                  </span>
                </span>
              ),
              content: simulatorPanel,
            },
            {
              id: "events",
              label: (
                <span className="group-behavior-tab-label">
                  <span className="group-behavior-tab-index" aria-hidden="true">03</span>
                  <span>
                    <strong>决策事件</strong>
                    <small aria-hidden="true">查看运行判定与投递结果</small>
                  </span>
                </span>
              ),
              content: eventsPanel,
            },
            {
              id: "privacy",
              label: (
                <span className="group-behavior-tab-label">
                  <span className="group-behavior-tab-index" aria-hidden="true">04</span>
                  <span>
                    <strong>成员隐私</strong>
                    <small aria-hidden="true">记忆、提及与删除控制</small>
                  </span>
                </span>
              ),
              content: privacyPanel,
            },
          ]}
        />
      </section>
    </div>
  );
}

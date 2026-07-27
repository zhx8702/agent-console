import { useEffect, useRef, useState } from "react";

import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { requireSelectedGroup, useConsoleConfig } from "../../state/console-config";
import type {
  DiscardChangesPrompt,
} from "./DiscardChangesDialog";
import { useMemberPrivacyController } from "./useMemberPrivacyController";
import { useParticipationEventsController } from "./useParticipationEventsController";
import { useParticipationPolicyController } from "./useParticipationPolicyController";

export function useGroupBehaviorController() {
  const { config, verifiedGroupIds, selectVerifiedGroup } = useConsoleConfig();
  const groupId = verifiedGroupIds.has(config.sessionId) ? config.sessionId : "";
  const previousGroupRef = useRef("");
  const approvedGroupSwitchRef = useRef("");
  const discardActionRef = useRef<(() => void) | null>(null);
  const [activeTab, setActiveTab] = useState("policy");
  const [notice, setNotice] = useState("");
  const [discardPrompt, setDiscardPrompt] = useState<DiscardChangesPrompt | null>(null);
  const { keyFor, clear: clearIdempotencyKey } = useStableIdempotencyKeys();

  const requestDiscard = (prompt: DiscardChangesPrompt, action: () => void) => {
    discardActionRef.current = action;
    setDiscardPrompt(prompt);
  };

  const cancelDiscard = () => {
    discardActionRef.current = null;
    setDiscardPrompt(null);
  };

  const confirmDiscard = () => {
    const action = discardActionRef.current;
    discardActionRef.current = null;
    setDiscardPrompt(null);
    action?.();
  };

  const selectedGroupForWrite = () => {
    if (!config.tenantId.trim()) {
      throw new Error("当前认证身份没有租户范围，无法执行群级写入");
    }
    return requireSelectedGroup(config, verifiedGroupIds);
  };

  const policy = useParticipationPolicyController({
    config,
    groupId,
    selectedGroupForWrite,
    keyFor,
    clearIdempotencyKey,
    setNotice,
    requestDiscard,
  });
  const events = useParticipationEventsController({
    config,
    selectedGroupForWrite,
  });
  const member = useMemberPrivacyController({
    config,
    selectedGroupForWrite,
    keyFor,
    clearIdempotencyKey,
    setNotice,
    requestDiscard,
  });

  const hasUnsavedChanges =
    policy.policyState.dirty
    || policy.globalControlState.dirty
    || policy.tenantControlState.dirty
    || member.memberState.dirty
    || member.tenantMemberState.dirty;

  useEffect(() => {
    const previousGroup = previousGroupRef.current;
    if (
      previousGroup
      && previousGroup !== groupId
      && hasUnsavedChanges
      && verifiedGroupIds.has(previousGroup)
      && approvedGroupSwitchRef.current !== groupId
    ) {
      const nextGroup = groupId;
      selectVerifiedGroup(previousGroup);
      requestDiscard(
        {
          title: "切换群聊并放弃修改？",
          description: `当前群 ${previousGroup} 仍有未保存修改。切换到 ${nextGroup} 前需要先放弃这些草稿。`,
          confirmLabel: "放弃并切换",
        },
        () => {
          approvedGroupSwitchRef.current = nextGroup;
          selectVerifiedGroup(nextGroup);
        },
      );
      return;
    }

    approvedGroupSwitchRef.current = "";
    previousGroupRef.current = groupId;
    member.resetMember();
    void policy.loadGlobalControl();
    const tenantId = config.tenantId.trim();
    if (tenantId) {
      void policy.loadTenantControl();
    } else {
      policy.resetTenantControl();
    }

    if (!groupId || !tenantId) {
      policy.resetPolicy(
        groupId && !tenantId
          ? "当前认证身份没有租户范围，已阻止群级请求。"
          : "",
      );
      events.resetEvents();
      return;
    }

    void policy.loadPolicy(groupId);
    void events.loadEvents(groupId);
    void policy.loadPolicyHistory(groupId);
    void policy.loadVoiceHistory(groupId);
  }, [config.apiBaseUrl, config.tenantId, groupId]);

  return {
    tenantId: config.tenantId,
    groupId,
    activeTab,
    setActiveTab,
    notice,
    discardPrompt,
    cancelDiscard,
    confirmDiscard,
    hasUnsavedChanges,
    ...policy,
    ...events,
    ...member,
  };
}

export type GroupBehaviorController = ReturnType<typeof useGroupBehaviorController>;

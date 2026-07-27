import { useState } from "react";

import {
  apiRequest,
  apiVersionedResource,
  createVersionedResourceState,
  editVersionedResource,
  markVersionedResourceError,
  markVersionedResourceLoaded,
  type GroupParticipationPolicyDocument,
  type GroupParticipationPolicyUpdate,
  type VersionedResourceState,
  type VoiceProfile,
  type VoiceProfilePreviewDocument,
  type VoiceProfilePreviewRequest,
} from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import type { DiscardChangesPrompt } from "./DiscardChangesDialog";
import { voiceProfileValidationError } from "./policyModel";
import type { ScopedParticipationControlDocument } from "./ReleaseControlsPanel";
import type { PolicyVersionMetadata } from "./VersionRollbackPanel";

type GroupPolicyRollbackUpdate = {
  rollback_to_version: number;
  change_reason: string;
};

type PolicyVersionPage = {
  items: PolicyVersionMetadata[];
  next_cursor: string | null;
};

type ScopedParticipationControlUpdate = {
  control: ScopedParticipationControlDocument["control"];
  change_reason: string;
};

type ParticipationPolicyControllerOptions = {
  config: ConsoleConfig;
  groupId: string;
  selectedGroupForWrite: () => string;
  keyFor: (intent: string) => string;
  clearIdempotencyKey: (intent: string) => void;
  setNotice: (notice: string) => void;
  requestDiscard: (prompt: DiscardChangesPrompt, action: () => void) => void;
};

export function useParticipationPolicyController({
  config,
  groupId,
  selectedGroupForWrite,
  keyFor,
  clearIdempotencyKey,
  setNotice,
  requestDiscard,
}: ParticipationPolicyControllerOptions) {
  const [policyState, setPolicyState] = useState<
    VersionedResourceState<GroupParticipationPolicyDocument>
  >(() => createVersionedResourceState());
  const [policyReason, setPolicyReason] = useState("");
  const [globalControlState, setGlobalControlState] = useState<
    VersionedResourceState<ScopedParticipationControlDocument>
  >(() => createVersionedResourceState());
  const [tenantControlState, setTenantControlState] = useState<
    VersionedResourceState<ScopedParticipationControlDocument>
  >(() => createVersionedResourceState());
  const [policyHistory, setPolicyHistory] = useState<PolicyVersionMetadata[]>([]);
  const [policyHistoryLoading, setPolicyHistoryLoading] = useState(false);
  const [policyHistoryLoadingMore, setPolicyHistoryLoadingMore] = useState(false);
  const [policyHistoryError, setPolicyHistoryError] = useState("");
  const [policyHistoryNextCursor, setPolicyHistoryNextCursor] = useState<string | null>(null);
  const [voiceHistory, setVoiceHistory] = useState<PolicyVersionMetadata[]>([]);
  const [voiceHistoryLoading, setVoiceHistoryLoading] = useState(false);
  const [voiceHistoryLoadingMore, setVoiceHistoryLoadingMore] = useState(false);
  const [voiceHistoryError, setVoiceHistoryError] = useState("");
  const [voiceHistoryNextCursor, setVoiceHistoryNextCursor] = useState<string | null>(null);

  const policyPath = (sessionId: string) =>
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(sessionId)}/participation-policy`;
  const policyHistoryPath = (sessionId: string) => `${policyPath(sessionId)}/history`;
  const voiceHistoryPath = (sessionId: string) =>
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(sessionId)}/voice-profile/history`;
  const voicePreviewPath = (sessionId: string) =>
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(sessionId)}/voice-profile/preview`;
  const globalControlPath = "/v1/admin/social/release-control";
  const tenantControlPath =
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/participation-control`;

  const resetTenantControl = () => {
    setTenantControlState(createVersionedResourceState());
  };

  const resetPolicy = (error = "") => {
    setPolicyState(
      error
        ? {
            ...createVersionedResourceState<GroupParticipationPolicyDocument>(),
            status: "error",
            error,
          }
        : createVersionedResourceState(),
    );
    setPolicyHistory([]);
    setPolicyHistoryError("");
    setPolicyHistoryNextCursor(null);
    setVoiceHistory([]);
    setVoiceHistoryError("");
    setVoiceHistoryNextCursor(null);
  };

  const loadPolicy = async (sessionId: string) => {
    setPolicyState((current) => ({ ...current, status: "loading", error: "" }));
    setNotice("");
    try {
      const result = await apiVersionedResource<GroupParticipationPolicyDocument>(
        config,
        policyPath(sessionId),
        { auth: true },
      );
      setPolicyState(markVersionedResourceLoaded(result.value, result.etag));
      setPolicyReason("");
    } catch (caught) {
      setPolicyState((current) => markVersionedResourceError(current, caught));
    }
  };

  const loadGlobalControl = async () => {
    setGlobalControlState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const result = await apiVersionedResource<ScopedParticipationControlDocument>(
        config,
        globalControlPath,
        { auth: true },
      );
      setGlobalControlState(markVersionedResourceLoaded(result.value, result.etag));
    } catch (caught) {
      setGlobalControlState((current) => markVersionedResourceError(current, caught));
    }
  };

  const loadTenantControl = async () => {
    setTenantControlState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const result = await apiVersionedResource<ScopedParticipationControlDocument>(
        config,
        tenantControlPath,
        { auth: true },
      );
      setTenantControlState(markVersionedResourceLoaded(result.value, result.etag));
    } catch (caught) {
      setTenantControlState((current) => markVersionedResourceError(current, caught));
    }
  };

  const loadPolicyHistory = async (
    sessionId: string,
    options: { cursor?: string; append?: boolean } = {},
  ) => {
    const append = Boolean(options.append);
    append ? setPolicyHistoryLoadingMore(true) : setPolicyHistoryLoading(true);
    setPolicyHistoryError("");
    try {
      const result = await apiRequest<PolicyVersionPage>(config, policyHistoryPath(sessionId), {
        auth: true,
        query: { limit: 50, cursor: options.cursor },
      });
      const incoming = result.items || [];
      setPolicyHistory((current) => append
        ? [...current, ...incoming.filter((item) => !current.some((known) => known.version === item.version))]
        : incoming);
      setPolicyHistoryNextCursor(result.next_cursor || null);
    } catch (caught) {
      if (!append) {
        setPolicyHistory([]);
        setPolicyHistoryNextCursor(null);
      }
      setPolicyHistoryError(caught instanceof Error ? caught.message : "群策略历史加载失败");
    } finally {
      append ? setPolicyHistoryLoadingMore(false) : setPolicyHistoryLoading(false);
    }
  };

  const loadVoiceHistory = async (
    sessionId: string,
    options: { cursor?: string; append?: boolean } = {},
  ) => {
    const append = Boolean(options.append);
    append ? setVoiceHistoryLoadingMore(true) : setVoiceHistoryLoading(true);
    setVoiceHistoryError("");
    try {
      const result = await apiRequest<PolicyVersionPage>(config, voiceHistoryPath(sessionId), {
        auth: true,
        query: { limit: 50, cursor: options.cursor },
      });
      const incoming = result.items || [];
      setVoiceHistory((current) => append
        ? [...current, ...incoming.filter((item) => !current.some((known) => known.version === item.version))]
        : incoming);
      setVoiceHistoryNextCursor(result.next_cursor || null);
    } catch (caught) {
      if (!append) {
        setVoiceHistory([]);
        setVoiceHistoryNextCursor(null);
      }
      setVoiceHistoryError(caught instanceof Error ? caught.message : "表达风格历史加载失败");
    } finally {
      append ? setVoiceHistoryLoadingMore(false) : setVoiceHistoryLoading(false);
    }
  };

  const updatePolicyDraft = (
    updater: (draft: GroupParticipationPolicyDocument) => GroupParticipationPolicyDocument,
  ) => {
    setPolicyState((current) =>
      current.draft ? editVersionedResource(current, updater(current.draft)) : current,
    );
    setNotice("");
  };

  const updateReleaseControl = (
    scope: "global" | "tenant",
    updater: (
      draft: ScopedParticipationControlDocument,
    ) => ScopedParticipationControlDocument,
  ) => {
    const setter = scope === "global" ? setGlobalControlState : setTenantControlState;
    setter((current) =>
      current.draft ? editVersionedResource(current, updater(current.draft)) : current,
    );
    setNotice("");
  };

  const saveReleaseControl = async (
    scope: "global" | "tenant",
    changeReason: string,
  ) => {
    const state = scope === "global" ? globalControlState : tenantControlState;
    const setter = scope === "global" ? setGlobalControlState : setTenantControlState;
    const path = scope === "global" ? globalControlPath : tenantControlPath;
    if (!state.draft || !state.etag) {
      throw new Error("请先成功读取发布控制及其版本");
    }
    const payload: ScopedParticipationControlUpdate = {
      control: state.draft.control,
      change_reason: changeReason,
    };
    const intent = `release-control:${scope}:${state.etag}:${JSON.stringify(payload)}`;
    setter((current) => ({ ...current, status: "saving", error: "" }));
    try {
      const result = await apiVersionedResource<
        ScopedParticipationControlDocument,
        ScopedParticipationControlUpdate
      >(config, path, {
        auth: true,
        method: "PUT",
        body: payload,
        ifMatch: state.etag,
        idempotencyKey: keyFor(intent),
      });
      clearIdempotencyKey(intent);
      setter(markVersionedResourceLoaded(result.value, result.etag));
      setNotice(scope === "global" ? "全局发布控制已保存。" : "租户发布控制已保存。");
      if (groupId) void loadPolicy(groupId);
    } catch (caught) {
      setter((current) => markVersionedResourceError(current, caught));
      throw caught;
    }
  };

  const savePolicy = async () => {
    setNotice("");
    try {
      const sessionId = selectedGroupForWrite();
      if (!policyState.draft || !policyState.etag) {
        throw new Error("请先成功读取当前群策略及其版本，再进行保存");
      }
      const voiceError = voiceProfileValidationError(
        policyState.draft.voice_profile,
        sessionId,
      );
      if (voiceError) throw new Error(voiceError);
      const payload: GroupParticipationPolicyUpdate = {
        kill_switches: policyState.draft.kill_switches,
        policy: policyState.draft.policy,
        voice_profile: policyState.draft.voice_profile,
        change_reason: policyReason.trim(),
      };
      const intent = `group-policy:save:${sessionId}:${policyState.etag}:${JSON.stringify(payload)}`;
      setPolicyState((current) => ({ ...current, status: "saving", error: "" }));
      const result = await apiVersionedResource<
        GroupParticipationPolicyDocument,
        GroupParticipationPolicyUpdate
      >(config, policyPath(sessionId), {
        auth: true,
        method: "PUT",
        body: payload,
        ifMatch: policyState.etag,
        idempotencyKey: keyFor(intent),
      });
      clearIdempotencyKey(intent);
      setPolicyState(markVersionedResourceLoaded(result.value, result.etag));
      setPolicyReason("");
      setNotice("群参与策略已保存，并记录了版本与审计信息。");
      void loadPolicyHistory(sessionId);
      void loadVoiceHistory(sessionId);
    } catch (caught) {
      setPolicyState((current) => markVersionedResourceError(current, caught));
    }
  };

  const previewVoiceProfile = async (
    profile: VoiceProfile,
    replyText: string,
    explicitlyDetailed: boolean,
  ): Promise<VoiceProfilePreviewDocument> => {
    const sessionId = selectedGroupForWrite();
    const payload: VoiceProfilePreviewRequest = {
      voice_profile: profile,
      reply_text: replyText.trim(),
      source_text: "",
      explicitly_detailed: explicitlyDetailed,
    };
    return apiRequest<VoiceProfilePreviewDocument>(
      config,
      voicePreviewPath(sessionId),
      {
        auth: true,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      },
    );
  };

  const rollbackPolicy = async (targetVersion: number, changeReason: string) => {
    setNotice("");
    const sessionId = selectedGroupForWrite();
    if (!policyState.draft || !policyState.etag) {
      throw new Error("请先成功读取当前群策略及其版本，再进行回滚");
    }
    if (policyState.dirty) {
      throw new Error("请先保存或放弃当前群策略草稿，再进行回滚");
    }
    const payload: GroupPolicyRollbackUpdate = {
      rollback_to_version: targetVersion,
      change_reason: changeReason,
    };
    const intent = `group-policy:rollback:${sessionId}:${policyState.etag}:${JSON.stringify(payload)}`;
    setPolicyState((current) => ({ ...current, status: "saving", error: "" }));
    try {
      const result = await apiVersionedResource<
        GroupParticipationPolicyDocument,
        GroupPolicyRollbackUpdate
      >(config, policyPath(sessionId), {
        auth: true,
        method: "PUT",
        body: payload,
        ifMatch: policyState.etag,
        idempotencyKey: keyFor(intent),
      });
      clearIdempotencyKey(intent);
      setPolicyState(markVersionedResourceLoaded(result.value, result.etag));
      setPolicyReason("");
      setNotice(`群参与策略已回滚到 v${targetVersion} 的内容，并生成新版本 v${result.value.version}。`);
      void loadPolicyHistory(sessionId);
      void loadVoiceHistory(sessionId);
    } catch (caught) {
      setPolicyState((current) => markVersionedResourceError(current, caught));
      throw caught;
    }
  };

  const reloadPolicy = () => {
    if (policyState.dirty) {
      requestDiscard(
        {
          title: "重新读取群策略？",
          description: "重新读取会用服务器当前版本替换本地群策略草稿。",
          confirmLabel: "放弃并重新读取",
        },
        () => void loadPolicy(groupId),
      );
      return;
    }
    void loadPolicy(groupId);
  };

  return {
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
    resetPolicy,
    resetTenantControl,
    loadPolicy,
    loadGlobalControl,
    loadTenantControl,
    loadPolicyHistory,
    loadVoiceHistory,
    updatePolicyDraft,
    updateReleaseControl,
    saveReleaseControl,
    savePolicy,
    previewVoiceProfile,
    rollbackPolicy,
    reloadPolicy,
  };
}

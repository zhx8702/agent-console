import { useState } from "react";

import {
  apiRequest,
  apiVersionedResource,
  createVersionedResourceState,
  editVersionedResource,
  markVersionedResourceError,
  markVersionedResourceLoaded,
  type MemberPrivacyPolicyDocument,
  type MemberPrivacyPolicyUpdate,
  type MemberPrivacyValues,
  type VersionedResourceState,
} from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import type {
  DiscardChangesPrompt,
} from "./DiscardChangesDialog";
import type { MemberMemoryItem } from "./MemberMemoryPanel";
import type { TenantMemberControlDocument } from "./TenantMemberControlPanel";
import type { PolicyVersionMetadata } from "./VersionRollbackPanel";

type MemberPolicyRollbackUpdate = {
  rollback_to_version: number;
  change_reason: string;
};

type PolicyVersionPage = {
  items: PolicyVersionMetadata[];
  next_cursor: string | null;
};

type MemberMemoryPage = {
  items: MemberMemoryItem[];
  next_cursor: string | null;
};

type TenantMemberControlUpdate = {
  control: TenantMemberControlDocument["control"];
  request_memory_deletion: boolean;
  change_reason: string;
};

type MemberPrivacyControllerOptions = {
  config: ConsoleConfig;
  selectedGroupForWrite: () => string;
  keyFor: (intent: string) => string;
  clearIdempotencyKey: (intent: string) => void;
  setNotice: (notice: string) => void;
  requestDiscard: (prompt: DiscardChangesPrompt, action: () => void) => void;
};

function editableMemberDocument(
  document: MemberPrivacyPolicyDocument,
): MemberPrivacyPolicyDocument {
  const configured = document.configured_policy;
  if (!configured) return document;
  return {
    ...document,
    policy: configured,
    configured_policy: configured,
    effective_policy: document.policy,
  };
}

export function useMemberPrivacyController({
  config,
  selectedGroupForWrite,
  keyFor,
  clearIdempotencyKey,
  setNotice,
  requestDiscard,
}: MemberPrivacyControllerOptions) {
  const [memberIdInput, setMemberIdInput] = useState("");
  const [activeMemberId, setActiveMemberId] = useState("");
  const [memberState, setMemberState] = useState<
    VersionedResourceState<MemberPrivacyPolicyDocument>
  >(() => createVersionedResourceState());
  const [memberReason, setMemberReason] = useState("");
  const [tenantMemberState, setTenantMemberState] = useState<
    VersionedResourceState<TenantMemberControlDocument>
  >(() => createVersionedResourceState());
  const [memberHistory, setMemberHistory] = useState<PolicyVersionMetadata[]>([]);
  const [memberHistoryLoading, setMemberHistoryLoading] = useState(false);
  const [memberHistoryLoadingMore, setMemberHistoryLoadingMore] = useState(false);
  const [memberHistoryError, setMemberHistoryError] = useState("");
  const [memberHistoryNextCursor, setMemberHistoryNextCursor] = useState<string | null>(null);
  const [memberMemoryItems, setMemberMemoryItems] = useState<MemberMemoryItem[]>([]);
  const [memberMemoryLoading, setMemberMemoryLoading] = useState(false);
  const [memberMemoryLoadingMore, setMemberMemoryLoadingMore] = useState(false);
  const [memberMemoryMutatingId, setMemberMemoryMutatingId] = useState<number | null>(null);
  const [memberMemoryError, setMemberMemoryError] = useState("");
  const [memberMemoryNextCursor, setMemberMemoryNextCursor] = useState<string | null>(null);

  const memberPath = (sessionId: string, userId: string) =>
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(sessionId)}/members/${encodeURIComponent(userId)}/privacy-policy`;
  const memberHistoryPath = (sessionId: string, userId: string) =>
    `${memberPath(sessionId, userId)}/history`;
  const tenantMemberPath = (userId: string) =>
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/members/${encodeURIComponent(userId)}/control`;
  const memberMemoryPath = (sessionId: string, userId: string, itemId?: number) =>
    `/v1/admin/tenants/${encodeURIComponent(config.tenantId)}/groups/${encodeURIComponent(sessionId)}/members/${encodeURIComponent(userId)}/memory-items${itemId ? `/${itemId}` : ""}`;

  const resetMember = () => {
    setActiveMemberId("");
    setMemberState(createVersionedResourceState());
    setTenantMemberState(createVersionedResourceState());
    setMemberHistory([]);
    setMemberHistoryError("");
    setMemberHistoryNextCursor(null);
    setMemberMemoryItems([]);
    setMemberMemoryError("");
    setMemberMemoryNextCursor(null);
  };

  const loadMemberHistory = async (
    sessionId: string,
    userId: string,
    options: { cursor?: string; append?: boolean } = {},
  ) => {
    const append = Boolean(options.append);
    append ? setMemberHistoryLoadingMore(true) : setMemberHistoryLoading(true);
    setMemberHistoryError("");
    try {
      const result = await apiRequest<PolicyVersionPage>(
        config,
        memberHistoryPath(sessionId, userId),
        { auth: true, query: { limit: 50, cursor: options.cursor } },
      );
      const incoming = result.items || [];
      setMemberHistory((current) => append
        ? [...current, ...incoming.filter((item) => !current.some((known) => known.version === item.version))]
        : incoming);
      setMemberHistoryNextCursor(result.next_cursor || null);
    } catch (caught) {
      if (!append) {
        setMemberHistory([]);
        setMemberHistoryNextCursor(null);
      }
      setMemberHistoryError(caught instanceof Error ? caught.message : "成员策略历史加载失败");
    } finally {
      append ? setMemberHistoryLoadingMore(false) : setMemberHistoryLoading(false);
    }
  };

  const loadMemberMemory = async (
    sessionId: string,
    userId: string,
    options: { cursor?: string; append?: boolean } = {},
  ) => {
    const append = Boolean(options.append);
    append ? setMemberMemoryLoadingMore(true) : setMemberMemoryLoading(true);
    setMemberMemoryError("");
    try {
      const result = await apiRequest<MemberMemoryPage>(
        config,
        memberMemoryPath(sessionId, userId),
        { auth: true, query: { limit: 50, cursor: options.cursor } },
      );
      const incoming = result.items || [];
      setMemberMemoryItems((current) => append
        ? [...current, ...incoming.filter((item) => !current.some((known) => known.item_id === item.item_id))]
        : incoming);
      setMemberMemoryNextCursor(result.next_cursor || null);
    } catch (caught) {
      if (!append) {
        setMemberMemoryItems([]);
        setMemberMemoryNextCursor(null);
      }
      setMemberMemoryError(caught instanceof Error ? caught.message : "成员记忆加载失败");
    } finally {
      append ? setMemberMemoryLoadingMore(false) : setMemberMemoryLoading(false);
    }
  };

  const loadTenantMemberControl = async (userId: string) => {
    setTenantMemberState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const result = await apiVersionedResource<TenantMemberControlDocument>(
        config,
        tenantMemberPath(userId),
        { auth: true },
      );
      setTenantMemberState(markVersionedResourceLoaded(result.value, result.etag));
    } catch (caught) {
      setTenantMemberState((current) => markVersionedResourceError(current, caught));
    }
  };

  const loadMemberById = async (userId: string) => {
    try {
      const sessionId = selectedGroupForWrite();
      setMemberState((current) => ({ ...current, status: "loading", error: "" }));
      const result = await apiVersionedResource<MemberPrivacyPolicyDocument>(
        config,
        memberPath(sessionId, userId),
        { auth: true },
      );
      setActiveMemberId(userId);
      setMemberState(
        markVersionedResourceLoaded(editableMemberDocument(result.value), result.etag),
      );
      setMemberReason("");
      void loadMemberHistory(sessionId, userId);
      void loadMemberMemory(sessionId, userId);
      void loadTenantMemberControl(userId);
    } catch (caught) {
      setMemberState((current) => markVersionedResourceError(current, caught));
    }
  };

  const loadMember = async () => {
    const userId = memberIdInput.trim();
    if (!userId) {
      setMemberState((current) => ({
        ...current,
        status: "error",
        error: "请输入成员微信标识",
      }));
      return;
    }
    if (memberState.dirty || tenantMemberState.dirty) {
      requestDiscard(
        {
          title: "读取另一位成员策略？",
          description: "读取会放弃当前成员尚未保存的隐私与记忆策略草稿。",
          confirmLabel: "放弃并读取",
        },
        () => {
          setMemberIdInput(userId);
          void loadMemberById(userId);
        },
      );
      return;
    }
    await loadMemberById(userId);
  };

  const updateMemberDraft = (
    updater: (policy: MemberPrivacyValues) => MemberPrivacyValues,
  ) => {
    setMemberState((current) =>
      current.draft
        ? editVersionedResource(current, {
            ...current.draft,
            policy: updater(current.draft.policy),
          })
        : current,
    );
    setNotice("");
  };

  const updateTenantMemberDraft = (
    updater: (value: TenantMemberControlDocument) => TenantMemberControlDocument,
  ) => {
    setTenantMemberState((current) =>
      current.draft ? editVersionedResource(current, updater(current.draft)) : current,
    );
    setNotice("");
  };

  const writeTenantMemberControl = async (
    requestMemoryDeletion: boolean,
    changeReason: string,
  ) => {
    if (!activeMemberId || !tenantMemberState.draft || !tenantMemberState.etag) {
      throw new Error("请先读取租户级成员控制及其版本");
    }
    const control = requestMemoryDeletion
      ? { ...tenantMemberState.draft.control, memory_opt_out: true }
      : tenantMemberState.draft.control;
    const payload: TenantMemberControlUpdate = {
      control,
      request_memory_deletion: requestMemoryDeletion,
      change_reason: changeReason,
    };
    const intent = `tenant-member-control:${activeMemberId}:${tenantMemberState.etag}:${JSON.stringify(payload)}`;
    setTenantMemberState((current) => ({ ...current, status: "saving", error: "" }));
    try {
      const result = await apiVersionedResource<
        TenantMemberControlDocument,
        TenantMemberControlUpdate
      >(config, tenantMemberPath(activeMemberId), {
        auth: true,
        method: "PUT",
        body: payload,
        ifMatch: tenantMemberState.etag,
        idempotencyKey: keyFor(intent),
      });
      clearIdempotencyKey(intent);
      setTenantMemberState(markVersionedResourceLoaded(result.value, result.etag));
      setNotice(
        requestMemoryDeletion
          ? `成员 ${activeMemberId} 的跨群记忆删除请求已持久化，删除期间保持退出召回。`
          : `成员 ${activeMemberId} 的跨群退出设置已保存。`,
      );
      void loadMemberById(activeMemberId);
    } catch (caught) {
      setTenantMemberState((current) => markVersionedResourceError(current, caught));
      throw caught;
    }
  };

  const saveTenantMember = (changeReason: string) =>
    writeTenantMemberControl(false, changeReason);

  const requestTenantMemberErasure = (changeReason: string) =>
    writeTenantMemberControl(true, changeReason);

  const saveMember = async () => {
    setNotice("");
    try {
      const sessionId = selectedGroupForWrite();
      if (!activeMemberId || !memberState.draft || !memberState.etag) {
        throw new Error("请先读取成员隐私策略及其版本");
      }
      if (
        memberState.draft.policy.audience_scope === "explicit"
        && !memberState.draft.policy.allowed_session_ids.length
      ) {
        throw new Error("选择“仅指定会话”时，至少需要填写一个允许会话");
      }
      const payload: MemberPrivacyPolicyUpdate = {
        policy: memberState.draft.policy,
        change_reason: memberReason.trim(),
      };
      const intent = `member-policy:save:${sessionId}:${activeMemberId}:${memberState.etag}:${JSON.stringify(payload)}`;
      setMemberState((current) => ({ ...current, status: "saving", error: "" }));
      const result = await apiVersionedResource<
        MemberPrivacyPolicyDocument,
        MemberPrivacyPolicyUpdate
      >(config, memberPath(sessionId, activeMemberId), {
        auth: true,
        method: "PUT",
        body: payload,
        ifMatch: memberState.etag,
        idempotencyKey: keyFor(intent),
      });
      clearIdempotencyKey(intent);
      setMemberState(
        markVersionedResourceLoaded(editableMemberDocument(result.value), result.etag),
      );
      setMemberReason("");
      setNotice(`成员 ${activeMemberId} 的隐私与记忆策略已保存。`);
      void loadMemberHistory(sessionId, activeMemberId);
      void loadMemberMemory(sessionId, activeMemberId);
    } catch (caught) {
      setMemberState((current) => markVersionedResourceError(current, caught));
    }
  };

  const rollbackMember = async (targetVersion: number, changeReason: string) => {
    setNotice("");
    const sessionId = selectedGroupForWrite();
    if (!activeMemberId || !memberState.draft || !memberState.etag) {
      throw new Error("请先读取成员隐私策略及其版本");
    }
    if (memberState.dirty) {
      throw new Error("请先保存或放弃当前成员策略草稿，再进行回滚");
    }
    const payload: MemberPolicyRollbackUpdate = {
      rollback_to_version: targetVersion,
      change_reason: changeReason,
    };
    const intent = `member-policy:rollback:${sessionId}:${activeMemberId}:${memberState.etag}:${JSON.stringify(payload)}`;
    setMemberState((current) => ({ ...current, status: "saving", error: "" }));
    try {
      const result = await apiVersionedResource<
        MemberPrivacyPolicyDocument,
        MemberPolicyRollbackUpdate
      >(config, memberPath(sessionId, activeMemberId), {
        auth: true,
        method: "PUT",
        body: payload,
        ifMatch: memberState.etag,
        idempotencyKey: keyFor(intent),
      });
      clearIdempotencyKey(intent);
      setMemberState(
        markVersionedResourceLoaded(editableMemberDocument(result.value), result.etag),
      );
      setMemberReason("");
      setNotice(`成员 ${activeMemberId} 的策略已回滚到 v${targetVersion} 的内容，并生成新版本 v${result.value.version}。`);
      void loadMemberHistory(sessionId, activeMemberId);
    } catch (caught) {
      setMemberState((current) => markVersionedResourceError(current, caught));
      throw caught;
    }
  };

  const correctMemberMemory = async (
    item: MemberMemoryItem,
    content: string,
    reason: string,
  ) => {
    const sessionId = selectedGroupForWrite();
    if (!activeMemberId) throw new Error("请先选择成员");
    const intent = `member-memory:correct:${sessionId}:${activeMemberId}:${item.item_id}:${item.etag}:${content}:${reason}`;
    setMemberMemoryMutatingId(item.item_id);
    setMemberMemoryError("");
    try {
      const updated = await apiRequest<MemberMemoryItem>(
        config,
        memberMemoryPath(sessionId, activeMemberId, item.item_id),
        {
          auth: true,
          init: {
            method: "PATCH",
            headers: {
              "Content-Type": "application/json",
              "If-Match": item.etag,
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({ content, reason }),
          },
        },
      );
      clearIdempotencyKey(intent);
      setMemberMemoryItems((current) => current.map((candidate) =>
        candidate.item_id === item.item_id ? updated : candidate));
      setNotice(`成员 ${activeMemberId} 的记忆已更正，审计记录未保存正文。`);
    } catch (caught) {
      setMemberMemoryError(caught instanceof Error ? caught.message : "成员记忆更正失败");
      throw caught;
    } finally {
      setMemberMemoryMutatingId(null);
    }
  };

  const deleteMemberMemory = async (item: MemberMemoryItem) => {
    const sessionId = selectedGroupForWrite();
    if (!activeMemberId) throw new Error("请先选择成员");
    const intent = `member-memory:delete:${sessionId}:${activeMemberId}:${item.item_id}:${item.etag}`;
    setMemberMemoryMutatingId(item.item_id);
    setMemberMemoryError("");
    try {
      await apiRequest(
        config,
        memberMemoryPath(sessionId, activeMemberId, item.item_id),
        {
          auth: true,
          query: { allow_pinned: true },
          init: {
            method: "DELETE",
            headers: {
              "If-Match": item.etag,
              "Idempotency-Key": keyFor(intent),
            },
          },
        },
      );
      clearIdempotencyKey(intent);
      setMemberMemoryItems((current) =>
        current.filter((candidate) => candidate.item_id !== item.item_id));
      setNotice(`成员 ${activeMemberId} 的记忆已删除。`);
    } catch (caught) {
      setMemberMemoryError(caught instanceof Error ? caught.message : "成员记忆删除失败");
      throw caught;
    } finally {
      setMemberMemoryMutatingId(null);
    }
  };

  return {
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
    resetMember,
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
  };
}

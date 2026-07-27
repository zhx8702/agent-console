import { useCallback, useEffect, useMemo, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { requireSelectedGroup, useConsoleConfig } from "../../state/console-config";
import {
  type GroupRosterCandidate,
  type ProfileEnrichmentCandidate,
  type ProfileEnrichmentReviewAction,
  type ProfileEnrichmentReviewState,
  type WxbotSession,
  friendlyApiError,
  getMemberDisplayName,
  getMemberProfileQuery,
  optionalText,
} from "./model";
import { ProfileEnrichmentPanel } from "./ProfileEnrichmentPanel";

interface ProfileEnrichmentWorkbenchProps {
  sessions: WxbotSession[];
  members: GroupRosterCandidate[];
  sessionId: string;
  userId: string;
  selectedSessionIsGroup: boolean;
  selectedMemberIsVerified: boolean;
  onOutput: (value: string) => void;
}

export function ProfileEnrichmentWorkbench({
  sessions,
  members,
  sessionId,
  userId,
  selectedSessionIsGroup,
  selectedMemberIsVerified,
  onOutput: setProfileEnrichmentOutput,
}: ProfileEnrichmentWorkbenchProps) {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [profileEnrichmentCandidates, setProfileEnrichmentCandidates] = useState<ProfileEnrichmentCandidate[]>([]);
  const [selectedProfileEnrichmentId, setSelectedProfileEnrichmentId] = useState<number | null>(null);
  const [profileEnrichmentChannel, setProfileEnrichmentChannel] = useState("wechat");
  const [profileEnrichmentSourceKey, setProfileEnrichmentSourceKey] = useState("wxbot");
  const [profileEnrichmentQuery, setProfileEnrichmentQuery] = useState("");
  const [profileEnrichmentHours, setProfileEnrichmentHours] = useState(168);
  const [profileEnrichmentLimit, setProfileEnrichmentLimit] = useState(8);
  const [profileEnrichmentExternalJson, setProfileEnrichmentExternalJson] = useState("[]");
  const [profileEnrichmentReviewState, setProfileEnrichmentReviewState] = useState<ProfileEnrichmentReviewState>("needs_review");
  const [profileEnrichmentIncludeHidden, setProfileEnrichmentIncludeHidden] = useState(false);
  const [profileEnrichmentListLimit, setProfileEnrichmentListLimit] = useState(100);
  const [profileEnrichmentNotes, setProfileEnrichmentNotes] = useState("");
  const [profileEnrichmentBusy, setProfileEnrichmentBusy] = useState<string | null>(null);

  const selectedProfileEnrichmentCandidate = useMemo(
    () => profileEnrichmentCandidates.find((item) => item.id === selectedProfileEnrichmentId) || null,
    [profileEnrichmentCandidates, selectedProfileEnrichmentId],
  );
  const selectedGroup = useMemo(
    () => sessions.find((item) => item.session_id === sessionId) || null,
    [sessionId, sessions],
  );
  const selectedMember = useMemo(
    () => members.find((item) => item.wxid === userId) || null,
    [members, userId],
  );
  const scopeReady = Boolean(
    sessionId.trim()
    && verifiedGroupIds.has(sessionId.trim())
    && userId.trim()
    && members.some((item) => item.wxid === userId.trim())
    && profileEnrichmentQuery.trim()
  );
  const scopeSummary = selectedMember
    ? `${getMemberDisplayName(selectedMember)} · ${selectedMember.wxid}`
    : userId.trim() || "尚未选择群成员";
  const requireMemoryMemberScope = useCallback(() => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const memberId = userId.trim();
    if (!memberId || !members.some((item) => item.wxid === memberId)) {
      throw new Error("请先从当前群的已验证成员名册选择记忆对象");
    }
    return { groupId, memberId };
  }, [config, members, userId, verifiedGroupIds]);

  useEffect(() => {
    setProfileEnrichmentQuery(selectedMember ? getMemberProfileQuery(selectedMember) : "");
    setProfileEnrichmentCandidates([]);
    setSelectedProfileEnrichmentId(null);
    setProfileEnrichmentChannel("wechat");
    setProfileEnrichmentSourceKey("wxbot");
    setProfileEnrichmentReviewState("needs_review");
    setProfileEnrichmentIncludeHidden(false);
  }, [selectedMember, sessionId]);

  const loadProfileEnrichmentCandidates = useCallback(async (overrides: Partial<{
    tenantId: string;
    channel: string;
    sourceKey: string;
    sessionId: string;
    userId: string;
    reviewState: ProfileEnrichmentReviewState;
    includeHidden: boolean;
    listLimit: number;
  }> = {}) => {
    const nextChannel = "channel" in overrides ? overrides.channel || "" : profileEnrichmentChannel;
    const nextSourceKey = "sourceKey" in overrides ? overrides.sourceKey || "" : profileEnrichmentSourceKey;
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setProfileEnrichmentCandidates([]);
      setProfileEnrichmentOutput(formatJson({ error: "请先选择已验证群聊和群成员" }));
      return;
    }
    const nextReviewState = "reviewState" in overrides ? overrides.reviewState || "" : profileEnrichmentReviewState;
    const nextIncludeHidden = "includeHidden" in overrides ? Boolean(overrides.includeHidden) : profileEnrichmentIncludeHidden;
    const nextListLimit = "listLimit" in overrides ? overrides.listLimit || 100 : profileEnrichmentListLimit;
    try {
      const result = await apiRequest<{ items?: ProfileEnrichmentCandidate[] }>(
        config,
        "/plugins/memory/profile-enrichment/candidates",
        {
          auth: true,
          query: {
            tenant_id: config.tenantId,
            channel: optionalText(nextChannel),
            source_key: optionalText(nextSourceKey),
            session_id: sessionId.trim(),
            user_id: userId.trim(),
            review_state: nextReviewState || undefined,
            include_hidden: nextIncludeHidden,
            limit: Math.max(1, Math.min(500, nextListLimit || 100)),
          },
        },
      );
      const items = result.items || [];
      setProfileEnrichmentCandidates(items);
      setSelectedProfileEnrichmentId((current) => (
        current && !items.some((item) => item.id === current) ? null : current
      ));
      setProfileEnrichmentOutput(formatJson(result));
    } catch (err) {
      setProfileEnrichmentCandidates([]);
      setProfileEnrichmentOutput(formatJson({ error: friendlyApiError(err, "人物画像候选列表读取失败") }));
    }
  }, [
    config,
    profileEnrichmentChannel,
    profileEnrichmentIncludeHidden,
    profileEnrichmentListLimit,
    profileEnrichmentReviewState,
    profileEnrichmentSourceKey,
    selectedMemberIsVerified,
    selectedSessionIsGroup,
    sessionId,
    userId,
  ]);

  const loadProfileEnrichmentDetail = useCallback(async (candidateId?: number | null) => {
    const targetId = candidateId ?? selectedProfileEnrichmentId;
    if (!targetId) {
      setProfileEnrichmentOutput(formatJson({ error: "请先选择一条人物画像候选" }));
      return;
    }
    try {
      const result = await apiRequest<ProfileEnrichmentCandidate>(
        config,
        `/plugins/memory/profile-enrichment/candidates/${encodeURIComponent(String(targetId))}`,
        { auth: true },
      );
      if (
        result.session_id !== sessionId.trim()
        || result.user_id !== userId.trim()
        || !selectedSessionIsGroup
        || !selectedMemberIsVerified
      ) {
        throw new Error("候选不属于当前已验证群成员，已拒绝展示");
      }
      setSelectedProfileEnrichmentId(result.id);
      setProfileEnrichmentCandidates((current) => {
        const exists = current.some((item) => item.id === result.id);
        return exists
          ? current.map((item) => item.id === result.id ? result : item)
          : [result, ...current];
      });
      setProfileEnrichmentOutput(formatJson(result));
    } catch (err) {
      setProfileEnrichmentOutput(formatJson({ error: friendlyApiError(err, "人物画像候选详情读取失败") }));
    }
  }, [config, selectedMemberIsVerified, selectedProfileEnrichmentId, selectedSessionIsGroup, sessionId, userId]);

  const generateProfileEnrichmentFromReport = async () => {
    const { groupId, memberId } = requireMemoryMemberScope();
    if (!profileEnrichmentQuery.trim()) {
      const error = new Error("请选择群成员并确认公开查询名称");
      setProfileEnrichmentOutput(formatJson({ error: error.message }));
      throw error;
    }
    let externalCandidates: unknown;
    try {
      externalCandidates = profileEnrichmentExternalJson.trim()
        ? JSON.parse(profileEnrichmentExternalJson)
        : [];
    } catch (err) {
      const error = new Error(err instanceof Error ? `外部候选数据（external_candidates）JSON 无效：${err.message}` : "外部候选数据（external_candidates）JSON 无效");
      setProfileEnrichmentOutput(formatJson({ error: error.message }));
      throw error;
    }
    if (!Array.isArray(externalCandidates)) {
      const error = new Error("外部候选数据（external_candidates）必须是 JSON 数组");
      setProfileEnrichmentOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `memory:profile-enrichment:${config.tenantId}:${groupId}:${memberId}:${profileEnrichmentQuery.trim()}`;
    setProfileEnrichmentBusy("generate");
    try {
      const result = await apiRequest<ProfileEnrichmentCandidate>(
        config,
        "/plugins/memory/profile-enrichment/candidates/from-report",
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({
              tenant_id: config.tenantId,
              channel: profileEnrichmentChannel.trim() || "wechat",
              source_key: profileEnrichmentSourceKey.trim() || "wxbot",
              session_id: groupId,
              user_id: memberId,
              query: profileEnrichmentQuery.trim(),
              hours: Math.max(1, Math.min(720, profileEnrichmentHours || 168)),
              limit: Math.max(1, Math.min(20, profileEnrichmentLimit || 8)),
              external_candidates: externalCandidates,
            }),
          },
        },
      );
      setSelectedProfileEnrichmentId(result.id);
      setProfileEnrichmentCandidates((current) => [result, ...current.filter((item) => item.id !== result.id)]);
      setProfileEnrichmentOutput(formatJson(result));
      await loadProfileEnrichmentCandidates();
      setSelectedProfileEnrichmentId(result.id);
      setProfileEnrichmentCandidates((current) => {
        const exists = current.some((item) => item.id === result.id);
        return exists
          ? current.map((item) => item.id === result.id ? result : item)
          : [result, ...current];
      });
      setProfileEnrichmentOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setProfileEnrichmentOutput(formatJson({ error: friendlyApiError(err, "从报告生成人物画像候选失败") }));
      throw err;
    } finally {
      setProfileEnrichmentBusy(null);
    }
  };

  const reviewProfileEnrichmentCandidate = async (action: ProfileEnrichmentReviewAction) => {
    const { groupId, memberId } = requireMemoryMemberScope();
    if (!selectedProfileEnrichmentId) {
      const error = new Error("请先选择一条人物画像候选");
      setProfileEnrichmentOutput(formatJson({ error: error.message }));
      throw error;
    }
    if (
      !selectedProfileEnrichmentCandidate
      || selectedProfileEnrichmentCandidate.session_id !== groupId
      || selectedProfileEnrichmentCandidate.user_id !== memberId
    ) {
      const error = new Error("只能复核当前已验证群成员的画像候选");
      setProfileEnrichmentOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `memory:profile-review:${selectedProfileEnrichmentId}:${action}:${profileEnrichmentNotes.trim()}`;
    setProfileEnrichmentBusy(action);
    try {
      const result = await apiRequest<ProfileEnrichmentCandidate>(
        config,
        `/plugins/memory/profile-enrichment/candidates/${encodeURIComponent(String(selectedProfileEnrichmentId))}/review`,
        {
          auth: true,
          query: { tenant_id: config.tenantId },
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({
              action,
              notes: profileEnrichmentNotes.trim() || undefined,
            }),
          },
        },
      );
      setSelectedProfileEnrichmentId(result.id);
      setProfileEnrichmentCandidates((current) => current.map((item) => item.id === result.id ? result : item));
      setProfileEnrichmentOutput(formatJson(result));
      await loadProfileEnrichmentCandidates();
      setSelectedProfileEnrichmentId(result.id);
      setProfileEnrichmentCandidates((current) => {
        const exists = current.some((item) => item.id === result.id);
        return exists
          ? current.map((item) => item.id === result.id ? result : item)
          : [result, ...current];
      });
      setProfileEnrichmentOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setProfileEnrichmentOutput(formatJson({ error: friendlyApiError(err, `人物画像候选复核操作 ${action} 失败`) }));
      throw err;
    } finally {
      setProfileEnrichmentBusy(null);
    }
  };

  const refreshCurrentProfileEnrichmentCandidates = useCallback(() => {
    setProfileEnrichmentReviewState("needs_review");
    setProfileEnrichmentIncludeHidden(false);
    void loadProfileEnrichmentCandidates({
      reviewState: "needs_review",
      includeHidden: false,
    });
  }, [loadProfileEnrichmentCandidates]);

  const showAllProfileEnrichmentNeedsReview = useCallback(() => {
    setProfileEnrichmentChannel("wechat");
    setProfileEnrichmentSourceKey("wxbot");
    setProfileEnrichmentReviewState("needs_review");
    setProfileEnrichmentIncludeHidden(false);
    void loadProfileEnrichmentCandidates({
      channel: "wechat",
      sourceKey: "wxbot",
      reviewState: "needs_review",
      includeHidden: false,
    });
  }, [loadProfileEnrichmentCandidates]);
  return (
    <ProfileEnrichmentPanel
      candidates={profileEnrichmentCandidates}
      selectedCandidateId={selectedProfileEnrichmentId}
      selectedCandidate={selectedProfileEnrichmentCandidate}
      selectedGroup={selectedGroup}
      scopeReady={scopeReady}
      scopeSummary={scopeSummary}
      sessionId={sessionId}
      userId={userId}
      query={profileEnrichmentQuery}
      hours={profileEnrichmentHours}
      limit={profileEnrichmentLimit}
      externalCandidatesJson={profileEnrichmentExternalJson}
      reviewState={profileEnrichmentReviewState}
      includeHidden={profileEnrichmentIncludeHidden}
      listLimit={profileEnrichmentListLimit}
      notes={profileEnrichmentNotes}
      busy={profileEnrichmentBusy}
      onGenerate={generateProfileEnrichmentFromReport}
      onQueryChange={setProfileEnrichmentQuery}
      onHoursChange={setProfileEnrichmentHours}
      onLimitChange={setProfileEnrichmentLimit}
      onExternalCandidatesJsonChange={setProfileEnrichmentExternalJson}
      onReviewStateChange={setProfileEnrichmentReviewState}
      onIncludeHiddenChange={setProfileEnrichmentIncludeHidden}
      onListLimitChange={setProfileEnrichmentListLimit}
      onNotesChange={setProfileEnrichmentNotes}
      onRefreshCurrent={refreshCurrentProfileEnrichmentCandidates}
      onShowAllNeedsReview={showAllProfileEnrichmentNeedsReview}
      onLoadDetail={loadProfileEnrichmentDetail}
      onReview={reviewProfileEnrichmentCandidate}
    />
  );
}

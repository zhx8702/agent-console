import { useCallback, useEffect, useMemo, useState } from "react";

import { apiRequest } from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import {
  formatSessionLabel,
  isGroupSession,
  type KnowledgeScopeMode,
  type KnowledgeTab,
  type WxbotSession,
} from "./model";

type ScopeOptions = {
  config: ConsoleConfig;
  verifiedGroupIds: ReadonlySet<string>;
  registerVerifiedGroups: (groupIds: readonly string[]) => void;
  selectVerifiedGroup: (groupId: string) => void;
};

export function useKnowledgeScope({
  config,
  verifiedGroupIds,
  registerVerifiedGroups,
  selectVerifiedGroup,
}: ScopeOptions) {
  const [scopeMode, setScopeMode] = useState<KnowledgeScopeMode>("global");
  const [targetSessionId, setTargetSessionId] = useState(config.sessionId);
  const [sessions, setSessions] = useState<WxbotSession[]>([]);
  const [activeTab, setActiveTab] = useState<KnowledgeTab>("faq");

  const requestedSessionId = targetSessionId.trim();
  const effectiveSessionId = scopeMode === "global"
    ? ""
    : verifiedGroupIds.has(requestedSessionId)
      ? requestedSessionId
      : "";
  const isScopeReady = scopeMode === "global" || Boolean(effectiveSessionId);

  const sessionOptions = useMemo(() => sessions.map((item) => ({
    value: item.session_id,
    label: formatSessionLabel(item),
    keywords: [item.session_id, item.session_name || "", item.kind || ""],
  })), [sessions]);

  const currentScopeText = scopeMode === "global"
    ? "全局知识库"
    : effectiveSessionId || config.sessionId || "未选择会话";

  const loadSessions = useCallback(async () => {
    if (!config.adminToken) {
      setSessions([]);
      return;
    }
    try {
      const result = await apiRequest<{ sessions?: WxbotSession[] }>(
        config,
        "/plugins/wxbot/admin/roster/groups",
        { auth: true },
      );
      const groups = (result.sessions || [])
        .filter((item) => item.session_id?.trim() && isGroupSession(item))
        .map((item) => ({ ...item, session_id: item.session_id.trim(), kind: "group" }))
        .sort((left, right) => (left.session_name || left.session_id)
          .localeCompare(right.session_name || right.session_id, "zh-CN"));
      setSessions(groups);
      registerVerifiedGroups(groups.map((item) => item.session_id));
    } catch {
      setSessions([]);
    }
  }, [config, registerVerifiedGroups]);

  useEffect(() => setTargetSessionId(config.sessionId), [config.sessionId]);
  useEffect(() => { void loadSessions(); }, [loadSessions]);

  return {
    scopeMode,
    setScopeMode,
    targetSessionId,
    setTargetSessionId,
    sessions,
    activeTab,
    setActiveTab,
    effectiveSessionId,
    isScopeReady,
    sessionOptions,
    currentScopeText,
    selectVerifiedGroup,
  };
}

export type KnowledgeScopeController = ReturnType<typeof useKnowledgeScope>;

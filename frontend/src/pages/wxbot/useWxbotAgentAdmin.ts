import { useCallback, useEffect, useMemo, useState } from "react";

import {
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  formatJson,
} from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import type {
  AgentToolAuditItem,
  AgentToolCatalogItem,
  AgentToolPolicy,
  WxbotTab,
} from "./model";

type AgentAdminOptions = {
  activeTab: WxbotTab;
  clearIdempotencyKey: (intent: string) => void;
  config: ConsoleConfig;
  effectiveGroupSessionId: string;
  keyFor: (intent: string) => string;
};

function editableAgentTools(policy: AgentToolPolicy): string[] {
  if (policy.inherits_default_tools) {
    return policy.available_tools || [];
  }
  return policy.allowed_tools || [];
}

export function useWxbotAgentAdmin({
  activeTab,
  clearIdempotencyKey,
  config,
  effectiveGroupSessionId,
  keyFor,
}: AgentAdminOptions) {
  const [agentToolCatalog, setAgentToolCatalog] = useState<AgentToolCatalogItem[]>([]);
  const [agentScopes, setAgentScopes] = useState<string[]>(["group_info"]);
  const [agentScope, setAgentScope] = useState("group_info");
  const [agentToolPolicySnapshot, setAgentToolPolicySnapshot] = useState<AgentToolPolicy | null>(null);
  const [agentToolPolicyEtag, setAgentToolPolicyEtag] = useState<string | null>(null);
  const [agentToolPolicyStatus, setAgentToolPolicyStatus] = useState<
    "idle" | "loading" | "loaded" | "saving" | "error" | "conflict"
  >("idle");
  const [agentToolAuditItems, setAgentToolAuditItems] = useState<AgentToolAuditItem[]>([]);
  const [agentEnabled, setAgentEnabled] = useState("true");
  const [agentAllowedTools, setAgentAllowedTools] = useState<string[]>([]);
  const [agentAuditSessionFilter, setAgentAuditSessionFilter] = useState("__current__");
  const [agentAuditToolName, setAgentAuditToolName] = useState("");
  const [agentAuditTraceId, setAgentAuditTraceId] = useState("");
  const [agentAuditLimit, setAgentAuditLimit] = useState(20);
  const [agentOutput, setAgentOutput] = useState('{\n  "status": "waiting"\n}');

  const agentToolOwners = useMemo(
    () => Array.from(new Set(agentToolCatalog.map((item) => (item.owner || "").trim()).filter(Boolean))),
    [agentToolCatalog],
  );

  const agentScopeLabel = useMemo(() => {
    if (agentScope === "group_plugin_status") return "群插件状态智能体";
    if (agentScope === "group_draw_generation") return "群绘图智能体";
    if (agentScope === "group_personal_map") return "高德个人地图智能体";
    if (agentScope === "file_analysis") return "群文件处理智能体";
    if (agentScope === "message_export") return "群消息文件导出智能体";
    return "群资料 / 成员查询智能体";
  }, [agentScope]);

  const loadAgentToolCatalog = useCallback(async () => {
    try {
      const result = await apiRequest<{
        items?: AgentToolCatalogItem[];
        count?: number;
        scope?: string;
        scopes?: string[];
      }>(config, "/plugins/wxbot/admin/agent-tools/catalog", {
        auth: true,
        query: { scope: agentScope },
      });
      setAgentToolCatalog(result.items || []);
      setAgentScopes(result.scopes?.length ? result.scopes : [agentScope]);
      setAgentOutput(formatJson(result));
    } catch (err) {
      setAgentOutput(formatJson({ error: err instanceof Error ? err.message : "读取智能体工具目录失败" }));
    }
  }, [agentScope, config]);

  const loadAgentToolPolicy = useCallback(async () => {
    if (!effectiveGroupSessionId) {
      setAgentToolPolicySnapshot(null);
      setAgentToolPolicyEtag(null);
      setAgentToolPolicyStatus("idle");
      setAgentAllowedTools([]);
      setAgentOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    setAgentToolPolicyStatus("loading");
    try {
      const resource = await apiVersionedResource<AgentToolPolicy>(
        config,
        `/plugins/wxbot/admin/agent-tools/policy/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveGroupSessionId)}`,
        { auth: true, query: { scope: agentScope } },
      );
      if (!resource.etag) throw new Error("服务器未返回智能体策略版本，已禁止覆盖保存");
      const result = resource.value;
      setAgentToolPolicySnapshot(result);
      setAgentToolPolicyEtag(resource.etag);
      setAgentToolPolicyStatus("loaded");
      setAgentEnabled(String(result.enabled ?? true));
      setAgentAllowedTools(editableAgentTools(result));
      setAgentOutput(formatJson(result));
    } catch (err) {
      setAgentToolPolicySnapshot(null);
      setAgentToolPolicyEtag(null);
      setAgentToolPolicyStatus("error");
      setAgentOutput(formatJson({ error: err instanceof Error ? err.message : "读取智能体策略失败" }));
    }
  }, [agentScope, config, effectiveGroupSessionId]);

  const saveAgentToolPolicy = async () => {
    if (!effectiveGroupSessionId) {
      setAgentOutput(formatJson({ error: "请先选择群会话" }));
      return;
    }
    if (!agentToolPolicyEtag || !agentToolPolicySnapshot) {
      setAgentOutput(formatJson({ error: "请先读取带版本的智能体策略，再保存本地草稿" }));
      return;
    }
    const normalizedAllowed = agentToolCatalog
      .map((item) => item.name)
      .filter((name) => agentAllowedTools.includes(name));
    if (agentEnabled === "true" && agentToolCatalog.length > 0 && normalizedAllowed.length === 0) {
      setAgentOutput(formatJson({ error: "至少保留一个工具，或直接关闭智能体" }));
      return;
    }
    const body = {
      enabled: agentEnabled === "true",
      allowed_tools: normalizedAllowed.length === agentToolCatalog.length ? [] : normalizedAllowed,
    };
    const intent = `wxbot:agent-policy:${config.tenantId}:${effectiveGroupSessionId}:${agentScope}:${agentToolPolicyEtag}:${JSON.stringify(body)}`;
    setAgentToolPolicyStatus("saving");
    try {
      const resource = await apiVersionedResource<AgentToolPolicy, typeof body>(
        config,
        `/plugins/wxbot/admin/agent-tools/policy/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveGroupSessionId)}`,
        {
          auth: true,
          query: { scope: agentScope },
          method: "POST",
          ifMatch: agentToolPolicyEtag,
          idempotencyKey: keyFor(intent),
          body,
        },
      );
      if (!resource.etag) throw new Error("保存成功但服务器未返回新版本，请重新读取");
      const result = resource.value;
      setAgentToolPolicySnapshot(result);
      setAgentToolPolicyEtag(resource.etag);
      setAgentToolPolicyStatus("loaded");
      setAgentEnabled(String(result.enabled ?? true));
      setAgentAllowedTools(editableAgentTools(result));
      clearIdempotencyKey(intent);
      setAgentOutput(formatJson(result));
    } catch (err) {
      setAgentToolPolicyStatus(err instanceof VersionConflictError ? "conflict" : "error");
      setAgentOutput(formatJson({
        error: err instanceof VersionConflictError
          ? "智能体策略已被其他操作者更新，本地草稿已保留"
          : err instanceof Error ? err.message : "保存智能体策略失败",
      }));
    }
  };

  const loadAgentToolAudit = useCallback(async () => {
    try {
      const result = await apiRequest<{ items?: AgentToolAuditItem[]; count?: number }>(
        config,
        "/plugins/wxbot/admin/agent-tools/audit",
        {
          auth: true,
          query: {
            tenant_id: config.tenantId,
            session_id: agentAuditSessionFilter === "__current__"
              ? effectiveGroupSessionId
              : agentAuditSessionFilter,
            scope: agentScope,
            tool_name: agentAuditToolName,
            trace_id: agentAuditTraceId,
            limit: agentAuditLimit,
          },
        },
      );
      setAgentToolAuditItems(result.items || []);
      setAgentOutput(formatJson(result));
    } catch (err) {
      setAgentOutput(formatJson({ error: err instanceof Error ? err.message : "读取智能体审计日志失败" }));
    }
  }, [agentAuditLimit, agentAuditSessionFilter, agentAuditToolName, agentAuditTraceId, agentScope, config, effectiveGroupSessionId]);

  useEffect(() => {
    if (config.adminToken) void loadAgentToolCatalog();
  }, [config.adminToken, loadAgentToolCatalog]);

  useEffect(() => {
    if (!config.adminToken || activeTab !== "agent") return;
    if (effectiveGroupSessionId) void loadAgentToolPolicy();
    void loadAgentToolAudit();
  }, [activeTab, config.adminToken, effectiveGroupSessionId, loadAgentToolAudit, loadAgentToolPolicy]);

  const agentLoadedTools = agentToolPolicySnapshot
    ? editableAgentTools(agentToolPolicySnapshot)
    : [];
  const agentToolPolicyDirty = Boolean(
    agentToolPolicySnapshot
      && (
        agentEnabled !== String(agentToolPolicySnapshot.enabled ?? true)
        || JSON.stringify([...agentAllowedTools].sort()) !== JSON.stringify([...agentLoadedTools].sort())
      ),
  );

  return {
    agentAllowedTools,
    agentAuditLimit,
    agentAuditSessionFilter,
    agentAuditToolName,
    agentAuditTraceId,
    agentEnabled,
    agentOutput,
    agentScope,
    agentScopeLabel,
    agentScopes,
    agentToolAuditItems,
    agentToolCatalog,
    agentToolOwners,
    agentToolPolicyDirty,
    agentToolPolicyEtag,
    agentToolPolicySnapshot,
    agentToolPolicyStatus,
    loadAgentToolAudit,
    loadAgentToolCatalog,
    loadAgentToolPolicy,
    saveAgentToolPolicy,
    setAgentAllowedTools,
    setAgentAuditLimit,
    setAgentAuditSessionFilter,
    setAgentAuditToolName,
    setAgentAuditTraceId,
    setAgentEnabled,
    setAgentOutput,
    setAgentScope,
    setAgentScopes,
    setAgentToolAuditItems,
    setAgentToolCatalog,
    setAgentToolPolicySnapshot,
  } as const;
}

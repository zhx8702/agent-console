import { useCallback, useEffect, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import type { WxbotSession, WxbotTab } from "./model";

export type GroupWebhookItem = {
  webhook_id: string;
  tenant_id?: string;
  session_id: string;
  canonical_session_id?: string;
  session_name?: string;
  token_hint?: string;
  enabled?: boolean;
  created_at?: string | null;
  last_used_at?: string | null;
  path?: string;
  url?: string;
  token?: string;
};

type Options = {
  activeTab: WxbotTab;
  config: ConsoleConfig;
  groupSessions: WxbotSession[];
};

export function useWxbotGroupWebhookAdmin({
  activeTab,
  config,
  groupSessions,
}: Options) {
  const [groupWebhooks, setGroupWebhooks] = useState<GroupWebhookItem[]>([]);
  const [groupWebhookStatus, setGroupWebhookStatus] = useState<
    "idle" | "loading" | "loaded" | "saving" | "error"
  >("idle");
  const [groupWebhookSessionId, setGroupWebhookSessionId] = useState("");
  const [revealedGroupWebhook, setRevealedGroupWebhook] = useState<GroupWebhookItem | null>(null);
  const [groupWebhookOutput, setGroupWebhookOutput] = useState("");

  const loadGroupWebhooks = useCallback(async () => {
    setGroupWebhookStatus("loading");
    try {
      const result = await apiRequest<{ items?: GroupWebhookItem[]; count?: number }>(
        config,
        "/plugins/wxbot/admin/group-webhooks",
        { auth: true, query: { tenant_id: config.tenantId } },
      );
      setGroupWebhooks(result.items || []);
      setGroupWebhookStatus("loaded");
      setGroupWebhookOutput(formatJson(result));
    } catch (err) {
      setGroupWebhookStatus("error");
      setGroupWebhookOutput(
        formatJson({ error: err instanceof Error ? err.message : "读取群 webhook 失败" }),
      );
    }
  }, [config]);

  useEffect(() => {
    if (activeTab !== "webhooks") {
      return;
    }
    void loadGroupWebhooks();
  }, [activeTab, loadGroupWebhooks]);

  const createGroupWebhook = async () => {
    const sessionId = groupWebhookSessionId.trim();
    if (!sessionId || !groupSessions.some((item) => item.session_id === sessionId)) {
      setGroupWebhookOutput(formatJson({ error: "请从已验证群列表选择目标群" }));
      return;
    }
    const session = groupSessions.find((item) => item.session_id === sessionId);
    setGroupWebhookStatus("saving");
    try {
      const result = await apiRequest<GroupWebhookItem>(config, "/plugins/wxbot/admin/group-webhooks", {
        auth: true,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tenant_id: config.tenantId,
            session_id: sessionId,
            session_name: session?.session_name || "",
          }),
        },
      });
      setRevealedGroupWebhook(result);
      setGroupWebhookOutput(formatJson(result));
      await loadGroupWebhooks();
    } catch (err) {
      setGroupWebhookStatus("error");
      setGroupWebhookOutput(
        formatJson({ error: err instanceof Error ? err.message : "创建群 webhook 失败" }),
      );
    }
  };

  const rotateGroupWebhook = async (webhookId: string) => {
    setGroupWebhookStatus("saving");
    try {
      const result = await apiRequest<GroupWebhookItem>(
        config,
        `/plugins/wxbot/admin/group-webhooks/${encodeURIComponent(webhookId)}/rotate`,
        { auth: true, init: { method: "POST" } },
      );
      setRevealedGroupWebhook(result);
      setGroupWebhookOutput(formatJson(result));
      await loadGroupWebhooks();
    } catch (err) {
      setGroupWebhookStatus("error");
      setGroupWebhookOutput(
        formatJson({ error: err instanceof Error ? err.message : "轮换 webhook 失败" }),
      );
    }
  };

  const revokeGroupWebhook = async (webhookId: string) => {
    setGroupWebhookStatus("saving");
    try {
      const result = await apiRequest<GroupWebhookItem>(
        config,
        `/plugins/wxbot/admin/group-webhooks/${encodeURIComponent(webhookId)}`,
        { auth: true, init: { method: "DELETE" } },
      );
      if (revealedGroupWebhook?.webhook_id === webhookId) {
        setRevealedGroupWebhook(null);
      }
      setGroupWebhookOutput(formatJson(result));
      await loadGroupWebhooks();
    } catch (err) {
      setGroupWebhookStatus("error");
      setGroupWebhookOutput(
        formatJson({ error: err instanceof Error ? err.message : "停用或删除 webhook 失败" }),
      );
    }
  };

  const copyGroupWebhookUrl = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setGroupWebhookOutput(formatJson({ copied: value }));
    } catch {
      setGroupWebhookOutput(formatJson({ error: "复制失败，请手动选择地址" }));
    }
  };

  return {
    copyGroupWebhookUrl,
    createGroupWebhook,
    groupWebhookOutput,
    groupWebhookSessionId,
    groupWebhookStatus,
    groupWebhooks,
    loadGroupWebhooks,
    revealedGroupWebhook,
    revokeGroupWebhook,
    rotateGroupWebhook,
    setGroupWebhookSessionId,
  };
}

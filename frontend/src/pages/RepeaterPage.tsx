import { useCallback, useEffect, useMemo, useState } from "react";

import { Alert } from "../components/Alert";
import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { UnsavedChangesGuard } from "../components/UnsavedChangesGuard";
import {
  ApiError,
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  formatJson,
} from "../lib/api";
import { useStableIdempotencyKeys } from "../lib/idempotency";
import { useConsoleConfig } from "../state/console-config";

type RepeaterConfig = {
  tenant_id: string;
  session_id: string;
  enabled: boolean;
  cooldown_seconds: number;
  version: number;
  updated_at?: string | null;
};

type ConfigStatus = "idle" | "loading" | "ready" | "saving" | "conflict" | "error";

type RepeaterDraft = { enabled: boolean; cooldown_seconds: number | "" };

const emptyDraft: RepeaterDraft = { enabled: false, cooldown_seconds: 300 };

export function RepeaterPage() {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [draft, setDraft] = useState(emptyDraft);
  const [baseline, setBaseline] = useState(emptyDraft);
  const [etag, setEtag] = useState("");
  const [status, setStatus] = useState<ConfigStatus>("idle");
  const [error, setError] = useState("");
  const [eventsOutput, setEventsOutput] = useState('{\n  "status": "waiting"\n}');
  const selectedGroup = verifiedGroupIds.has(config.sessionId) ? config.sessionId : "";
  const basePath = "/plugins/repeater";
  const dirty = useMemo(
    () => draft.enabled !== baseline.enabled || draft.cooldown_seconds !== baseline.cooldown_seconds,
    [baseline, draft],
  );
  const draftValid = typeof draft.cooldown_seconds === "number"
    && draft.cooldown_seconds >= 1
    && draft.cooldown_seconds <= 86_400;

  const configPath = selectedGroup
    ? `${basePath}/config/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(selectedGroup)}`
    : "";

  const loadConfig = useCallback(async () => {
    if (!configPath) {
      setStatus("idle");
      setEtag("");
      setError("请先从后端验证的群列表中选择一个群聊");
      return;
    }
    setStatus("loading");
    setError("");
    try {
      const result = await apiVersionedResource<RepeaterConfig>(config, configPath, { auth: true });
      if (!result.etag) throw new Error("服务器未返回配置版本，请勿保存");
      const loaded = {
        enabled: Boolean(result.value.enabled),
        cooldown_seconds: Number(result.value.cooldown_seconds || 300),
      };
      setDraft(loaded);
      setBaseline(loaded);
      setEtag(result.etag);
      setStatus("ready");
    } catch (caught) {
      setStatus("error");
      setError(caught instanceof Error ? caught.message : "读取失败");
    }
  }, [config, configPath]);

  const saveConfig = async () => {
    if (!configPath || !etag || !draftValid || status === "loading" || status === "saving") return;
    const payload = {
      enabled: draft.enabled,
      cooldown_seconds: Number(draft.cooldown_seconds),
    };
    const intent = `repeater-config:${config.tenantId}:${selectedGroup}:${etag}:${JSON.stringify(draft)}`;
    setStatus("saving");
    setError("");
    try {
      const result = await apiVersionedResource<RepeaterConfig, typeof payload>(config, configPath, {
        auth: true,
        method: "POST",
        body: payload,
        ifMatch: etag,
        idempotencyKey: keyFor(intent),
      });
      if (!result.etag) throw new Error("保存成功但服务器未返回新版本，请重新读取");
      const saved = {
        enabled: Boolean(result.value.enabled),
        cooldown_seconds: Number(result.value.cooldown_seconds || 300),
      };
      setDraft(saved);
      setBaseline(saved);
      setEtag(result.etag);
      setStatus("ready");
      clear(intent);
    } catch (caught) {
      if (caught instanceof VersionConflictError) {
        setStatus("conflict");
        setError("服务器配置已被其他操作者更新；本地草稿已保留，请重新读取后再比较保存。");
      } else {
        setStatus("error");
        setError(caught instanceof ApiError || caught instanceof Error ? caught.message : "保存失败");
      }
    }
  };

  const loadEvents = async () => {
    if (!selectedGroup) return;
    try {
      const result = await apiRequest(config, `${basePath}/events/${encodeURIComponent(config.tenantId)}`, {
        auth: true,
        query: { session_id: selectedGroup, limit: 50 },
      });
      setEventsOutput(formatJson(result));
    } catch (caught) {
      setEventsOutput(formatJson({ error: caught instanceof Error ? caught.message : "读取失败" }));
    }
  };

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  return (
    <div className="page-grid">
      <UnsavedChangesGuard when={dirty} />
      <section className="panel span-2">
        <PageHeader
          eyebrow="复读策略"
          title="复读策略"
          description="按当前已验证群聊配置。复读内容还会经过真实成员数、敏感信息、链接、命令和统一发言预算检查。SDK 群消息门禁只由回复策略的一键聚合配置调整。"
        />
        {!selectedGroup ? (
          <Alert variant="warning" title="尚未选择已验证群聊">选择群聊后才能读取或修改复读策略。</Alert>
        ) : null}
        {error ? (
          <Alert variant={status === "conflict" ? "warning" : "danger"} title={status === "conflict" ? "版本冲突" : "配置操作失败"}>
            {error}
          </Alert>
        ) : null}
        <div className="form-grid">
          <label className="field field-toggle">
            <span>启用群复读</span>
            <span className="toggle-chip">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(event) => setDraft((current) => ({ ...current, enabled: event.target.checked }))}
                disabled={status === "loading" || status === "saving" || !selectedGroup}
              />
              <em>{draft.enabled ? "已启用" : "已停用"}</em>
            </span>
          </label>
          <label className="field">
            <span>相同内容冷却秒数</span>
            <input
              type="number"
              min={1}
              max={86_400}
              value={draft.cooldown_seconds}
              onChange={(event) => {
                const value = event.target.value;
                setDraft((current) => ({
                  ...current,
                  cooldown_seconds: value === "" ? "" : Number(value),
                }));
              }}
              disabled={status === "loading" || status === "saving" || !selectedGroup}
            />
          </label>
        </div>
        {!draftValid ? (
          <Alert variant="warning" title="冷却时间无效">请输入 1–86400 秒之间的整数。</Alert>
        ) : null}
        <div className="action-row">
          <span className="pill pill-muted">{status === "ready" ? "已加载" : status}</span>
          <span className="pill pill-muted">{dirty ? "有未保存修改" : "已同步"}</span>
          <span className="pill pill-muted">版本 {etag || "-"}</span>
        </div>
        <div className="action-row">
          <button className="button button-secondary" type="button" onClick={() => void loadConfig()} disabled={!selectedGroup || status === "loading" || status === "saving"}>
            重新读取
          </button>
          <button className="button button-primary" type="button" onClick={() => void saveConfig()} disabled={!selectedGroup || !etag || !dirty || !draftValid || status === "loading" || status === "saving" || status === "conflict"}>
            {status === "saving" ? "保存中…" : "保存配置"}
          </button>
          <button className="button button-secondary" type="button" onClick={() => void loadEvents()} disabled={!selectedGroup}>
            查看触发记录
          </button>
        </div>
      </section>

      <OutputPanel title="复读触发记录（技术详情）" value={eventsOutput} />
    </div>
  );
}

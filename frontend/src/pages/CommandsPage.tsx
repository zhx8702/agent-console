import { useCallback, useEffect, useRef, useState } from "react";

import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { UnsavedChangesGuard } from "../components/UnsavedChangesGuard";
import { TechnicalDetails } from "../components/TechnicalDetails";
import {
  VersionConflictError,
  apiVersionedResource,
  formatJson,
} from "../lib/api";
import { useConsoleConfig } from "../state/console-config";

type CommandCatalogItem = {
  plugin_name: string;
  command: string;
  aliases?: string[];
  description?: string;
  admin_only?: boolean;
  usage?: string;
};

type CommandConfig = {
  tenant_id: string;
  version: number;
  admin_user_ids_text?: string;
  admin_user_ids?: string[];
  user_commands_text?: string;
  user_commands?: string[];
  admin_commands_text?: string;
  admin_commands?: string[];
  available_user_commands?: string[];
  available_admin_commands?: string[];
  catalog?: CommandCatalogItem[];
};

type CommandDraft = {
  adminUserIdsText: string;
  userCommandsText: string;
  adminCommandsText: string;
};

type ConfigStatus = "idle" | "loading" | "loaded" | "saving" | "error" | "conflict";

function countItems(value: string) {
  return value.split(/\n|,/).map((item) => item.trim()).filter(Boolean).length;
}

const CONFIG_STATUS_LABELS: Record<ConfigStatus, string> = {
  idle: "等待读取",
  loading: "正在读取",
  loaded: "已同步",
  saving: "正在保存",
  error: "操作失败",
  conflict: "发现版本冲突",
};

export function CommandsPage() {
  const { config } = useConsoleConfig();
  const [adminUserIdsText, setAdminUserIdsText] = useState("");
  const [userCommandsText, setUserCommandsText] = useState("");
  const [adminCommandsText, setAdminCommandsText] = useState("");
  const [catalog, setCatalog] = useState<CommandCatalogItem[]>([]);
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');
  const [loadedConfig, setLoadedConfig] = useState<CommandConfig | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [serverEtag, setServerEtag] = useState<string | null>(null);
  const [configStatus, setConfigStatus] = useState<ConfigStatus>("idle");

  const draft: CommandDraft = {
    adminUserIdsText,
    userCommandsText,
    adminCommandsText,
  };
  const configLoadedForScope = Boolean(
    loadedConfig && loadedConfig.tenant_id === config.tenantId,
  );
  const loadedDraft: CommandDraft | null = configLoadedForScope && loadedConfig
    ? {
        adminUserIdsText: String(loadedConfig.admin_user_ids_text || ""),
        userCommandsText: String(loadedConfig.user_commands_text || ""),
        adminCommandsText: String(loadedConfig.admin_commands_text || ""),
      }
    : null;
  const configDirty = Boolean(
    loadedDraft
      && JSON.stringify(draft) !== JSON.stringify(loadedDraft),
  );
  const configDirtyRef = useRef(configDirty);
  configDirtyRef.current = configDirty;

  const applyConfig = useCallback((result: CommandConfig) => {
    setAdminUserIdsText(String(result.admin_user_ids_text || ""));
    setUserCommandsText(String(result.user_commands_text || ""));
    setAdminCommandsText(String(result.admin_commands_text || ""));
    setCatalog(result.catalog || []);
  }, []);

  const loadConfig = useCallback(async () => {
    setConfigStatus("loading");
    try {
      const result = await apiVersionedResource<CommandConfig>(
        config,
        `/plugins/commands/config/${encodeURIComponent(config.tenantId)}`,
      );
      applyConfig(result.value);
      setLoadedConfig(result.value);
      setEtag(result.etag);
      setServerEtag(null);
      setConfigStatus("loaded");
      setOutput(formatJson(result.value));
    } catch (err) {
      setConfigStatus("error");
      setOutput(formatJson({ error: err instanceof Error ? err.message : "读取失败" }));
    }
  }, [applyConfig, config]);

  const saveConfig = useCallback(async () => {
    if (!loadedConfig || !etag || !configLoadedForScope) {
      setConfigStatus("error");
      setOutput(formatJson({ error: "尚未成功读取配置，已阻止覆盖服务器数据" }));
      return;
    }
    setConfigStatus("saving");
    try {
      const result = await apiVersionedResource<CommandConfig, {
        admin_user_ids_text: string;
        user_commands_text: string;
        admin_commands_text: string;
      }>(
        config,
        `/plugins/commands/config/${encodeURIComponent(config.tenantId)}`,
        {
          method: "POST",
          ifMatch: etag,
          body: {
            admin_user_ids_text: adminUserIdsText,
            user_commands_text: userCommandsText,
            admin_commands_text: adminCommandsText,
          },
        },
      );
      applyConfig(result.value);
      setLoadedConfig(result.value);
      setEtag(result.etag);
      setServerEtag(null);
      setConfigStatus("loaded");
      setOutput(formatJson(result.value));
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setServerEtag(err.serverEtag);
        setConfigStatus("conflict");
      } else {
        setConfigStatus("error");
      }
      setOutput(formatJson({ error: err instanceof Error ? err.message : "保存失败" }));
    }
  }, [
    adminCommandsText,
    adminUserIdsText,
    applyConfig,
    config,
    configLoadedForScope,
    etag,
    loadedConfig,
    userCommandsText,
  ]);

  const discardDraft = useCallback(() => {
    if (!loadedConfig || !configLoadedForScope) {
      return;
    }
    applyConfig(loadedConfig);
    setConfigStatus("loaded");
    setServerEtag(null);
  }, [applyConfig, configLoadedForScope, loadedConfig]);

  useEffect(() => {
    if (!configDirtyRef.current) {
      void loadConfig();
    }
  }, [loadConfig]);

  return (
    <div className="page-grid">
      <UnsavedChangesGuard when={configDirty} />
      <section className="panel span-2">
        <PageHeader
          eyebrow="命令中心"
          title="全局命令中心"
          description="统一管理聊天命令、管理员成员标识和普通 / 管理员命令清单。积分与绘图能力已接入这里，避免继续分散在各插件中。"
        />

        <p className="muted-copy">
          当前为租户级配置，会影响该租户下所有接入命令中心的会话。
        </p>

        <div className="summary-grid">
          <div className="summary-card">
            <span>管理员</span>
            <strong>{countItems(adminUserIdsText)}</strong>
          </div>
          <div className="summary-card">
            <span>普通命令</span>
            <strong>{countItems(userCommandsText)}</strong>
          </div>
          <div className="summary-card">
            <span>管理员命令</span>
            <strong>{countItems(adminCommandsText)}</strong>
          </div>
          <div className="summary-card">
            <span>命令目录</span>
            <strong>{catalog.length}</strong>
          </div>
        </div>
      </section>

      <section className="panel span-2">
        <div className="panel-header">
          <div>
            <p className="section-kicker">配置</p>
            <h3>租户级命令权限</h3>
          </div>
        </div>
        <div className="form-grid">
          <label className="field span-2">
            <span>管理员成员标识</span>
            <textarea
              rows={5}
              value={adminUserIdsText}
              onChange={(event) => setAdminUserIdsText(event.target.value)}
              disabled={!configLoadedForScope || configStatus === "loading" || configStatus === "saving"}
              placeholder={"每行一个成员微信标识\n例如：wxid_xxx"}
            />
          </label>
          <label className="field span-2">
            <span>普通用户可用命令</span>
            <textarea
              rows={7}
              value={userCommandsText}
              onChange={(event) => setUserCommandsText(event.target.value)}
              disabled={!configLoadedForScope || configStatus === "loading" || configStatus === "saving"}
              placeholder={"/签到\n/checkin\n/余额\n/balance"}
            />
          </label>
          <label className="field span-2">
            <span>管理员可用命令</span>
            <textarea
              rows={7}
              value={adminCommandsText}
              onChange={(event) => setAdminCommandsText(event.target.value)}
              disabled={!configLoadedForScope || configStatus === "loading" || configStatus === "saving"}
              placeholder={"/赠送\n/grant\n/sign-in\n/signin\n/签到模式"}
            />
          </label>
        </div>
        <div className="action-row">
          <button
            className="button button-secondary"
            onClick={() => void loadConfig()}
            disabled={configDirty || configStatus === "loading" || configStatus === "saving"}
          >
            {configStatus === "loading" ? "读取中…" : "读取配置"}
          </button>
          {configDirty ? (
            <button className="button button-secondary" onClick={discardDraft}>
              放弃未保存修改
            </button>
          ) : null}
          <button
            className="button button-primary"
            onClick={() => void saveConfig()}
            disabled={!configLoadedForScope || !configDirty || configStatus === "saving" || configStatus === "loading"}
          >
            {configStatus === "saving" ? "保存中…" : "保存配置"}
          </button>
        </div>
        <div className="route-list" aria-live="polite">
          <div>
            加载状态：<strong>{CONFIG_STATUS_LABELS[configStatus]}</strong>
            {" · "}草稿：<strong>{configDirty ? "有未保存修改" : "已同步"}</strong>
          </div>
          <div>
            配置版本：<span>{configLoadedForScope ? loadedConfig?.version : "-"}</span>
          </div>
          <TechnicalDetails
            summary="查看版本令牌"
            value={configLoadedForScope ? etag || "尚无版本令牌" : "尚未读取配置"}
          />
        </div>
        {configStatus === "conflict" ? (
          <div className="alert alert-warning" role="alert">
            <span className="alert-icon" aria-hidden="true">!</span>
            <div className="alert-content">
              <strong>服务器配置已被其他操作者更新</strong>
              <div>
                当前草稿仍保留。请先复制需要保留的草稿；重新加载会用服务器版本覆盖当前表单。
              </div>
              <TechnicalDetails summary="查看服务器版本令牌" value={serverEtag || "未知"} />
              <button className="button button-secondary" onClick={() => void loadConfig()}>
                加载服务器版本（覆盖草稿）
              </button>
            </div>
          </div>
        ) : null}
        <p className="muted-copy">
          普通命令清单里的命令任何人都可触发。管理员命令只有发送者的成员标识命中上方管理员列表时才会放行。
        </p>
      </section>

      <section className="panel span-2">
        <div className="panel-header">
          <div>
            <p className="section-kicker">命令目录</p>
            <h3>已注册命令目录</h3>
          </div>
        </div>
        <div className="table-scroll compact-table-scroll">
          <table>
            <caption className="sr-only">已注册命令目录</caption>
            <thead>
              <tr>
                <th scope="col">插件</th>
                <th scope="col">主命令</th>
                <th scope="col">别名</th>
                <th scope="col">权限</th>
                <th scope="col">说明</th>
                <th scope="col">用法</th>
              </tr>
            </thead>
            <tbody>
              {catalog.map((item) => (
                <tr key={`${item.plugin_name}-${item.command}`}>
                  <td><TechnicalDetails summary="查看提供方标识" value={item.plugin_name} /></td>
                  <td className="mono">{item.command}</td>
                  <td className="mono">{(item.aliases || []).join(", ") || "-"}</td>
                  <td>
                    <span className={`pill ${item.admin_only ? "pill-danger" : "pill-ok"}`}>
                      {item.admin_only ? "仅管理员" : "所有成员"}
                    </span>
                  </td>
                  <td>{item.description || "-"}</td>
                  <td className="mono">{item.usage || "-"}</td>
                </tr>
              ))}
              {!catalog.length && (
                <tr>
                  <td colSpan={6} className="empty-cell">
                    当前还没有插件向命令中心注册命令。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <OutputPanel title="命令中心接口结果" value={output} />
    </div>
  );
}

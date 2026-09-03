import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { StatusTile } from "../components/StatusTile";
import { UnsavedChangesGuard } from "../components/UnsavedChangesGuard";
import { TechnicalDetails, friendlyErrorMessage } from "../components/TechnicalDetails";
import {
  VersionConflictError,
  apiVersionedResource,
  formatJson,
} from "../lib/api";
import { useStableIdempotencyKeys } from "../lib/idempotency";
import { useConsoleConfig } from "../state/console-config";

type FieldSource =
  | "environment"
  | "secret_provider"
  | "persisted_override"
  | "dotenv_or_default";

type RuntimeLlmConfig = {
  loaded: true;
  version: number;
  llm_provider: string;
  openai_base_url: string;
  openai_api_mode: string;
  openai_web_search_enabled: boolean;
  openai_web_search_tool: string;
  openai_web_search_live_enabled: boolean;
  llm_embed_provider: string;
  knowledge_features_enabled: boolean;
  customer_service_prompt_enabled: boolean;
  llm_model_tier1: string;
  llm_model_tier2: string;
  llm_model_tier3: string;
  llm_embed_model: string;
  field_sources: Record<keyof RuntimeLlmConfigDraft, FieldSource>;
  secret_provider_status: {
    openai_api_key: {
      configured: boolean;
      source: string;
      mutable: false;
    };
  };
  validation_errors: string[];
  restart_required: boolean;
  apply_status: "no_persisted_change" | "restart_required_or_unverified";
  affected_roles: string[];
  updated_at: string | null;
};

type RuntimeLlmConfigDraft = {
  llm_provider: string;
  openai_base_url: string;
  openai_api_mode: string;
  openai_web_search_enabled: boolean;
  openai_web_search_tool: string;
  openai_web_search_live_enabled: boolean;
  llm_embed_provider: string;
  knowledge_features_enabled: boolean;
  customer_service_prompt_enabled: boolean;
  llm_model_tier1: string;
  llm_model_tier2: string;
  llm_model_tier3: string;
  llm_embed_model: string;
};

type ConfigStatus = "idle" | "loading" | "loaded" | "saving" | "error" | "conflict";

const DRAFT_FIELDS = [
  "llm_provider",
  "openai_base_url",
  "openai_api_mode",
  "openai_web_search_enabled",
  "openai_web_search_tool",
  "openai_web_search_live_enabled",
  "llm_embed_provider",
  "knowledge_features_enabled",
  "customer_service_prompt_enabled",
  "llm_model_tier1",
  "llm_model_tier2",
  "llm_model_tier3",
  "llm_embed_model",
] as const satisfies readonly (keyof RuntimeLlmConfigDraft)[];

const SOURCE_LABELS: Record<FieldSource, string> = {
  environment: "运行环境默认值",
  secret_provider: "密钥提供方（只读）",
  persisted_override: "控制台覆盖",
  dotenv_or_default: "部署默认值",
};

const STATUS_LABELS: Record<ConfigStatus, string> = {
  idle: "等待读取",
  loading: "正在读取",
  loaded: "已同步",
  saving: "正在保存",
  error: "操作失败",
  conflict: "版本冲突",
};

function roleLabel(role: string) {
  return ({ api: "接口服务", inbound: "入站处理", scheduler: "调度服务" } as Record<string, string>)[role]
    || "其他服务";
}

function toDraft(config: RuntimeLlmConfig): RuntimeLlmConfigDraft {
  return Object.fromEntries(DRAFT_FIELDS.map((field) => [field, config[field]])) as RuntimeLlmConfigDraft;
}

function isExternallyManaged(source: FieldSource | undefined) {
  return source === "secret_provider";
}

function changedPayload(
  draft: RuntimeLlmConfigDraft,
  loaded: RuntimeLlmConfig,
): Partial<RuntimeLlmConfigDraft> {
  const baseline = toDraft(loaded);
  return Object.fromEntries(
    DRAFT_FIELDS.flatMap((field) => {
      if (draft[field] === baseline[field] || isExternallyManaged(loaded.field_sources[field])) {
        return [];
      }
      return [[field, draft[field]]];
    }),
  ) as Partial<RuntimeLlmConfigDraft>;
}

function SourceBadge({ source }: { source: FieldSource | undefined }) {
  if (!source || source === "environment" || source === "dotenv_or_default") {
    return null;
  }
  return (
    <span className={`pill ${isExternallyManaged(source) ? "pill-muted" : "pill-feature"}`}>
      {SOURCE_LABELS[source]}
    </span>
  );
}

export function LlmConfigPage() {
  const { config } = useConsoleConfig();
  const { keyFor, clear: clearIdempotencyKey } = useStableIdempotencyKeys();
  const [loadedConfig, setLoadedConfig] = useState<RuntimeLlmConfig | null>(null);
  const [draft, setDraft] = useState<RuntimeLlmConfigDraft | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [serverEtag, setServerEtag] = useState<string | null>(null);
  const [status, setStatus] = useState<ConfigStatus>("idle");
  const [error, setError] = useState("");
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');

  const dirty = Boolean(
    loadedConfig && draft && JSON.stringify(draft) !== JSON.stringify(toDraft(loadedConfig)),
  );
  const dirtyRef = useRef(dirty);
  const loadedConfigRef = useRef<RuntimeLlmConfig | null>(null);
  dirtyRef.current = dirty;

  const updateDraft = useCallback(
    <K extends keyof RuntimeLlmConfigDraft>(field: K, value: RuntimeLlmConfigDraft[K]) => {
      setDraft((current) => (current ? { ...current, [field]: value } : current));
    },
    [],
  );

  const applyLoadedConfig = useCallback((value: RuntimeLlmConfig, nextEtag: string) => {
    loadedConfigRef.current = value;
    setLoadedConfig(value);
    setDraft(toDraft(value));
    setEtag(nextEtag);
    setServerEtag(null);
    setStatus("loaded");
    setError("");
    setOutput(formatJson(value));
  }, []);

  const loadConfig = useCallback(async (replaceDraft = false) => {
    if (dirtyRef.current && !replaceDraft) {
      return;
    }
    setStatus("loading");
    setError("");
    try {
      const result = await apiVersionedResource<RuntimeLlmConfig>(
        config,
        "/v1/admin/runtime/llm-config",
        { auth: true },
      );
      if (!result.etag) {
        throw new Error("服务器响应缺少 ETag，已阻止进入可编辑状态");
      }
      applyLoadedConfig(result.value, result.etag);
    } catch (err) {
      const message = friendlyErrorMessage(err, "模型配置读取失败，请稍后重试。");
      setStatus("error");
      setError(message);
      setOutput(formatJson({ error: message }));
      // A failed first read must never expose a writable default draft.  If a
      // previously loaded draft exists, preserve it for recovery instead of
      // silently replacing it with local constants.
      if (!loadedConfigRef.current) {
        setDraft(null);
        setEtag(null);
      }
    }
  }, [applyLoadedConfig, config]);

  const saveConfig = useCallback(async () => {
    if (!loadedConfig || !draft || !etag) {
      const message = "尚未成功读取带版本号的配置，已阻止覆盖服务器数据";
      setStatus("error");
      setError(message);
      setOutput(formatJson({ error: message }));
      return;
    }
    const payload = changedPayload(draft, loadedConfig);
    if (!Object.keys(payload).length) {
      return;
    }
    const mutationIntent = `runtime-llm-config:${etag}:${JSON.stringify(payload)}`;

    setStatus("saving");
    setError("");
    try {
      const result = await apiVersionedResource<RuntimeLlmConfig, Partial<RuntimeLlmConfigDraft>>(
        config,
        "/v1/admin/runtime/llm-config",
        {
          auth: true,
          method: "POST",
          ifMatch: etag,
          idempotencyKey: keyFor(mutationIntent),
          body: payload,
        },
      );
      if (!result.etag) {
        throw new Error("保存响应缺少 ETag，请重新读取后再编辑");
      }
      applyLoadedConfig(result.value, result.etag);
      clearIdempotencyKey(mutationIntent);
    } catch (err) {
      const message = friendlyErrorMessage(err, "模型配置保存失败，请稍后重试；当前草稿仍会保留。");
      if (err instanceof VersionConflictError) {
        setStatus("conflict");
        setServerEtag(err.serverEtag);
      } else {
        setStatus("error");
      }
      setError(message);
      setOutput(formatJson({ error: message }));
    }
  }, [applyLoadedConfig, clearIdempotencyKey, config, draft, etag, keyFor, loadedConfig]);

  const discardDraft = useCallback(() => {
    if (!loadedConfig) {
      return;
    }
    setDraft(toDraft(loadedConfig));
    setServerEtag(null);
    setStatus("loaded");
    setError("");
  }, [loadedConfig]);

  useEffect(() => {
    if (config.adminToken && !dirtyRef.current) {
      void loadConfig();
    }
  }, [config.adminToken, config.apiBaseUrl, loadConfig]);

  const lockedCount = useMemo(
    () => DRAFT_FIELDS.filter((field) => isExternallyManaged(loadedConfig?.field_sources[field])).length,
    [loadedConfig],
  );
  const secretStatus = loadedConfig?.secret_provider_status.openai_api_key;
  const editingDisabled = !draft || status === "loading" || status === "saving" || status === "conflict";

  const sourceFor = (field: keyof RuntimeLlmConfigDraft) => loadedConfig?.field_sources[field];
  const fieldDisabled = (field: keyof RuntimeLlmConfigDraft) => (
    editingDisabled || isExternallyManaged(sourceFor(field))
  );

  return (
    <div className="page-grid">
      <UnsavedChangesGuard when={dirty} onDiscard={discardDraft} />

      <section className="panel panel-hero span-2">
        <PageHeader
          eyebrow="运行时模型控制"
          title="大模型配置"
          description="直接编辑并保存非敏感模型参数。控制台版本会覆盖运行环境默认值；密钥提供方只读。保存不会改写部署文件，也不会热替换正在服务的模型。"
          actions={
            <div className="action-row">
              <button
                type="button"
                className="button button-primary"
                onClick={() => void saveConfig()}
                disabled={!dirty || !etag || status === "saving" || status === "loading" || status === "conflict"}
              >
                {status === "saving" ? "提交版本中…" : "保存为新版本"}
              </button>
              <button
                type="button"
                className="button button-secondary"
                onClick={() => void loadConfig(true)}
                disabled={status === "loading" || status === "saving"}
              >
                {status === "loading" ? "读取中…" : dirty ? "放弃草稿并重新读取" : "重新读取"}
              </button>
              {dirty ? (
                <button type="button" className="button button-ghost" onClick={discardDraft}>
                  还原本地草稿
                </button>
              ) : null}
            </div>
          }
        />

        <div className="status-grid" aria-live="polite">
          <StatusTile label="状态" value={STATUS_LABELS[status]} />
          <StatusTile label="配置版本" value={loadedConfig ? `v${loadedConfig.version}` : "未加载"} />
          <StatusTile label="待保存" value={dirty ? "有本地修改" : "无"} />
          <StatusTile label="可编辑字段" value={`${DRAFT_FIELDS.length - lockedCount}`} />
          <StatusTile
            label="影响服务"
            value={
              loadedConfig?.affected_roles.length
                ? loadedConfig.affected_roles.length > 2
                  ? `${loadedConfig.affected_roles.length} 个服务`
                  : loadedConfig.affected_roles.map(roleLabel).join("、")
                : "-"
            }
          />
          <StatusTile
            label="OpenAI 凭据"
            value={secretStatus?.configured ? "由外部提供" : "未配置"}
          />
        </div>

        {status === "conflict" ? (
          <div className="admin-notice admin-notice-warning" role="alert">
            <strong>服务器配置已被其他管理员更新。</strong>
            <span>
              本地草稿仍保留；请先重新读取服务器版本，再人工合并并提交。
            </span>
            <TechnicalDetails summary="查看服务器版本令牌" value={serverEtag || "版本令牌已变化"} />
          </div>
        ) : null}
        {error ? <p className="admin-notice admin-notice-danger" role="alert">{error}</p> : null}
      </section>

      <section className="panel panel-scroll">
        <div className="panel-header">
          <div>
            <p className="section-kicker">安全边界</p>
            <h2>部署与密钥状态</h2>
          </div>
          <span className={`pill ${secretStatus?.configured ? "pill-ok" : "pill-danger"}`}>
            {secretStatus?.configured ? "凭据已就绪" : "凭据缺失"}
          </span>
        </div>
        <dl className="detail-list">
          <div>
            <dt>OpenAI 接口密钥</dt>
            <dd>{secretStatus?.configured ? "已配置（不回显）" : "未配置"}</dd>
          </div>
          <div>
            <dt>凭据来源</dt>
            <dd>{secretStatus?.configured ? "由部署环境安全提供" : "未读取"}</dd>
          </div>
          <div>
            <dt>控制台可变更</dt>
            <dd>否</dd>
          </div>
          <div>
            <dt>生效方式</dt>
            <dd>滚动重启接口、入站处理与调度服务</dd>
          </div>
        </dl>
        <p className="muted-copy">
          密钥只能通过部署环境或密钥提供方轮换。这个页面没有密钥输入、清空或复制入口。
        </p>
      </section>

      <section className="panel span-2">
        <div className="panel-header">
          <div>
            <p className="section-kicker">版本化草稿</p>
            <h2>模型与能力</h2>
          </div>
          <span className={`pill ${(loadedConfig?.validation_errors || []).length ? "pill-danger" : "pill-ok"}`}>
            {(loadedConfig?.validation_errors || []).length ? "当前配置有误" : "当前配置可用"}
          </span>
        </div>

        {!draft ? (
          <div className="empty-state" role="status">
            <strong>尚未取得服务器配置</strong>
            <p>读取成功并取得版本令牌后才会创建可编辑草稿，避免用页面默认值覆盖真实配置。</p>
          </div>
        ) : (
          <div className="form-grid">
            <p className="muted-copy span-2">未标注来源的字段使用运行环境默认值；控制台覆盖和密钥提供方会单独标出。</p>
            <label className="field">
              <span>聊天模型提供方</span>
              <SourceBadge source={sourceFor("llm_provider")} />
              <select value={draft.llm_provider} onChange={(event) => updateDraft("llm_provider", event.target.value)} disabled={fieldDisabled("llm_provider")}>
                <option value="fake">本地模拟</option>
                <option value="openai">OpenAI</option>
              </select>
            </label>
            <label className="field">
              <span>向量模型提供方</span>
              <SourceBadge source={sourceFor("llm_embed_provider")} />
              <select value={draft.llm_embed_provider} onChange={(event) => updateDraft("llm_embed_provider", event.target.value)} disabled={fieldDisabled("llm_embed_provider")}>
                <option value="fake">本地模拟</option>
                <option value="openai">OpenAI</option>
              </select>
            </label>
            <label className="field span-2">
              <span>OpenAI 接口地址</span>
              <SourceBadge source={sourceFor("openai_base_url")} />
              <input value={draft.openai_base_url} onChange={(event) => updateDraft("openai_base_url", event.target.value)} disabled={fieldDisabled("openai_base_url")} />
            </label>
            <label className="field">
              <span>OpenAI 协议模式</span>
              <SourceBadge source={sourceFor("openai_api_mode")} />
              <select value={draft.openai_api_mode} onChange={(event) => updateDraft("openai_api_mode", event.target.value)} disabled={fieldDisabled("openai_api_mode")}>
                <option value="chat">聊天补全接口</option>
                <option value="responses">响应接口</option>
              </select>
            </label>
            <label className="field">
              <span>OpenAI 联网搜索</span>
              <SourceBadge source={sourceFor("openai_web_search_enabled")} />
              <select value={String(draft.openai_web_search_enabled)} onChange={(event) => updateDraft("openai_web_search_enabled", event.target.value === "true")} disabled={fieldDisabled("openai_web_search_enabled")}>
                <option value="false">关闭</option>
                <option value="true">开启</option>
              </select>
            </label>
            <label className="field">
              <span>联网搜索工具</span>
              <SourceBadge source={sourceFor("openai_web_search_tool")} />
              <select value={draft.openai_web_search_tool} onChange={(event) => updateDraft("openai_web_search_tool", event.target.value)} disabled={fieldDisabled("openai_web_search_tool")}>
                <option value="web_search">联网搜索</option>
                <option value="web_search_preview">联网搜索预览</option>
              </select>
            </label>
            <label className="field">
              <span>实时网页访问</span>
              <SourceBadge source={sourceFor("openai_web_search_live_enabled")} />
              <select value={String(draft.openai_web_search_live_enabled)} onChange={(event) => updateDraft("openai_web_search_live_enabled", event.target.value === "true")} disabled={fieldDisabled("openai_web_search_live_enabled")}>
                <option value="true">开启</option>
                <option value="false">关闭</option>
              </select>
            </label>
            <label className="field">
              <span>知识库能力</span>
              <SourceBadge source={sourceFor("knowledge_features_enabled")} />
              <select value={String(draft.knowledge_features_enabled)} onChange={(event) => updateDraft("knowledge_features_enabled", event.target.value === "true")} disabled={fieldDisabled("knowledge_features_enabled")}>
                <option value="true">开启</option>
                <option value="false">关闭</option>
              </select>
            </label>
            <label className="field">
              <span>对话提示词模式</span>
              <SourceBadge source={sourceFor("customer_service_prompt_enabled")} />
              <select value={String(draft.customer_service_prompt_enabled)} onChange={(event) => updateDraft("customer_service_prompt_enabled", event.target.value === "true")} disabled={fieldDisabled("customer_service_prompt_enabled")}>
                <option value="false">群聊机器人模式</option>
                <option value="true">服务台模式</option>
              </select>
            </label>
            <label className="field">
              <span>向量模型</span>
              <SourceBadge source={sourceFor("llm_embed_model")} />
              <input value={draft.llm_embed_model} onChange={(event) => updateDraft("llm_embed_model", event.target.value)} disabled={fieldDisabled("llm_embed_model")} />
            </label>
            {(["llm_model_tier1", "llm_model_tier2", "llm_model_tier3"] as const).map((field, index) => (
              <label className="field span-2" key={field}>
                <span>第 {index + 1} 档模型</span>
                <SourceBadge source={sourceFor(field)} />
                <input value={draft[field]} onChange={(event) => updateDraft(field, event.target.value)} disabled={fieldDisabled(field)} />
              </label>
            ))}
          </div>
        )}

        <ul className="route-list" aria-label="配置校验结果">
          {(loadedConfig?.validation_errors || []).length ? <li>配置校验未通过，请展开技术详情定位字段。</li> : null}
          {loadedConfig && !(loadedConfig.validation_errors || []).length ? <li>未发现校验错误</li> : null}
        </ul>
        {(loadedConfig?.validation_errors || []).length ? (
          <TechnicalDetails summary="查看校验技术详情" value={loadedConfig?.validation_errors || []} />
        ) : null}
        {loadedConfig?.restart_required ? (
          <p className="admin-notice admin-notice-warning">
            新版本已持久化，但尚未热应用。完成接口、入站处理与调度服务的滚动重启后才会生效。
          </p>
        ) : null}
      </section>

      <div className="span-3">
        <OutputPanel flush title="安全响应（不含密钥）" value={output} />
      </div>
    </div>
  );
}

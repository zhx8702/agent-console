import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Alert } from "../components/Alert";
import { OutputPanel } from "../components/OutputPanel";
import { PageHeader } from "../components/PageHeader";
import { StatusTile } from "../components/StatusTile";
import { UnsavedChangesGuard } from "../components/UnsavedChangesGuard";
import {
  ApiError,
  VersionConflictError,
  apiVersionedResource,
  formatJson,
} from "../lib/api";
import { useStableIdempotencyKeys } from "../lib/idempotency";
import { useConsoleConfig } from "../state/console-config";

type AMapConfig = {
  api_key_configured: boolean;
  api_key_mutable_via_api: boolean;
  api_key_source: "environment_or_file_secret";
  runtime_config_mutable: boolean;
  timeout_seconds: number;
  storage_dir: string;
  storage_dir_exists: boolean;
  storage_dir_writable: boolean;
  agent_scope: string;
  tools: string[];
  restart_required?: boolean;
};

export function AmapPage() {
  const { config } = useConsoleConfig();
  const { keyFor, clear: clearIdempotencyKey } = useStableIdempotencyKeys();
  const activeSaveIntentRef = useRef("");
  const [data, setData] = useState<AMapConfig | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [timeoutSeconds, setTimeoutSeconds] = useState("15");
  const [storageDir, setStorageDir] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');

  const dirty = Boolean(
    data &&
      (Number(timeoutSeconds) !== Number(data.timeout_seconds ?? 15) ||
        storageDir !== (data.storage_dir || "")),
  );

  const applyConfig = (next: AMapConfig, nextEtag: string | null) => {
    setData(next);
    setEtag(nextEtag);
    setTimeoutSeconds(String(Number(next.timeout_seconds ?? 15)));
    setStorageDir(next.storage_dir || "");
  };

  const loadConfig = async () => {
    setLoading(true);
    setError("");
    try {
      const result = await apiVersionedResource<AMapConfig>(config, "/plugins/amap/admin/config", {
        auth: true,
      });
      applyConfig(result.value, result.etag);
      if (activeSaveIntentRef.current) {
        clearIdempotencyKey(activeSaveIntentRef.current);
        activeSaveIntentRef.current = "";
      }
      setOutput(formatJson(result.value));
    } catch (err) {
      const message = err instanceof Error ? err.message : "读取高德配置失败";
      setError(message);
      setOutput(formatJson({ error: message }));
    } finally {
      setLoading(false);
    }
  };

  const saveConfig = async () => {
    setSaving(true);
    setError("");
    try {
      if (!etag) {
        throw new ApiError(428, "428 保存前必须先读取最新配置版本", {});
      }
      const body = {
        timeout_seconds: Number(timeoutSeconds),
        storage_dir: storageDir,
      };
      const intent = `amap:runtime-config:${etag}:${JSON.stringify(body)}`;
      if (activeSaveIntentRef.current && activeSaveIntentRef.current !== intent) {
        clearIdempotencyKey(activeSaveIntentRef.current);
      }
      activeSaveIntentRef.current = intent;
      const result = await apiVersionedResource<
        AMapConfig,
        { timeout_seconds: number; storage_dir: string }
      >(config, "/plugins/amap/admin/config", {
        auth: true,
        method: "POST",
        ifMatch: etag,
        idempotencyKey: keyFor(intent),
        body,
      });
      applyConfig(result.value, result.etag);
      clearIdempotencyKey(intent);
      activeSaveIntentRef.current = "";
      setOutput(formatJson(result.value));
    } catch (err) {
      const code = amapMutationErrorCode(err);
      const mutationId = amapMutationId(err);
      const message =
        code === "amap_config_mutation_indeterminate"
          ? "配置写入结果无法自动判定，已停止自动重试。本地编辑已保留；请记录变更 ID，重新读取并核对部署配置后再操作。"
          : code === "amap_config_mutation_pending"
            ? "配置写入仍在恢复中。本地编辑和请求版本已保留，请直接重试同一保存操作，不要先修改表单或重新读取。"
            : code === "idempotency_key_conflict"
              ? "保存意图与幂等键不一致，已禁止继续提交。本地编辑已保留，请重新读取后重新编辑。"
              : err instanceof VersionConflictError
          ? "配置已被其他管理员修改。本地编辑已保留，请重新读取并比较后再保存。"
          : err instanceof ApiError && err.status === 428
            ? "缺少配置版本。本地编辑已保留，请先重新读取最新配置。"
            : err instanceof Error
              ? err.message
              : "保存高德配置失败";
      setError(message);
      setOutput(
        formatJson({
          error: message,
          code: code || null,
          mutation_id: mutationId,
          current_etag: err instanceof VersionConflictError ? err.serverEtag : null,
          recovery: code === "amap_config_mutation_pending"
            ? "保持当前表单、ETag 和幂等键不变，直接重试保存"
            : code === "amap_config_mutation_indeterminate"
              ? "停止重试，重新读取并人工核对部署配置"
              : undefined,
        }),
      );
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => {
    if (config.adminToken) {
      void loadConfig();
    }
  }, [config.adminToken, config.apiBaseUrl]);

  return (
    <div className="page-grid">
      <UnsavedChangesGuard when={dirty} />
      <section className="panel panel-hero span-2">
        <PageHeader
          eyebrow="高德地图"
          title="高德个人地图插件"
          description="查看由外部密钥提供方注入的高德 Web 服务凭据状态，配置二维码保存目录和超时参数，并检查智能体工具注册状态。控制台永不接收或回显接口密钥。"
        />
        <div className="action-row">
          <button
            className="button button-primary"
            onClick={() => void saveConfig()}
            disabled={
              saving ||
              !config.adminToken ||
              !data?.runtime_config_mutable ||
              !etag ||
              !Number.isFinite(Number(timeoutSeconds)) ||
              Number(timeoutSeconds) <= 0 ||
              !dirty
            }
          >
            {saving ? "保存中..." : "保存配置"}
          </button>
          <button className="button button-secondary" onClick={() => void loadConfig()} disabled={loading || !config.adminToken}>
            {loading ? "刷新中..." : "重新读取"}
          </button>
          <Link className="button button-secondary" to="/wxbot">
            智能体工具白名单
          </Link>
          {dirty ? <span className="pill pill-muted">有未保存修改</span> : null}
        </div>
        <div className="status-grid">
          <StatusTile label="接口密钥" value={data?.api_key_configured ? "已配置" : "缺失"} />
          <StatusTile label="作用范围" value={data?.agent_scope || "群个人地图"} />
          <StatusTile label="工具数量" value={`${data?.tools?.length ?? 0}`} />
          <StatusTile label="二维码目录" value={data?.storage_dir_writable ? "可写" : "需检查"} />
        </div>
        <p className="muted-copy">
          {data?.restart_required
            ? "非密钥配置已保存。实际处理群消息的工作进程需要重启后使用新配置。"
            : data?.runtime_config_mutable
              ? "非密钥配置仅允许在开发/测试环境修改，并使用 ETag 防止并发覆盖。"
              : "当前部署为只读配置；请通过部署系统变更非密钥参数。"}
        </p>
        {error ? <Alert variant="danger">{error}</Alert> : null}
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">运行状态</p>
            <h2>当前状态</h2>
          </div>
          <span className={`pill ${data?.api_key_configured ? "pill-ok" : "pill-danger"}`}>
            {data?.api_key_configured ? "可调用" : "缺少接口密钥"}
          </span>
        </div>
        <ul className="route-list">
          <li>高德接口密钥：<span>{data?.api_key_configured ? "已配置" : "未配置"}</span></li>
          <li>请求超时：<span>{data?.timeout_seconds ?? "-"} 秒</span></li>
          <li>二维码目录：<span className="mono">{data?.storage_dir || "-"}</span></li>
          <li>目录存在：<span>{data?.storage_dir_exists ? "是" : "否"}</span></li>
          <li>目录可写：<span>{data?.storage_dir_writable ? "是" : "否"}</span></li>
        </ul>
      </section>

      <section className="panel span-2">
        <div className="panel-header">
          <div>
            <p className="section-kicker">配置</p>
            <h2>插件配置</h2>
          </div>
        </div>
        <div className="form-grid">
          <div className="span-2 admin-notice">
            <strong>AMAP_API_KEY 由外部密钥提供方管理</strong>
            <p className="muted-copy">
              通过进程环境变量、容器密钥或挂载的密钥文件注入。管理接口不接受写入或清空密钥，也不会显示密钥内容。
            </p>
          </div>
          <label className="field">
            <span>请求超时秒数</span>
            <input
              type="number"
              min={1}
              step={1}
              value={timeoutSeconds}
              onChange={(event) => setTimeoutSeconds(event.target.value)}
              disabled={!data?.runtime_config_mutable}
            />
          </label>
          <label className="field span-2">
            <span>二维码保存目录</span>
            <input
              value={storageDir}
              onChange={(event) => setStorageDir(event.target.value)}
              placeholder="/mnt/c/Users/Public/agent-console-amap"
              disabled={!data?.runtime_config_mutable}
            />
          </label>
        </div>
        <p className="muted-copy">
          如果微信机器人 SDK 在 Windows 侧发送图片，二维码目录要配置成 Windows 与 WSL 都能访问的共享路径。
        </p>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">工具目录</p>
            <h2>智能体工具</h2>
          </div>
        </div>
        <ul className="route-list">
          {(data?.tools || []).map((tool) => (
            <li key={tool}><span className="mono">{tool}</span></li>
          ))}
          {!data?.tools?.length && <li>尚未读取到工具目录</li>}
        </ul>
      </section>

      <OutputPanel title="高德插件配置响应" value={output} />
    </div>
  );
}

function amapMutationErrorCode(error: unknown) {
  if (!(error instanceof ApiError)) return "";
  const payload = error.payload;
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return "";
  const detail = (payload as { detail?: unknown }).detail;
  if (detail && typeof detail === "object" && "code" in detail) {
    return String((detail as { code?: unknown }).code || "");
  }
  return "";
}

function amapMutationId(error: unknown) {
  if (!(error instanceof ApiError)) return null;
  const payload = error.payload;
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return null;
  const detail = (payload as { detail?: unknown }).detail;
  if (detail && typeof detail === "object" && "mutation_id" in detail) {
    return String((detail as { mutation_id?: unknown }).mutation_id || "") || null;
  }
  return null;
}

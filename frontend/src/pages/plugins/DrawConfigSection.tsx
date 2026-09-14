import { Alert } from "../../components/Alert";

export type DrawRuntimeConfig = {
  version: number;
  enabled: boolean;
  api_url: string;
  api_edit_url: string;
  api_model: string;
  api_provider: string;
  api_key_configured: boolean;
  api_key_hint: string;
  api_host: string;
};

type DrawConfigSectionProps = {
  config: DrawRuntimeConfig | null;
  apiUrl: string;
  apiKey: string;
  apiModel: string;
  apiEditUrl: string;
  loading: boolean;
  saving: boolean;
  error: string;
  notice: string;
  canManage: boolean;
  onApiUrlChange: (value: string) => void;
  onApiKeyChange: (value: string) => void;
  onApiModelChange: (value: string) => void;
  onApiEditUrlChange: (value: string) => void;
  onSave: () => Promise<void>;
  onDisable: () => Promise<void>;
};

export function DrawConfigSection({
  config,
  apiUrl,
  apiKey,
  apiModel,
  apiEditUrl,
  loading,
  saving,
  error,
  notice,
  canManage,
  onApiUrlChange,
  onApiKeyChange,
  onApiModelChange,
  onApiEditUrlChange,
  onSave,
  onDisable,
}: DrawConfigSectionProps) {
  return (
    <section className="panel panel-scroll span-3">
      <div className="panel-header">
        <div>
          <p className="section-kicker">画图</p>
          <h3>画图接口</h3>
        </div>
        <span className={`plugin-badge ${config?.enabled ? "" : "is-muted"}`}>
          {loading ? "读取中" : config?.enabled ? "已启用" : "已停用"}
        </span>
      </div>
      <p className="muted-copy">
        这里改完立刻生效，不用改服务器环境变量。地址留空等于停用。密钥只在这次保存时提交，页面不会回显完整 Key。
      </p>
      {error && <Alert variant="danger" title="未能保存">{error}</Alert>}
      {notice && <Alert variant="success" title="已更新">{notice}</Alert>}
      <div className="form-grid">
        <label className="field span-2">
          <span>接口地址</span>
          <input
            value={apiUrl}
            onChange={(event) => onApiUrlChange(event.target.value)}
            placeholder="https://example.com/v1"
            disabled={!canManage || loading || saving}
          />
        </label>
        <label className="field">
          <span>模型</span>
          <input
            value={apiModel}
            onChange={(event) => onApiModelChange(event.target.value)}
            placeholder="grok-imagine-image"
            disabled={!canManage || loading || saving}
          />
        </label>
        <label className="field span-2">
          <span>API Key</span>
          <input
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(event) => onApiKeyChange(event.target.value)}
            placeholder={
              config?.api_key_configured
                ? `已配置${config.api_key_hint ? ` · 末四位 ${config.api_key_hint}` : ""}，留空则不改`
                : "尚未配置"
            }
            disabled={!canManage || loading || saving}
          />
        </label>
        <label className="field span-2">
          <span>重绘地址</span>
          <input
            value={apiEditUrl}
            onChange={(event) => onApiEditUrlChange(event.target.value)}
            placeholder="可选，/v1/images/edits"
            disabled={!canManage || loading || saving}
          />
        </label>
      </div>
      <div className="action-row">
        <button
          type="button"
          className="button"
          disabled={!canManage || loading || saving}
          onClick={() => void onSave()}
        >
          {saving ? "保存中" : "保存接口"}
        </button>
        <button
          type="button"
          className="button button-secondary"
          disabled={!canManage || loading || saving || !config?.enabled}
          onClick={() => void onDisable()}
        >
          停用接口
        </button>
      </div>
    </section>
  );
}

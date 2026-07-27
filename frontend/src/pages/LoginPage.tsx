import { type FormEvent, useState } from "react";

import { ApiError, apiRequest } from "../lib/api";
import { useConsoleConfig } from "../state/console-config";

type LoginPageProps = {
  checking?: boolean;
  connectionMessage?: string;
  onAuthenticated: () => void;
};

export function LoginPage({ checking = false, connectionMessage = "", onAuthenticated }: LoginPageProps) {
  const { config, updateConfig } = useConsoleConfig();
  const secureTransport = window.location.protocol === "https:";
  const [token, setToken] = useState(config.adminToken);
  const [showToken, setShowToken] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const candidate = token.trim();
    if (!candidate) {
      setError("请输入管理员令牌");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await apiRequest<{ authenticated: boolean }>(
        { ...config, adminToken: candidate },
        "/v1/admin/auth/session",
        {
          auth: true,
          init: { method: "POST" },
        },
      );
      updateConfig({ adminToken: candidate });
      onAuthenticated();
    } catch (caught) {
      if (caught instanceof ApiError && (caught.status === 401 || caught.status === 403)) {
        setError("管理员令牌无效，请确认后重试");
      } else {
        setError("暂时无法连接控制台接口，请检查服务状态后重试");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-page">
      <a className="skip-link" href="#login-form">跳到登录表单</a>
      <section className="login-signal-panel" aria-label="智能体控制台管理通道">
        <div className="login-brand-mark" aria-hidden="true">
          AC
        </div>
        <div className="login-signal-copy">
          <p className="login-eyebrow">管理控制面 / 01</p>
          <h1>智能体控制台</h1>
          <p>多消息平台连接、机器人行为与运行状态的统一管理入口。</p>
        </div>
        <div className="login-system-list" aria-label="系统状态说明">
          <div>
            <span>传输</span>
            <strong className={secureTransport ? "is-secure" : "is-warning"}>
              {secureTransport ? "HTTPS 加密传输" : "同源代理（当前 HTTP）"}
            </strong>
          </div>
          <div>
            <span>鉴权</span>
            <strong>管理员持有令牌</strong>
          </div>
          <div>
            <span>端点</span>
            <strong>{window.location.host}</strong>
          </div>
        </div>
        <div className="login-grid-decoration" aria-hidden="true" />
      </section>

      <section className="login-form-panel">
        <form id="login-form" className="login-card" onSubmit={(event) => void submit(event)}>
          <div className="login-card-header">
            <span className="login-status-dot" aria-hidden="true" />
            <div>
              <p className="login-eyebrow">管理员访问</p>
              <h2>登录管理后台</h2>
            </div>
          </div>
          <p className="login-card-copy">
            输入服务器配置的管理员令牌。令牌只用于本次验证且不会写入浏览器存储，
            后续登录状态由服务器的安全会话保持。
          </p>

          <div className="login-token-field">
            <label htmlFor="admin-token">管理员令牌</label>
            <div className="login-token-input">
              <input
                id="admin-token"
                name="admin-token"
                autoFocus
                autoComplete="current-password"
                required
                aria-invalid={Boolean(error)}
                aria-describedby={error || connectionMessage ? "login-alert" : "login-token-help"}
                type={showToken ? "text" : "password"}
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="输入管理员令牌"
              />
              <button type="button" onClick={() => setShowToken((current) => !current)}>
                {showToken ? "隐藏" : "显示"}
              </button>
            </div>
            <span id="login-token-help" className="sr-only">输入服务器配置的管理员令牌</span>
          </div>

          {(error || connectionMessage) && (
            <div className="login-alert" id="login-alert" role="alert">
              <strong>{error ? "登录失败" : "连接异常"}</strong>
              <span>{error || connectionMessage}</span>
            </div>
          )}

          <button className="login-submit" type="submit" disabled={submitting || checking}>
            <span>{submitting || checking ? "正在验证管理通道" : "验证并进入控制台"}</span>
            <span aria-hidden="true">→</span>
          </button>
          <div className="login-footnote">
            <span className="login-lock-icon" aria-hidden="true">◆</span>
            接口请求通过当前站点的 /api 代理发送，并携带服务器签发的 HttpOnly 会话。
          </div>
        </form>
      </section>
    </main>
  );
}

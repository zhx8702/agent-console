import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";

import { GlobalSessionBar } from "./components/GlobalSessionBar";
import { AmapPage } from "./pages/AmapPage";
import { CreditsPage } from "./pages/CreditsPage";
import { CommandsPage } from "./pages/CommandsPage";
import { ConnectionsPage } from "./pages/connections/ConnectionsPage";
import { DlqPage } from "./pages/DlqPage";
import { GroupBehaviorPage } from "./pages/GroupBehaviorPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { LoginPage } from "./pages/LoginPage";
import { LlmConfigPage } from "./pages/LlmConfigPage";
import { MessageQueuesPage } from "./pages/MessageQueuesPage";
import { MemoryPage } from "./pages/MemoryPage";
import { ModerationPage } from "./pages/ModerationPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PersonaPage } from "./pages/PersonaPage";
import { PlaygroundPage } from "./pages/PlaygroundPage";
import { PluginMarketplacePage } from "./pages/PluginMarketplacePage";
import { PluginsPage } from "./pages/PluginsPage";
import { RelationshipGraphPage } from "./pages/RelationshipGraphPage";
import { RepeaterPage } from "./pages/RepeaterPage";
import { WxbotPage } from "./pages/WxbotPage";
import {
  ApiError,
  apiDocumentUrl,
  apiRequest,
  type AdminPrincipalResponse,
  type CapabilityLoadState,
  type TenantCapabilitiesResponse,
} from "./lib/api";
import {
  AUTH_INVALID_EVENT,
  COOKIE_SESSION_MARKER,
  useConsoleConfig,
} from "./state/console-config";

export type NavigationDomain =
  | "上线向导"
  | "消息接入"
  | "机器人行为"
  | "知识"
  | "群运营"
  | "能力与集成"
  | "系统运维";

export type NavigationItem = {
  to: string;
  label: string;
  icon: string;
  domain: NavigationDomain;
  groupScoped: boolean;
  capabilityPath?: string;
  end?: boolean;
};

export const NAVIGATION_DOMAINS: NavigationDomain[] = [
  "上线向导",
  "消息接入",
  "机器人行为",
  "知识",
  "群运营",
  "能力与集成",
  "系统运维",
];

export const NAV_ITEMS: NavigationItem[] = [
  { to: "/", label: "概览与上线", icon: "◈", domain: "上线向导", groupScoped: false, end: true },
  { to: "/channels", label: "平台连接", icon: "▣", domain: "消息接入", groupScoped: false },
  { to: "/playground", label: "链路测试", icon: "▷", domain: "消息接入", groupScoped: true },
  { to: "/llm", label: "模型配置", icon: "◍", domain: "机器人行为", groupScoped: false },
  {
    to: "/group-behavior",
    label: "群参与与行为",
    icon: "◐",
    domain: "机器人行为",
    groupScoped: true,
  },
  { to: "/persona", label: "回复风格", icon: "◎", domain: "机器人行为", groupScoped: true },
  { to: "/repeater", label: "复读策略", icon: "↺", domain: "机器人行为", groupScoped: true },
  { to: "/knowledge", label: "常见问答 / 知识库", icon: "◉", domain: "知识", groupScoped: true },
  { to: "/memory", label: "成员记忆", icon: "◌", domain: "知识", groupScoped: true },
  { to: "/relationship-graph", label: "群聊关系图", icon: "◎", domain: "知识", groupScoped: true },
  { to: "/commands", label: "命令中心", icon: "⌘", domain: "能力与集成", groupScoped: false },
  { to: "/credits", label: "积分运营", icon: "◆", domain: "群运营", groupScoped: true },
  { to: "/moderation", label: "内容审核", icon: "◫", domain: "群运营", groupScoped: true },
  { to: "/amap", label: "高德地图", icon: "⌖", domain: "能力与集成", groupScoped: false },
  { to: "/plugins", label: "插件管理", icon: "⬢", domain: "能力与集成", groupScoped: false, end: true },
  { to: "/plugins/marketplace", label: "插件市场", icon: "▧", domain: "能力与集成", groupScoped: false },
  { to: "/queues", label: "消息队列", icon: "≋", domain: "系统运维", groupScoped: false },
  { to: "/dlq", label: "失败消息", icon: "◇", domain: "系统运维", groupScoped: false },
];

export function routeMetadataForPath(pathname: string) {
  return NAV_ITEMS.find((item) => item.to === pathname) || null;
}

export function routeTitleForPath(pathname: string) {
  if (pathname === "/login") {
    return "登录";
  }
  if (pathname === "/wxbot") {
    return "微信扩展控制台";
  }
  return routeMetadataForPath(pathname)?.label || "页面不存在";
}

export function navigationItemsForCapabilities(state: CapabilityLoadState) {
  if (!state.data) {
    return NAV_ITEMS.filter((item) => item.to === "/");
  }
  const visiblePaths = new Set(
    state.data.navigation.filter((item) => item.visible).map((item) => item.path),
  );
  return NAV_ITEMS.filter((item) => visiblePaths.has(item.capabilityPath || item.to));
}

function CapabilityRoute({
  path,
  state,
  children,
}: {
  path: string;
  state: CapabilityLoadState;
  children: ReactNode;
}) {
  const metadata = routeMetadataForPath(path);
  const capabilityPath = metadata?.capabilityPath || metadata?.to || path;
  if (state.status === "idle" || state.status === "loading") {
    return (
      <section className="panel" aria-busy="true" aria-label="正在校验入口权限">
        <p className="section-kicker">入口校验</p>
        <h1>正在校验入口权限</h1>
        <p>控制台正在读取服务端能力与当前身份范围。</p>
      </section>
    );
  }
  const decision = state.data?.navigation.find((item) => item.path === capabilityPath);
  if (!decision?.visible) {
    return (
      <section className="not-found-panel" aria-labelledby="route-access-denied-title">
        <div className="not-found-code" aria-hidden="true">403</div>
        <div>
          <p className="section-kicker">入口不可用</p>
          <h1 id="route-access-denied-title">当前入口不可用</h1>
          <p>这个功能不在当前身份与群聊范围内，或其运行依赖尚未就绪。</p>
          <div className="action-row">
            <Link className="button button-primary" to="/">返回控制台概览</Link>
          </div>
        </div>
      </section>
    );
  }
  return children;
}

export function tenantIdForPrincipal(
  principal: Pick<AdminPrincipalResponse, "tenant_ids" | "default_tenant_id">,
  currentTenantId: string,
) {
  const tenantIds = principal.tenant_ids.map((item) => item.trim()).filter(Boolean);
  const current = currentTenantId.trim();
  if (tenantIds.includes("*")) {
    return principal.default_tenant_id.trim();
  }
  if (current && tenantIds.includes(current)) {
    return current;
  }
  return tenantIds[0] || "";
}

type SidebarProps = {
  open: boolean;
  onClose: () => void;
  onLogout: () => Promise<void>;
  capabilityState: CapabilityLoadState;
  principal: AdminPrincipalResponse;
  onRetryCapabilities: () => void;
};

const ROLE_LABELS: Record<string, string> = {
  platform_admin: "平台管理员",
  platform_operator: "平台运营",
  platform_reader: "只读成员",
  tenant_admin: "租户管理员",
  group_operator: "群运营",
  moderator: "内容审核员",
  reviewer: "审核员",
  observer: "只读观察者",
  service_account: "服务账号",
};

function Sidebar({
  open,
  onClose,
  onLogout,
  capabilityState,
  principal,
  onRetryCapabilities,
}: SidebarProps) {
  const { config } = useConsoleConfig();
  const [configOpen, setConfigOpen] = useState(false);
  const navigationItems = navigationItemsForCapabilities(capabilityState);

  return (
    <aside
      id="primary-navigation"
      className={`sidebar${open ? " is-open" : ""}`}
      aria-label="主导航"
    >
      <div className="brand-block">
        <div className="brand-heading-row">
          <h1>智能体控制台</h1>
          <button
            className="sidebar-close-button"
            type="button"
            aria-label="关闭导航"
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <p className="sidebar-copy">插件化多渠道消息运营后台</p>
      </div>

      <p className="section-kicker">导航</p>
      <div
        className={`sidebar-capability-state is-${capabilityState.status}`}
        role="status"
        aria-live="polite"
      >
        {capabilityState.status === "loading" || capabilityState.status === "idle" ? (
          <>
            <span className="sidebar-capability-pulse" aria-hidden="true" />
            <span>正在读取租户能力…</span>
          </>
        ) : capabilityState.status === "degraded" ? (
          <>
            <span>能力清单暂不可用，仅保留安全入口</span>
            <button type="button" onClick={onRetryCapabilities}>重试</button>
          </>
        ) : (
          <>
            <span className="sidebar-capability-dot" aria-hidden="true" />
            <span>
              {capabilityState.data?.summary.visible_navigation ?? 0} 个当前可用入口
            </span>
          </>
        )}
      </div>
      <nav
        className="nav-list"
        aria-label="控制台页面"
        aria-busy={capabilityState.status === "loading"}
      >
        {NAVIGATION_DOMAINS.map((domain) => {
          const domainItems = navigationItems.filter((item) => item.domain === domain);
          if (!domainItems.length) {
            return null;
          }
          const labelId = `navigation-domain-${domain}`;
          return (
            <div key={domain} role="group" aria-labelledby={labelId}>
              <p id={labelId} className="section-kicker">{domain}</p>
              {domainItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}
                  onClick={onClose}
                >
                  <span className="nav-icon">{item.icon}</span>
                  {item.label}
                </NavLink>
              ))}
            </div>
          );
        })}
      </nav>

      <div className="sidebar-config">
        <button
          className="sidebar-config-toggle"
          onClick={() => setConfigOpen((prev) => !prev)}
          aria-expanded={configOpen}
          aria-controls="sidebar-connection-settings"
        >
          管理会话
          <span className={`toggle-arrow${configOpen ? " open" : ""}`}>▾</span>
        </button>
        {configOpen && (
          <div id="sidebar-connection-settings" className="sidebar-config-body">
            <div className="form-grid">
              <div className="sidebar-connection span-2">
                <span className="sidebar-connection-dot" aria-hidden="true" />
                <div>
                  <strong>管理通道已验证</strong>
                  <span>{window.location.origin}</span>
                </div>
              </div>
              <div className="sidebar-connection span-2">
                <div>
                  <strong>{principal.subject}</strong>
                  <span>
                    {principal.roles.map((role) => ROLE_LABELS[role] || "其他已授权角色").join(" · ") || "未分配角色"}
                  </span>
                </div>
              </div>
              <div className="sidebar-connection span-2">
                <div>
                  <strong>当前租户：{config.tenantId}</strong>
                  <span>租户范围由已验证身份决定，浏览器不能手动覆盖</span>
                </div>
              </div>
            </div>
            <div className="action-row">
              <a
                className="button button-secondary"
                href={apiDocumentUrl(config, "/docs")}
                target="_blank"
                rel="noreferrer"
              >
                接口文档
              </a>
              <button
                className="button sidebar-logout-button"
                onClick={() => void onLogout()}
              >
                退出登录
              </button>
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

type AuthStatus = "checking" | "authenticated" | "anonymous" | "unavailable";

function LoginChecking() {
  return (
    <main id="main-content" className="login-page login-page-checking" tabIndex={-1}>
      <div className="login-checking-card">
        <span className="login-checking-spinner" aria-hidden="true" />
        <p className="login-eyebrow">安全会话</p>
        <h1>正在验证管理通道</h1>
        <p>正在恢复当前浏览器保存的管理员会话。</p>
      </div>
    </main>
  );
}

export function App() {
  const { config, updateConfig } = useConsoleConfig();
  const location = useLocation();
  const navigate = useNavigate();
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const mainContentRef = useRef<HTMLElement>(null);
  const previousPathRef = useRef<string | null>(null);
  const [authStatus, setAuthStatus] = useState<AuthStatus>("checking");
  const [principal, setPrincipal] = useState<AdminPrincipalResponse | null>(null);
  const [connectionMessage, setConnectionMessage] = useState("");
  const [navOpen, setNavOpen] = useState(false);
  const [routeAnnouncement, setRouteAnnouncement] = useState("");
  const [capabilityRefreshKey, setCapabilityRefreshKey] = useState(0);
  const [capabilityState, setCapabilityState] = useState<CapabilityLoadState>({
    status: "idle",
    data: null,
    error: "",
  });
  const currentRoute = routeMetadataForPath(location.pathname);
  const showGroupSelector = currentRoute?.groupScoped === true;
  const routeTitle = routeTitleForPath(location.pathname);
  const bearerToken =
    config.adminToken && config.adminToken !== COOKIE_SESSION_MARKER
      ? config.adminToken
      : "";

  useEffect(() => {
    let cancelled = false;
    setAuthStatus("checking");
    setPrincipal(null);
    setConnectionMessage("");
    void apiRequest<AdminPrincipalResponse>(config, "/v1/admin/auth/me", { auth: true })
      .then((payload) => {
        if (!cancelled) {
          if (
            payload.authenticated !== true ||
            !payload.subject ||
            !Array.isArray(payload.roles) ||
            !Array.isArray(payload.tenant_ids) ||
            !Array.isArray(payload.group_ids) ||
            typeof payload.default_tenant_id !== "string"
          ) {
            throw new Error("管理员身份响应格式无效");
          }
          if (config.adminToken !== COOKIE_SESSION_MARKER) {
            updateConfig({ adminToken: COOKIE_SESSION_MARKER });
          }
          setPrincipal(payload);
          setAuthStatus("authenticated");
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          updateConfig({ adminToken: "" });
          setPrincipal(null);
          setAuthStatus("anonymous");
          return;
        }
        setAuthStatus("unavailable");
        setConnectionMessage("API 暂时离线或网关不可用。可以稍后重试，管理员令牌不会写入浏览器存储。");
      });
    return () => {
      cancelled = true;
    };
  }, [bearerToken, config.apiBaseUrl]);

  useEffect(() => {
    if (!principal) {
      return;
    }
    const tenantId = tenantIdForPrincipal(principal, config.tenantId);
    if (tenantId && tenantId !== config.tenantId) {
      updateConfig({ tenantId, sessionId: "" });
    }
  }, [config.tenantId, principal, updateConfig]);

  useEffect(() => {
    const invalidateSession = () => {
      setAuthStatus("anonymous");
      setPrincipal(null);
      setConnectionMessage("");
      setCapabilityState({ status: "idle", data: null, error: "" });
    };
    window.addEventListener(AUTH_INVALID_EVENT, invalidateSession);
    return () => window.removeEventListener(AUTH_INVALID_EVENT, invalidateSession);
  }, []);

  useEffect(() => {
    if (authStatus !== "authenticated" || !principal) {
      setCapabilityState({ status: "idle", data: null, error: "" });
      return;
    }
    const tenantId = tenantIdForPrincipal(principal, config.tenantId);
    if (!tenantId) {
      setCapabilityState({
        status: "degraded",
        data: null,
        error: "当前身份未分配可用租户",
      });
      return;
    }
    // The identity effect above first replaces any browser/default value with
    // the authenticated tenant.  Do not issue even a transient scoped request
    // until that replacement is visible in state.
    if (config.tenantId !== tenantId) {
      return;
    }
    let cancelled = false;
    setCapabilityState({ status: "loading", data: null, error: "" });
    void apiRequest<TenantCapabilitiesResponse>(
      config,
      `/v1/admin/tenants/${encodeURIComponent(tenantId)}/capabilities`,
      { auth: true },
    )
      .then((payload) => {
        if (cancelled) {
          return;
        }
        if (!Array.isArray(payload.capabilities) || !Array.isArray(payload.navigation)) {
          throw new Error("能力清单响应格式无效");
        }
        setCapabilityState({ status: "ready", data: payload, error: "" });
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setCapabilityState({
          status: "degraded",
          data: null,
          error: error instanceof Error ? error.message : "能力清单请求失败",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [
    authStatus,
    bearerToken,
    capabilityRefreshKey,
    config.apiBaseUrl,
    config.tenantId,
    principal,
  ]);

  useEffect(() => {
    document.title = `${routeTitle} · 智能体控制台`;
  }, [routeTitle]);

  useEffect(() => {
    if (authStatus !== "authenticated") {
      return;
    }
    setNavOpen(false);
    setRouteAnnouncement(`${routeTitle}页面已加载`);
    const shouldFocus = previousPathRef.current !== location.pathname;
    previousPathRef.current = location.pathname;
    if (!shouldFocus) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      mainContentRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [authStatus, location.pathname, routeTitle]);

  useEffect(() => {
    document.body.classList.toggle("nav-drawer-open", navOpen);
    if (navOpen) {
      const frame = window.requestAnimationFrame(() => {
        document.querySelector<HTMLButtonElement>(".sidebar-close-button")?.focus();
      });
      return () => {
        window.cancelAnimationFrame(frame);
        document.body.classList.remove("nav-drawer-open");
      };
    }
    return () => document.body.classList.remove("nav-drawer-open");
  }, [navOpen]);

  useEffect(() => {
    if (!navOpen) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setNavOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [navOpen]);

  const loginState = location.state as { from?: string } | null;
  const finishLogin = () => {
    setAuthStatus("authenticated");
    setPrincipal(null);
    setConnectionMessage("");
    navigate(loginState?.from || "/", { replace: true });
  };

  const logout = async () => {
    try {
      await apiRequest(config, "/v1/admin/auth/session", {
        auth: true,
        init: { method: "DELETE" },
      });
    } catch {
      // A missing/expired server session is already effectively logged out.
    } finally {
      updateConfig({ adminToken: "" });
      setAuthStatus("anonymous");
      setPrincipal(null);
      setNavOpen(false);
      navigate("/login", { replace: true });
    }
  };

  if (location.pathname === "/login") {
    if (authStatus === "authenticated") {
      return <Navigate to={loginState?.from || "/"} replace />;
    }
    return (
      <LoginPage
        checking={authStatus === "checking"}
        connectionMessage={connectionMessage}
        onAuthenticated={finishLogin}
      />
    );
  }

  if (authStatus === "checking") {
    return <LoginChecking />;
  }

  if (authStatus !== "authenticated") {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}${location.hash}` }}
      />
    );
  }

  if (!principal) {
    return <LoginChecking />;
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <header className="mobile-topbar">
        <button
          ref={menuButtonRef}
          className="mobile-menu-button"
          type="button"
          aria-label="打开导航"
          aria-controls="primary-navigation"
          aria-expanded={navOpen}
          onClick={() => setNavOpen(true)}
        >
          <span aria-hidden="true">☰</span>
        </button>
        <div className="mobile-route-title">
          <span>智能体控制台</span>
          <strong>{routeTitle}</strong>
        </div>
        <span
          className="mobile-session-indicator"
          role="status"
          aria-label="管理会话已连接"
        />
      </header>
      {navOpen && (
        <button
          className="sidebar-backdrop"
          type="button"
          aria-label="关闭导航"
          onClick={() => {
            setNavOpen(false);
            menuButtonRef.current?.focus();
          }}
        />
      )}
      <Sidebar
        open={navOpen}
        onClose={() => {
          if (navOpen) {
            setNavOpen(false);
            menuButtonRef.current?.focus();
          }
        }}
        onLogout={logout}
        capabilityState={capabilityState}
        principal={principal}
        onRetryCapabilities={() => setCapabilityRefreshKey((value) => value + 1)}
      />
      <main
        id="main-content"
        ref={mainContentRef}
        className="main-area"
        tabIndex={-1}
        aria-label={routeTitle}
      >
        <div className="route-announcer" aria-live="polite" aria-atomic="true">
          {routeAnnouncement}
        </div>
        {showGroupSelector && <GlobalSessionBar />}
        <div className="main-routes">
          <Routes>
            <Route
              path="/"
              element={(
                <OverviewPage
                  capabilityState={capabilityState}
                  accessScope={principal.access_scope}
                  onRetryCapabilities={() => setCapabilityRefreshKey((value) => value + 1)}
                />
              )}
            />
            <Route path="/llm" element={<CapabilityRoute path="/llm" state={capabilityState}><LlmConfigPage /></CapabilityRoute>} />
            <Route path="/group-behavior" element={<CapabilityRoute path="/group-behavior" state={capabilityState}><GroupBehaviorPage /></CapabilityRoute>} />
            <Route path="/plugins" element={<CapabilityRoute path="/plugins" state={capabilityState}><PluginsPage /></CapabilityRoute>} />
            <Route path="/plugins/marketplace" element={<CapabilityRoute path="/plugins/marketplace" state={capabilityState}><PluginMarketplacePage /></CapabilityRoute>} />
            <Route path="/amap" element={<CapabilityRoute path="/amap" state={capabilityState}><AmapPage /></CapabilityRoute>} />
            <Route path="/commands" element={<CapabilityRoute path="/commands" state={capabilityState}><CommandsPage /></CapabilityRoute>} />
            <Route path="/playground" element={<CapabilityRoute path="/playground" state={capabilityState}><PlaygroundPage /></CapabilityRoute>} />
            <Route path="/channels" element={<CapabilityRoute path="/channels" state={capabilityState}><ConnectionsPage /></CapabilityRoute>} />
            <Route path="/queues" element={<CapabilityRoute path="/queues" state={capabilityState}><MessageQueuesPage /></CapabilityRoute>} />
            <Route path="/knowledge" element={<CapabilityRoute path="/knowledge" state={capabilityState}><KnowledgePage /></CapabilityRoute>} />
            <Route path="/memory" element={<CapabilityRoute path="/memory" state={capabilityState}><MemoryPage /></CapabilityRoute>} />
            <Route path="/relationship-graph" element={<CapabilityRoute path="/relationship-graph" state={capabilityState}><RelationshipGraphPage /></CapabilityRoute>} />
            <Route path="/credits" element={<CapabilityRoute path="/credits" state={capabilityState}><CreditsPage /></CapabilityRoute>} />
            <Route path="/moderation" element={<CapabilityRoute path="/moderation" state={capabilityState}><ModerationPage /></CapabilityRoute>} />
            <Route path="/persona" element={<CapabilityRoute path="/persona" state={capabilityState}><PersonaPage /></CapabilityRoute>} />
            <Route path="/repeater" element={<CapabilityRoute path="/repeater" state={capabilityState}><RepeaterPage /></CapabilityRoute>} />
            <Route path="/wxbot" element={<CapabilityRoute path="/channels" state={capabilityState}><WxbotPage /></CapabilityRoute>} />
            <Route path="/dlq" element={<CapabilityRoute path="/dlq" state={capabilityState}><DlqPage /></CapabilityRoute>} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}

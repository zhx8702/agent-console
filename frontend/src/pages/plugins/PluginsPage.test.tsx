import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PluginsPage } from "../PluginsPage";
import { ConsoleConfigProvider, useConsoleConfig } from "../../state/console-config";
import { PluginDiagnosticsSections } from "./PluginDiagnosticsSections";
import { PluginOverviewSection } from "./PluginOverviewSection";
import { FlowRuntimeSection } from "./FlowRuntimeSection";
import type { InstalledPlugin, PluginSummary } from "./models";
import { pluginEnablementLabel } from "./models";

function AuthenticatedPluginsPage() {
  const { updateConfig } = useConsoleConfig();

  useEffect(() => {
    updateConfig({ adminToken: "test-admin-token", tenantId: "tenant-test" });
  }, [updateConfig]);

  return <PluginsPage />;
}

describe("PluginsPage composition", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      status: "ready",
      checks: {},
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps compatibility summaries with missing optional collections renderable", () => {
    const incompleteSummary = { plugins: [] } as unknown as PluginSummary;

    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <PluginOverviewSection
          data={incompleteSummary}
          pluginCards={[]}
          runtime={{}}
          loading={false}
          canRefresh
          onRefresh={() => undefined}
        />
        <PluginDiagnosticsSections
          data={incompleteSummary}
          pluginEvents={[]}
          output="{}"
          groupOutput="{}"
        />
      </MemoryRouter>,
    );

    const overview = screen.getByRole("heading", { name: "插件管理总览" }).closest("section");
    expect(overview).not.toBeNull();
    expect(overview).toHaveClass("span-3");
    expect(overview).not.toHaveClass("span-2");
    const routesTile = within(overview as HTMLElement).getByText("路由").closest("article");
    const channelsTile = within(overview as HTMLElement).getByText("适配器声明").closest("article");
    expect(routesTile).not.toBeNull();
    expect(channelsTile).not.toBeNull();
    expect(within(routesTile as HTMLElement).getByText("0")).toBeInTheDocument();
    expect(within(channelsTile as HTMLElement).getByText("0")).toBeInTheDocument();
    expect(screen.getByText("暂无适配器声明")).toBeInTheDocument();
    expect(screen.getByText("未发现插件路由")).toBeInTheDocument();
  });

  it("keeps every task-domain section mounted after the page split", async () => {
    render(
      <MemoryRouter
        initialEntries={["/plugins"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ConsoleConfigProvider>
          <PluginsPage />
        </ConsoleConfigProvider>
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "插件管理总览" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Flow / Effect 运行视图" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "画图接口" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "已加载插件" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "群级插件开关" })).toBeInTheDocument();
    expect(screen.getByText("插件摘要响应")).toBeInTheDocument();
    expect(screen.getByText("群级插件控制响应")).toBeInTheDocument();

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(await screen.findByText("请先填写 Admin Token 以查看 admin runtime 详情")).toBeInTheDocument();
  });

  const flowRuntimeProps = {
    flowStatus: {
      runtime: {
        enabled: true,
        name: "auto",
        allowed: true,
        allow_target_flows: true,
        allow_compatible_fallback: false,
      },
    },
    readyzFlow: null,
    effectLog: null,
    effectSummary: null,
    effectTraceFilter: "",
    effectAuditFilters: {},
    traceAggregate: null,
    traceAggregateLoading: false,
    traceAggregateError: "",
    flowLoading: false,
    flowError: "",
    onRefresh: vi.fn(),
    onSelectTrace: vi.fn(),
    onClearTraceFilter: vi.fn(),
    onSelectAuditFilters: vi.fn(),
    onClearAuditFilters: vi.fn(),
    onClearAllFilters: vi.fn(),
  };

  it("keeps the platform runtime view free of WeChat reply controls", () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <FlowRuntimeSection {...flowRuntimeProps} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "Flow / Effect 运行视图" })).toBeInTheDocument();
    expect(screen.queryByText("微信私聊")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /兼容回复链路/ })).not.toBeInTheDocument();
    expect(document.querySelector(".plugins-flow-details")).not.toHaveAttribute("open");
  });

  it("opens the step/effect panel for a deep-linked trace instead of repeating the story page", () => {
    HTMLElement.prototype.scrollIntoView = vi.fn();

    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <FlowRuntimeSection
          {...flowRuntimeProps}
          effectTraceFilter="trace-story-1"
          traceAggregate={{
            traceId: "trace-story-1",
            inbound: [{ id: "in-1", channel: "wechat", session_id: "room-1", payload: { text: "hello" } }],
            outbound: [],
            effects: [{ owner: "wxbot", type: "reply", status: "committed", payload_keys: ["text"] }],
            replyQueue: [{ id: 1, status: "sent" }],
            runtimeResult: {
              trace_id: "trace-story-1",
              status: "ok",
              steps: [{ id: "decide", kind: "gate", status: "ok", action: "continue" }],
              effect_dispatches: [{ owner: "wxbot", type: "reply", status: "ok" }],
            },
            errors: [],
          }}
        />
      </MemoryRouter>,
    );

    expect(document.querySelector(".plugins-flow-details")).toHaveAttribute("open");
    expect(screen.getByRole("heading", { name: "这条消息的步骤与 Effect" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "回到这条消息" })).toHaveAttribute("href", "/queues/traces/trace-story-1");
    expect(screen.getByRole("heading", { name: "Flow / Step" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Effect / Handler" })).toBeInTheDocument();
    expect(screen.queryByText("单条 Trace 聚合")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看流转图" })).not.toBeInTheDocument();
    expect(screen.queryByText("消息身份")).not.toBeInTheDocument();
    expect(screen.queryByText("原始聚合明细")).not.toBeInTheDocument();
  });

  it("keeps group-scoped writes disabled until an operator explicitly selects a verified group", async () => {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      let body: Record<string, unknown> = {};
      if (
        url.includes("/v1/admin/plugins/credits/scopes") &&
        init?.method === "POST"
      ) {
        body = {
          scope_state: {
            plugin_name: "credits",
            enabled: true,
            version: 1,
          },
        };
      } else if (url.includes("/v1/admin/plugins/summary")) {
        body = {
          plugins: [
            { name: "wxbot", version: "1", description: "wechat" },
            { name: "credits", version: "1", description: "credits" },
          ],
          plugin_routes: [],
          hooks: {},
          channels: [],
          channel_labels: {},
        };
      } else if (url.includes("/v1/admin/plugins/installed")) {
        body = {
          plugins: [
            {
              name: "wxbot",
              version: "1",
              enabled: true,
              system: false,
              status: "active",
              restart_required: false,
            },
            {
              name: "credits",
              version: "1",
              enabled: true,
              system: false,
              status: "active",
              restart_required: false,
              admin_ui: {
                scope: "group",
                label: "群积分",
                summary: "按群启用积分能力。",
              },
              config_schema: {},
            },
          ],
        };
      } else if (url.includes("/plugins/wxbot/admin/roster/groups")) {
        body = {
          sessions: [{ session_id: "group-1@chatroom", session_name: "测试群" }],
        };
      } else if (url.includes("/v1/admin/plugins/events")) {
        body = { events: [] };
      } else if (url.includes("/v1/admin/message-flows/effects/summary")) {
        body = { enabled: true, backend: "test", summary: {} };
      } else if (url.includes("/v1/admin/message-flows/effects")) {
        body = { enabled: true, backend: "test", items: [] };
      } else if (url.includes("/readyz")) {
        body = { status: "ready", checks: {} };
      }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    render(
      <MemoryRouter
        initialEntries={["/plugins"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ConsoleConfigProvider>
          <AuthenticatedPluginsPage />
        </ConsoleConfigProvider>
      </MemoryRouter>,
    );

    const groupSelect = await screen.findByRole("combobox", { name: "目标群" });
    expect(await within(groupSelect).findByRole("option", { name: "测试群" })).toBeInTheDocument();
    expect(groupSelect).toHaveValue("");

    const scopePanel = screen.getByRole("heading", { name: "群级插件开关" }).closest("section");
    expect(scopePanel).not.toBeNull();
    for (const button of within(scopePanel as HTMLElement).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
    expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("/v1/admin/plugins/scopes"))).toBe(false);

    fireEvent.change(groupSelect, { target: { value: "group-1@chatroom" } });
    await waitFor(() => expect(groupSelect).toHaveValue("group-1@chatroom"));
    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("/v1/admin/plugins/scopes"))).toBe(true);
    });

    fireEvent.click(within(scopePanel as HTMLElement).getByRole("button", { name: "关闭" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认关闭" }));
    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("/v1/admin/plugins/credits/scopes"))).toBe(true);
    });
    const scopeWrite = vi.mocked(fetch).mock.calls.find(
      ([input]) => String(input).includes("/v1/admin/plugins/credits/scopes"),
    );
    expect(scopeWrite).toBeDefined();
    const writeHeaders = new Headers(scopeWrite?.[1]?.headers);
    expect(writeHeaders.get("If-Match")).toBe('"plugin-scope-0"');
    expect(writeHeaders.get("Idempotency-Key")).toMatch(/^agent-console:/);

    const installedPanel = screen.getByRole("heading", { name: "已加载插件" }).closest("section");
    expect(installedPanel).not.toBeNull();
    const creditsCard = within(installedPanel as HTMLElement).getByText("credits").closest("article");
    expect(creditsCard).not.toBeNull();
    fireEvent.click(within(creditsCard as HTMLElement).getByRole("button", { name: "全局停用" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认停用" }));
    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.some(([input]) => String(input).includes("/v1/admin/plugins/credits/disable"))).toBe(true);
    });
    const lifecycleWrite = vi.mocked(fetch).mock.calls.find(
      ([input]) => String(input).includes("/v1/admin/plugins/credits/disable"),
    );
    const lifecycleHeaders = new Headers(lifecycleWrite?.[1]?.headers);
    expect(lifecycleHeaders.get("Idempotency-Key")).toMatch(/^agent-console:/);
  });

  it("opens the installed plugin card named in the query string", async () => {
    vi.mocked(fetch).mockImplementation(async (input) => {
      const url = String(input);
      let body: Record<string, unknown> = {};
      if (url.includes("/v1/admin/plugins/summary")) {
        body = {
          plugins: [{ name: "tibo_reset", version: "1", description: "tibo" }],
          plugin_routes: [],
          hooks: {},
          channels: [],
          channel_labels: {},
        };
      } else if (url.includes("/v1/admin/plugins/installed")) {
        body = {
          plugins: [
            {
              name: "tibo_reset",
              version: "1",
              enabled: false,
              system: false,
              status: "disabled",
              restart_required: false,
              description: "Tibo Reset",
            },
          ],
        };
      } else if (url.includes("/v1/admin/plugins/tibo_reset/runtime")) {
        body = {
          plugin_name: "tibo_reset",
          runtime_status: { configured_enabled: false, api_url_configured: false },
        };
      } else if (url.includes("/v1/admin/plugins/events")) {
        body = { events: [] };
      } else if (url.includes("/v1/admin/message-flows/effects/summary")) {
        body = { enabled: true, backend: "test", summary: {} };
      } else if (url.includes("/v1/admin/message-flows/effects")) {
        body = { enabled: true, backend: "test", items: [] };
      } else if (url.includes("/readyz")) {
        body = { status: "ready", checks: {} };
      }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    render(
      <MemoryRouter
        initialEntries={["/plugins?plugin=tibo_reset"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ConsoleConfigProvider>
          <AuthenticatedPluginsPage />
        </ConsoleConfigProvider>
      </MemoryRouter>,
    );

    const name = await screen.findByText("tibo_reset");
    const card = name.closest("article");
    expect(card).not.toBeNull();
    expect(card).toHaveClass("is-selected");
    expect(within(card as HTMLElement).getAllByText("已停用").length).toBeGreaterThan(0);
    expect(within(card as HTMLElement).getByText("接口地址")).toBeInTheDocument();
    expect(within(card as HTMLElement).getAllByText("未配置").length).toBeGreaterThan(0);
  });

  it("saves and disables the live draw endpoint without restart", async () => {
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      const method = String(init?.method || "GET").toUpperCase();
      let body: Record<string, unknown> = {};
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (url.includes("/plugins/draw/admin/config") && method === "GET") {
        body = {
          version: 0,
          enabled: false,
          api_url: "",
          api_edit_url: "",
          api_model: "",
          api_provider: "",
          api_key_configured: false,
          api_key_hint: "",
          api_host: "",
        };
        headers.ETag = '"0"';
      } else if (url.includes("/plugins/draw/admin/config") && method === "POST") {
        const payload = JSON.parse(String(init?.body || "{}")) as Record<string, string>;
        const enabled = Boolean(payload.api_url);
        body = {
          version: 1,
          enabled,
          api_url: payload.api_url || "",
          api_edit_url: payload.api_edit_url || "",
          api_model: payload.api_model || "",
          api_provider: "",
          api_key_configured: Boolean(payload.api_key),
          api_key_hint: payload.api_key ? String(payload.api_key).slice(-4) : "",
          api_host: enabled ? "draw.example" : "",
        };
        headers.ETag = '"1"';
      } else if (url.includes("/v1/admin/plugins/summary")) {
        body = { plugins: [], plugin_routes: [], hooks: {}, channels: [], channel_labels: {} };
      } else if (url.includes("/v1/admin/plugins/installed")) {
        body = { plugins: [] };
      } else if (url.includes("/v1/admin/plugins/events")) {
        body = { events: [] };
      } else if (url.includes("/v1/admin/message-flows/effects/summary")) {
        body = { enabled: true, backend: "test", summary: {} };
      } else if (url.includes("/v1/admin/message-flows/effects")) {
        body = { enabled: true, backend: "test", items: [] };
      } else if (url.includes("/readyz")) {
        body = { status: "ready", checks: {} };
      }
      return new Response(JSON.stringify(body), { status: 200, headers });
    });

    render(
      <MemoryRouter
        initialEntries={["/plugins"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ConsoleConfigProvider>
          <AuthenticatedPluginsPage />
        </ConsoleConfigProvider>
      </MemoryRouter>,
    );

    const panel = (await screen.findByRole("heading", { name: "画图接口" })).closest("section");
    expect(panel).not.toBeNull();
    expect(await within(panel as HTMLElement).findByText("已停用")).toBeInTheDocument();

    fireEvent.change(within(panel as HTMLElement).getByPlaceholderText("https://example.com/v1"), {
      target: { value: "https://draw.example/v1" },
    });
    fireEvent.change(within(panel as HTMLElement).getByPlaceholderText("尚未配置"), {
      target: { value: "sk-live-key" },
    });
    fireEvent.change(within(panel as HTMLElement).getByPlaceholderText("grok-imagine-image"), {
      target: { value: "grok-imagine-image" },
    });
    fireEvent.click(within(panel as HTMLElement).getByRole("button", { name: "保存接口" }));

    await waitFor(() => {
      expect(within(panel as HTMLElement).getByText("已启用")).toBeInTheDocument();
    });
    const saveCall = vi.mocked(fetch).mock.calls.find(
      ([input, requestInit]) =>
        String(input).includes("/plugins/draw/admin/config")
        && String(requestInit?.method || "GET").toUpperCase() === "POST"
        && String(requestInit?.body || "").includes("draw.example"),
    );
    expect(saveCall).toBeDefined();
    expect(new Headers(saveCall?.[1]?.headers).get("If-Match")).toBe('"0"');
    expect(JSON.parse(String(saveCall?.[1]?.body))).toMatchObject({
      api_url: "https://draw.example/v1",
      api_key: "sk-live-key",
      api_model: "grok-imagine-image",
    });

    fireEvent.click(within(panel as HTMLElement).getByRole("button", { name: "停用接口" }));
    await waitFor(() => {
      expect(within(panel as HTMLElement).getByText("已停用")).toBeInTheDocument();
    });
    const disableCall = vi.mocked(fetch).mock.calls.find(
      ([input, requestInit]) =>
        String(input).includes("/plugins/draw/admin/config")
        && String(requestInit?.method || "GET").toUpperCase() === "POST"
        && String(requestInit?.body || "") === JSON.stringify({ api_url: "" }),
    );
    expect(disableCall).toBeDefined();
    expect(new Headers(disableCall?.[1]?.headers).get("If-Match")).toBe('"1"');
  });
});

describe("pluginEnablementLabel", () => {
  const enabledPlugin = (name: string): InstalledPlugin => ({
    name,
    version: "1",
    enabled: true,
    system: false,
    status: "active",
    restart_required: false,
  });

  it("reports unconfigured for any plugin whose runtime says so", () => {
    expect(
      pluginEnablementLabel(enabledPlugin("tibo_reset"), {
        tibo_reset: { api_url_configured: false },
      }),
    ).toBe("未配置");
    expect(
      pluginEnablementLabel(enabledPlugin("draw"), {
        draw: { configured: false, fallback_configured: false },
      } as never),
    ).toBe("未配置");
    expect(
      pluginEnablementLabel(enabledPlugin("amap"), {
        amap: { api_key_configured: false },
      } as never),
    ).toBe("未配置");
  });

  it("treats a working fallback endpoint as configured", () => {
    expect(
      pluginEnablementLabel(enabledPlugin("draw"), {
        draw: { configured: false, fallback_configured: true },
      } as never),
    ).toBe("已启用");
  });

  it("keeps enabled label when runtime facts are absent", () => {
    expect(pluginEnablementLabel(enabledPlugin("draw"), {})).toBe("已启用");
    expect(pluginEnablementLabel(enabledPlugin("draw"))).toBe("已启用");
  });
});

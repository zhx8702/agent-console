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
    expect(screen.getByRole("heading", { name: "已加载插件" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "群级插件开关" })).toBeInTheDocument();
    expect(screen.getByText("插件摘要响应")).toBeInTheDocument();
    expect(screen.getByText("群级插件控制响应")).toBeInTheDocument();

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(await screen.findByText("请先填写 Admin Token 以查看 admin runtime 详情")).toBeInTheDocument();
  });

  it("keeps the platform runtime view free of WeChat reply controls", () => {
    render(
      <FlowRuntimeSection
        flowStatus={{
          runtime: {
            enabled: true,
            name: "auto",
            allowed: true,
            allow_target_flows: true,
            allow_compatible_fallback: false,
          },
        }}
        readyzFlow={null}
        effectLog={null}
        effectSummary={null}
        effectTraceFilter=""
        effectAuditFilters={{}}
        traceAggregate={null}
        traceAggregateLoading={false}
        traceAggregateError=""
        flowLoading={false}
        flowError=""
        onRefresh={vi.fn()}
        onSelectTrace={vi.fn()}
        onClearTraceFilter={vi.fn()}
        onSelectAuditFilters={vi.fn()}
        onClearAuditFilters={vi.fn()}
        onClearAllFilters={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Flow / Effect 运行视图" })).toBeInTheDocument();
    expect(screen.queryByText("微信私聊")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /兼容回复链路/ })).not.toBeInTheDocument();
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

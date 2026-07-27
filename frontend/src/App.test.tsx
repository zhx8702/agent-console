import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  App,
  NAV_ITEMS,
  routeMetadataForPath,
  routeTitleForPath,
  tenantIdForPrincipal,
} from "./App";
import { apiRequest } from "./lib/api";
import { ConsoleConfigProvider } from "./state/console-config";
import { createCapabilityResponse } from "./test/capability-fixtures";

vi.mock("./lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
  };
});

const apiRequestMock = vi.mocked(apiRequest);

const principalResponse = {
  authenticated: true as const,
  subject: "test-admin",
  roles: ["platform_admin"],
  tenant_ids: ["default"],
  group_ids: ["*"],
  default_tenant_id: "default",
  access_scope: "tenant",
  auth_kind: "session",
};

function renderRoute(path: string) {
  return render(
    <ConsoleConfigProvider>
      <MemoryRouter
        initialEntries={[path]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <App />
      </MemoryRouter>
    </ConsoleConfigProvider>,
  );
}

describe("application routes and shell accessibility", () => {
  beforeEach(() => {
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/auth/me") {
        return principalResponse;
      }
      if (path === "/v1/admin/plugins/marketplace") {
        return { items: [], restart_required: false };
      }
      if (path.endsWith("/capabilities")) {
        return createCapabilityResponse({ navigationPaths: NAV_ITEMS.map((item) => item.to) });
      }
      return {};
    });
  });

  it("maps deep routes to stable page titles", () => {
    expect(routeTitleForPath("/plugins")).toBe("插件管理");
    expect(routeTitleForPath("/plugins/marketplace")).toBe("插件市场");
    expect(routeTitleForPath("/group-behavior")).toBe("群参与与行为");
    expect(routeTitleForPath("/missing")).toBe("页面不存在");
    expect(routeMetadataForPath("/channels")).toMatchObject({
      domain: "消息接入",
      groupScoped: false,
    });
    expect(routeTitleForPath("/wxbot")).toBe("微信扩展控制台");
    expect(routeMetadataForPath("/queues")).toMatchObject({
      domain: "系统运维",
      groupScoped: false,
    });
    expect(routeMetadataForPath("/group-behavior")).toMatchObject({
      domain: "机器人行为",
      groupScoped: true,
    });
    expect(
      tenantIdForPrincipal(
        { tenant_ids: ["tenant-a"], default_tenant_id: "tenant-a" },
        "untrusted-tenant",
      ),
    ).toBe("tenant-a");
    expect(
      tenantIdForPrincipal(
        { tenant_ids: ["*"], default_tenant_id: "server-default" },
        "browser-controlled",
      ),
    ).toBe("server-default");
  });

  it("renders the plugin marketplace from a direct deep link and announces focus", async () => {
    renderRoute("/plugins/marketplace");

    expect(await screen.findByRole("heading", { name: "插件市场" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("插件市场 · 智能体控制台"));
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
    expect(screen.getByText("插件市场页面已加载")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "当前操作群聊" })).not.toBeInTheDocument();
  });

  it("provides an operable mobile navigation drawer", async () => {
    const user = userEvent.setup();
    renderRoute("/plugins/marketplace");

    const menuButton = await screen.findByRole("button", { name: "打开导航" });
    await user.click(menuButton);
    expect(menuButton).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("主导航")).toHaveClass("is-open");

    await user.click(
      within(screen.getByLabelText("主导航")).getByRole("button", { name: "关闭导航" }),
    );
    expect(menuButton).toHaveAttribute("aria-expanded", "false");
  });

  it("uses the server capability and RBAC decision as the navigation source of truth", async () => {
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/auth/me") {
        return {
          ...principalResponse,
          roles: ["platform_reader"],
        };
      }
      if (path.endsWith("/capabilities")) {
        return createCapabilityResponse({ navigationPaths: ["/", "/queues"] });
      }
      return {};
    });

    renderRoute("/");

    const navigation = await screen.findByRole("navigation", { name: "控制台页面" });
    await waitFor(() => expect(within(navigation).getAllByRole("link")).toHaveLength(2));
    expect(within(navigation).getByRole("link", { name: /概览/ })).toBeInTheDocument();
    expect(within(navigation).getByRole("link", { name: /消息队列/ })).toBeInTheDocument();
    expect(within(navigation).queryByRole("link", { name: /插件管理/ })).not.toBeInTheDocument();
    expect(within(navigation).queryByRole("link", { name: /平台连接/ })).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /管理会话/ }));
    expect(screen.getByText("只读成员")).toBeInTheDocument();
    expect(screen.queryByLabelText("Tenant ID")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Tenant Secret")).not.toBeInTheDocument();
  });

  it("blocks a direct deep link that is outside the server-authorized capability set", async () => {
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/auth/me") {
        return {
          ...principalResponse,
          roles: ["group_operator"],
          access_scope: "group",
        };
      }
      if (path.endsWith("/capabilities")) {
        const response = createCapabilityResponse({ navigationPaths: ["/", "/group-behavior"] });
        response.access.scope = "group";
        return response;
      }
      return {};
    });

    renderRoute("/plugins");

    expect(await screen.findByRole("heading", { name: "当前入口不可用" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "插件管理" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回控制台概览" })).toHaveAttribute("href", "/");
  });

  it("falls back to the safe overview entry and offers retry when capability loading fails", async () => {
    let capabilityAttempts = 0;
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/auth/me") {
        return principalResponse;
      }
      if (path.endsWith("/capabilities")) {
        capabilityAttempts += 1;
        if (capabilityAttempts === 1) {
          throw new Error("capability registry unavailable");
        }
        return createCapabilityResponse({ navigationPaths: ["/", "/queues"] });
      }
      return {};
    });
    const user = userEvent.setup();

    renderRoute("/");

    const degradedMessage = await screen.findByText("能力清单暂不可用，仅保留安全入口");
    const status = degradedMessage.closest('[role="status"]');
    expect(status).not.toBeNull();
    expect(within(screen.getByRole("navigation", { name: "控制台页面" })).getAllByRole("link")).toHaveLength(1);

    await user.click(within(status as HTMLElement).getByRole("button", { name: "重试" }));

    await waitFor(() =>
      expect(within(status as HTMLElement).getByText("2 个当前可用入口")).toBeInTheDocument(),
    );
    expect(capabilityAttempts).toBe(2);
  });

  it("renders an accessible wildcard page", async () => {
    renderRoute("/route-that-does-not-exist");

    expect(await screen.findByRole("heading", { name: "页面不存在" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "跳到主要内容" })).toHaveAttribute(
      "href",
      "#main-content",
    );
    const results = await axe.run(document.body, {
      rules: {
        "color-contrast": { enabled: false },
      },
    });
    expect(results.violations).toEqual([]);
  });

  it("mounts the verified group selector only on group-scoped routes", async () => {
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/auth/me") {
        return principalResponse;
      }
      if (path.endsWith("/capabilities")) {
        return createCapabilityResponse({ navigationPaths: NAV_ITEMS.map((item) => item.to) });
      }
      if (path === "/plugins/wxbot/admin/roster/groups") {
        return {
          sessions: [
            { session_id: "verified@chatroom", session_name: "已验证测试群", kind: "group" },
            { session_id: "private-user", session_name: "私聊", kind: "private" },
          ],
        };
      }
      return {};
    });

    const user = userEvent.setup();
    renderRoute("/repeater");

    const selector = await screen.findByRole("region", { name: "当前操作群聊" });
    const combobox = within(selector).getByRole("combobox", { name: "选择已同步群聊" });
    await waitFor(() => expect(combobox).not.toBeDisabled());
    await user.click(combobox);
    expect(within(selector).getByRole("option", { name: "已验证测试群" })).toBeInTheDocument();
    expect(within(selector).queryByRole("option", { name: "私聊" })).not.toBeInTheDocument();

    await user.type(combobox, "任意手填群");
    expect(within(selector).queryByRole("option")).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(combobox).toHaveValue("");
    expect(within(selector).queryByText("手动填写")).not.toBeInTheDocument();
    expect(within(selector).queryByRole("button", { name: "应用目标" })).not.toBeInTheDocument();
  });
});

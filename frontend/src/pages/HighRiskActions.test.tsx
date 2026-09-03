import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../lib/api";
import { DlqPage } from "./DlqPage";
import { PluginMarketplacePage } from "./PluginMarketplacePage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
  };
});

vi.mock("../state/console-config", () => {
  const config = {
    apiBaseUrl: "http://localhost",
    adminToken: "test-admin-token",
    tenantId: "default",
    sessionId: "room@chatroom",
    userId: "operator",
  };
  return {
    useConsoleConfig: () => ({ config, updateConfig: vi.fn() }),
  };
});

const apiRequestMock = vi.mocked(apiRequest);

function idempotencyKeyOf(call: (typeof apiRequestMock.mock.calls)[number]) {
  const options = call[2] as { init?: { headers?: Record<string, string> } } | undefined;
  return options?.init?.headers?.["Idempotency-Key"];
}

describe("high-risk action dialogs", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
  });

  it("summarizes DLQ replay impact, blocks duplicate submits, and reuses the key when retrying", async () => {
    const user = userEvent.setup();
    let rejectFirst: ((reason?: unknown) => void) | undefined;
    apiRequestMock.mockImplementationOnce(
      () => new Promise((_resolve, reject) => {
        rejectFirst = reject;
      }),
    );

    render(<DlqPage />);
    await user.type(screen.getByLabelText("消息标识"), "entry-42");
    await user.click(screen.getByRole("button", { name: "重放消息" }));

    const dialog = screen.getByRole("dialog", { name: "确认重放死信消息" });
    expect(within(dialog).getByText("entry-42")).toBeInTheDocument();
    expect(within(dialog).getByText("从死信队列移除原记录")).toBeInTheDocument();
    expect(apiRequestMock).not.toHaveBeenCalled();

    const axeResult = await axe.run(document.body, {
      rules: {
        "color-contrast": { enabled: false },
        region: { enabled: false },
      },
    });
    expect(axeResult.violations).toEqual([]);

    await user.click(within(dialog).getByRole("button", { name: "确认重放" }));
    const pendingButton = within(dialog).getByRole("button", { name: "正在重放…" });
    expect(pendingButton).toBeDisabled();
    expect(pendingButton).toHaveAttribute("aria-busy", "true");
    expect(apiRequestMock).toHaveBeenCalledTimes(1);

    rejectFirst?.(new Error("temporary replay failure"));
    expect(await within(dialog).findByText("temporary replay failure")).toBeInTheDocument();
    const firstKey = idempotencyKeyOf(apiRequestMock.mock.calls[0]);
    expect(firstKey).toMatch(/^agent-console:/);

    apiRequestMock.mockResolvedValueOnce({ ok: true });
    await user.click(within(dialog).getByRole("button", { name: "确认重放" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(apiRequestMock).toHaveBeenCalledTimes(2);
    expect(idempotencyKeyOf(apiRequestMock.mock.calls[1])).toBe(firstKey);
  });

  it("does not uninstall a plugin until the impact dialog is confirmed", async () => {
    const user = userEvent.setup();
    const marketplace = {
      restart_required: false,
      items: [
        {
          name: "memory",
          display_name: "Memory",
          version: "2.0.0",
          description: "Long-term memory",
          source: "builtin",
          package_type: "builtin",
          installed: true,
          installed_version: "2.0.0",
          enabled: true,
          compatible: true,
          status: "installed",
          restart_required: false,
          permissions: [{ id: "memory.read" }],
          dependencies: [],
          capabilities: {},
          restart_policy: "required",
          warnings: [],
        },
      ],
    };
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/plugins/marketplace") {
        return marketplace;
      }
      if (path === "/v1/admin/plugins/memory/uninstall") {
        return { ok: true, restart_required: true };
      }
      if (path === "/v1/admin/runtime/restart-instructions") {
        return { actionable: true, restart_required: true, message: "Restart the service" };
      }
      return {};
    });

    render(
      <MemoryRouter>
        <PluginMarketplacePage />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "卸载插件" }));

    const dialog = screen.getByRole("dialog", { name: "确认卸载 Memory" });
    expect(within(dialog).getByText("插件会被标记为已卸载，重启服务后停止提供相关能力。")).toBeInTheDocument();
    expect(apiRequestMock.mock.calls.some(([, path]) => path === "/v1/admin/plugins/memory/uninstall")).toBe(false);

    await user.click(within(dialog).getByRole("button", { name: "确认卸载" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const uninstallCall = apiRequestMock.mock.calls.find(([, path]) => path === "/v1/admin/plugins/memory/uninstall");
    expect(uninstallCall).toBeDefined();
    expect(idempotencyKeyOf(uninstallCall!)).toMatch(/^agent-console:/);
  });

  it("requires an explicit preview and dialog confirmation before installing a plugin", async () => {
    const user = userEvent.setup();
    const item = {
      name: "notes",
      display_name: "Notes",
      version: "1.4.0",
      description: "Shared notes",
      source: "builtin",
      package_type: "builtin",
      installed: false,
      installed_version: "",
      enabled: false,
      compatible: true,
      status: "available",
      restart_required: false,
      permissions: [{ id: "notes.read" }],
      dependencies: [],
      capabilities: {},
      restart_policy: "required",
      warnings: [],
    };
    const preview = {
      name: item.name,
      version: item.version,
      compatible: true,
      installed_version: "",
      permission_changes: { added: ["notes.write"], removed: [] },
      restart_required: true,
      permissions: [{ id: "notes.read" }, { id: "notes.write" }],
      dependencies: [{ name: "memory", required: true }],
      warnings: ["restart required"],
    };
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/plugins/marketplace") {
        return { restart_required: false, items: [item] };
      }
      if (path === "/v1/admin/plugins/install/preview") {
        return preview;
      }
      if (path === "/v1/admin/plugins/install") {
        return { ok: true, restart_required: true };
      }
      if (path === "/v1/admin/runtime/restart-instructions") {
        return { actionable: true, restart_required: true, message: "Restart the service" };
      }
      return {};
    });

    render(
      <MemoryRouter>
        <PluginMarketplacePage />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "预览安装" }));
    await waitFor(() => {
      expect(apiRequestMock.mock.calls.some(([, path]) => path === "/v1/admin/plugins/install/preview")).toBe(true);
    });
    expect(apiRequestMock.mock.calls.some(([, path]) => path === "/v1/admin/plugins/install")).toBe(false);
    expect(screen.getByText("插件市场接口响应").closest("details")).not.toHaveAttribute("open");

    await user.click(await screen.findByRole("button", { name: "确认安装" }));
    const dialog = screen.getByRole("dialog", { name: "确认安装 Notes" });
    expect(within(dialog).getByText("notes.write")).toBeInTheDocument();
    expect(within(dialog).getByText("提交后需要重启服务才会生效。")).toBeInTheDocument();
    expect(apiRequestMock.mock.calls.some(([, path]) => path === "/v1/admin/plugins/install")).toBe(false);

    await user.click(within(dialog).getByRole("button", { name: "确认安装" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const installCall = apiRequestMock.mock.calls.find(([, path]) => path === "/v1/admin/plugins/install");
    expect(installCall).toBeDefined();
    expect(idempotencyKeyOf(installCall!)).toMatch(/^agent-console:/);
  });
});

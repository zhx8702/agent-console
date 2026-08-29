import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, VersionConflictError, apiVersionedResource } from "../lib/api";
import { AmapPage } from "./AmapPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, apiVersionedResource: vi.fn() };
});

vi.mock("../state/console-config", () => ({
  useConsoleConfig: () => ({
    config: {
      apiBaseUrl: "http://localhost",
      adminToken: "test-admin-token",
      tenantId: "",
      sessionId: "",
      userId: "operator",
    },
  }),
}));

const apiVersionedMock = vi.mocked(apiVersionedResource);

function config(overrides: Record<string, unknown> = {}) {
  return {
    api_key_configured: true,
    api_key_mutable_via_api: false,
    api_key_source: "environment_or_file_secret",
    runtime_config_mutable: true,
    timeout_seconds: 30,
    storage_dir: "/srv/amap",
    storage_dir_exists: true,
    storage_dir_writable: true,
    agent_scope: "group_personal_map",
    tools: ["amap_geo", "amap_static_map"],
    restart_required: false,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <main>
        <AmapPage />
      </main>
    </MemoryRouter>,
  );
}

describe("AmapPage secret and concurrency controls", () => {
  beforeEach(() => {
    apiVersionedMock.mockReset();
    apiVersionedMock.mockResolvedValue({ value: config(), etag: '"amap-config-v1"' });
  });

  it("keeps the secret external and saves only non-secret fields with If-Match", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("AMAP_API_KEY 由外部密钥提供方管理")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "AMAP_API_KEY" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /清空当前高德 API Key/ })).not.toBeInTheDocument();

    const timeout = await screen.findByRole("spinbutton", { name: "请求超时秒数" });
    await waitFor(() => expect(timeout).toBeEnabled());
    await user.clear(timeout);
    await user.type(timeout, "45");
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(apiVersionedMock).toHaveBeenLastCalledWith(
      expect.anything(),
      "/plugins/amap/admin/config",
      expect.objectContaining({
        method: "POST",
        ifMatch: '"amap-config-v1"',
        idempotencyKey: expect.stringMatching(/^agent-console:/),
        body: {
          timeout_seconds: 45,
          storage_dir: "/srv/amap",
        },
      }),
    ));
    expect(JSON.stringify(apiVersionedMock.mock.calls)).not.toContain("amap_api_key");

    const results = await axe.run(document.body, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it("preserves the local draft when the server reports a version conflict", async () => {
    const user = userEvent.setup();
    apiVersionedMock
      .mockResolvedValueOnce({ value: config(), etag: '"amap-config-v1"' })
      .mockRejectedValueOnce(
        new VersionConflictError(
          "409 version conflict",
          { detail: { code: "amap_config_version_conflict" } },
          '"amap-config-v2"',
        ),
      );
    renderPage();

    const timeout = await screen.findByRole("spinbutton", { name: "请求超时秒数" });
    await user.clear(timeout);
    await user.type(timeout, "48");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "配置已被其他管理员修改。本地编辑已保留",
    );
    expect(timeout).toHaveValue(48);
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
  });

  it("warns before navigating away with unsaved runtime configuration", async () => {
    const user = userEvent.setup();
    renderPage();

    const timeout = await screen.findByRole("spinbutton", { name: "请求超时秒数" });
    await user.clear(timeout);
    await user.type(timeout, "45");
    await user.click(screen.getByRole("link", { name: "智能体工具白名单" }));

    expect(screen.getByRole("dialog", { name: "放弃未保存的修改？" })).toBeInTheDocument();
    expect(screen.getByText("当前页面有尚未保存的修改，确定要离开吗？")).toBeInTheDocument();
  });

  it("reuses the same mutation key while a prepared write is pending", async () => {
    const user = userEvent.setup();
    const pending = new ApiError(
      503,
      "503 amap_config_mutation_pending",
      { detail: { code: "amap_config_mutation_pending", mutation_id: "mutation-7" } },
    );
    apiVersionedMock
      .mockResolvedValueOnce({ value: config(), etag: '"amap-config-v1"' })
      .mockRejectedValue(pending);
    renderPage();

    const timeout = await screen.findByRole("spinbutton", { name: "请求超时秒数" });
    await user.clear(timeout);
    await user.type(timeout, "48");
    const save = screen.getByRole("button", { name: "保存配置" });
    await user.click(save);
    expect(await screen.findByRole("alert")).toHaveTextContent("配置写入仍在恢复中");
    await waitFor(() => expect(save).toBeEnabled());
    await user.click(save);

    await waitFor(() => {
      const writes = apiVersionedMock.mock.calls.filter(([, , options]) => options?.method === "POST");
      expect(writes).toHaveLength(2);
      expect(writes[0][2]?.idempotencyKey).toBe(writes[1][2]?.idempotencyKey);
      expect(writes[0][2]?.ifMatch).toBe('"amap-config-v1"');
      expect(writes[1][2]?.ifMatch).toBe('"amap-config-v1"');
      expect(writes[0][2]?.body).toEqual(writes[1][2]?.body);
    });
    expect(timeout).toHaveValue(48);
  });

  it("renders production configuration as read-only", async () => {
    apiVersionedMock.mockResolvedValueOnce({
      value: config({ runtime_config_mutable: false }),
      etag: '"amap-config-read-only"',
    });
    renderPage();

    expect(await screen.findByText(/当前部署为只读配置/)).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "请求超时秒数" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "二维码保存目录" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "保存配置" })).toBeDisabled();
  });
});

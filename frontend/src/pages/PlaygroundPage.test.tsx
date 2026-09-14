import { useEffect } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../lib/api";
import { ConsoleConfigProvider, useConsoleConfig } from "../state/console-config";
import { PlaygroundPage } from "./PlaygroundPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, apiRequest: vi.fn() };
});

const apiRequestMock = vi.mocked(apiRequest);

function VerifiedGroupSeed() {
  const {
    config,
    updateConfig,
    verifiedGroupIds,
    registerVerifiedGroups,
    selectVerifiedGroup,
  } = useConsoleConfig();

  useEffect(() => {
    updateConfig({ tenantId: "tenant-a", adminToken: "cookie-session" });
    registerVerifiedGroups(["room@chatroom"]);
  }, [registerVerifiedGroups, updateConfig]);

  useEffect(() => {
    if (verifiedGroupIds.has("room@chatroom") && config.sessionId !== "room@chatroom") {
      selectVerifiedGroup("room@chatroom");
    }
  }, [config.sessionId, selectVerifiedGroup, verifiedGroupIds]);

  return null;
}

describe("PlaygroundPage", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
  });

  it("keeps send disabled until a backend-verified group is selected", () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ConsoleConfigProvider>
          <PlaygroundPage />
        </ConsoleConfigProvider>
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "先选择一个已验证群聊" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "发送模拟消息" })).not.toBeInTheDocument();
  });

  it("uses the server-side simulator without a browser tenant secret", async () => {
    apiRequestMock.mockResolvedValue({
      status: "accepted",
      message_id: "admin-sim-1",
      trace_id: "trace-1",
      session_id: "room@chatroom",
      session_name: "测试群",
    });
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ConsoleConfigProvider>
          <VerifiedGroupSeed />
          <PlaygroundPage />
        </ConsoleConfigProvider>
      </MemoryRouter>,
    );

    const input = screen.getByRole("textbox", { name: /^模拟群消息/ });
    fireEvent.change(input, { target: { value: "@机器人 帮我总结一下" } });
    const send = await screen.findByRole("button", { name: "发送模拟消息" });
    await waitFor(() => expect(send).toBeEnabled());
    fireEvent.click(send);

    await waitFor(() => expect(apiRequestMock).toHaveBeenCalledTimes(1));
    const [config, path, options] = apiRequestMock.mock.calls[0];
    expect(config.tenantId).toBe("tenant-a");
    expect(path).toBe(
      "/plugins/wxbot/admin/tenants/tenant-a/groups/room%40chatroom/simulate-inbound",
    );
    expect(options?.init?.headers).toMatchObject({
      "Content-Type": "application/json",
    });
    expect(String((options?.init?.headers as Record<string, string>)["Idempotency-Key"]))
      .toMatch(/^agent-console:/);
    expect(options?.init?.body).toBe(JSON.stringify({ message: "@机器人 帮我总结一下" }));
    expect(JSON.stringify(config)).not.toContain("secret");
    expect(await screen.findByText(/已进入消息处理队列/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看这条消息" })).toHaveAttribute(
      "href",
      "/queues/traces/trace-1",
    );
  });
});

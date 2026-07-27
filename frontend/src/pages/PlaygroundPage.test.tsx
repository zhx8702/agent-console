import { useEffect } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
      <ConsoleConfigProvider>
        <PlaygroundPage />
      </ConsoleConfigProvider>,
    );

    expect(screen.getByText("尚未选择目标群")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送模拟消息" })).toBeDisabled();
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
      <ConsoleConfigProvider>
        <VerifiedGroupSeed />
        <PlaygroundPage />
      </ConsoleConfigProvider>,
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
  });
});

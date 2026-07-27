import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { WxbotSendTab } from "./WxbotSendTab";
import type { WxbotPageController } from "./useWxbotPageController";

function buildController() {
  return {
    actionOutput: "",
    config: { adminToken: "test-admin-token" },
    loadReplyQueueMessages: vi.fn(),
    loadSdkQueueMessages: vi.fn(),
    queueLimit: 50,
    queueStatusFilter: "pending",
    reconcileSdkQueueMessage: vi.fn(async () => ({ ok: true })),
    replyQueueItems: [],
    sdkQueueItems: [
      {
        id: 7,
        command_id: "agent-console:command-7",
        session_id: "room@chatroom",
        session_name: "产品群",
        reply_text: "待核对消息",
        status: "uncertain",
        error: "delivery outcome unknown",
        attempt_count: 1,
        created_ts: 1_721_283_200,
        claimed_ts: 1_721_283_210,
      },
      {
        id: 8,
        command_id: "agent-console:command-8",
        session_id: "room@chatroom",
        reply_text: "发送中消息",
        status: "running",
        attempt_count: 0,
      },
    ],
    sdkQueueReconcileBusy: "",
    sdkQueueStatusFilter: "uncertain",
    setQueueLimit: vi.fn(),
    setQueueStatusFilter: vi.fn(),
    setSdkQueueStatusFilter: vi.fn(),
  } as unknown as WxbotPageController;
}

describe("WxbotSendTab uncertain delivery reconciliation", () => {
  it("shows structured guidance and confirms both reconciliation choices", async () => {
    const user = userEvent.setup();
    const controller = buildController();
    render(
      <MemoryRouter>
        <WxbotSendTab controller={controller} />
      </MemoryRouter>,
    );

    expect(screen.getByText("发送结果待人工核对")).toBeInTheDocument();
    expect(screen.getByText("正在与微信客户端交互")).toBeInTheDocument();
    expect(screen.getByText("agent-console:command-7")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "确认已发送" }));
    let dialog = screen.getByRole("dialog", { name: "确认消息 #7 已发送" });
    expect(within(dialog).getByText(/不会再次投递/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认已发送" }));
    expect(controller.reconcileSdkQueueMessage).toHaveBeenCalledWith(7, "confirm_sent");

    await user.click(screen.getByRole("button", { name: "确认未发送并重试" }));
    dialog = screen.getByRole("dialog", { name: "重新投递消息 #7" });
    expect(within(dialog).getByText(/会造成重复消息/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认未发送，允许重试" }));
    expect(controller.reconcileSdkQueueMessage).toHaveBeenCalledWith(7, "retry");
  });
});

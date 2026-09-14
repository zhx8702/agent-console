import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { WxbotWebhookTab } from "./WxbotWebhookTab";
import type { WxbotPageController } from "./useWxbotPageController";

function buildController() {
  return {
    copyGroupWebhookUrl: vi.fn(),
    createGroupWebhook: vi.fn(),
    groupSessions: [
      { session_id: "room@chatroom", session_name: "产品群", kind: "group" },
    ],
    groupWebhookOutput: "",
    groupWebhookSessionId: "room@chatroom",
    groupWebhookStatus: "loaded",
    groupWebhooks: [
      {
        webhook_id: "gwh_1",
        session_id: "room@chatroom",
        session_name: "产品群",
        token_hint: "ab12",
        enabled: true,
      },
      {
        webhook_id: "gwh_old",
        session_id: "old@chatroom",
        session_name: "旧群",
        token_hint: "zz99",
        enabled: false,
      },
    ],
    loadGroupWebhooks: vi.fn(),
    revealedGroupWebhook: {
      webhook_id: "gwh_1",
      session_id: "room@chatroom",
      url: "https://bot.example/api/v1/webhook/groups/whg_secret",
    },
    revokeGroupWebhook: vi.fn(),
    rotateGroupWebhook: vi.fn(),
    setGroupWebhookSessionId: vi.fn(),
  } as unknown as WxbotPageController;
}

describe("WxbotWebhookTab", () => {
  it("creates, copies, rotates, and confirms revoke for a group webhook", async () => {
    const user = userEvent.setup();
    const controller = buildController();
    render(<WxbotWebhookTab controller={controller} />);

    expect(screen.getByRole("heading", { name: "群 webhook" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://bot.example/api/v1/webhook/groups/whg_secret")).toBeInTheDocument();
    expect(screen.getByText("ab12")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "生成 webhook" }));
    expect(controller.createGroupWebhook).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "复制地址" }));
    expect(controller.copyGroupWebhookUrl).toHaveBeenCalledWith(
      "https://bot.example/api/v1/webhook/groups/whg_secret",
    );

    const activeRow = screen.getByText("ab12").closest("tr");
    const revokedRow = screen.getByText("zz99").closest("tr");
    expect(activeRow).toBeTruthy();
    expect(revokedRow).toBeTruthy();

    await user.click(within(activeRow as HTMLElement).getByRole("button", { name: "轮换" }));
    expect(controller.rotateGroupWebhook).toHaveBeenCalledWith("gwh_1");

    await user.click(within(activeRow as HTMLElement).getByRole("button", { name: "停用" }));
    const dialog = screen.getByRole("dialog", { name: "停用 产品群 的 webhook" });
    expect(within(dialog).getByText(/原地址立即失效/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认停用" }));
    expect(controller.revokeGroupWebhook).toHaveBeenCalledWith("gwh_1");

    await user.click(within(revokedRow as HTMLElement).getByRole("button", { name: "删除" }));
    const deleteDialog = screen.getByRole("dialog", { name: "删除 旧群 的停用记录" });
    await user.click(within(deleteDialog).getByRole("button", { name: "确认删除" }));
    expect(controller.revokeGroupWebhook).toHaveBeenCalledWith("gwh_old");
  });
});

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../lib/api";
import { MemoryPage } from "./MemoryPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
    getMemoryExtractionJobStats: vi.fn().mockResolvedValue({ counts: {} }),
  };
});

vi.mock("../state/console-config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../state/console-config")>();
  const config = {
    apiBaseUrl: "http://localhost",
    adminToken: "",
    tenantId: "default",
    sessionId: "room@chatroom",
    userId: "",
  };
  return {
    ...actual,
    useConsoleConfig: () => ({
      config,
      verifiedGroupIds: new Set(["room@chatroom"]),
      registerVerifiedGroups: vi.fn(),
      selectVerifiedGroup: vi.fn(),
      clearSelectedGroup: vi.fn(),
      updateConfig: vi.fn(),
    }),
  };
});

const apiRequestMock = vi.mocked(apiRequest);

describe("memory page with HttpOnly authentication", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/plugins/wxbot/admin/roster/groups") {
        return {
          sessions: [{ session_id: "room@chatroom", session_name: "测试群", kind: "group" }],
        };
      }
      if (path.includes("/plugins/wxbot/admin/roster/groups/room%40chatroom/members")) {
        return { candidates: [{ wxid: "wxid_member", name: "成员甲", msg_count: 12 }] };
      }
      return {};
    });
  });

  it("keeps maintenance available without an Admin Token input", async () => {
    const { unmount } = render(<MemoryPage />);

    expect(await screen.findByRole("button", { name: "刷新会话列表" })).toBeEnabled();
    expect(screen.getByRole("table", { name: "当前已验证群聊成员名册" })).toBeInTheDocument();
    expect(screen.queryByText(/Admin Token/i)).not.toBeInTheDocument();
    await waitFor(() => {
      expect(apiRequestMock.mock.calls.some(([, path]) => path === "/plugins/wxbot/admin/roster/groups")).toBe(true);
    });
    unmount();
  });

  it("separates memory tasks and gives history backfill a full-width non-scrolling workspace", async () => {
    render(<MemoryPage />);

    expect(screen.queryByRole("heading", { name: "SDK 历史回填与运行态" })).not.toBeInTheDocument();
    const backfillTab = screen.getByRole("tab", { name: "历史回填" });
    fireEvent.click(backfillTab);

    const backfillHeading = screen.getByRole("heading", { name: "SDK 历史回填与运行态" });
    const backfillPanel = backfillHeading.closest("section");
    expect(backfillPanel).toHaveClass("memory-backfill-panel");
    expect(backfillPanel).not.toHaveClass("panel-scroll");
    expect(backfillPanel?.parentElement).toHaveAttribute("role", "tabpanel");
    expect(backfillPanel?.parentElement).toHaveClass("memory-workspace-panel");

    fireEvent.click(screen.getByRole("tab", { name: "成员与档案" }));
    expect(backfillPanel?.parentElement).toHaveAttribute("hidden");
  });

  it("inherits the verified group and member scope inside profile enrichment", () => {
    render(<MemoryPage />);

    fireEvent.click(screen.getByRole("tab", { name: "画像复核" }));
    const panel = screen.getByRole("tabpanel", { name: "画像复核" });
    expect(within(panel).getByText("继承当前群")).toBeInTheDocument();
    expect(within(panel).getByText("继承当前成员")).toBeInTheDocument();
    expect(within(panel).queryByText("选择微信群")).not.toBeInTheDocument();
    expect(within(panel).queryByText("选择群成员")).not.toBeInTheDocument();
  });
});

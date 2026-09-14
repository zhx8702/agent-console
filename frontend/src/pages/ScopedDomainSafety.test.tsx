import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../lib/api";
import { RelationshipGraphPage } from "./RelationshipGraphPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
    getGroupGraph: vi.fn().mockResolvedValue({ nodes: [], edges: [], summary: {} }),
    getGroupGraphEdgeEvidence: vi.fn().mockResolvedValue({ entities: [], records: [] }),
    getGroupGraphHistoryDates: vi.fn().mockResolvedValue({ items: [] }),
    getGroupGraphWindowStats: vi.fn().mockResolvedValue({ totals: {}, acceptance_counts: {} }),
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

describe("scoped domain pages with HttpOnly authentication", () => {
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
      if (path === "/plugins/memory/group-graph/extract-daily") {
        return { ok: true, status: "completed", more_remain: false, jobs: {}, counts: {} };
      }
      return {};
    });
  });

  it("allows relationship extraction without treating an empty browser token field as logged out", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<RelationshipGraphPage />);

    await user.click(screen.getByRole("button", { name: "抽取控制" }));
    const action = screen.getByRole("button", { name: "运行所选日期 AI 抽取" });
    expect(action).toBeEnabled();

    await user.click(action);
    const dialog = screen.getByRole("dialog", { name: "确认运行所选日期抽取" });
    await user.click(within(dialog).getByRole("button", { name: "确认运行" }));

    await waitFor(() => {
      expect(apiRequestMock.mock.calls.some(([, path]) => path === "/plugins/memory/group-graph/extract-daily")).toBe(true);
    });
    await waitFor(() => expect(action).toBeEnabled());
    const writeCall = apiRequestMock.mock.calls.find(([, path]) => path === "/plugins/memory/group-graph/extract-daily");
    expect(writeCall?.[2]?.auth).toBe(true);
    expect((writeCall?.[2]?.init?.headers as Record<string, string>)["Idempotency-Key"]).toBeUndefined();
    unmount();
  });

  it("binds relationship history sync to the explicit legacy connection scope", async () => {
    const user = userEvent.setup();
    render(<RelationshipGraphPage />);

    await user.click(screen.getByRole("button", { name: "抽取控制" }));
    await user.click(screen.getByRole("button", { name: "同步日期并排队抽取" }));
    const dialog = screen.getByRole("dialog", { name: "确认同步群聊历史" });
    await user.click(within(dialog).getByRole("button", { name: "确认同步" }));

    await waitFor(() => {
      expect(
        apiRequestMock.mock.calls.some(([, path]) => path === "/plugins/memory/backfill"),
      ).toBe(true);
    });
    const writeCall = apiRequestMock.mock.calls.find(
      ([, path]) => path === "/plugins/memory/backfill",
    );
    const body = JSON.parse(String(writeCall?.[2]?.init?.body || "{}")) as {
      connection_id?: string;
      session_ids?: string[];
    };
    expect(body.connection_id).toBe("legacy-wechat-default");
    expect(body.session_ids).toEqual(["room@chatroom"]);
    expect(
      (writeCall?.[2]?.init?.headers as Record<string, string>)["Idempotency-Key"],
    ).toMatch(/^agent-console:/);
  });

});

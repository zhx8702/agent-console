import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
} from "../lib/api";
import { CommandsPage } from "./CommandsPage";
import { ModerationPage } from "./ModerationPage";

const consoleState = vi.hoisted(() => ({
  config: {
    apiBaseUrl: "http://localhost",
    adminToken: "session-authenticated",
    tenantId: "default",
    sessionId: "room@chatroom",
    userId: "",
  },
  registerVerifiedGroups: vi.fn(),
  selectVerifiedGroup: vi.fn(),
  updateConfig: vi.fn(),
  verifiedGroupIds: new Set(["room@chatroom"]),
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
    apiVersionedResource: vi.fn(),
  };
});

vi.mock("../state/console-config", () => ({
  useConsoleConfig: () => ({
    ...consoleState,
  }),
}));

const apiRequestMock = vi.mocked(apiRequest);
const apiVersionedMock = vi.mocked(apiVersionedResource);

const commandConfig = {
  tenant_id: "default",
  version: 1,
  admin_user_ids_text: "wxid-original",
  admin_user_ids: ["wxid-original"],
  user_commands_text: "/hello",
  user_commands: ["/hello"],
  admin_commands_text: "/admin",
  admin_commands: ["/admin"],
  catalog: [],
};

const moderationConfig = {
  tenant_id: "default",
  session_id: "room@chatroom",
  enabled: true,
  reminder_mode: "append",
  reminder_text: "原提醒",
  webhook_url: "",
  webhook_enabled: false,
  version: 3,
};

describe("versioned plugin configuration pages", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
    apiVersionedMock.mockReset();
    consoleState.registerVerifiedGroups.mockReset();
    consoleState.selectVerifiedGroup.mockReset();
    consoleState.updateConfig.mockReset();
  });

  it("keeps a Commands draft on 409 and sends the loaded If-Match", async () => {
    const user = userEvent.setup();
    apiVersionedMock.mockImplementation(async (_config, path, options) => {
      if (path.includes("/plugins/commands/config/") && options?.method === "POST") {
        throw new VersionConflictError(
          "409 version_conflict",
          { detail: { code: "version_conflict", current_version: 2 } },
          '"2"',
        );
      }
      return { value: commandConfig, etag: '"1"' };
    });

    render(<CommandsPage />);
    const admins = await screen.findByLabelText("管理员成员标识");
    expect(admins).toHaveValue("wxid-original");

    await user.clear(admins);
    await user.type(admins, "wxid-local-draft");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    expect(await screen.findByText("服务器配置已被其他操作者更新")).toBeInTheDocument();
    expect(admins).toHaveValue("wxid-local-draft");
    const mutation = apiVersionedMock.mock.calls.find(([, path, options]) => (
      path.includes("/plugins/commands/config/") && options?.method === "POST"
    ));
    expect(mutation?.[2]?.ifMatch).toBe('"1"');
    expect(mutation?.[2]?.body).toMatchObject({ admin_user_ids_text: "wxid-local-draft" });
  });

  it("does not replace a loaded Commands form when a refresh fails", async () => {
    const user = userEvent.setup();
    let failRead = false;
    apiVersionedMock.mockImplementation(async () => {
      if (failRead) {
        throw new Error("temporary read failure");
      }
      return { value: commandConfig, etag: '"1"' };
    });

    render(<CommandsPage />);
    const admins = await screen.findByLabelText("管理员成员标识");
    expect(admins).toHaveValue("wxid-original");
    failRead = true;
    await user.click(screen.getByRole("button", { name: "读取配置" }));

    await waitFor(() => expect(screen.getByText("操作失败")).toBeInTheDocument());
    expect(admins).toHaveValue("wxid-original");
  });

  it("keeps a Moderation draft on conflict and scopes config/keywords by ETag", async () => {
    const user = userEvent.setup();
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path.endsWith("/admin/roster/groups")) {
        return {
          sessions: [
            {
              session_id: "room@chatroom",
              session_name: "产品群",
              kind: "group",
            },
          ],
        };
      }
      if (path.includes("/events/")) {
        return { items: [] };
      }
      return {};
    });
    apiVersionedMock.mockImplementation(async (_config, path, options) => {
      if (path.includes("/plugins/moderation/config/") && options?.method === "POST") {
        throw new VersionConflictError(
          "409 version_conflict",
          { detail: { code: "version_conflict", current_version: 4 } },
          '"4"',
        );
      }
      if (path.includes("/plugins/moderation/config/")) {
        return { value: moderationConfig, etag: '"3"' };
      }
      if (path.includes("/plugins/moderation/keywords/")) {
        return { value: { items: [], count: 0, version: 3 }, etag: '"3"' };
      }
      throw new Error(`unexpected versioned request: ${path}`);
    });

    render(<ModerationPage />);
    const reminder = await screen.findByLabelText("提醒文案");
    await waitFor(() => expect(reminder).toBeEnabled());
    expect(reminder).toHaveValue("原提醒");

    await user.clear(reminder);
    await user.type(reminder, "本地提醒草稿");
    await user.click(screen.getByRole("button", { name: "保存当前群配置" }));

    expect(await screen.findByText("审核配置已被其他操作者更新")).toBeInTheDocument();
    expect(reminder).toHaveValue("本地提醒草稿");
    const mutation = apiVersionedMock.mock.calls.find(([, path, options]) => (
      path.includes("/plugins/moderation/config/") && options?.method === "POST"
    ));
    expect(mutation?.[2]?.ifMatch).toBe('"3"');
    expect(mutation?.[2]?.body).toMatchObject({ reminder_text: "本地提醒草稿" });
  });

  it("reuses the Moderation delete key after a lost response", async () => {
    const user = userEvent.setup();
    let deleteAttempts = 0;
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path.endsWith("/admin/roster/groups")) {
        return {
          sessions: [
            {
              session_id: "room@chatroom",
              session_name: "产品群",
              kind: "group",
            },
          ],
        };
      }
      if (path.includes("/events/")) {
        return { items: [] };
      }
      return {};
    });
    apiVersionedMock.mockImplementation(async (_config, path, options) => {
      if (path.includes("/plugins/moderation/config/")) {
        return { value: moderationConfig, etag: '"3"' };
      }
      if (path.includes("/plugins/moderation/keywords/") && options?.method === "DELETE") {
        deleteAttempts += 1;
        if (deleteAttempts === 1) {
          throw new Error("response lost");
        }
        return { value: { items: [], count: 0, version: 4 }, etag: '"4"' };
      }
      if (path.includes("/plugins/moderation/keywords/")) {
        return {
          value: {
            items: [{ id: 1, keyword: "alpha", enabled: true }],
            count: 1,
            version: 3,
          },
          etag: '"3"',
        };
      }
      throw new Error(`unexpected versioned request: ${path}`);
    });

    render(<ModerationPage />);
    await screen.findAllByText("alpha");
    await user.click(screen.getByRole("button", { name: "删除" }));
    const dialog = screen.getByRole("dialog", { name: "删除关键词“alpha”" });
    await user.click(within(dialog).getByRole("button", { name: "确认删除" }));
    expect(await within(dialog).findByText("response lost")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(deleteAttempts).toBe(2));
    const deletes = apiVersionedMock.mock.calls.filter(([, path, options]) => (
      path.includes("/plugins/moderation/keywords/") && options?.method === "DELETE"
    ));
    expect(deletes[0]?.[2]?.idempotencyKey).toMatch(/^agent-console:/);
    expect(deletes[1]?.[2]?.idempotencyKey).toBe(deletes[0]?.[2]?.idempotencyKey);
    expect(deletes[0]?.[2]?.ifMatch).toBe('"3"');
  });
});

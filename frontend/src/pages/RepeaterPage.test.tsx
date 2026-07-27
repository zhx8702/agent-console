import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { VersionConflictError, apiRequest, apiVersionedResource } from "../lib/api";
import { RepeaterPage } from "./RepeaterPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
    apiVersionedResource: vi.fn(),
  };
});

vi.mock("../state/console-config", () => {
  const config = {
      apiBaseUrl: "http://localhost",
      adminToken: "admin-token",
      tenantId: "default",
      sessionId: "room@chatroom",
      userId: "",
  };
  const verifiedGroupIds = new Set(["room@chatroom"]);
  return {
    useConsoleConfig: () => ({ config, verifiedGroupIds }),
  };
});

const apiRequestMock = vi.mocked(apiRequest);
const apiVersionedMock = vi.mocked(apiVersionedResource);

describe("RepeaterPage optimistic concurrency", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
    apiVersionedMock.mockReset();
    apiVersionedMock.mockImplementation(async (_config, _path, options) => ({
      value: {
        tenant_id: "default",
        session_id: "room@chatroom",
        enabled: options?.method === "POST"
          ? Boolean((options.body as { enabled?: boolean } | undefined)?.enabled)
          : false,
        cooldown_seconds: Number(
          (options?.body as { cooldown_seconds?: number } | undefined)?.cooldown_seconds || 300,
        ),
        version: options?.method === "POST" ? 2 : 1,
      },
      etag: options?.method === "POST" ? '"2"' : '"1"',
    }));
  });

  it("loads an ETag and conditionally saves the edited draft", async () => {
    const user = userEvent.setup();
    render(<RepeaterPage />);

    const toggle = await screen.findByRole("checkbox", { name: /启用群复读/ });
    await waitFor(() => expect(toggle).toBeEnabled());
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    const save = apiVersionedMock.mock.calls.find(([, , options]) => options?.method === "POST");
    expect(save?.[2]?.ifMatch).toBe('"1"');
    expect(save?.[2]?.idempotencyKey).toMatch(/^agent-console:/);
    expect(save?.[2]?.body).toMatchObject({ enabled: true, cooldown_seconds: 300 });
    expect(await screen.findByText("版本 \"2\"")).toBeInTheDocument();
  });

  it("retains the draft when another operator wins the version race", async () => {
    const user = userEvent.setup();
    apiVersionedMock.mockImplementation(async (_config, _path, options) => {
      if (options?.method === "POST") {
        throw new VersionConflictError("409 version_conflict", {}, '"2"');
      }
      return {
        value: {
          tenant_id: "default",
          session_id: "room@chatroom",
          enabled: false,
          cooldown_seconds: 300,
          version: 1,
        },
        etag: '"1"',
      };
    });
    render(<RepeaterPage />);

    const cooldown = await screen.findByRole("spinbutton", { name: "相同内容冷却秒数" });
    await waitFor(() => expect(cooldown).toBeEnabled());
    await user.clear(cooldown);
    await user.type(cooldown, "600");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    expect(await screen.findByText("版本冲突")).toBeInTheDocument();
    expect(cooldown).toHaveValue(600);
  });
});

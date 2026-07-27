import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ConsoleConfigProvider,
  GroupSelectionRequiredError,
  requireSelectedGroup,
  useConsoleConfig,
} from "./console-config";

function ConfigHarness() {
  const { config, updateConfig } = useConsoleConfig();
  return (
    <div>
      <output data-testid="session">{config.sessionId}</output>
      <output data-testid="token">{config.adminToken}</output>
      <button
        type="button"
        onClick={() =>
          updateConfig({
            adminToken: "should-not-persist",
          })
        }
      >
        set sensitive values
      </button>
    </div>
  );
}

describe("console config persistence", () => {
  it("starts without a demo session and purges legacy browser secrets", async () => {
    window.localStorage.setItem(
      "agent-console-frontend-config",
      JSON.stringify({
        tenantId: "default",
        sessionId: "rogue@chatroom",
        adminToken: "legacy-token",
        tenantSecret: "legacy-secret",
      }),
    );

    render(
      <ConsoleConfigProvider>
        <ConfigHarness />
      </ConsoleConfigProvider>,
    );

    expect(screen.getByTestId("session")).toHaveTextContent("");
    expect(screen.getByTestId("token")).toHaveTextContent("");

    fireEvent.click(screen.getByRole("button", { name: "set sensitive values" }));
    expect(screen.getByTestId("token")).toHaveTextContent("should-not-persist");

    await waitFor(() => {
      const persisted = window.localStorage.getItem("agent-console-frontend-config") || "";
      expect(persisted).not.toContain("should-not-persist");
      expect(persisted).not.toContain("legacy-token");
      expect(persisted).not.toContain("legacy-secret");
      expect(persisted).not.toContain("rogue@chatroom");
      expect(persisted).not.toContain("default");
    });
  });

  it("requires a selected group to be present in the authenticated roster", () => {
    const verified = new Set(["verified@chatroom"]);

    expect(() => requireSelectedGroup({ sessionId: "" }, verified)).toThrow(
      GroupSelectionRequiredError,
    );
    expect(() =>
      requireSelectedGroup({ sessionId: "typed-by-hand@chatroom" }, verified),
    ).toThrowError("未通过后端会话列表验证");
    expect(
      requireSelectedGroup({ sessionId: " verified@chatroom " }, verified),
    ).toBe("verified@chatroom");
  });
});

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import { useWxbotQueueAdmin } from "./useWxbotQueueAdmin";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, apiRequest: vi.fn() };
});

const apiRequestMock = vi.mocked(apiRequest);

describe("useWxbotQueueAdmin reconciliation idempotency", () => {
  beforeEach(() => apiRequestMock.mockReset());

  it("reuses the key after failure and clears it only after mutation plus refresh succeed", async () => {
    let reconcileAttempts = 0;
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (typeof path === "string" && path.endsWith("/reconcile")) {
        reconcileAttempts += 1;
        if (reconcileAttempts === 1) throw new Error("network interrupted");
        return { id: 7, status: "sent" };
      }
      return { items: [] };
    });
    const keyFor = vi.fn(() => "stable-reconcile-key");
    const clearIdempotencyKey = vi.fn();
    const setActionOutput = vi.fn();
    const config = {
      apiBaseUrl: "http://localhost",
      adminToken: "token",
      tenantId: "default",
      sessionId: "room@chatroom",
      userId: "",
    } as ConsoleConfig;
    const { result } = renderHook(() => useWxbotQueueAdmin({
      clearIdempotencyKey,
      config,
      effectiveGroupSessionId: "room@chatroom",
      keyFor,
      setActionOutput,
    }));

    await waitFor(() => expect(apiRequestMock).toHaveBeenCalled());
    await act(async () => {
      await expect(result.current.reconcileSdkQueueMessage(7, "confirm_sent")).rejects.toThrow("network interrupted");
    });
    expect(clearIdempotencyKey).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.reconcileSdkQueueMessage(7, "confirm_sent");
    });

    const reconcileCalls = apiRequestMock.mock.calls.filter(([, path]) => (
      typeof path === "string" && path.endsWith("/reconcile")
    ));
    expect(reconcileCalls).toHaveLength(2);
    for (const call of reconcileCalls) {
      expect(call[2]?.init?.headers).toMatchObject({ "Idempotency-Key": "stable-reconcile-key" });
    }
    expect(keyFor).toHaveBeenNthCalledWith(1, "wxbot:sdk-queue-reconcile:7:confirm_sent");
    expect(keyFor).toHaveBeenNthCalledWith(2, "wxbot:sdk-queue-reconcile:7:confirm_sent");
    expect(clearIdempotencyKey).toHaveBeenCalledWith("wxbot:sdk-queue-reconcile:7:confirm_sent");
  });
});

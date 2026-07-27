import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GroupGraphResponse } from "../../lib/api";
import { useRelationshipGraphController } from "./useRelationshipGraphController";

const apiMocks = vi.hoisted(() => ({
  getGroupGraph: vi.fn(),
  getGroupGraphHistoryDates: vi.fn(),
  getGroupGraphWindowStats: vi.fn(),
  getMemoryExtractionJobStats: vi.fn(),
}));

const consoleHarness = vi.hoisted(() => ({
  value: {
    config: {
      apiBaseUrl: "http://localhost",
      adminToken: "",
      tenantId: "default",
      sessionId: "group-a@chatroom",
      userId: "",
    },
    verifiedGroupIds: new Set(["group-a@chatroom", "group-b@chatroom"]),
  },
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    getGroupGraph: apiMocks.getGroupGraph,
    getGroupGraphHistoryDates: apiMocks.getGroupGraphHistoryDates,
    getGroupGraphWindowStats: apiMocks.getGroupGraphWindowStats,
    getMemoryExtractionJobStats: apiMocks.getMemoryExtractionJobStats,
  };
});

vi.mock("../../state/console-config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../state/console-config")>();
  return {
    ...actual,
    useConsoleConfig: () => consoleHarness.value,
  };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function graphFor(groupId: string): GroupGraphResponse {
  return {
    scope: { session_id: groupId },
    nodes: [{ id: `person:${groupId}`, type: "person", label: groupId }],
    edges: [],
  };
}

describe("useRelationshipGraphController verified group loading", () => {
  beforeEach(() => {
    consoleHarness.value.config = {
      apiBaseUrl: "http://localhost",
      adminToken: "",
      tenantId: "default",
      sessionId: "group-a@chatroom",
      userId: "",
    };
    apiMocks.getGroupGraph.mockReset();
    apiMocks.getGroupGraphHistoryDates.mockReset().mockResolvedValue({ items: [] });
    apiMocks.getGroupGraphWindowStats.mockReset().mockResolvedValue({ totals: {} });
    apiMocks.getMemoryExtractionJobStats.mockReset().mockResolvedValue({ counts: {} });
  });

  it("auto-loads a switched group and ignores the previous group's late response", async () => {
    const groupARequest = deferred<GroupGraphResponse>();
    const groupBRequest = deferred<GroupGraphResponse>();
    apiMocks.getGroupGraph.mockImplementation((_, query) => {
      return query.session_id === "group-a@chatroom" ? groupARequest.promise : groupBRequest.promise;
    });

    const { result, rerender } = renderHook(() => useRelationshipGraphController());

    await waitFor(() => {
      expect(apiMocks.getGroupGraph).toHaveBeenCalledWith(
        expect.anything(),
        expect.objectContaining({ session_id: "group-a@chatroom" }),
      );
    });
    expect(result.current.graph).toBeNull();

    act(() => {
      consoleHarness.value.config = {
        ...consoleHarness.value.config,
        sessionId: "group-b@chatroom",
      };
      rerender();
    });

    await waitFor(() => {
      expect(apiMocks.getGroupGraph).toHaveBeenCalledWith(
        expect.anything(),
        expect.objectContaining({ session_id: "group-b@chatroom" }),
      );
    });
    expect(result.current.graph).toBeNull();

    const groupBGraph = graphFor("group-b@chatroom");
    await act(async () => {
      groupBRequest.resolve(groupBGraph);
      await groupBRequest.promise;
    });
    await waitFor(() => expect(result.current.graph).toEqual(groupBGraph));

    await act(async () => {
      groupARequest.resolve(graphFor("group-a@chatroom"));
      await groupARequest.promise;
    });
    expect(result.current.graph).toEqual(groupBGraph);
    expect(result.current.loading).toBe(false);
  });
});

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GroupGraphResponse } from "../../lib/api";
import { useRelationshipGraphController } from "./useRelationshipGraphController";

const apiMocks = vi.hoisted(() => ({
  apiRequest: vi.fn(),
  getGroupGraph: vi.fn(),
  getGroupGraphEdgeEvidence: vi.fn(),
  getGroupGraphHistoryDates: vi.fn(),
  getGroupGraphWindowStats: vi.fn(),
  getMemoryExtractionJobStats: vi.fn(),
  reviewGroupGraphEdge: vi.fn(),
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
    apiRequest: apiMocks.apiRequest,
    getGroupGraph: apiMocks.getGroupGraph,
    getGroupGraphEdgeEvidence: apiMocks.getGroupGraphEdgeEvidence,
    getGroupGraphHistoryDates: apiMocks.getGroupGraphHistoryDates,
    getGroupGraphWindowStats: apiMocks.getGroupGraphWindowStats,
    getMemoryExtractionJobStats: apiMocks.getMemoryExtractionJobStats,
    reviewGroupGraphEdge: apiMocks.reviewGroupGraphEdge,
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
    apiMocks.apiRequest.mockReset().mockResolvedValue({ ok: true, jobs: {}, counts: {} });
    apiMocks.getGroupGraph.mockReset();
    apiMocks.getGroupGraphEdgeEvidence.mockReset().mockResolvedValue({ edge: { id: "edge-1" } });
    apiMocks.getGroupGraphHistoryDates.mockReset().mockResolvedValue({ items: [] });
    apiMocks.getGroupGraphWindowStats.mockReset().mockResolvedValue({ totals: {} });
    apiMocks.getMemoryExtractionJobStats.mockReset().mockResolvedValue({ counts: {} });
    apiMocks.reviewGroupGraphEdge.mockReset().mockResolvedValue({ ok: true, action: "accept", edge_id: "edge-1" });
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

  it("reviews the selected edge and reloads the graph", async () => {
    const edge = {
      id: "edge-1",
      from: "person:a",
      to: "person:b",
      type: "replied_to",
      acceptance_status: "needs_review",
    };
    const graph = {
      scope: { session_id: "group-a@chatroom" },
      nodes: [
        { id: "person:a", type: "person", label: "甲" },
        { id: "person:b", type: "person", label: "乙" },
      ],
      edges: [edge],
    };
    apiMocks.getGroupGraph.mockResolvedValue(graph);

    const { result } = renderHook(() => useRelationshipGraphController());
    await waitFor(() => expect(result.current.graph).toEqual(graph));

    act(() => {
      result.current.setSelection({ kind: "edge", item: edge });
    });
    await act(async () => {
      await result.current.reviewEdge("accept");
    });

    expect(apiMocks.reviewGroupGraphEdge).toHaveBeenCalledWith(
      expect.anything(),
      "edge-1",
      expect.objectContaining({
        tenant_id: "default",
        channel: "wechat",
        source_key: "wxbot",
        session_id: "group-a@chatroom",
        action: "accept",
      }),
    );
    expect(apiMocks.getGroupGraph.mock.calls.length).toBeGreaterThan(1);
  });

  it("keeps the selected edge after a graph reload and can review a queued edge", async () => {
    const edge = {
      id: "edge-1",
      from: "person:a",
      to: "person:b",
      type: "replied_to",
      acceptance_status: "needs_review",
    };
    const nextEdge = { ...edge, evidence_count: 4 };
    const graph = {
      scope: { session_id: "group-a@chatroom" },
      nodes: [
        { id: "person:a", type: "person", label: "甲" },
        { id: "person:b", type: "person", label: "乙" },
      ],
      edges: [edge],
    };
    apiMocks.getGroupGraph.mockResolvedValue(graph);

    const { result } = renderHook(() => useRelationshipGraphController());
    await waitFor(() => expect(result.current.graph).toEqual(graph));

    act(() => {
      result.current.setSelection({ kind: "edge", item: edge });
    });

    apiMocks.getGroupGraph.mockResolvedValue({ ...graph, edges: [nextEdge] });
    await act(async () => {
      await result.current.loadGraph();
    });
    expect(result.current.selectedEdge).toEqual(nextEdge);

    await act(async () => {
      await result.current.reviewEdge("expire", nextEdge);
    });
    expect(apiMocks.reviewGroupGraphEdge).toHaveBeenCalledWith(
      expect.anything(),
      "edge-1",
      expect.objectContaining({ action: "expire" }),
    );
  });

  it("loads pending review and recent range filters", async () => {
    apiMocks.getGroupGraph.mockResolvedValue({
      scope: { session_id: "group-a@chatroom" },
      nodes: [],
      edges: [],
    });
    const { result } = renderHook(() => useRelationshipGraphController());
    await waitFor(() => expect(result.current.graph).not.toBeNull());

    await act(async () => {
      await result.current.showPendingReview();
    });
    expect(apiMocks.getGroupGraph).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ acceptance_status: "needs_review,candidate" }),
    );

    await act(async () => {
      await result.current.applyGraphRangeDays(14);
    });
    expect(result.current.graphRangeDays).toBe(14);
    expect(apiMocks.getGroupGraph).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ from: expect.any(String) }),
    );

    await act(async () => {
      await result.current.applyPlaybackDate("2026-08-20");
    });
    expect(result.current.playbackDate).toBe("2026-08-20");
    expect(apiMocks.getGroupGraph).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ from: "2026-08-20", to: "2026-08-21" }),
    );
  });

  it("uses admin auth for pending queries and keeps their endpoint nodes", async () => {
    const pendingEdge = {
      id: "edge-pending",
      from: "person:review-a",
      to: "topic:review-b",
      type: "replied_to",
      acceptance_status: "needs_review",
    };
    apiMocks.getGroupGraph.mockImplementation((_config, query) => {
      if (query.acceptance_status === "needs_review,candidate") {
        return Promise.resolve({
          nodes: [
            { id: "person:review-a", type: "person", label: "待审成员甲" },
            { id: "topic:review-b", type: "topic", label: "待审话题乙" },
          ],
          edges: [pendingEdge],
        });
      }
      return Promise.resolve(graphFor("group-a@chatroom"));
    });

    const { result } = renderHook(() => useRelationshipGraphController());

    await waitFor(() => expect(result.current.pendingEdges).toEqual([pendingEdge]));
    expect(apiMocks.getGroupGraph).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ acceptance_status: "needs_review,candidate" }),
    );
    expect(result.current.nodesById.get("person:review-a")?.label).toBe("待审成员甲");
    expect(result.current.nodesById.get("topic:review-b")?.label).toBe("待审话题乙");
  });

  it("exposes pending-query and review failures without claiming review idempotency", async () => {
    const edge = {
      id: "edge-1",
      from: "person:a",
      to: "person:b",
      type: "replied_to",
      acceptance_status: "needs_review",
    };
    apiMocks.getGroupGraph.mockImplementation((_config, query) => (
      query.acceptance_status === "needs_review,candidate"
        ? Promise.reject(new Error("admin session required"))
        : Promise.resolve({
            nodes: [
              { id: "person:a", type: "person", label: "甲" },
              { id: "person:b", type: "person", label: "乙" },
            ],
            edges: [edge],
          })
    ));
    apiMocks.reviewGroupGraphEdge.mockRejectedValue(new Error("review conflict"));

    const { result } = renderHook(() => useRelationshipGraphController());
    await waitFor(() => expect(result.current.pendingReviewError).toContain("admin session required"));

    act(() => {
      result.current.setSelection({ kind: "edge", item: edge });
    });
    await act(async () => {
      await expect(result.current.reviewEdge("accept")).rejects.toThrow("review conflict");
    });

    expect(result.current.reviewError).toContain("review conflict");
    expect(apiMocks.reviewGroupGraphEdge).toHaveBeenCalledWith(
      expect.anything(),
      "edge-1",
      expect.objectContaining({ action: "accept" }),
    );
    expect(apiMocks.reviewGroupGraphEdge.mock.calls[0]).toHaveLength(3);
  });

  it("calls extraction endpoints without attaching frontend-only idempotency options", async () => {
    apiMocks.getGroupGraph.mockResolvedValue(graphFor("group-a@chatroom"));
    const { result } = renderHook(() => useRelationshipGraphController());
    await waitFor(() => expect(result.current.graph).not.toBeNull());

    await act(async () => {
      await result.current.runDailyExtraction();
    });

    expect(apiMocks.apiRequest).toHaveBeenCalledWith(
      expect.anything(),
      "/plugins/memory/group-graph/extract-daily",
      expect.objectContaining({ auth: true }),
    );
    const extractionCall = apiMocks.apiRequest.mock.calls.find(([, path]) => (
      path === "/plugins/memory/group-graph/extract-daily"
    ));
    expect(new Headers(extractionCall?.[2]?.init?.headers).has("Idempotency-Key")).toBe(false);
  });
});

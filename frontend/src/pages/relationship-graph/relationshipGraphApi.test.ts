import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ConsoleConfig } from "../../state/console-config";
import {
  getGroupGraph,
  getGroupGraphEdgeEvidence,
  getGroupGraphHistoryDates,
  reviewGroupGraphEdge,
} from "../../lib/api";

const config: ConsoleConfig = {
  apiBaseUrl: "http://console.test",
  adminToken: "admin-token",
  tenantId: "default",
  sessionId: "group-a@chatroom",
  userId: "",
};

describe("relationship graph API contracts", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => Promise.resolve(
        new Response(JSON.stringify({ nodes: [], edges: [], items: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )),
    );
  });

  it("uses management authentication for graph, pending, evidence and history queries", async () => {
    await getGroupGraph(config, {
      tenant_id: "default",
      session_id: "group-a@chatroom",
    });
    await getGroupGraph(config, {
      tenant_id: "default",
      session_id: "group-a@chatroom",
      acceptance_status: "needs_review,candidate",
    });
    await getGroupGraphEdgeEvidence(config, "edge/1", {
      tenant_id: "default",
      session_id: "group-a@chatroom",
    });
    await getGroupGraphHistoryDates(config, {
      tenant_id: "default",
      session_id: "group-a@chatroom",
    });

    for (const [, init] of vi.mocked(fetch).mock.calls) {
      expect(init).toEqual(expect.objectContaining({ credentials: "include" }));
      expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer admin-token");
    }
  });

  it("does not send a fake idempotency key for non-idempotent edge review", async () => {
    await reviewGroupGraphEdge(config, "edge-1", {
      tenant_id: "default",
      session_id: "group-a@chatroom",
      action: "accept",
    });

    const [, init] = vi.mocked(fetch).mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(headers.get("Authorization")).toBe("Bearer admin-token");
    expect(headers.has("Idempotency-Key")).toBe(false);
  });
});

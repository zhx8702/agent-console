import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { JevPanel } from "./JevPanel";

const mocks = vi.hoisted(() => ({ request: vi.fn(), config: { tenantId: "demo" } }));
vi.mock("../../lib/api", () => ({ apiRequest: mocks.request }));
vi.mock("../../state/console-config", () => ({ useConsoleConfig: () => ({ config: mocks.config }) }));
const policy = { enabled: true, shadow_only: true, min_confidence: .8, sample_rate: 1, relationship: true, memory: true, intent: true, moderation: true, participation: true, participation_shadow_only: true, help_sessions: [] };
const dashboard = { policy, version: 1, runtime: { enabled: true, key_configured: true, model: "jev-latest" }, summary: [], items: [] };
beforeEach(() => mocks.request.mockReset());

describe("Jev tenant policy", () => {
  it("saves version and idempotency key, preserving drafts on failure", async () => {
    mocks.request.mockResolvedValueOnce(dashboard).mockRejectedValueOnce(new Error("409 version conflict"));
    render(<JevPanel />);
    await screen.findByText(/服务器开关/);
    fireEvent.change(screen.getByLabelText("评估模式"), { target: { value: "active" } });
    expect(screen.getByRole("button", { name: "重新加载" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "保存策略" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("评估模式")).toHaveValue("active");
    const options = mocks.request.mock.calls[1][2];
    expect(options.query.tenant_id).toBe("demo");
    expect(JSON.parse(options.init.body)).toEqual({ version: 1, policy: { ...policy, shadow_only: false } });
    expect(options.init.headers["Idempotency-Key"]).toBeTruthy();
    mocks.request.mockResolvedValueOnce({ policy: { ...policy, shadow_only: false }, version: 2 });
    fireEvent.click(screen.getByRole("button", { name: "保存策略" }));
    await screen.findByRole("status");
    expect(mocks.request.mock.calls[2][2].init.headers["Idempotency-Key"]).toBe(options.init.headers["Idempotency-Key"]);
  });
  it("retries asynchronous intent evaluations within the current tenant", async () => {
    mocks.request.mockResolvedValueOnce({ ...dashboard, items: [{ id: "job", domain: "intent", status: "failed", retryable: true, target_id: null, duration_ms: 20, attempts: 3, error_type: "TimeoutError" }] }).mockResolvedValueOnce({}).mockResolvedValueOnce(dashboard);
    render(<JevPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "重新评估" }));
    await waitFor(() => expect(mocks.request).toHaveBeenCalledTimes(3));
    expect(mocks.request.mock.calls[1][1]).toBe("/v1/admin/jev/jobs/job/retry");
    expect(mocks.request.mock.calls[1][2].query.tenant_id).toBe("demo");
  });
});

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
  it("explains a low-confidence reply and can filter its session", async () => {
    mocks.request.mockResolvedValue({ ...dashboard, items: [{ id: "help", session_id: "room@chatroom", domain: "participation", status: "completed", applied: false, duration_ms: 108, attempts: 1, result: { answers: { decision: { choice: "reply", confidence: .61 } }, _audit: { reason: "low_confidence", min_confidence: .8, trace_id: "tr-help" } } }] });
    render(<JevPanel />);
    expect(await screen.findAllByText("建议回复")).not.toHaveLength(0);
    expect(screen.getAllByText("置信度未达到阈值").length).toBeGreaterThan(0);
    expect(screen.getByText("阈值 0.80")).toBeInTheDocument();
    expect(screen.getByText("tr-help")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("评估会话"), { target: { value: "room@chatroom" } });
    await waitFor(() => expect(mocks.request.mock.lastCall?.[2].query.session_id).toBe("room@chatroom"));
  });
  it("shows help-desk conflicts, funnel, and the original message for a blocked reply", async () => {
    mocks.request.mockResolvedValue({
      ...dashboard,
      desk: { session_id: "room@chatroom", in_help_sessions: true, participation_enabled: true, participation_shadow_only: false, min_confidence: .8, channel: { reply_mode: "contains", configured_reply_mode: "contains", inherits_global_keywords: false, keyword_count: 46, mention_sender: false, has_session_row: true }, conflicts: [] },
      funnel: { evaluated: 4, lanes: { observe: 2, reply_blocked: 1, reply_applied: 1, failed: 0, pending: 0, other: 0 }, blocked_reasons: { low_confidence: 1 } },
      items: [{ id: "help", session_id: "room@chatroom", domain: "participation", status: "completed", applied: false, duration_ms: 80, attempts: 1, result: { answers: { decision: { choice: "reply", confidence: .71 } }, _audit: { reason: "low_confidence", min_confidence: .8, trace_id: "tr-312" } }, turn: { lane: "reply_blocked", message: "一直 312 要换节点吗", keyword_hit: true, outcome: "jev_blocked" } }],
    });
    render(<JevPanel />);
    expect(await screen.findByText(/已在主动答疑白名单/)).toBeInTheDocument();
    expect(screen.getByText(/关键词 46 个/)).toBeInTheDocument();
    expect(screen.getByText(/建议回复未应用 1/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "建议回复但未应用" })).toHaveTextContent("一直 312 要换节点吗");
    expect(screen.getAllByText("关键词会命中").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Jev 建议回复但未应用").length).toBeGreaterThan(0);
  });
  it("flags a help group that is still in observe mode or missing keywords", async () => {
    mocks.request.mockResolvedValue({
      ...dashboard,
      desk: { session_id: "room@chatroom", in_help_sessions: true, participation_enabled: true, participation_shadow_only: true, min_confidence: .8, channel: { reply_mode: "contains", configured_reply_mode: "contains", inherits_global_keywords: true, keyword_count: 0, mention_sender: false, has_session_row: true }, conflicts: ["jev_observe_only", "contains_without_keywords"] },
      funnel: { evaluated: 0, lanes: { observe: 0, reply_blocked: 0, reply_applied: 0, failed: 0, pending: 0, other: 0 }, blocked_reasons: {} },
    });
    render(<JevPanel />);
    expect(await screen.findByText("求助判断仍是观察模式，只记录建议，不会主动开口")).toBeInTheDocument();
    expect(screen.getByText("回复模式是包含关键词，但当前没有关键词")).toBeInTheDocument();
  });
  it("shows the effective memory disposition separately from the recommendation", async () => {
    mocks.request.mockResolvedValueOnce({ ...dashboard, items: [{ id: "memory", domain: "memory", status: "completed", applied: true, duration_ms: 10, attempts: 1, result: { answers: { decision: { choice: "accepted", confidence: .95 } }, _audit: { reason: "evidence_review_required", effective_decision: "needs_review" } } }] });
    render(<JevPanel />);
    expect(await screen.findByText("实际处理：待人工审核")).toBeInTheDocument();
    expect(screen.getByText("证据或敏感性检查要求人工审核")).toBeInTheDocument();
  });
});

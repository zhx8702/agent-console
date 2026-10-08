import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { JevKnowledgePanel } from "./JevKnowledgePanel";
const mocks = vi.hoisted(() => ({ request: vi.fn(), config: { tenantId: "demo" } }));
vi.mock("../../lib/api", () => ({ apiRequest: mocks.request }));
vi.mock("../../state/console-config", () => ({ useConsoleConfig: () => ({ config: mocks.config }) }));
const item = { id: "candidate", session_id: "group@chatroom", status: "ready", version: 2, reason: "supported_resolved_experience", error_type: "", kb_doc_id: null, review: { answers: { decision: { confidence: .95 } } }, comparisons: [], draft: { title: "修复证书错误", question: "连接失败", environment: "版本 2", solution: "更新证书", outcome: "恢复正常", evidence_ids: [1, 2], resolution_ids: [2] } };
beforeEach(() => mocks.request.mockReset());
describe("daily knowledge review", () => {
  it("loads only on request and scopes jobs and evidence", async () => {
    mocks.request.mockResolvedValueOnce({ jobs: [], candidates: [item] }).mockResolvedValueOnce({ messages: [{ id: 2, speaker: "member_1", text: "更新后恢复正常", occurred_ts: 10 }] });
    render(<JevKnowledgePanel sessionId="group@chatroom" disabled={false} />);
    expect(mocks.request).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("加载知识任务与候选"));
    await screen.findByText(/修复证书错误/);
    expect(mocks.request.mock.calls[0][2].query).toEqual({ tenant_id: "demo", session_id: "group@chatroom" });
    fireEvent.click(screen.getByText("查看原始证据（脱敏）"));
    await screen.findByText("更新后恢复正常");
    expect(mocks.request.mock.calls[1][1]).toBe("/v1/admin/jev/knowledge/candidates/candidate/evidence");
  });
  it("requires review reason and sends versioned, idempotent publication", async () => {
    mocks.request.mockResolvedValueOnce({ jobs: [], candidates: [item] }).mockResolvedValueOnce({ status: "published" }).mockResolvedValueOnce({ jobs: [], candidates: [{ ...item, status: "published", kb_doc_id: 7 }] });
    render(<JevKnowledgePanel sessionId="" disabled={false} />);
    fireEvent.click(screen.getByText("加载知识任务与候选"));
    const publish = await screen.findByText("发布到本群知识库");
    expect(publish).toBeDisabled();
    fireEvent.change(screen.getByLabelText("审核说明 candidate"), { target: { value: "已核对原文" } });
    fireEvent.click(publish);
    fireEvent.click(screen.getByText("确认执行"));
    await screen.findByText("已发布为知识文档 #7");
    const options = mocks.request.mock.calls[1][2];
    expect(JSON.parse(options.init.body)).toEqual({ version: 2, action: "publish", reason: "已核对原文" });
    expect(options.init.headers["Idempotency-Key"]).toBeTruthy();
  });
  it("never offers publication for unresolved or conflicting knowledge", async () => {
    mocks.request.mockResolvedValueOnce({ jobs: [], candidates: [{ ...item, status: "unresolved", reason: "awaiting_resolution" }] });
    render(<JevKnowledgePanel sessionId="" disabled={false} />);
    fireEvent.click(screen.getByText("加载知识任务与候选"));
    await screen.findByText(/等待解决结果/);
    expect(screen.queryByText("发布到本群知识库")).not.toBeInTheDocument();
  });
  it("discards a stale response after switching groups", async () => {
    let resolve!: (data: unknown) => void;
    mocks.request.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
    const view = render(<JevKnowledgePanel sessionId="group@chatroom" disabled={false} />);
    fireEvent.click(screen.getByText("加载知识任务与候选"));
    view.rerender(<JevKnowledgePanel sessionId="other@chatroom" disabled={false} />);
    resolve({ jobs: [], candidates: [item] });
    await waitFor(() => expect(screen.getByText("加载知识任务与候选")).toBeEnabled());
    expect(screen.queryByText(/修复证书错误/)).not.toBeInTheDocument();
  });
});

it("loads older candidates without dropping visible records and applies status filters", async () => {
  mocks.request.mockResolvedValueOnce({ jobs: [], candidates: [item], pagination: { candidates: "cursor-one" } })
    .mockResolvedValueOnce({ jobs: [], candidates: [{ ...item, id: "older", draft: { ...item.draft, title: "历史证书方案" } }], pagination: {} })
    .mockResolvedValueOnce({ jobs: [], candidates: [] });
  render(<JevKnowledgePanel sessionId="group@chatroom" disabled={false} />);
  fireEvent.click(screen.getByText("加载知识任务与候选"));
  fireEvent.click(await screen.findByText("加载更多知识候选"));
  await screen.findByText(/历史证书方案/);
  expect(screen.getByText(/修复证书错误/)).toBeInTheDocument();
  expect(mocks.request.mock.calls[1][2].query.candidate_cursor).toBe("cursor-one");
  fireEvent.change(screen.getByLabelText("知识候选状态"), { target: { value: "needs_review" } });
  expect(screen.queryByText(/历史证书方案/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("加载知识任务与候选"));
  await waitFor(() => expect(mocks.request).toHaveBeenCalledTimes(3));
  expect(mocks.request.mock.calls[2][2].query).toEqual({ tenant_id: "demo", session_id: "group@chatroom", candidate_status: "needs_review" });
});

it("requires viewed evidence before confirming a quality finding", async () => {
  const finding = { id: "finding", status: "needs_review", reason: "low_confidence", finding: { title: "可能漏答", explanation: "待核验", kind: "missed_help", evidence_ids: [2], trace_ids: ["trace"] } };
  mocks.request.mockResolvedValueOnce({ jobs: [], candidates: [], findings: [finding] })
    .mockResolvedValueOnce({ messages: [{ id: 2, speaker: "member_1", text: "问题原文" }], evidence_hash: "current-evidence", runtime: [{ message_id: 2, processing_status: "permanent_failure", processing_reason: "provider_error", decisions: [], deliveries: [] }] })
    .mockResolvedValueOnce({ status: "confirmed" })
    .mockResolvedValueOnce({ jobs: [], candidates: [], findings: [{ ...finding, status: "confirmed", review: { _operator: { actor: "admin", reason: "核对失败记录" } } }] });
  render(<JevKnowledgePanel sessionId="g" disabled={false} />);
  fireEvent.click(screen.getByText("加载知识任务与候选"));
  const confirm = await screen.findByText("确认复盘结论");
  fireEvent.change(screen.getByLabelText("复盘处理说明 finding"), { target: { value: "核对失败记录" } });
  expect(confirm).toBeDisabled();
  fireEvent.click(screen.getByText("查看复盘证据"));
  await screen.findByText("问题原文");
  fireEvent.click(confirm);
  fireEvent.click(screen.getByText("确认执行"));
  await screen.findByText(/人工处理：admin/);
  expect(JSON.parse(mocks.request.mock.calls[2][2].init.body)).toEqual({ action: "confirm", expected_status: "needs_review", reason: "核对失败记录", evidence_hash: "current-evidence" });
  expect(screen.queryByText("确认复盘结论")).not.toBeInTheDocument();
});

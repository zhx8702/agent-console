import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../../lib/api";
import { MessageStoryPage } from "./MessageStoryPage";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, apiRequest: vi.fn() };
});

const consoleConfig = {
  apiBaseUrl: "http://localhost",
  adminToken: "test-admin-token",
  tenantId: "default",
  sessionId: "room@chatroom",
  userId: "operator",
};

vi.mock("../../state/console-config", () => ({
  useConsoleConfig: () => ({
    config: consoleConfig,
  }),
}));

const apiRequestMock = vi.mocked(apiRequest);

function renderStory(traceId = "trace-1") {
  return render(
    <MemoryRouter
      initialEntries={[`/queues/traces/${traceId}`]}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <Routes>
        <Route path="/queues/traces/:traceId" element={<MessageStoryPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("MessageStoryPage", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
  });

  it("shows inbound text, decision, reply, and delivery instead of flow jargon", async () => {
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/streams/recent-messages") {
        return {
          items: [{
            id: "in-1",
            session_id: "room@chatroom",
            channel: "wechat",
            created_ts_ms: 1_700_000_000_000,
            payload: {
              message: { content: "flutter 调 ui 怎么调都怪" },
              metadata: { sender_name: "张三", session_name: "收购腾讯讨论组" },
            },
          }],
        };
      }
      if (path === "/plugins/wxbot/admin/reply-queue/messages") {
        return {
          items: [{
            id: 9,
            session_id: "room@chatroom",
            reply_text: "那倒是，RN 先让 AI 铺一版。",
            status: "sent",
          }],
        };
      }
      if (path.includes("/participation-events")) {
        return {
          items: [{
            event_id: "evt-1",
            tenant_id: "default",
            session_id: "room@chatroom",
            policy_version: 3,
            event_kind: "runtime",
            status: "may_reply",
            score: 60,
            reason_codes: ["explicit_question_to_bot"],
            signal_summary: {},
            trace_id: "trace-1",
            created_at: "2026-09-04T08:32:00Z",
          }],
          next_before: null,
        };
      }
      return { items: [] };
    });

    renderStory();

    expect(await screen.findByRole("heading", { name: "这条消息已经发出去了" })).toBeInTheDocument();
    expect(screen.getAllByText("flutter 调 ui 怎么调都怪").length).toBeGreaterThan(0);
    expect(screen.getAllByText("那倒是，RN 先让 AI 铺一版。").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/明确向机器人提问/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("已发送").length).toBeGreaterThan(0);
    expect(screen.queryByText(/FlowRunner/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看 Flow / Effect 细节" })).toHaveAttribute(
      "href",
      "/plugins?trace_id=trace-1",
    );
    expect(apiRequestMock.mock.calls.some(([, path, options]) => (
      String(path).includes("/participation-events")
      && options?.query?.trace_id === "trace-1"
    ))).toBe(true);
  });
});

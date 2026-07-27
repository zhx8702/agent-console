import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../lib/api";
import { MessageQueuesPage } from "./MessageQueuesPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, apiRequest: vi.fn() };
});

vi.mock("../components/AuthenticatedImage", () => ({
  AuthenticatedImage: ({
    source,
    alt,
    className,
  }: {
    source: string;
    alt: string;
    className: string;
  }) => <img src={source} alt={alt} className={className} data-media-source={source} />,
  sdkImageProxyPath: (source: string) =>
    source.startsWith("media:") ? `/plugins/wxbot/admin/images/${source.slice(6)}` : "",
  sdkImageDisplayPath: (source: string) =>
    source.startsWith("media:") ? "受保护媒体" : source,
}));

vi.mock("../state/console-config", () => ({
  useConsoleConfig: () => ({
    config: {
      apiBaseUrl: "http://localhost",
      adminToken: "test-admin-token",
      tenantId: "default",
      sessionId: "",
      userId: "operator",
    },
  }),
}));

const apiRequestMock = vi.mocked(apiRequest);

function message(id: string, content: string, createdTsMs = 1_700_000_000_000) {
  return {
    id,
    stream_key: "inbound",
    stream: "bus:inbound",
    tenant_id: "default",
    session_id: "group-1@chatroom",
    trace_id: `trace-${id}`,
    channel: "wechat",
    attempts: 0,
    created_ts_ms: createdTsMs,
    payload: { content },
    headers: {},
  };
}

function summary() {
  return {
    streams: [
      {
        stream_key: "inbound",
        stream: "bus:inbound",
        length: 2,
        pending_total: 0,
        groups: [],
      },
    ],
  };
}

function renderPage() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <MessageQueuesPage />
    </MemoryRouter>,
  );
}

function recentCalls() {
  return apiRequestMock.mock.calls.filter(([, path]) => path === "/v1/admin/streams/recent-messages");
}

describe("MessageQueuesPage live browser", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("refreshes the latest page without reusing the next-page cursor", async () => {
    let recentRequest = 0;
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/streams/summary") {
        return summary();
      }
      if (path === "/v1/admin/streams/recent-messages") {
        recentRequest += 1;
        return recentRequest === 1
          ? { items: [message("200-0", "原有消息")], next_before_id: "200-0" }
          : {
              items: [
                message("201-0", "刚刚到达的新消息", 1_700_000_001_000),
                message("200-0", "原有消息"),
              ],
              next_before_id: "200-0",
            };
      }
      return {};
    });

    const user = userEvent.setup();
    renderPage();

    expect(await screen.findAllByText("原有消息")).not.toHaveLength(0);
    expect(screen.queryByText(/查看第\s*\d+\s*条/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "立即刷新" }));

    expect(await screen.findAllByText("刚刚到达的新消息")).not.toHaveLength(0);
    const calls = recentCalls();
    expect(calls).toHaveLength(2);
    expect(calls[0][2]?.query).toMatchObject({ before_id: "" });
    expect(calls[1][2]?.query).toMatchObject({ before_id: "" });
    expect(screen.getByRole("checkbox", { name: "自动刷新" })).toBeChecked();
  });

  it("uses the pagination cursor only for loading older messages", async () => {
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      if (path === "/v1/admin/streams/summary") {
        return summary();
      }
      if (path === "/v1/admin/streams/recent-messages") {
        return options?.query?.before_id
          ? { items: [message("100-0", "更早的消息")], next_before_id: null }
          : { items: [message("200-0", "最新页消息")], next_before_id: "200-0" };
      }
      return {};
    });

    const user = userEvent.setup();
    renderPage();
    expect(await screen.findAllByText("最新页消息")).not.toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "加载更早消息" }));

    expect(await screen.findAllByText("更早的消息")).not.toHaveLength(0);
    expect(recentCalls()[1][2]?.query).toMatchObject({ before_id: "200-0" });
    expect(screen.getByText("正在浏览历史消息，自动刷新已暂停")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "自动刷新" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "回到最新" })).toBeInTheDocument();
  });

  it("shows the newest detail immediately and switches detail from the whole message card", async () => {
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/streams/summary") {
        return summary();
      }
      if (path === "/v1/admin/streams/recent-messages") {
        return {
          items: [
            message("300-0", "第一条自动展示", 1_700_000_003_000),
            message("299-0", "第二条无需点查看按钮", 1_700_000_002_000),
          ],
          next_before_id: "299-0",
        };
      }
      return {};
    });

    const user = userEvent.setup();
    renderPage();

    const inspector = await screen.findByRole("complementary");
    expect(within(inspector).getByText("第一条自动展示")).toBeInTheDocument();
    const secondCard = screen.getByRole("button", { name: /选择消息 2：第二条无需点查看按钮/ });
    await user.click(secondCard);
    expect(within(inspector).getByText("第二条无需点查看按钮")).toBeInTheDocument();
    expect(secondCard).toHaveAttribute("aria-pressed", "true");
  });

  it("shows pending image placeholders with the sender and conversation", async () => {
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/streams/summary") {
        return summary();
      }
      if (path === "/v1/admin/streams/recent-messages") {
        return {
          items: [
            {
              ...message("image-1", ""),
              payload: {
                message: { type: "text", content: "", attachments: [] },
                metadata: {
                  sender_name: "用户A",
                  sender_wxid: "wxid_user_a",
                  session_name: "测试群",
                  media_status: "pending",
                  media: { type: "image", status: "pending" },
                },
              },
            },
          ],
          next_before_id: "image-1",
        };
      }
      return {};
    });

    renderPage();

    expect((await screen.findAllByText("[图片]")).length).toBeGreaterThan(0);
    expect(screen.getByText("发送人 用户A")).toBeInTheDocument();
    expect(screen.getByText("会话 测试群")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /选择消息 1：\[图片\]，发送人 用户A/ }),
    ).toBeInTheDocument();
    const inspector = screen.getByRole("complementary");
    expect(within(inspector).getByText("用户A")).toBeInTheDocument();
    expect(within(inspector).getByText("测试群")).toBeInTheDocument();
  });

  it("renders ready images and the quoted original message through signed media IDs", async () => {
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/streams/summary") {
        return summary();
      }
      if (path === "/v1/admin/streams/recent-messages") {
        return {
          items: [
            {
              ...message("image-ready-1", ""),
              payload: {
                message: {
                  type: "text",
                  content: "[图片]",
                  attachments: [
                    { type: "image", media_id: "mid1.ZGlyZWN0.c2ln" },
                  ],
                },
                metadata: {
                  sender_name: "用户A",
                  session_name: "测试群",
                  quote_text: "这是被引用的原消息",
                  quote_media_id: "mid1.cXVvdGU.c2ln",
                  quote: {
                    sender_name: "用户B",
                    message_id: "quoted-message-1",
                  },
                },
              },
            },
          ],
          next_before_id: "image-ready-1",
        };
      }
      return {};
    });

    renderPage();

    const inspector = await screen.findByRole("complementary");
    expect(screen.getAllByText("这是被引用的原消息").length).toBeGreaterThan(0);
    expect(within(inspector).getByText("引用的原消息")).toBeInTheDocument();
    expect(within(inspector).getByText("用户B")).toBeInTheDocument();
    expect(within(inspector).getByText("原消息标识 quoted-message-1")).toBeInTheDocument();
    expect(within(inspector).getByAltText("消息图片预览")).toHaveAttribute(
      "data-media-source",
      "media:mid1.ZGlyZWN0.c2ln",
    );
    expect(within(inspector).getByAltText("引用原图预览")).toHaveAttribute(
      "data-media-source",
      "media:mid1.cXVvdGU.c2ln",
    );
  });

  it("polls the latest page every five seconds while the page is visible", async () => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    const intervalSpy = vi.spyOn(window, "setInterval");
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/v1/admin/streams/summary") {
        return summary();
      }
      if (path === "/v1/admin/streams/recent-messages") {
        return { items: [message("200-0", "轮询消息")], next_before_id: "200-0" };
      }
      return {};
    });

    renderPage();
    await waitFor(() => expect(recentCalls()).toHaveLength(1));
    const intervalCall = intervalSpy.mock.calls.find(([, delay]) => delay === 5_000);
    expect(intervalCall).toBeDefined();

    await act(async () => {
      (intervalCall?.[0] as () => void)();
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => expect(recentCalls()).toHaveLength(2));
    expect(recentCalls()[1][2]?.query).toMatchObject({ before_id: "" });
  });
});

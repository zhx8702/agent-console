import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../lib/api";
import { KnowledgePage } from "./KnowledgePage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, apiRequest: vi.fn() };
});

vi.mock("../state/console-config", () => {
  const config = {
    apiBaseUrl: "http://localhost",
    adminToken: "test-admin-token",
    tenantId: "default",
    sessionId: "room@chatroom",
    userId: "operator",
  };
  const verifiedGroupIds = new Set(["room@chatroom"]);
  const registerVerifiedGroups = vi.fn();
  const selectVerifiedGroup = vi.fn();
  return {
    useConsoleConfig: () => ({
      config,
      verifiedGroupIds,
      registerVerifiedGroups,
      selectVerifiedGroup,
    }),
  };
});

const apiRequestMock = vi.mocked(apiRequest);
const faqItem = {
  id: 7,
  tenant_id: "default",
  scope: "global" as const,
  question: "退款怎么操作",
  answer: "在订单页申请退款",
  variants: ["怎么退款"],
  tags: ["售后"],
  status: "published",
};
const documentItem = {
  id: 9,
  tenant_id: "default",
  scope: "global" as const,
  title: "售后政策",
  source: "manual",
  content: "七天内可以申请退款",
  metadata: {},
};

function methodOf(options: unknown) {
  return (options as { init?: { method?: string } } | undefined)?.init?.method || "GET";
}

function idempotencyKeyOf(options: unknown) {
  const headers = (options as { init?: { headers?: HeadersInit } } | undefined)?.init?.headers;
  return new Headers(headers).get("Idempotency-Key");
}

let failNextFaqDelete = false;

describe("KnowledgePage task-domain composition", () => {
  beforeEach(() => {
    failNextFaqDelete = false;
    apiRequestMock.mockReset();
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      const method = methodOf(options);
      if (path.endsWith("/admin/roster/groups")) {
        return { sessions: [
          { session_id: "room@chatroom", session_name: "产品讨论群", kind: "group" },
          { session_id: "wxid_private", session_name: "未验证私聊", kind: "private" },
        ] };
      }
      if (path === "/v1/admin/faqs" && method === "GET") {
        return { scope: "global", items: [faqItem] };
      }
      if (path === "/v1/admin/faqs" && method === "POST") {
        return { ...faqItem, id: 10 };
      }
      if (path === "/v1/admin/kb/documents" && method === "GET") {
        return { scope: "global", items: [documentItem] };
      }
      if (path === `/v1/admin/kb/documents/${documentItem.id}` && method === "GET") {
        return documentItem;
      }
      if (method === "DELETE") {
        if (failNextFaqDelete && path.endsWith("/faqs/7")) {
          failNextFaqDelete = false;
          throw new Error("response lost");
        }
        return { ok: true };
      }
      return {};
    });
  });

  it("keeps the FAQ, document, and chat-import task domains reachable from the page shell", async () => {
    const user = userEvent.setup();
    render(<KnowledgePage />);

    expect(await screen.findByRole("heading", { name: "常见问答列表" })).toBeInTheDocument();
    expect(screen.getByText("退款怎么操作")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "知识文档" }));
    expect(screen.getByRole("heading", { name: "知识文档列表" })).toBeInTheDocument();
    expect(screen.getByText("售后政策")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "聊天转 FAQ" }));
    expect(screen.getByRole("heading", { name: "聊天记录来源" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "常见问答草稿确认区" })).toBeInTheDocument();
  });

  it("offers only authenticated roster groups and has no free-form session id scope", async () => {
    const user = userEvent.setup();
    render(<KnowledgePage />);
    await screen.findByRole("heading", { name: "常见问答列表" });

    const scopeSelect = screen.getByLabelText("作用域");
    expect(within(scopeSelect).queryByRole("option", { name: "手动填写会话 ID" })).not.toBeInTheDocument();
    await user.selectOptions(scopeSelect, "session");
    const groupPicker = screen.getByRole("combobox", { name: "目标群 / 会话" });
    await user.click(groupPicker);
    expect(screen.getByRole("option", { name: /产品讨论群/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /未验证私聊/ })).not.toBeInTheDocument();
  });

  it("does not delete an FAQ before its impact dialog is confirmed", async () => {
    const user = userEvent.setup();
    render(<KnowledgePage />);
    await screen.findByText("退款怎么操作");

    await user.click(screen.getByRole("button", { name: "删除 FAQ" }));
    const dialog = screen.getByRole("dialog", { name: "删除 FAQ" });
    expect(within(dialog).getByText(/FAQ #7 将从当前知识范围中永久删除/)).toBeInTheDocument();
    expect(apiRequestMock.mock.calls.some(([, path, options]) => path.endsWith("/faqs/7") && methodOf(options) === "DELETE")).toBe(false);

    await user.click(within(dialog).getByRole("button", { name: "确认执行" }));
    await waitFor(() => expect(apiRequestMock.mock.calls.some(
      ([, path, options]) => path.endsWith("/faqs/7") && methodOf(options) === "DELETE",
    )).toBe(true));
    const deleteCall = apiRequestMock.mock.calls.find(
      ([, path, options]) => path.endsWith("/faqs/7") && methodOf(options) === "DELETE",
    );
    expect(idempotencyKeyOf(deleteCall?.[2])).toMatch(/^agent-console:/);
  });

  it("does not delete a document before its impact dialog is confirmed", async () => {
    const user = userEvent.setup();
    render(<KnowledgePage />);
    await user.click(await screen.findByRole("button", { name: "知识文档" }));
    await user.click(screen.getByText("售后政策"));

    await user.click(screen.getByRole("button", { name: "删除文档" }));
    const dialog = screen.getByRole("dialog", { name: "删除知识文档" });
    expect(within(dialog).getByText(/文档 #9 及其召回索引将被永久删除/)).toBeInTheDocument();
    expect(apiRequestMock.mock.calls.some(([, path, options]) => path.endsWith("/documents/9") && methodOf(options) === "DELETE")).toBe(false);

    await user.click(within(dialog).getByRole("button", { name: "确认执行" }));
    await waitFor(() => expect(apiRequestMock.mock.calls.some(
      ([, path, options]) => path.endsWith("/documents/9") && methodOf(options) === "DELETE",
    )).toBe(true));
    const deleteCall = apiRequestMock.mock.calls.find(
      ([, path, options]) => path.endsWith("/documents/9") && methodOf(options) === "DELETE",
    );
    expect(idempotencyKeyOf(deleteCall?.[2])).toMatch(/^agent-console:/);
  });

  it("reuses the FAQ delete key when the response is lost", async () => {
    const user = userEvent.setup();
    render(<KnowledgePage />);
    await screen.findByText("退款怎么操作");
    failNextFaqDelete = true;

    await user.click(screen.getByRole("button", { name: "删除 FAQ" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "删除 FAQ" })).getByRole(
        "button",
        { name: "确认执行" },
      ),
    );
    await waitFor(() => expect(apiRequestMock.mock.calls.filter(
      ([, path, options]) => path.endsWith("/faqs/7") && methodOf(options) === "DELETE",
    )).toHaveLength(1));

    await user.click(screen.getByRole("button", { name: "删除 FAQ" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "删除 FAQ" })).getByRole(
        "button",
        { name: "确认执行" },
      ),
    );
    await waitFor(() => expect(apiRequestMock.mock.calls.filter(
      ([, path, options]) => path.endsWith("/faqs/7") && methodOf(options) === "DELETE",
    )).toHaveLength(2));

    const deleteCalls = apiRequestMock.mock.calls.filter(
      ([, path, options]) => path.endsWith("/faqs/7") && methodOf(options) === "DELETE",
    );
    expect(idempotencyKeyOf(deleteCalls[0]?.[2])).toBe(idempotencyKeyOf(deleteCalls[1]?.[2]));
  });

  it("keeps extracted chat FAQ drafts local until batch-import confirmation", async () => {
    const user = userEvent.setup();
    render(<KnowledgePage />);
    await user.click(await screen.findByRole("button", { name: "聊天转 FAQ" }));
    await user.type(screen.getByLabelText("原始聊天记录"), "张三：退款怎么操作？\n李四：在订单页申请退款");
    await user.click(screen.getByRole("button", { name: "生成 FAQ 草稿" }));

    expect(screen.getByDisplayValue("退款怎么操作")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认导入选中项" }));
    const dialog = screen.getByRole("dialog", { name: "批量导入 FAQ 草稿" });
    expect(within(dialog).getByText(/将创建 1 条 FAQ/)).toBeInTheDocument();
    expect(apiRequestMock.mock.calls.some(([, path, options]) => path === "/v1/admin/faqs" && methodOf(options) === "POST")).toBe(false);

    await user.click(within(dialog).getByRole("button", { name: "确认导入" }));
    await waitFor(() => expect(apiRequestMock.mock.calls.some(
      ([, path, options]) => path === "/v1/admin/faqs" && methodOf(options) === "POST",
    )).toBe(true));
  });
});

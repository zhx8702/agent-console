import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest } from "../lib/api";
import { ConsoleConfigProvider } from "../state/console-config";
import { createCapabilityResponse } from "../test/capability-fixtures";
import { OverviewPage } from "./OverviewPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
  };
});

const apiRequestMock = vi.mocked(apiRequest);

describe("OverviewPage degraded loading", () => {
  beforeEach(() => {
    window.localStorage.clear();
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/healthz") {
        return { status: "ok" };
      }
      if (path === "/readyz") {
        throw new ApiError(503, "503 dependencies unavailable", {
          status: "degraded",
          checks: {
            redis: { ok: false },
            db: { backend: "postgres" },
            qdrant: { backend: "qdrant" },
            knowledge_features: { enabled: true },
          },
        });
      }
      if (path === "/openapi.json") {
        return { paths: { "/plugins/wxbot/admin/sessions": {} } };
      }
      if (path === "/v1/admin/plugins/summary") {
        return {
          plugins: [],
          plugin_routes: [],
          hooks: {},
          channels: [],
          channel_labels: {},
        };
      }
      return {};
    });
  });

  function renderOverviewWithCapabilities(onRetryCapabilities = vi.fn()) {
    return {
      onRetryCapabilities,
      ...render(
        <ConsoleConfigProvider>
          <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <OverviewPage
              capabilityState={{
                status: "ready",
                data: createCapabilityResponse(),
                error: "",
              }}
              onRetryCapabilities={onRetryCapabilities}
            />
          </MemoryRouter>
        </ConsoleConfigProvider>,
      ),
    };
  }

  it("keeps successful health and plugin results when readiness fails", async () => {
    render(
      <ConsoleConfigProvider>
        <OverviewPage />
      </ConsoleConfigProvider>,
    );

    expect(
      await screen.findByText("控制面可访问，但部分检查处于降级状态"),
    ).toBeInTheDocument();
    const technicalDetails = screen.getByText("查看插件与适配器技术清单").closest("details");
    expect(technicalDetails).not.toHaveAttribute("open");
    const readinessTile = screen
      .getAllByText("启动就绪")
      .find((element) => element.closest("article"))
      ?.closest("article");
    expect(readinessTile).toHaveTextContent("不可用");
    expect(screen.getByText("关系数据库").closest("article")).toHaveTextContent("可用");
    expect(screen.getAllByText(/最后成功：/)).toHaveLength(3);
    expect(screen.getByText("尚无成功记录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新检查" })).toBeInTheDocument();
  });

  it("renders the server-owned launch sequence, blockers, recovery, and hidden capability diagnostics", async () => {
    renderOverviewWithCapabilities();

    const checklist = screen.getByRole("heading", { name: "从依赖检查到正式参与" }).closest("section");
    expect(checklist).not.toBeNull();
    const items = within(checklist as HTMLElement).getAllByRole("listitem", { hidden: false });
    const stepLabels = [
      "检查运行依赖",
      "配置 LLM",
      "添加平台连接",
      "同步会话",
      "设置参与策略",
      "发送测试消息",
      "正式上线",
    ];
    for (const label of stepLabels) {
      expect(within(checklist as HTMLElement).getByText(label, { selector: "strong" })).toBeInTheDocument();
    }
    const optionalTestStep = within(checklist as HTMLElement)
      .getByText("发送测试消息", { selector: "strong" })
      .closest("li");
    expect(optionalTestStep).not.toBeNull();
    expect(within(optionalTestStep as HTMLElement).getByText("可选")).toBeInTheDocument();
    expect(optionalTestStep).toHaveTextContent("已有正常收发记录时无需重复执行，也不影响上线");
    expect(items.length).toBeGreaterThanOrEqual(7);
    expect(within(checklist as HTMLElement).getByText("connection_required")).toBeInTheDocument();
    expect(within(checklist as HTMLElement).getByRole("link", { name: /添加平台连接/ })).toHaveAttribute(
      "href",
      "/channels",
    );

    const diagnostics = screen.getByText("能力与依赖诊断").closest("section");
    expect(diagnostics).not.toBeNull();
    expect(within(diagnostics as HTMLElement).getByRole("heading", { name: "消息平台连接" })).toBeInTheDocument();
    expect(within(diagnostics as HTMLElement).getByText("未启用 · 入口不可用")).toBeInTheDocument();

    expect(screen.queryByText("群聊与私聊的回复覆盖")).not.toBeInTheDocument();
  });

  it("dismisses and restores the first-login checklist without changing runtime state", async () => {
    const user = userEvent.setup();
    renderOverviewWithCapabilities();

    await user.click(screen.getByRole("button", { name: "稍后处理" }));

    expect(screen.getByText("上线清单已收起")).toBeInTheDocument();
    expect(window.localStorage.getItem("agent-console:launch-checklist:v1:default")).toBe("dismissed");

    await user.click(screen.getByRole("button", { name: "重新打开清单" }));
    expect(screen.getByRole("heading", { name: "从依赖检查到正式参与" })).toBeInTheDocument();
    expect(window.localStorage.getItem("agent-console:launch-checklist:v1:default")).toBeNull();
  });

  it("renders an explicit empty launch state instead of claiming zero of zero steps are ready", () => {
    const capabilities = createCapabilityResponse();
    capabilities.onboarding = { state: "ready", steps: [] };

    render(
      <ConsoleConfigProvider>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <OverviewPage capabilityState={{ status: "ready", data: capabilities, error: "" }} />
        </MemoryRouter>
      </ConsoleConfigProvider>,
    );

    const checklist = screen.getByRole("heading", { name: "从依赖检查到正式参与" }).closest("section");
    expect(checklist).not.toBeNull();
    const scope = within(checklist as HTMLElement);
    expect(scope.getByText("暂无上线步骤")).toBeInTheDocument();
    expect(scope.getByText(/这不表示所有能力已就绪/)).toBeInTheDocument();
    expect(scope.getByLabelText("上线步骤尚未生成")).toHaveTextContent("暂无步骤尚未生成");
    expect(scope.queryByText("0/0")).not.toBeInTheDocument();
    expect(scope.queryByRole("list")).not.toBeInTheDocument();
  });

  it("keeps the launch checklist and capability diagnostics free of automated accessibility violations", async () => {
    renderOverviewWithCapabilities();

    await waitFor(() =>
      expect(screen.getByText("控制面可访问，但部分检查处于降级状态")).toBeInTheDocument(),
    );
    const results = await axe.run(document.body, {
      rules: {
        "color-contrast": { enabled: false },
        region: { enabled: false },
      },
    });
    expect(results.violations).toEqual([]);
  });

  it("shows a scoped group workspace without requesting tenant-wide diagnostics", async () => {
    const groupCapabilities = createCapabilityResponse();
    groupCapabilities.access.scope = "group";
    groupCapabilities.onboarding = {
      state: "action_required",
      steps: [
        {
          id: "group_scope",
          label: "确认授权群聊",
          description: "只管理授权群聊",
          state: "ready",
          dependencies: [],
          recovery_actions: [],
        },
      ],
    };

    render(
      <ConsoleConfigProvider>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <OverviewPage
            accessScope="group"
            capabilityState={{ status: "ready", data: groupCapabilities, error: "" }}
          />
        </MemoryRouter>
      </ConsoleConfigProvider>,
    );

    expect(screen.getByRole("heading", { name: "从授权群到安全参与" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "授权群工作台" })).toBeInTheDocument();
    expect(screen.queryByText("后端运行概览")).not.toBeInTheDocument();
    expect(screen.queryByText("插件与适配器摘要")).not.toBeInTheDocument();
    expect(apiRequestMock).not.toHaveBeenCalled();
  });
});

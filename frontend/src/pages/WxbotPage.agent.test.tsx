import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest, apiVersionedResource } from "../lib/api";
import type { AgentToolPolicy } from "./wxbot/model";
import { WxbotPage } from "./WxbotPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
    apiVersionedResource: vi.fn(),
  };
});

vi.mock("../state/console-config", () => {
  const config = {
    apiBaseUrl: "http://localhost",
    adminToken: "test-admin-token",
    tenantId: "default",
    sessionId: "room@chatroom",
    userId: "",
  };
  const updateConfig = vi.fn();
  const registerVerifiedGroups = vi.fn();
  const selectVerifiedGroup = vi.fn();
  return {
    useConsoleConfig: () => ({
      config,
      updateConfig,
      registerVerifiedGroups,
      selectVerifiedGroup,
    }),
  };
});

const apiRequestMock = vi.mocked(apiRequest);
const apiVersionedMock = vi.mocked(apiVersionedResource);

const groupSession = {
  session_id: "room@chatroom",
  session_name: "产品讨论群",
  kind: "group",
};

const catalog = [
  { name: "get_group_info", owner: "wxbot", scope: "group_info" },
  { name: "search_group_messages", owner: "wxbot", scope: "group_info" },
];

function defaultPolicy(overrides: Partial<AgentToolPolicy> = {}): AgentToolPolicy {
  return {
    tenant_id: "default",
    session_id: groupSession.session_id,
    enabled: true,
    policy_configured: false,
    allowed_tools: [],
    available_tools: catalog.map((item) => item.name),
    effective_tools: catalog.map((item) => item.name),
    inherits_default_tools: true,
    denial_reason: "",
    scope: "group_info",
    ...overrides,
  };
}

function renderAgentPage() {
  render(
    <MemoryRouter
      initialEntries={["/wxbot?tab=agent"]}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <WxbotPage />
    </MemoryRouter>,
  );
}

describe("wxbot agent tool defaults", () => {
  let policy = defaultPolicy();

  beforeEach(() => {
    policy = defaultPolicy();
    apiRequestMock.mockReset();
    apiVersionedMock.mockReset();
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/plugins/wxbot/bridge/status") return { running: true, sdk_online: true };
      if (path.endsWith("/admin/sessions") || path.endsWith("/admin/roster/groups")) {
        return { sessions: [groupSession] };
      }
      if (path.endsWith("/agent-tools/catalog")) {
        return { items: catalog, scopes: ["group_info"] };
      }
      if (path.endsWith("/agent-tools/audit")) return { items: [], count: 0 };
      return {};
    });
    apiVersionedMock.mockImplementation(async (_config, path, options) => {
      if (path.includes("/agent-tools/policy/")) {
        if (options?.method === "POST") {
          policy = defaultPolicy({ ...(options.body as Partial<AgentToolPolicy>), policy_configured: true });
        }
        return { value: policy, etag: '"agent-1"' };
      }
      if (path.includes("/reply-policy/global/")) {
        return {
          value: {
            tenant_id: "default",
            private_reply_mode: "all",
            group_reply_mode: "contains",
            group_reply_mention_sender: false,
            trigger_keywords_text: "",
          },
          etag: '"global-1"',
        };
      }
      if (path.includes("/reply-policy/default/room%40chatroom")) {
        return {
          value: {
            tenant_id: "default",
            session_id: groupSession.session_id,
            reply_mode: "inherit",
            mention_sender_mode: "inherit",
            trigger_keywords_text: "",
          },
          etag: '"reply-1"',
        };
      }
      if (path.includes("/session-state/")) {
        return { value: { state: "chatting", auto_reply_enabled: true }, etag: '"state-1"' };
      }
      if (path.includes("/participation-policy")) {
        return {
          value: {
            tenant_id: "default",
            session_id: groupSession.session_id,
            kill_switches: { global_enabled: true, tenant_enabled: true, group_enabled: true },
            effective_enabled: true,
            policy: {},
          },
          etag: '"participation-1"',
        };
      }
      throw new Error(`unexpected versioned request: ${path}`);
    });
  });

  it("shows an unconfigured group as enabled with all default tools", async () => {
    renderAgentPage();

    expect(await screen.findByText("系统默认")).toBeInTheDocument();
    expect(screen.getByText("继承默认全部工具")).toBeInTheDocument();
    expect(screen.getByLabelText("默认模式")).toHaveValue("全部工具");
    expect(screen.getByRole("checkbox", { name: /get_group_info/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /search_group_messages/ })).toBeChecked();

    const effectiveTools = screen.getByText("有效工具").closest(".summary-card");
    expect(effectiveTools).not.toBeNull();
    expect(within(effectiveTools as HTMLElement).getByText("2")).toBeInTheDocument();
  });

  it("shows an explicitly disabled group with zero effective tools", async () => {
    policy = defaultPolicy({
      enabled: false,
      policy_configured: true,
      effective_tools: [],
      denial_reason: "policy_disabled",
    });
    renderAgentPage();

    expect(await screen.findByText("当前群明确配置")).toBeInTheDocument();
    expect(screen.getAllByText("已停用").length).toBeGreaterThan(0);
    const effectiveTools = screen.getByText("有效工具").closest(".summary-card");
    expect(effectiveTools).not.toBeNull();
    expect(within(effectiveTools as HTMLElement).getByText("0")).toBeInTheDocument();
  });

  it("restores the inherited default tool set with an empty allowlist sentinel", async () => {
    const user = userEvent.setup();
    policy = defaultPolicy({
      enabled: false,
      policy_configured: true,
      effective_tools: [],
      denial_reason: "policy_disabled",
    });
    renderAgentPage();

    await screen.findByText("当前群明确配置");
    await user.click(screen.getByRole("button", { name: "恢复默认全部工具" }));
    await user.click(screen.getByRole("button", { name: "保存智能体策略" }));
    await user.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => {
      const saveCall = apiVersionedMock.mock.calls.find(([, path, options]) => (
        path.includes("/agent-tools/policy/") && options?.method === "POST"
      ));
      expect(saveCall?.[2]?.body).toEqual({ enabled: true, allowed_tools: [] });
    });
  });

  it("labels file scopes explicitly and points to the group file master switch", async () => {
    const user = userEvent.setup();
    const baseRequest = apiRequestMock.getMockImplementation();
    expect(baseRequest).toBeDefined();
    apiRequestMock.mockImplementation(async (...args) => {
      const [, path] = args;
      if (path.endsWith("/agent-tools/catalog")) {
        return {
          items: catalog,
          scopes: ["group_info", "file_analysis", "message_export"],
        };
      }
      return baseRequest!(...args);
    });
    renderAgentPage();

    await screen.findByText("系统默认");
    await user.selectOptions(screen.getByLabelText("工具作用域"), "file_analysis");

    expect(await screen.findByRole("heading", { name: "群文件处理智能体" })).toBeInTheDocument();
    expect(screen.getByText(/允许群文件发送.*总开关约束/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("工具作用域"), "message_export");
    expect(
      await screen.findByRole("heading", { name: "群消息文件导出智能体" }),
    ).toBeInTheDocument();
  });
});

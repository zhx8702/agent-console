import axe from "axe-core";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createChannelConnection,
  deleteChannelConnection,
  disableChannelConnection,
  enableChannelConnection,
  getChannelAdapters,
  getChannelConnection,
  getChannelConnections,
  normalizeChannelAdapter,
  normalizeChannelConnection,
  probeChannelConnection,
  updateChannelConnection,
  validateChannelConnection,
  type ChannelConnection,
  type ChannelConnectionActionResult,
} from "../../lib/channel-connections";
import { VersionConflictError } from "../../lib/api";
import { ConnectionsPage } from "./ConnectionsPage";
import { emptyConnectionDraft, validateConnectionDraft } from "./model";

vi.mock("../../state/console-config", () => ({
  useConsoleConfig: () => ({
    config: {
      apiBaseUrl: "http://agent-console.test",
      adminToken: "test-admin",
      tenantId: "tenant-a",
      sessionId: "",
      userId: "test-admin",
    },
  }),
}));

vi.mock("../../lib/channel-connections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/channel-connections")>();
  return {
    ...actual,
    getChannelAdapters: vi.fn(),
    getChannelConnections: vi.fn(),
    getChannelConnection: vi.fn(),
    createChannelConnection: vi.fn(),
    updateChannelConnection: vi.fn(),
    deleteChannelConnection: vi.fn(),
    probeChannelConnection: vi.fn(),
    enableChannelConnection: vi.fn(),
    disableChannelConnection: vi.fn(),
    validateChannelConnection: vi.fn(),
  };
});

const getAdaptersMock = vi.mocked(getChannelAdapters);
const getConnectionsMock = vi.mocked(getChannelConnections);
const getConnectionMock = vi.mocked(getChannelConnection);
const createConnectionMock = vi.mocked(createChannelConnection);
const updateConnectionMock = vi.mocked(updateChannelConnection);
const deleteConnectionMock = vi.mocked(deleteChannelConnection);
const probeConnectionMock = vi.mocked(probeChannelConnection);
const enableConnectionMock = vi.mocked(enableChannelConnection);
const disableConnectionMock = vi.mocked(disableChannelConnection);
const validateConnectionMock = vi.mocked(validateChannelConnection);

const adapters = [
  normalizeChannelAdapter({
    adapter_id: "wechat-sdk",
    display_name: "微信 SDK",
    description: "通过自托管 SDK 桥接微信消息。",
    plugin_name: "wxbot",
    version: "2.0.0",
    installed: true,
    enabled: true,
    available: true,
    supports_multiple_connections: true,
    capabilities: ["inbound_text", "outbound_text", "outbound_image", "health_probe"],
    runtime_modes: ["bridge_worker"],
    config_schema: {
      type: "object",
      additionalProperties: false,
      required: ["sdk_url"],
      properties: {
        sdk_url: { type: "string", format: "uri", title: "SDK / 网关地址" },
        media_base_url: { type: "string", format: "uri", title: "媒体访问基址" },
        poll_interval_seconds: {
          type: "number",
          title: "入站轮询间隔（秒）",
          default: 3,
          minimum: 0.1,
        },
        send_interval_seconds: {
          type: "number",
          title: "出站发送间隔（秒）",
          default: 2,
          minimum: 0.1,
        },
      },
    },
    ui_schema: { order: ["sdk_url", "media_base_url", "poll_interval_seconds", "send_interval_seconds"] },
    secret_fields: [],
  }),
  normalizeChannelAdapter({
    adapter_id: "feixin-sdk",
    display_name: "飞信 SDK",
    description: "飞信消息平台适配器。",
    plugin_name: "feixin",
    installed: true,
    enabled: true,
    available: true,
    supports_multiple_connections: false,
    capabilities: ["inbound_text"],
    runtime_modes: [],
    config_schema: {
      type: "object",
      additionalProperties: false,
      required: ["service_url", "tenant_code"],
      properties: {
        service_url: { type: "string", format: "uri", title: "飞信服务地址" },
        tenant_code: {
          type: "string",
          title: "企业标识",
          description: "输入 3–8 个字符的企业标识。",
          minLength: 3,
          maxLength: 8,
        },
        retry_limit: {
          type: "integer",
          title: "重试次数",
          default: 4,
          minimum: 1,
          maximum: 10,
        },
        routing_tag: {
          type: "string",
          title: "路由标签",
          description: "可选；留空时由服务端选择默认路由。",
        },
      },
    },
    ui_schema: { order: ["service_url", "tenant_code", "retry_limit", "routing_tag"] },
    secret_fields: [{
      name: "app_secret",
      label: "飞信应用密钥",
      required: true,
      accepted_ref_schemes: ["env", "vault"],
      environment_variable: "FEIXIN_APP_SECRET",
    }],
  }),
];

function connectionFixture(
  id: string,
  displayName: string,
  overrides: Record<string, unknown> = {},
) {
  return normalizeChannelConnection({
    tenant_id: "tenant-a",
    connection_id: id,
    adapter_id: "wechat-sdk",
    display_name: displayName,
    config_json: {
      sdk_url: `http://gateway.test/${id}`,
      poll_interval_seconds: 3,
      send_interval_seconds: 2,
    },
    secret_ref: "",
    secret_status: "not_required",
    required_for_launch: false,
    desired_state: "enabled",
    effective_state: "enabled",
    managed_by: "platform",
    version: 4,
    priority: 100,
    last_probed_at: new Date(Date.now() - 120_000).toISOString(),
    last_probe_status: "ready",
    last_error_code: "",
    created_at: "2026-07-18T01:00:00Z",
    updated_at: "2026-07-18T02:00:00Z",
    ...overrides,
  });
}

const wxPrimary = connectionFixture("wx-primary", "生产微信客服 A");
const wxSecondary = connectionFixture("wx-secondary", "生产微信客服 B", {
  last_probe_status: "degraded",
  last_error_code: "sdk_offline",
});
const wxDraft = connectionFixture("wx-draft", "微信营销号草稿", {
  desired_state: "draft",
  effective_state: "draft",
  last_probe_status: "valid",
});
const feixinDisabled = connectionFixture("feixin-disabled", "飞信通知机器人", {
  adapter_id: "feixin-sdk",
  config_json: {
    service_url: "https://feixin.example.test",
    tenant_code: "acme-cn",
    retry_limit: 4,
    obsolete_region: "north-1",
  },
  secret_ref: "env://FEIXIN_APP_SECRET",
  desired_state: "disabled",
  effective_state: "disabled",
  last_probed_at: null,
  last_probe_status: "",
});
const legacyEnvironment = connectionFixture("wx-legacy-env", "部署环境微信连接", {
  managed_by: "environment",
  config_source: "legacy_env",
  read_only: true,
  secret_ref: "env://LEGACY_WXBOT_TOKEN",
});
const defaultConnections = [wxPrimary, wxSecondary, wxDraft, feixinDisabled, legacyEnvironment];

const backendProbeResult = {
  ok: true,
  status: "ready",
  error_codes: [],
  connection: wxPrimary,
} as unknown as ChannelConnectionActionResult;

function renderPage(entry = "/channels") {
  return render(
    <MemoryRouter
      initialEntries={[entry]}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <main>
        <ConnectionsPage />
      </main>
    </MemoryRouter>,
  );
}

function connectionById(id: string): ChannelConnection {
  return defaultConnections.find((item) => item.id === id) || wxPrimary;
}

describe("ConnectionsPage", () => {
  beforeEach(() => {
    getAdaptersMock.mockResolvedValue({ items: adapters, readOnly: false });
    getConnectionsMock.mockResolvedValue({ items: defaultConnections, readOnly: false });
    getConnectionMock.mockImplementation(async (_config, connectionId) => ({
      value: connectionById(connectionId),
      etag: `\"${connectionById(connectionId).version}\"`,
    }));
    createConnectionMock.mockResolvedValue({ value: wxPrimary, etag: "\"1\"" });
    updateConnectionMock.mockResolvedValue({ value: wxPrimary, etag: "\"5\"" });
    deleteConnectionMock.mockResolvedValue({ value: {}, etag: null });
    probeConnectionMock.mockResolvedValue(backendProbeResult);
    enableConnectionMock.mockResolvedValue(backendProbeResult);
    disableConnectionMock.mockResolvedValue(backendProbeResult);
    validateConnectionMock.mockResolvedValue(backendProbeResult);
  });

  it("shows loading state, the four management stages, and multiple instances of one platform", async () => {
    let resolveConnections!: (value: { items: ChannelConnection[]; readOnly: boolean }) => void;
    getConnectionsMock.mockReturnValueOnce(new Promise((resolve) => {
      resolveConnections = resolve;
    }));

    renderPage();

    expect(screen.getByLabelText("正在加载连接列表")).toBeInTheDocument();
    const domainModel = screen.getByLabelText("消息平台连接的四个管理环节");
    expect(within(domainModel).getByText("消息平台")).toBeInTheDocument();
    expect(within(domainModel).getByText("连接实例")).toBeInTheDocument();
    expect(within(domainModel).getByText("配置校验")).toBeInTheDocument();
    expect(within(domainModel).getByText("主动探测")).toBeInTheDocument();

    await act(async () => {
      resolveConnections({ items: defaultConnections, readOnly: false });
    });

    const list = await screen.findByRole("list", { name: "消息平台连接实例" });
    expect(within(list).getByRole("button", { name: /生产微信客服 A/ })).toBeInTheDocument();
    expect(within(list).getByRole("button", { name: /生产微信客服 B/ })).toBeInTheDocument();
    expect(within(list).getAllByText("微信 SDK")).toHaveLength(4);
    const statusSummary = within(screen.getByLabelText("连接状态摘要"));
    expect(statusSummary.getByText("健康连接").nextElementSibling).toHaveTextContent("2");
    expect(statusSummary.getByText("待处理").nextElementSibling).toHaveTextContent("1");
    expect(statusSummary.getByText("配置草稿").nextElementSibling).toHaveTextContent("1");
    expect(statusSummary.getByText("已停用").nextElementSibling).toHaveTextContent("1");
  });

  it("supports platform and status filters without collapsing same-platform connections", async () => {
    const user = userEvent.setup();
    renderPage();

    const listPanel = (await screen.findByRole("heading", { name: "已配置连接" })).closest("section");
    expect(listPanel).not.toBeNull();
    const scope = within(listPanel as HTMLElement);
    await user.selectOptions(scope.getByLabelText("平台"), "wechat-sdk");

    expect(scope.getByRole("button", { name: /生产微信客服 A/ })).toBeInTheDocument();
    expect(scope.getByRole("button", { name: /生产微信客服 B/ })).toBeInTheDocument();
    expect(scope.queryByRole("button", { name: /飞信通知机器人/ })).not.toBeInTheDocument();

    await user.selectOptions(scope.getByLabelText("状态"), "draft");
    expect(scope.getByRole("button", { name: /微信营销号草稿/ })).toBeInTheDocument();
    expect(scope.queryByRole("button", { name: /生产微信客服 A/ })).not.toBeInTheDocument();
  });

  it("keeps a disable request in attention until the runtime actually stops", async () => {
    const stopping = connectionFixture("wx-stopping", "正在停用的微信连接", {
      desired_state: "disabled",
      effective_state: "enabled",
    });
    getConnectionsMock.mockResolvedValue({ items: [stopping], readOnly: false });
    getConnectionMock.mockResolvedValue({ value: stopping, etag: "\"4\"" });

    renderPage("/channels?connection=wx-stopping");

    expect(await screen.findByText("停用中")).toBeInTheDocument();
    const statusSummary = within(screen.getByLabelText("连接状态摘要"));
    expect(statusSummary.getByText("待处理").nextElementSibling).toHaveTextContent("1");
    expect(statusSummary.getByText("已停用").nextElementSibling).toHaveTextContent("0");
    expect(screen.getByRole("button", { name: "删除连接" })).toBeDisabled();
  });

  it("shows the backend ready lifecycle as awaiting runtime convergence, not already enabled", async () => {
    const awaitingRuntime = connectionFixture("wx-awaiting-runtime", "等待运行时接管", {
      desired_state: "enabled",
      effective_state: "ready",
    });
    getConnectionsMock.mockResolvedValue({ items: [awaitingRuntime], readOnly: false });
    getConnectionMock.mockResolvedValue({ value: awaitingRuntime, etag: "\"4\"" });

    renderPage("/channels?connection=wx-awaiting-runtime");

    const card = await screen.findByRole("button", { name: /等待运行时接管/ });
    expect(within(card).getByText("已就绪")).toBeInTheDocument();
    expect(within(card).getByText("已降级")).toBeInTheDocument();
    expect(within(card).queryByText("已启用")).not.toBeInTheDocument();
    const statusSummary = within(screen.getByLabelText("连接状态摘要"));
    expect(statusSummary.getByText("健康连接").nextElementSibling).toHaveTextContent("0");
    expect(statusSummary.getByText("待处理").nextElementSibling).toHaveTextContent("1");
  });

  it("keeps disabled plus ready or unverified consistent between list and detail", async () => {
    const user = userEvent.setup();
    const ready = connectionFixture("wx-disable-ready", "期望停用但实际就绪", {
      desired_state: "disabled",
      effective_state: "ready",
      last_probe_status: "valid",
    });
    const unverified = connectionFixture("wx-disable-unverified", "期望停用但尚未验证", {
      desired_state: "disabled",
      effective_state: "unverified",
      last_probed_at: null,
      last_probe_status: "",
    });
    const values = [ready, unverified];
    getConnectionsMock.mockResolvedValue({ items: values, readOnly: false });
    getConnectionMock.mockImplementation(async (_config, connectionId) => ({
      value: values.find((item) => item.id === connectionId) || ready,
      etag: "\"4\"",
    }));

    renderPage("/channels?connection=wx-disable-ready");

    const list = await screen.findByRole("list", { name: "消息平台连接实例" });
    const readyConnection = within(list).getByRole("button", { name: /期望停用但实际就绪/ });
    expect(within(readyConnection).getByText("待处理")).toBeInTheDocument();
    expect(within(readyConnection).getByText("已就绪")).toBeInTheDocument();
    expect(within(readyConnection).queryByText("已启用")).not.toBeInTheDocument();
    expect(within(within(list).getByRole("button", { name: /期望停用但尚未验证/ })).getByText("待处理")).toBeInTheDocument();
    const statusSummary = within(screen.getByLabelText("连接状态摘要"));
    expect(statusSummary.getByText("待处理").nextElementSibling).toHaveTextContent("2");
    expect(statusSummary.getByText("已停用").nextElementSibling).toHaveTextContent("0");

    let detail = (await screen.findByRole("heading", { name: "期望停用但实际就绪" })).closest("section");
    expect(detail).not.toBeNull();
    expect(within(detail!.querySelector(".connection-detail-platform-line") as HTMLElement).getByText("停用中")).toBeInTheDocument();

    await user.click(within(list).getByRole("button", { name: /期望停用但尚未验证/ }));
    detail = (await screen.findByRole("heading", { name: "期望停用但尚未验证" })).closest("section");
    expect(detail).not.toBeNull();
    expect(within(detail!.querySelector(".connection-detail-platform-line") as HTMLElement).getByText("停用中")).toBeInTheDocument();
  });

  it("renders only health and detail fields supported by the connection document", async () => {
    renderPage("/channels?connection=wx-primary");

    const detail = (await screen.findByRole("heading", { name: "生产微信客服 A" })).closest("section");
    expect(detail).not.toBeNull();
    const health = within(detail as HTMLElement).getByLabelText("连接分维度健康状态");
    expect(within(health).getByText("配置校验")).toBeInTheDocument();
    expect(within(health).queryByText("鉴权证据")).not.toBeInTheDocument();
    expect(within(health).getByText("运行证据")).toBeInTheDocument();
    expect(within(health).getByText("主动探测")).toBeInTheDocument();
    expect(within(detail as HTMLElement).getByText("平台凭据")).toBeInTheDocument();
    expect(within(detail as HTMLElement).getByText("无需配置")).toBeInTheDocument();
    expect(within(health).queryByText("入站")).not.toBeInTheDocument();
    expect(within(health).queryByText("出站")).not.toBeInTheDocument();
    expect(within(health).queryByText("同步")).not.toBeInTheDocument();
    expect(within(detail as HTMLElement).queryByText("平台身份")).not.toBeInTheDocument();
    expect(within(detail as HTMLElement).queryByText("已同步会话")).not.toBeInTheDocument();
    expect(within(detail as HTMLElement).queryByText("已同步参与者")).not.toBeInTheDocument();
    expect(within(detail as HTMLElement).queryByText("最近心跳")).not.toBeInTheDocument();
  });

  it("gates duplicate creation, probe actions, and connector guidance from descriptor metadata", async () => {
    const user = userEvent.setup();
    const feixinEnabled = connectionFixture("feixin-enabled", "飞信在线通知", {
      adapter_id: "feixin-sdk",
      config_json: {
        service_url: "https://feixin.example.test",
        tenant_code: "acme-cn",
        retry_limit: 4,
      },
      secret_ref: "env://FEIXIN_APP_SECRET",
      desired_state: "enabled",
      effective_state: "enabled",
      last_probe_status: "ready",
    });
    getConnectionsMock.mockResolvedValue({ items: [wxPrimary, feixinEnabled], readOnly: false });
    getConnectionMock.mockResolvedValue({ value: feixinEnabled, etag: "\"4\"" });

    renderPage("/channels?connection=feixin-enabled");

    const feixinCard = (await screen.findByText("飞信消息平台适配器。")).closest("article");
    const wechatCard = screen.getByText("通过自托管 SDK 桥接微信消息。").closest("article");
    expect(feixinCard).not.toBeNull();
    expect(wechatCard).not.toBeNull();
    expect(within(feixinCard as HTMLElement).getByRole("button", { name: "添加此平台连接" })).toBeDisabled();
    expect(within(wechatCard as HTMLElement).getByRole("button", { name: "添加此平台连接" })).toBeEnabled();

    const detail = (await screen.findByRole("heading", { name: "飞信在线通知" })).closest("section");
    expect(detail).not.toBeNull();
    expect(within(detail as HTMLElement).getByRole("button", { name: "测试连接" })).toBeDisabled();
    expect(within(detail as HTMLElement).queryByText(/启动对应连接器/)).not.toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "添加连接" })[0]);
    const dialog = screen.getByRole("dialog", { name: "添加消息平台连接" });
    expect(within(dialog).getByRole("option", { name: /飞信 SDK/ })).toBeDisabled();
    expect(within(dialog).getByRole("option", { name: "微信 SDK" })).toBeEnabled();
  });

  it("renders an actionable empty state and opens the create dialog", async () => {
    const user = userEvent.setup();
    getConnectionsMock.mockResolvedValue({ items: [], readOnly: false });

    renderPage("/channels?adapter=wechat-sdk");

    expect(await screen.findByRole("heading", { name: "还没有消息平台连接" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "添加第一个连接" }));
    const dialog = screen.getByRole("dialog", { name: "添加消息平台连接" });
    expect(dialog).toBeInTheDocument();
    expect(screen.getByLabelText("消息平台适配器")).toHaveValue("wechat-sdk");
    expect(screen.getByText(/当前平台不需要额外 Token 或配置文件路径/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox", { name: /启动必需项/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/启动必需项|requiredForLaunch|required_for_launch/)).not.toBeInTheDocument();
  });

  it("creates a tokenless WeChat connection from directly entered config", async () => {
    const user = userEvent.setup();
    const created = connectionFixture("wx-new", "新品发布群机器人", {
      version: 1,
      desired_state: "draft",
      effective_state: "draft",
    });
    getConnectionsMock.mockResolvedValue({ items: [], readOnly: false });
    createConnectionMock.mockResolvedValue({ value: created, etag: "\"1\"" });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "添加第一个连接" }));
    await user.selectOptions(screen.getByLabelText("消息平台适配器"), "wechat-sdk");
    await user.type(screen.getByLabelText("连接名称"), "新品发布群机器人");
    await user.type(screen.getByLabelText(/SDK \/ 网关地址/), "https://wx-gateway.example.test");
    expect(screen.queryByLabelText(/secret_ref/)).not.toBeInTheDocument();
    expect(screen.getByText(/当前平台不需要额外 Token/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "创建连接草稿" }));

    await waitFor(() => expect(createConnectionMock).toHaveBeenCalledTimes(1));
    expect(createConnectionMock.mock.calls[0][1]).toMatchObject({
      adapterId: "wechat-sdk",
      displayName: "新品发布群机器人",
      configValues: {
        sdk_url: "https://wx-gateway.example.test",
        poll_interval_seconds: 3,
        send_interval_seconds: 2,
      },
      secretRef: "",
      requiredForLaunch: false,
      desiredState: "draft",
    });
    expect(createConnectionMock.mock.calls[0][1].configValues).not.toHaveProperty("media_base_url");
    expect(screen.queryByRole("dialog", { name: "添加消息平台连接" })).not.toBeInTheDocument();
  });

  it("rejects URI query strings, fragments, and leading or trailing whitespace", async () => {
    const user = userEvent.setup();
    getConnectionsMock.mockResolvedValue({ items: [], readOnly: false });

    for (const invalid of [
      " https://gateway.example.test/path",
      "https://gateway.example.test/path ",
    ]) {
      const draft = emptyConnectionDraft("wechat-sdk", adapters[0]);
      draft.displayName = "严格地址校验";
      draft.configValues = { ...draft.configValues, sdk_url: invalid };
      expect(validateConnectionDraft(draft, adapters[0]).config?.sdk_url).toMatch(/空白/);
    }

    renderPage();
    await user.click(await screen.findByRole("button", { name: "添加第一个连接" }));
    await user.selectOptions(screen.getByLabelText("消息平台适配器"), "wechat-sdk");
    await user.type(screen.getByLabelText("连接名称"), "严格地址校验");
    const endpoint = screen.getByLabelText(/SDK \/ 网关地址/);

    for (const invalid of [
      "https://gateway.example.test/path?token=redacted",
      "https://gateway.example.test/path#section",
    ]) {
      fireEvent.change(endpoint, { target: { value: invalid } });
      expect(endpoint).toHaveValue(invalid);
      await user.click(screen.getByRole("button", { name: "创建连接草稿" }));
      await waitFor(() => expect(endpoint).toHaveAttribute("aria-invalid", "true"));
      expect(createConnectionMock).not.toHaveBeenCalled();
    }

    await user.clear(endpoint);
    await user.type(endpoint, "https://gateway.example.test/path");
    await user.click(screen.getByRole("button", { name: "创建连接草稿" }));
    await waitFor(() => expect(createConnectionMock).toHaveBeenCalledTimes(1));
  });

  it("creates a non-WeChat connection from the selected adapter schema", async () => {
    const user = userEvent.setup();
    const created = connectionFixture("feixin-new", "飞信生产通知", {
      adapter_id: "feixin-sdk",
      version: 1,
      desired_state: "draft",
      effective_state: "draft",
      config_json: {
        service_url: "https://feixin.example.test",
        tenant_code: "acme-cn",
        retry_limit: 4,
      },
    });
    getConnectionsMock.mockResolvedValue({ items: [], readOnly: false });
    createConnectionMock.mockResolvedValue({ value: created, etag: "\"1\"" });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "添加第一个连接" }));
    await user.selectOptions(screen.getByLabelText("消息平台适配器"), "feixin-sdk");
    await user.type(screen.getByLabelText("连接名称"), "飞信生产通知");
    await user.type(screen.getByLabelText(/飞信服务地址/), "https://feixin.example.test");
    await user.type(screen.getByLabelText(/企业标识/), "acme-cn");
    expect(screen.getByLabelText(/重试次数/)).toHaveValue(4);
    await user.type(screen.getByLabelText("飞信应用密钥（secret_ref）"), "env://FEIXIN_APP_SECRET");
    await user.click(screen.getByRole("button", { name: "创建连接草稿" }));

    await waitFor(() => expect(createConnectionMock).toHaveBeenCalledTimes(1));
    const draft = createConnectionMock.mock.calls[0][1];
    expect(draft).toMatchObject({
      adapterId: "feixin-sdk",
      displayName: "飞信生产通知",
      secretRef: "env://FEIXIN_APP_SECRET",
      requiredForLaunch: false,
      desiredState: "draft",
    });
    expect(draft.configValues).toEqual({
      service_url: "https://feixin.example.test",
      tenant_code: "acme-cn",
      retry_limit: 4,
    });
    expect(draft.configValues).not.toHaveProperty("endpoint_url");
    expect(draft.configValues).not.toHaveProperty("poll_interval_seconds");
    expect(draft.configValues).not.toHaveProperty("send_interval_seconds");
  });

  it("validates schema string lengths, wires field errors, and omits cleared optional values", async () => {
    const user = userEvent.setup();
    getConnectionsMock.mockResolvedValue({ items: [], readOnly: false });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "添加第一个连接" }));
    await user.selectOptions(screen.getByLabelText("消息平台适配器"), "feixin-sdk");
    await user.type(screen.getByLabelText("连接名称"), "飞信动态字段校验");
    await user.type(screen.getByLabelText(/飞信服务地址/), "https://feixin.example.test");
    await user.type(screen.getByLabelText("飞信应用密钥（secret_ref）"), "env://FEIXIN_APP_SECRET");
    const tenantCode = screen.getByLabelText(/企业标识/);
    const retryLimit = screen.getByLabelText(/重试次数/);
    const routingTag = screen.getByLabelText(/路由标签/);

    await user.type(tenantCode, "ab");
    await user.click(screen.getByRole("button", { name: "创建连接草稿" }));
    let tenantError = tenantCode.closest("label")?.querySelector<HTMLElement>(".connection-field-error");
    expect(tenantError).toBeTruthy();
    expect(tenantError).toHaveTextContent(/3/);
    expect(tenantError?.id).not.toBe("");
    expect(tenantCode.getAttribute("aria-describedby")?.split(/\s+/)).toContain(tenantError?.id);
    const tenantHelp = tenantCode.closest("label")?.querySelector<HTMLElement>("small:not(.connection-field-error)");
    expect(tenantHelp?.id).not.toBe("");
    expect(tenantCode.getAttribute("aria-describedby")?.split(/\s+/)).toContain(tenantHelp?.id);

    await user.clear(tenantCode);
    await user.type(tenantCode, "123456789");
    expect(tenantCode).toHaveValue("12345678");
    expect(tenantCode).toHaveAttribute("maxlength", "8");
    expect(createConnectionMock).not.toHaveBeenCalled();

    await user.clear(tenantCode);
    await user.type(tenantCode, "acme-cn");
    await user.clear(retryLimit);
    expect(retryLimit).toHaveValue(null);
    await user.type(routingTag, "   ");
    await user.click(screen.getByRole("button", { name: "创建连接草稿" }));

    await waitFor(() => expect(createConnectionMock).toHaveBeenCalledTimes(1));
    expect(createConnectionMock.mock.calls[0][1].configValues).toEqual({
      service_url: "https://feixin.example.test",
      tenant_code: "acme-cn",
      retry_limit: undefined,
      routing_tag: "   ",
    });
  });

  it("drops stale config keys that are no longer declared by the adapter", async () => {
    const user = userEvent.setup();
    renderPage("/channels?connection=feixin-disabled");

    await user.click(await screen.findByRole("button", { name: "编辑配置" }));
    const dialog = screen.getByRole("dialog", { name: /编辑连接/ });
    expect(within(dialog).queryByRole("checkbox", { name: /启动必需项/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/启动必需项|requiredForLaunch|required_for_launch/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存连接配置" }));

    await waitFor(() => expect(updateConnectionMock).toHaveBeenCalledTimes(1));
    expect(updateConnectionMock.mock.calls[0][2].configValues).toEqual({
      service_url: "https://feixin.example.test",
      tenant_code: "acme-cn",
      retry_limit: 4,
    });
    expect(updateConnectionMock.mock.calls[0][2].configValues).not.toHaveProperty("obsolete_region");
    expect(updateConnectionMock.mock.calls[0][2].requiredForLaunch).toBe(false);
  });

  it("keeps deployment-managed legacy connections safely read-only", async () => {
    renderPage("/channels?connection=wx-legacy-env");

    expect(await screen.findByText("部署环境托管 / 只读")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑配置" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "测试连接" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "校验配置" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "删除连接" })).not.toBeInTheDocument();
    expect(screen.getByText(/控制台只显示安全摘要/)).toBeInTheDocument();
  });

  it("recovers from a failed list request through an explicit retry", async () => {
    const user = userEvent.setup();
    getConnectionsMock
      .mockRejectedValueOnce(new Error("连接服务暂时不可用"))
      .mockResolvedValue({ items: defaultConnections, readOnly: false });

    renderPage();

    expect(await screen.findByText("连接列表加载失败")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("button", { name: /生产微信客服 A/ })).toBeInTheDocument();
    expect(getConnectionsMock).toHaveBeenCalledTimes(2);
  });

  it("preserves the local edit draft when the server reports an ETag conflict", async () => {
    const user = userEvent.setup();
    updateConnectionMock.mockRejectedValueOnce(
      new VersionConflictError("连接配置版本冲突", { detail: "etag_mismatch" }, "\"5\""),
    );
    renderPage("/channels?connection=wx-primary");

    await user.click(await screen.findByRole("button", { name: "编辑配置" }));
    const nameInput = screen.getByLabelText("连接名称");
    await user.clear(nameInput);
    await user.type(nameInput, "本地未保存的微信连接名称");
    await user.click(screen.getByRole("button", { name: "保存连接配置" }));

    expect(await screen.findByText("连接版本冲突")).toBeInTheDocument();
    expect(screen.getByDisplayValue("本地未保存的微信连接名称")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "放弃当前表单并重新读取" })).toBeInTheDocument();
  });

  it("has no detectable automated accessibility violations in the loaded workspace", async () => {
    renderPage("/channels?connection=wx-primary");
    await screen.findByRole("button", { name: "编辑配置" });

    const results = await axe.run(document.body, {
      rules: {
        "color-contrast": { enabled: false },
      },
    });

    expect(results.violations).toEqual([]);
  });
});

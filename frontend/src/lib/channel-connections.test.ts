import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createChannelConnection,
  getChannelAdapters,
  getChannelConnections,
  normalizeChannelAdapter,
  normalizeChannelAdapterCollection,
  normalizeChannelConnection,
  normalizeChannelConnectionCollection,
  probeChannelConnection,
  updateChannelConnection,
  type ChannelConnectionWrite,
} from "./channel-connections";
import { apiRequest, apiVersionedResource } from "./api";
import type { ConsoleConfig } from "../state/console-config";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
    apiVersionedResource: vi.fn(),
  };
});

const apiRequestMock = vi.mocked(apiRequest);
const apiVersionedResourceMock = vi.mocked(apiVersionedResource);

const config: ConsoleConfig = {
  apiBaseUrl: "http://agent-console.test",
  adminToken: "admin",
  tenantId: "tenant-a",
  sessionId: "",
  userId: "operator-a",
};

const write: ChannelConnectionWrite = {
  adapterId: "wechat-sdk",
  displayName: "生产微信连接",
  endpointUrl: "https://gateway.example.test",
  pollIntervalSeconds: 5,
  sendIntervalSeconds: 2,
  secretRef: "vault://messaging/wechat-prod",
  requiredForLaunch: false,
  desiredState: "draft",
};

const serverConnection = {
  tenant_id: "tenant-a",
  connection_id: "wechat-prod",
  adapter_id: "wechat-sdk",
  display_name: "生产微信连接",
  config_json: {
    sdk_url: "https://gateway.example.test",
    media_base_url: "https://media.example.test",
    poll_interval_seconds: 5,
    send_interval_seconds: 2,
  },
  secret_ref: "vault://messaging/wechat-prod",
  secret_status: "reference_configured",
  desired_state: "draft",
  effective_state: "draft",
  version: 1,
  priority: 100,
  required_for_launch: false,
  last_probed_at: null,
  last_probe_status: "",
  last_error_code: "",
  managed_by: "platform",
  read_only: false,
};

describe("channel connection API normalization", () => {
  beforeEach(() => {
    apiVersionedResourceMock.mockResolvedValue({ value: serverConnection, etag: "\"1\"" });
    apiRequestMock.mockResolvedValue({ items: [] });
  });

  it("accepts alternate adapter and collection field names", () => {
    const result = normalizeChannelAdapterCollection({
      platforms: [{
        platform_id: "wechat-sdk",
        label: "微信",
        plugin: "wxbot",
        installed: "true",
        enabled: 1,
        available: "ready",
        supported_capabilities: ["groups", "groups", "members"],
      }],
      mutable: false,
    });

    expect(result.readOnly).toBe(true);
    expect(result.items).toEqual([expect.objectContaining({
      id: "wechat-sdk",
      displayName: "微信",
      pluginName: "wxbot",
      available: true,
      capabilities: ["groups", "members"],
    })]);
  });

  it("preserves descriptor runtime modes and string length constraints", () => {
    const result = normalizeChannelAdapter({
      adapter_id: "mattermost-sdk",
      display_name: "Mattermost",
      runtime_modes: ["bridge_worker", "webhook"],
      capabilities: ["inbound_text", "health_probe"],
      supports_multiple_connections: true,
      config_schema: {
        type: "object",
        required: ["server_url", "team_slug"],
        properties: {
          server_url: { type: "string", format: "uri" },
          team_slug: { type: "string", minLength: 3, maxLength: 24 },
        },
      },
    });

    expect(result).toMatchObject({
      runtimeModes: ["bridge_worker", "webhook"],
      capabilities: ["inbound_text", "health_probe"],
      supportsMultipleConnections: true,
      configFields: {
        team_slug: { minLength: 3, maxLength: 24 },
      },
    });
  });

  it("normalizes the real connection document without inventing unsupported telemetry", () => {
    const lastProbedAt = new Date(Date.now() - 60_000).toISOString();
    const result = normalizeChannelConnection({
      tenant_id: "tenant-a",
      connection_id: "wechat-prod",
      adapter_id: "wechat-sdk",
      display_name: "生产微信",
      desired_state: "enabled",
      effective_state: "enabled",
      config_json: { sdk_url: "http://wxbot:5080" },
      secret_ref: "env://WXBOT_API_TOKEN",
      secret_status: "reference_configured",
      version: 7,
      priority: 100,
      required_for_launch: false,
      last_probed_at: lastProbedAt,
      last_probe_status: "ready",
      last_error_code: "",
      managed_by: "platform",
      read_only: false,
      created_at: "2026-07-18T01:00:00Z",
      updated_at: "2026-07-18T02:00:00Z",
    });

    expect(result).toMatchObject({
      id: "wechat-prod",
      adapterId: "wechat-sdk",
      displayName: "生产微信",
      managedBy: "platform",
      readOnly: false,
      config: { endpointUrl: "http://wxbot:5080" },
      health: {
        configured: "unknown",
        auth: "unknown",
        runtime: "ready",
        probe: "ready",
        lastProbeAt: lastProbedAt,
      },
    });
    expect(result).not.toHaveProperty("externalIdentity");
    expect(result).not.toHaveProperty("conversationCount");
    expect(result).not.toHaveProperty("participantCount");
    expect(result).not.toHaveProperty("capabilities");
    expect(result.health).not.toHaveProperty("ingress");
    expect(result.health).not.toHaveProperty("egress");
    expect(result.health).not.toHaveProperty("sync");
    expect(result.health).not.toHaveProperty("lastHeartbeatAt");
  });

  it("normalizes connection arrays while discarding entries without an id", () => {
    const result = normalizeChannelConnectionCollection({
      instances: [serverConnection, { adapter_id: "wechat-sdk", display_name: "无标识连接" }],
      read_only: true,
    });

    expect(result.readOnly).toBe(true);
    expect(result.items).toHaveLength(1);
    expect(result.items[0].id).toBe("wechat-prod");
    expect(result.items[0].config).toMatchObject({
      endpointUrl: "https://gateway.example.test",
      extra: { media_base_url: "https://media.example.test" },
    });
  });

  it("marks an enabled but unprobed connection as awaiting verification", () => {
    const result = normalizeChannelConnection({
      connection_id: "enabled-without-evidence",
      adapter_id: "wechat-sdk",
      display_name: "只声明启用",
      config_json: { sdk_url: "https://gateway.example.test" },
      secret_status: "valid",
      desired_state: "enabled",
      effective_state: "enabled",
    });

    expect(result.health.aggregate).toBe("action_required");
    expect(result.health.runtime).toBe("action_required");
    expect(result.health.probe).toBe("action_required");
    expect(result.health.reason).toContain("等待运行时自动同步");

    const unverified = normalizeChannelConnection({
      connection_id: "enabled-unverified",
      adapter_id: "wechat-sdk",
      display_name: "等待 Bridge 回写",
      desired_state: "enabled",
      effective_state: "unverified",
      last_probe_status: "",
      last_probed_at: null,
    });

    expect(unverified.health).toMatchObject({
      aggregate: "action_required",
      runtime: "action_required",
      probe: "action_required",
    });

    const validatedDraft = normalizeChannelConnection({
      connection_id: "validated-without-probe",
      adapter_id: "wechat-sdk",
      display_name: "只完成配置校验",
      config_json: { sdk_url: "https://gateway.example.test" },
      secret_status: "reference_configured",
      desired_state: "draft",
      effective_state: "ready",
      last_probe_status: "valid",
      last_probed_at: new Date(Date.now() - 60_000).toISOString(),
    });

    expect(validatedDraft.health.aggregate).not.toBe("ready");
    expect(validatedDraft.health.auth).toBe("unknown");
    expect(validatedDraft.health.runtime).toBe("unknown");
    expect(validatedDraft.health).not.toHaveProperty("ingress");
    expect(validatedDraft.health).not.toHaveProperty("egress");
    expect(validatedDraft.health).not.toHaveProperty("sync");
  });

  it("keeps the backend ready lifecycle distinct while an enabled request awaits runtime convergence", () => {
    const lastProbedAt = new Date(Date.now() - 60_000).toISOString();
    const result = normalizeChannelConnection({
      connection_id: "enabled-awaiting-runtime",
      adapter_id: "wechat-sdk",
      display_name: "等待运行时接管",
      desired_state: "enabled",
      effective_state: "ready",
      last_probe_status: "ready",
      last_probed_at: lastProbedAt,
    });

    expect(result).toMatchObject({
      desiredState: "enabled",
      effectiveState: "ready",
      health: {
        aggregate: "degraded",
        runtime: "degraded",
        probe: "ready",
      },
    });
  });

  it("only reports lifecycle drift after connectivity was actually verified", () => {
    const lastProbedAt = new Date(Date.now() - 60_000).toISOString();
    const result = normalizeChannelConnection({
      connection_id: "verified-then-unverified",
      adapter_id: "wechat-sdk",
      display_name: "已验证但运行状态漂移",
      desired_state: "enabled",
      effective_state: "unverified",
      last_probe_status: "ready",
      last_probed_at: lastProbedAt,
    });

    expect(result.health).toMatchObject({
      aggregate: "degraded",
      runtime: "degraded",
      probe: "ready",
    });
  });

  it("degrades an otherwise running connection only when its probe evidence expires", () => {
    const fresh = normalizeChannelConnection({
      connection_id: "running-fresh",
      adapter_id: "wechat-sdk",
      desired_state: "enabled",
      effective_state: "running",
      last_probe_status: "ready",
      last_probed_at: new Date(Date.now() - 60_000).toISOString(),
    });
    const stale = normalizeChannelConnection({
      connection_id: "running-stale",
      adapter_id: "wechat-sdk",
      desired_state: "enabled",
      effective_state: "running",
      last_probe_status: "ready",
      last_probed_at: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString(),
    });

    expect(fresh.health).toMatchObject({
      aggregate: "ready",
      runtime: "ready",
      probe: "ready",
    });
    expect(stale.health).toMatchObject({
      aggregate: "degraded",
      runtime: "unknown",
      probe: "degraded",
    });
    expect(stale.health.reason).toContain("探测已过期");
  });

  it("sends only nonsecret config plus secret_ref when creating and uses If-Match on update", async () => {
    const callerAttemptingToRequireLaunch = { ...write, requiredForLaunch: true };
    await createChannelConnection(config, callerAttemptingToRequireLaunch, "create-key");
    expect(apiVersionedResourceMock).toHaveBeenNthCalledWith(
      1,
      config,
      "/v1/admin/channel-connections",
      expect.objectContaining({
        auth: true,
        query: { tenant_id: "tenant-a" },
        method: "POST",
        idempotencyKey: "create-key",
        body: {
          adapter_id: "wechat-sdk",
          display_name: "生产微信连接",
          config_json: {
            endpoint_url: "https://gateway.example.test",
            poll_interval_seconds: 5,
            send_interval_seconds: 2,
          },
          secret_ref: "vault://messaging/wechat-prod",
          required_for_launch: false,
          desired_state: "draft",
        },
      }),
    );

    await updateChannelConnection(
      config,
      "wechat-prod",
      callerAttemptingToRequireLaunch,
      "\"1\"",
      "update-key",
    );
    expect(apiVersionedResourceMock).toHaveBeenNthCalledWith(
      2,
      config,
      "/v1/admin/channel-connections/wechat-prod",
      expect.objectContaining({
        method: "PATCH",
        query: { tenant_id: "tenant-a" },
        ifMatch: "\"1\"",
        idempotencyKey: "update-key",
        body: {
          display_name: "生产微信连接",
          config_json: {
            endpoint_url: "https://gateway.example.test",
            poll_interval_seconds: 5,
            send_interval_seconds: 2,
          },
          secret_ref: "vault://messaging/wechat-prod",
          required_for_launch: false,
        },
      }),
    );
    expect(apiVersionedResourceMock.mock.calls[1]?.[2]?.body).not.toHaveProperty("desired_state");
  });

  it("sends only fields declared by a non-WeChat adapter schema", async () => {
    const feixinWrite: ChannelConnectionWrite = {
      adapterId: "feixin-sdk",
      displayName: "飞信生产连接",
      endpointUrl: "https://wechat-field-must-not-leak.example.test",
      pollIntervalSeconds: 99,
      sendIntervalSeconds: 88,
      extraConfig: { endpoint_url: "https://legacy-field-must-not-leak.example.test" },
      configValues: {
        service_url: "https://feixin.example.test",
        tenant_code: "acme-cn",
        retry_limit: 4,
        obsolete_region: "must-not-leak",
      },
      configFieldNames: ["service_url", "tenant_code", "retry_limit"],
      secretRef: "env://FEIXIN_APP_SECRET",
      requiredForLaunch: false,
      desiredState: "draft",
    };

    await createChannelConnection(config, feixinWrite, "create-feixin-key");

    expect(apiVersionedResourceMock).toHaveBeenCalledWith(
      config,
      "/v1/admin/channel-connections",
      {
        auth: true,
        query: { tenant_id: "tenant-a" },
        method: "POST",
        idempotencyKey: "create-feixin-key",
        body: {
          adapter_id: "feixin-sdk",
          display_name: "飞信生产连接",
          config_json: {
            service_url: "https://feixin.example.test",
            tenant_code: "acme-cn",
            retry_limit: 4,
          },
          secret_ref: "env://FEIXIN_APP_SECRET",
          required_for_launch: false,
          desired_state: "draft",
        },
      },
    );
  });

  it("omits untouched optional schema fields without dropping false or zero", async () => {
    const write: ChannelConnectionWrite = {
      adapterId: "wechat-sdk",
      displayName: "微信主连接",
      endpointUrl: "",
      pollIntervalSeconds: 3,
      sendIntervalSeconds: 2,
      extraConfig: {},
      configValues: {
        sdk_url: "http://wxbot.internal:5080",
        media_base_url: "   ",
        cleared_optional_number: undefined,
        optional_flag: false,
        retry_limit: 0,
      },
      secretRef: "env://WXBOT_API_TOKEN",
      requiredForLaunch: false,
      desiredState: "draft",
    };

    await createChannelConnection(config, write, "create-wechat-key");

    expect(apiVersionedResourceMock).toHaveBeenCalledWith(
      config,
      "/v1/admin/channel-connections",
      expect.objectContaining({
        body: expect.objectContaining({
          config_json: {
            sdk_url: "http://wxbot.internal:5080",
            optional_flag: false,
            retry_limit: 0,
          },
        }),
      }),
    );
  });

  it("normalizes a real probe response into one supported connection check", async () => {
    apiVersionedResourceMock.mockResolvedValueOnce({
      value: {
        ok: true,
        status: "ready",
        error_codes: [],
        connection: serverConnection,
      },
      etag: "\"2\"",
    });
    const result = await probeChannelConnection(config, "wechat/prod", "\"1\"", "probe-key");

    expect(apiVersionedResourceMock).toHaveBeenCalledWith(
      config,
      "/v1/admin/channel-connections/wechat%2Fprod/probe",
      {
        auth: true,
        query: { tenant_id: "tenant-a" },
        method: "POST",
        ifMatch: "\"1\"",
        idempotencyKey: "probe-key",
      },
    );
    expect(result).toMatchObject({
      ok: true,
      status: "ready",
      checks: [{ label: "服务端连接检查", state: "ready" }],
    });
    expect(result.checks).toHaveLength(1);
    expect(result.checks[0].id).not.toMatch(/ingress|egress|sync/);
  });

  it("scopes adapter and connection collection reads to the selected tenant", async () => {
    await getChannelAdapters(config);
    await getChannelConnections(config);

    expect(apiRequestMock).toHaveBeenNthCalledWith(1, config, "/v1/admin/channel-adapters", {
      auth: true,
      query: { tenant_id: "tenant-a" },
    });
    expect(apiRequestMock).toHaveBeenNthCalledWith(2, config, "/v1/admin/channel-connections", {
      auth: true,
      query: { tenant_id: "tenant-a" },
    });
  });
});

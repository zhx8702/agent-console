import type {
  CapabilityHealth,
  LaunchChecklistStep,
  TenantCapabilitiesResponse,
} from "../lib/api";

const CHECKLIST_STEPS: Array<
  Pick<LaunchChecklistStep, "id" | "label" | "description" | "optional">
> = [
  { id: "dependencies", label: "检查运行依赖", description: "确认数据库、缓存与运行服务可用。" },
  { id: "llm", label: "配置 LLM", description: "验证模型提供方和凭据。" },
  { id: "connections", label: "添加平台连接", description: "选择已安装的消息适配器并配置一个连接实例。" },
  { id: "sync_groups", label: "同步会话", description: "同步允许机器人参与的群聊、频道或私聊。" },
  { id: "participation_policy", label: "设置参与策略", description: "定义回复边界和参与方式。" },
  {
    id: "test",
    label: "发送测试消息",
    description: "可选复测；已有正常收发记录时无需重复执行，也不影响上线。",
    optional: true,
  },
  { id: "launch", label: "正式上线", description: "复核状态后允许机器人参与真实群聊。" },
];

type CapabilityFixtureOptions = {
  navigationPaths?: string[];
  state?: CapabilityHealth;
};

export function createCapabilityResponse({
  navigationPaths = ["/"],
  state = "blocked",
}: CapabilityFixtureOptions = {}): TenantCapabilitiesResponse {
  const steps = CHECKLIST_STEPS.map<LaunchChecklistStep>((step, index) => ({
    ...step,
    state: index === 0 ? "ready" : index === 1 ? "action_required" : "blocked",
    dependencies:
      step.id === "connections"
        ? [
            {
              id: "channels.connections",
              required: true,
              state: "blocked",
              reason: "connection_required",
            },
          ]
        : [],
    recovery_actions:
      step.id === "llm"
        ? [
            {
              type: "configure",
              label: "配置 LLM",
              target: "/llm",
              requires_admin: true,
            },
          ]
        : step.id === "connections"
          ? [
              {
                type: "configure",
                label: "添加平台连接",
                target: "/channels",
                requires_admin: true,
              },
            ]
          : [],
  }));

  return {
    schema_version: "1.0",
    tenant_id: "default",
    state,
    access: {
      subject: "test-admin",
      roles: ["admin"],
      tenant_ids: ["default"],
      permissions: ["admin:read", "admin:write", "admin:danger"],
      scope: "tenant",
    },
    message_flow_runtime: {
      enabled: true,
      name: "auto",
      allowed: true,
      reason: "allowed",
      allowed_names: ["auto", "default_private_channel_flow", "default_wechat_group_flow"],
      allow_target_flows: true,
      allow_compatible_fallback: false,
    },
    capabilities: [
      {
        id: "core.overview",
        label: "控制台概览",
        category: "core",
        enabled: true,
        available: true,
        health: "ready",
        status_reason: "service_registered",
        dependencies: [],
        recovery_actions: [],
        source: "core",
        plugin: null,
        permissions: ["admin:read"],
        entry_route: "/",
      },
      {
        id: "channels.connections",
        label: "消息平台连接",
        category: "channel",
        enabled: false,
        available: false,
        health: "blocked",
        status_reason: "connection_required",
        dependencies: [
          {
            id: "channel_connection",
            required: true,
            state: "blocked",
            reason: "connection_required",
          },
        ],
        recovery_actions: [
          {
            type: "configure",
            label: "添加平台连接",
            target: "/channels",
            requires_admin: true,
          },
        ],
        source: "plugin_registry",
        plugin: null,
        permissions: ["admin:write"],
        entry_route: "/channels",
      },
    ],
    navigation: navigationPaths.map((path) => ({
      path,
      capability_id: path === "/" ? "core.overview" : `fixture.${path.slice(1).replace(/\//g, ".")}`,
      required_permission: path === "/" ? "admin:read" : "admin:write",
      visible: true,
      reason: "available",
    })),
    onboarding: { state, steps },
    summary: {
      total: 2,
      ready: 1,
      attention: 1,
      visible_navigation: navigationPaths.length,
    },
  };
}

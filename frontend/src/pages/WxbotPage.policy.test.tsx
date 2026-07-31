import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
} from "../lib/api";
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

const replyPolicy = {
  tenant_id: "default",
  session_id: groupSession.session_id,
  reply_mode: "contains",
  mention_sender_mode: "off",
  trigger_keywords_text: "",
  participation_policy: {
    threshold: 75,
    quiet_start_hour: 22,
    quiet_end_hour: 8,
    timezone: "Asia/Shanghai",
    max_soft_replies_10m: 1,
    max_soft_replies_hour: 4,
    max_bot_ratio_last_40: 0.1,
    max_consecutive_bot_messages: 1,
  },
};

const globalPolicy = {
  tenant_id: "default",
  private_reply_mode: "all",
  group_reply_mode: "contains",
  group_reply_mention_sender: false,
  trigger_keywords_text: "",
  version: 3,
};

const aggregatePolicy = {
  tenant_id: "default",
  session_id: groupSession.session_id,
  global_policy: globalPolicy,
  session_policy: { ...replyPolicy, version: 7 },
  repeater_config: {
    tenant_id: "default",
    session_id: groupSession.session_id,
    enabled: false,
    cooldown_seconds: 300,
    version: 2,
  },
  sdk_gate: { group_require_at_me: true, status: "prepared" },
  versions: { global: 3, session: 7, repeater: 2, aggregate: 5 },
};

const groupParticipationPolicy = {
  tenant_id: "default",
  session_id: groupSession.session_id,
  version: 0,
  kill_switches: {
    global_enabled: true,
    tenant_enabled: true,
    group_enabled: true,
  },
  effective_enabled: true,
  policy: {
    threshold: 60,
    quiet_start_hour: 23,
    quiet_end_hour: 8,
    timezone: "Asia/Shanghai",
    max_soft_replies_10m: 2,
    max_soft_replies_hour: 6,
    max_bot_ratio_last_40: 0.15,
    max_consecutive_bot_messages: 2,
    proactive_enabled: false,
    max_proactive_per_day: 1,
    proactive_min_silence_seconds: 10800,
    mention_sender_strategy: "never" as const,
    prompt_context_retention_seconds: 3600,
    file_send_enabled: false,
  },
  voice_profile: null,
  updated_by: "",
  updated_at: null,
};

const activityConfig = {
  tenant_id: "default",
  session_id: groupSession.session_id,
  session_name: groupSession.session_name,
  enabled: true,
  active_start: "09:00",
  active_end: "18:00",
  quiet_start: "23:00",
  quiet_end: "08:00",
  timezone: "Asia/Shanghai",
  idle_minutes: 180,
  lookback_minutes: 120,
  min_send_interval_minutes: 240,
  max_per_day: 1,
  topic_repeat_window_minutes: 2880,
  llm_model_tier: "tier-2",
  temperature: 0.8,
  agent_tool_scope: "group_info",
  version: 4,
};

let activityReadConfig = activityConfig;

function requestBody(options: unknown) {
  const typed = options as {
    init?: { body?: unknown };
    body?: unknown;
  } | undefined;
  const body = typed?.body ?? typed?.init?.body;
  if (typeof body === "object" && body !== null) {
    return body as Record<string, unknown>;
  }
  return JSON.parse(String(body || "{}")) as Record<string, unknown>;
}

describe("wxbot participation and group activity controls", () => {
  beforeEach(() => {
    activityReadConfig = activityConfig;
    apiRequestMock.mockReset();
    apiVersionedMock.mockReset();
    apiVersionedMock.mockImplementation(async (_config, path, options) => {
      if (path.endsWith("/v1/admin/tenants/default/groups/room%40chatroom/participation-policy")) {
        const saved = options?.method === "PUT";
        const body = requestBody(options);
        const killSwitches = saved
          ? body.kill_switches as typeof groupParticipationPolicy.kill_switches
          : groupParticipationPolicy.kill_switches;
        return {
          value: {
            ...groupParticipationPolicy,
            ...(saved ? body : {}),
            version: saved ? 1 : 0,
            kill_switches: killSwitches,
            effective_enabled: Boolean(
              killSwitches.global_enabled
                && killSwitches.tenant_enabled
                && killSwitches.group_enabled,
            ),
          },
          etag: saved ? '"1"' : '"0"',
        };
      }
      if (path.includes("/plugins/group_activity/config/")) {
        const saved = options?.method === "POST";
        return {
          value: {
            ...activityReadConfig,
            ...requestBody(options),
            version: saved ? 5 : 4,
          },
          etag: saved ? '"5"' : '"4"',
        };
      }
      if (path.includes("/session-state/")) {
        return {
          value: { state: "chatting", auto_reply_enabled: true, suppress_ai_reply: false, version: 0 },
          etag: '"0"',
        };
      }
      if (path.includes("/reply-policy/global/")) {
        const saved = options?.method === "POST";
        return {
          value: { ...globalPolicy, ...requestBody(options), version: saved ? 4 : 3 },
          etag: saved ? '"4"' : '"3"',
        };
      }
      if (path.includes("/reply-policy/default/room%40chatroom")) {
        const saved = options?.method === "POST";
        return {
          value: { ...replyPolicy, ...requestBody(options), version: saved ? 8 : 7 },
          etag: saved ? '"8"' : '"7"',
        };
      }
      if (path.endsWith("/reply-policy/aggregate")) {
        if (options?.method !== "POST") {
          return { value: aggregatePolicy, etag: '"reply-policy-g3-s7-r2-a5"' };
        }
        const body = requestBody(options);
        return {
          value: {
            ...aggregatePolicy,
            global_policy: {
              ...globalPolicy,
              private_reply_mode: body.private_reply_mode,
              group_reply_mode: body.group_reply_mode,
              group_reply_mention_sender: body.group_reply_mention_sender,
              trigger_keywords_text: body.trigger_keywords_text,
              version: 4,
            },
            session_policy: { ...replyPolicy, version: 8 },
            repeater_config: { ...aggregatePolicy.repeater_config, version: 3 },
            sdk_gate: { group_require_at_me: body.sdk_group_require_at_me, status: "prepared" },
            versions: { global: 4, session: 8, repeater: 3, aggregate: 6 },
          },
          etag: '"reply-policy-g4-s8-r3-a6"',
        };
      }
      throw new Error(`unexpected versioned request: ${path}`);
    });
    apiRequestMock.mockImplementation(async (_config, path, _options) => {
      if (path === "/plugins/wxbot/bridge/status") {
        return { running: false, sdk_online: false };
      }
      if (path.endsWith("/reply-queue/stats")) {
        return {};
      }
      if (path.endsWith("/sdk/queue/stats")) {
        return {};
      }
      if (path.endsWith("/admin/sessions") || path.endsWith("/admin/roster/groups")) {
        return { sessions: [groupSession] };
      }
      if (path.endsWith("/agent-tools/catalog")) {
        return { items: [], scopes: ["group_info", "group_plugin_status"] };
      }
      if (path.endsWith("/sdk/debug/trigger-config")) {
        return { group_require_at_me: true };
      }
      if (path.includes("/plugins/group_activity/events/")) {
        return {
          items: [
            {
              id: 9,
              session_id: groupSession.session_id,
              status: "skipped",
              reason_code: "awaiting_human_response",
              message_count: 6,
              created_at: "2026-07-17T08:00:00Z",
            },
          ],
        };
      }
      if (path.includes("/plugins/group_activity/trigger/")) {
        return {
          status: "dry_run",
          reason: "would_trigger",
          reason_code: "would_trigger",
          message_count: 6,
        };
      }
      return {};
    });
  });

  it("loads and persists the current group switch and warm-up policy fields", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter
        initialEntries={["/wxbot?tab=policy"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <WxbotPage />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "当前群参与总开关" })).toBeInTheDocument();
    const groupParticipationToggle = await screen.findByRole("checkbox", {
      name: /允许当前群参与回复/,
    });
    await waitFor(() => expect(groupParticipationToggle).toBeChecked());
    expect(screen.getByLabelText("群空闲多久后检查（分钟）")).toHaveValue(180);
    expect(screen.getByLabelText("群空闲多久后检查（分钟）")).toHaveAttribute("min", "180");
    expect(screen.getByLabelText("每天最多暖场（建议 1）")).toHaveValue(1);
    expect(screen.getByLabelText("每天最多暖场（建议 1）")).toHaveAttribute("max", "3");
    expect(await screen.findByText("上一条暖场后还没人回应，先保持安静")).toBeInTheDocument();

    await user.click(groupParticipationToggle);
    await user.click(screen.getByRole("button", { name: "保存群参与总开关" }));

    await waitFor(() => {
      const saveCall = apiVersionedMock.mock.calls.find(([, path, options]) => (
        path.endsWith("/v1/admin/tenants/default/groups/room%40chatroom/participation-policy")
        && options?.method === "PUT"
      ));
      expect(saveCall).toBeDefined();
      expect(requestBody(saveCall?.[2])).toMatchObject({
        kill_switches: {
          global_enabled: true,
          tenant_enabled: true,
          group_enabled: false,
        },
      });
      expect(saveCall?.[2]?.ifMatch).toBe('"0"');
    });

    await user.clear(screen.getByLabelText("两次暖场最短间隔（分钟）"));
    await user.type(screen.getByLabelText("两次暖场最短间隔（分钟）"), "300");
    await user.click(screen.getByRole("button", { name: "保存暖场配置" }));

    await waitFor(() => {
      const saveCall = apiVersionedMock.mock.calls.find(([, path, options]) => (
        path.includes("/plugins/group_activity/config/default/room%40chatroom")
        && options?.method === "POST"
      ));
      expect(saveCall).toBeDefined();
      expect(requestBody(saveCall?.[2])).toMatchObject({
        enabled: true,
        min_send_interval_minutes: 300,
        max_per_day: 1,
        topic_repeat_window_minutes: 2880,
        llm_model_tier: "tier-2",
        temperature: 0.8,
        agent_tool_scope: "group_info",
      });
      expect(saveCall?.[2]?.ifMatch).toBe('"4"');
    });

    await user.click(screen.getByRole("button", { name: "安全检查（不发送）" }));
    expect(await screen.findByText("条件满足；正式运行时会发起暖场")).toBeInTheDocument();
    const dryRunCall = apiRequestMock.mock.calls.find(([, path]) => (
      path.includes("/plugins/group_activity/trigger/default/room%40chatroom")
    ));
    expect(requestBody(dryRunCall?.[2])).toEqual({ dry_run: true, force: false });
  });

  it("normalizes legacy warm-up values before enabling another edit", async () => {
    const user = userEvent.setup();
    activityReadConfig = { ...activityConfig, idle_minutes: 60 };
    render(
      <MemoryRouter
        initialEntries={["/wxbot?tab=policy"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <WxbotPage />
      </MemoryRouter>,
    );

    const idle = await screen.findByLabelText("群空闲多久后检查（分钟）");
    await waitFor(() => expect(idle).toHaveValue(180));
    await user.clear(screen.getByLabelText("两次暖场最短间隔（分钟）"));
    await user.type(screen.getByLabelText("两次暖场最短间隔（分钟）"), "300");

    const save = screen.getByRole("button", { name: "保存暖场配置" });
    expect(save).toBeEnabled();
    await user.click(save);

    await waitFor(() => {
      const saveCall = apiVersionedMock.mock.calls.find(([, path, options]) => (
        path.includes("/plugins/group_activity/config/default/room%40chatroom")
        && options?.method === "POST"
      ));
      expect(requestBody(saveCall?.[2])).toMatchObject({
        idle_minutes: 180,
        min_send_interval_minutes: 300,
      });
      expect(saveCall?.[2]?.ifMatch).toBe('"4"');
    });
  });

  it("keeps an explicitly invalid warm-up edit blocked with a visible reason", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter
        initialEntries={["/wxbot?tab=policy"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <WxbotPage />
      </MemoryRouter>,
    );

    const idle = await screen.findByLabelText("群空闲多久后检查（分钟）");
    await waitFor(() => expect(idle).toBeEnabled());
    await user.clear(idle);
    await user.type(idle, "120");

    expect(screen.getByRole("button", { name: "保存暖场配置" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("群空闲时间不能少于 180 分钟");
  });

  it("applies the simple preset through one durable aggregate mutation", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter
        initialEntries={["/wxbot?tab=policy"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <WxbotPage />
      </MemoryRouter>,
    );

    await user.click(await screen.findByRole("button", {
      name: "一键设为 私聊直接回复 / 群里@回复",
    }));

    await waitFor(() => {
      const calls = apiVersionedMock.mock.calls.filter(([, path, options]) => (
        path.endsWith("/reply-policy/aggregate")
        && options?.method === "POST"
      ));
      expect(calls).toHaveLength(1);
      expect(requestBody(calls[0]?.[2])).toEqual({
        tenant_id: "default",
        session_id: "room@chatroom",
        private_reply_mode: "all",
        group_reply_mode: "contains",
        group_reply_mention_sender: false,
        trigger_keywords_text: "",
        session_reply_mode: "contains",
        session_mention_sender_mode: "off",
        session_trigger_keywords_text: "",
        participation_policy: replyPolicy.participation_policy,
        repeater_enabled: false,
        repeater_cooldown_seconds: 300,
        sdk_group_require_at_me: true,
      });
      expect(calls[0]?.[2]?.ifMatch).toBe('"reply-policy-g3-s7-r2-a5"');
      expect(calls[0]?.[2]?.idempotencyKey).toMatch(/^agent-console:/);
    });
  });

  it("keeps the warm-up draft and exposes recovery when the ETag conflicts", async () => {
    const user = userEvent.setup();
    apiVersionedMock.mockImplementation(async (_config, path, options) => {
      if (path.endsWith("/v1/admin/tenants/default/groups/room%40chatroom/participation-policy")) {
        return { value: groupParticipationPolicy, etag: '"0"' };
      }
      if (path.includes("/plugins/group_activity/config/") && options?.method === "POST") {
        throw new VersionConflictError(
          "409 version_conflict",
          { detail: { code: "version_conflict", current_version: 5 } },
          '"5"',
        );
      }
      if (path.includes("/plugins/group_activity/config/")) {
        return { value: activityConfig, etag: '"4"' };
      }
      if (path.includes("/session-state/")) {
        return {
          value: { state: "chatting", auto_reply_enabled: true, suppress_ai_reply: false, version: 0 },
          etag: '"0"',
        };
      }
      if (path.includes("/reply-policy/global/")) {
        return { value: globalPolicy, etag: '"3"' };
      }
      if (path.includes("/reply-policy/default/room%40chatroom")) {
        return { value: replyPolicy, etag: '"7"' };
      }
      throw new Error(`unexpected versioned request: ${path}`);
    });

    render(
      <MemoryRouter
        initialEntries={["/wxbot?tab=policy"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <WxbotPage />
      </MemoryRouter>,
    );

    const interval = await screen.findByLabelText("两次暖场最短间隔（分钟）");
    await waitFor(() => expect(interval).toBeEnabled());
    await user.clear(interval);
    await user.type(interval, "360");
    await user.click(screen.getByRole("button", { name: "保存暖场配置" }));

    expect(await screen.findByText("暖场配置已被其他操作者更新")).toBeInTheDocument();
    expect(interval).toHaveValue(360);
    const mutation = apiVersionedMock.mock.calls.find(([, path, options]) => (
      path.includes("/plugins/group_activity/config/") && options?.method === "POST"
    ));
    expect(mutation?.[2]?.ifMatch).toBe('"4"');
  });
});

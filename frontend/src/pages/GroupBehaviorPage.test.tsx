import axe from "axe-core";
import { useEffect, type PropsWithChildren } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  type GroupParticipationPolicyDocument,
  type MemberPrivacyPolicyDocument,
  type ParticipationEventDocument,
} from "../lib/api";
import { ConsoleConfigProvider, useConsoleConfig } from "../state/console-config";
import { GroupBehaviorPage } from "./GroupBehaviorPage";
import type { ScopedParticipationControlDocument } from "./group-behavior/ReleaseControlsPanel";
import type { TenantMemberControlDocument } from "./group-behavior/TenantMemberControlPanel";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    apiRequest: vi.fn(),
    apiVersionedResource: vi.fn(),
  };
});

const apiRequestMock = vi.mocked(apiRequest);
const apiVersionedMock = vi.mocked(apiVersionedResource);

const policyDocument: GroupParticipationPolicyDocument = {
  tenant_id: "default",
  session_id: "room@chatroom",
  version: 3,
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
    mention_sender_strategy: "never",
    prompt_context_retention_seconds: 3600,
  },
  voice_profile: {
    profile_id: "group-natural",
    version: 2,
    enabled: true,
    sample_source: "persona",
    sample_scope: "none",
    authorized_sample_session_ids: [],
    authorization_reference: "persona-profile-7",
    valid_from: "2026-01-01T00:00:00Z",
    expires_at: "2099-01-01T00:00:00Z",
    display_name: "自然群聊",
    tone: "natural",
    verbosity: "concise",
    phrase_preferences: ["行", "我看看"],
    emoji_frequency: 0.05,
    list_format_policy: "avoid_by_default",
    identity_disclosure: "contextual",
    source_persona_version: 7,
  },
  updated_by: "operator",
  updated_at: "2026-07-17T08:00:00Z",
};

const memberDocument: MemberPrivacyPolicyDocument = {
  tenant_id: "default",
  session_id: "room@chatroom",
  user_id: "wxid_member",
  version: 2,
  configured_policy: {
    memory_enabled: false,
    allow_group_recall: false,
    allow_private_recall: true,
    proactive_participation_enabled: false,
    soft_reply_opt_out: false,
    no_group_mentions: false,
    retention_days: 30,
    audience_scope: "private",
    allowed_session_ids: [],
    sensitive_memory_enabled: false,
    correction_enabled: true,
    deletion_enabled: true,
  },
  policy: {
    memory_enabled: false,
    allow_group_recall: false,
    allow_private_recall: true,
    proactive_participation_enabled: false,
    soft_reply_opt_out: false,
    no_group_mentions: false,
    retention_days: 30,
    audience_scope: "private",
    allowed_session_ids: [],
    sensitive_memory_enabled: false,
    correction_enabled: true,
    deletion_enabled: true,
  },
  updated_by: "",
  updated_at: null,
};

const globalControlDocument: ScopedParticipationControlDocument = {
  scope: "global",
  tenant_id: "",
  version: 1,
  control: { enabled: true, rollout_stage: "contextual" },
  updated_by: "platform-operator",
  updated_at: "2026-07-17T07:00:00Z",
};

const tenantControlDocument: ScopedParticipationControlDocument = {
  scope: "tenant",
  tenant_id: "default",
  version: 2,
  control: { enabled: true, rollout_stage: "style_10" },
  updated_by: "tenant-operator",
  updated_at: "2026-07-17T07:30:00Z",
};

const tenantMemberControlDocument: TenantMemberControlDocument = {
  tenant_id: "default",
  user_id: "wxid_member",
  version: 4,
  control: {
    memory_opt_out: false,
    participation_opt_out: false,
    no_group_mentions: false,
  },
  deletion_state: "none",
  deletion_intent_key: "",
  updated_by: "operator",
  updated_at: "2026-07-17T08:30:00Z",
};

const runtimeEvent: ParticipationEventDocument = {
  event_id: "event-1",
  tenant_id: "default",
  session_id: "room@chatroom",
  policy_version: 3,
  event_kind: "runtime",
  status: "observe_only",
  score: 18,
  reason_codes: ["rapid_multi_party_chat"],
  signal_summary: { rapid_multi_party_chat: true, total_messages_last_40: 40 },
  trace_id: "trace-1",
  created_at: "2026-07-17T09:30:00Z",
};

const versionHistoryPage = {
  items: [
    {
      version: 2,
      parent_version: 1,
      rollback_from_version: null,
      actor: "operator",
      change_summary: ["policy:updated"],
      reason_present: true,
      created_at: "2026-07-16T08:00:00Z",
    },
  ],
  next_cursor: null,
};

function VerifiedGroupBootstrap({ children }: PropsWithChildren) {
  const {
    config,
    verifiedGroupIds,
    registerVerifiedGroups,
    selectVerifiedGroup,
    updateConfig,
  } = useConsoleConfig();

  useEffect(() => {
    registerVerifiedGroups(["room@chatroom"]);
  }, [registerVerifiedGroups]);

  useEffect(() => {
    if (config.tenantId !== "default") {
      updateConfig({ tenantId: "default" });
    }
  }, [config.tenantId, updateConfig]);

  useEffect(() => {
    if (
      config.tenantId === "default" &&
      verifiedGroupIds.has("room@chatroom") &&
      config.sessionId !== "room@chatroom"
    ) {
      selectVerifiedGroup("room@chatroom");
    }
  }, [config.sessionId, config.tenantId, selectVerifiedGroup, verifiedGroupIds]);

  return children;
}

function renderPage({ selected = true }: { selected?: boolean } = {}) {
  return render(
    <ConsoleConfigProvider>
      {selected ? (
        <VerifiedGroupBootstrap>
          <main>
            <GroupBehaviorPage />
          </main>
        </VerifiedGroupBootstrap>
      ) : (
        <main>
          <GroupBehaviorPage />
        </main>
      )}
    </ConsoleConfigProvider>,
  );
}

describe("GroupBehaviorPage", () => {
  beforeEach(() => {
    window.localStorage.clear();
    apiVersionedMock.mockImplementation(async (_config, path, options) => {
      if (path === "/v1/admin/social/release-control") {
        if (options?.method === "PUT") {
          const body = options.body as { control?: ScopedParticipationControlDocument["control"] };
          return {
            value: { ...globalControlDocument, version: 2, control: body.control || globalControlDocument.control },
            etag: '"2"',
          };
        }
        return { value: globalControlDocument, etag: '"1"' };
      }
      if (path === "/v1/admin/tenants/default/participation-control") {
        if (options?.method === "PUT") {
          const body = options.body as { control?: ScopedParticipationControlDocument["control"] };
          return {
            value: { ...tenantControlDocument, version: 3, control: body.control || tenantControlDocument.control },
            etag: '"3"',
          };
        }
        return { value: tenantControlDocument, etag: '"2"' };
      }
      if (path === "/v1/admin/tenants/default/members/wxid_member/control") {
        if (options?.method === "PUT") {
          const body = options.body as {
            control?: TenantMemberControlDocument["control"];
            request_memory_deletion?: boolean;
          };
          return {
            value: {
              ...tenantMemberControlDocument,
              version: 5,
              control: body.control || tenantMemberControlDocument.control,
              deletion_state: body.request_memory_deletion ? "requested" : "none",
              deletion_intent_key: body.request_memory_deletion ? "forget-member:default:wxid_member" : "",
            },
            etag: '"5"',
          };
        }
        return { value: tenantMemberControlDocument, etag: '"4"' };
      }
      if (path.includes("/groups/") && path.includes("/members/")) {
        if (options?.method === "PUT") {
          const body = options.body as {
            policy?: MemberPrivacyPolicyDocument["policy"];
            rollback_to_version?: number;
          };
          const rolledBack = Boolean(body.rollback_to_version);
          return {
            value: {
              ...memberDocument,
              version: rolledBack ? 4 : 3,
              configured_policy: rolledBack
                ? memberDocument.policy
                : body.policy || memberDocument.policy,
              policy: rolledBack ? memberDocument.policy : body.policy || memberDocument.policy,
            },
            etag: rolledBack ? '"4"' : '"3"',
          };
        }
        return { value: memberDocument, etag: '"2"' };
      }
      if (options?.method === "PUT") {
        const body = options.body as {
          kill_switches?: GroupParticipationPolicyDocument["kill_switches"];
          policy?: GroupParticipationPolicyDocument["policy"];
          voice_profile?: GroupParticipationPolicyDocument["voice_profile"];
          rollback_to_version?: number;
        };
        return {
          value: {
            ...policyDocument,
            version: 4,
            kill_switches: body.kill_switches || policyDocument.kill_switches,
            policy: body.policy || policyDocument.policy,
            voice_profile: body.rollback_to_version
              ? policyDocument.voice_profile
              : (body.voice_profile ?? null),
          },
          etag: '"4"',
        };
      }
      return { value: policyDocument, etag: '"3"' };
    });
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      if (path.endsWith("/voice-profile/preview") && options?.init?.method === "POST") {
        const body = JSON.parse(String(options.init.body || "{}")) as {
          voice_profile?: { identity_disclosure?: string };
        };
        const always = body.voice_profile?.identity_disclosure === "always";
        return {
          profile_id: "group-natural",
          version: 2,
          runtime_reason: "voice_profile_active",
          applied: true,
          output_text: always
            ? "我是 AI 助手。这个思路可以继续看。"
            : "这个思路可以继续看。",
          mode: "two_sentences",
          transformed: always,
          emoji: "",
          catchphrase: "",
          identity_disclosed: always,
          reason_codes: always ? ["identity_disclosed", "identity_prefix_added"] : [],
        };
      }
      if (path.endsWith("/participation-preview") && options?.init?.method === "POST") {
        return {
          event_id: "preview-1",
          tenant_id: "default",
          session_id: "room@chatroom",
          policy_version: 3,
          status: "must_reply",
          score: 140,
          reason_codes: ["mentioned_me"],
          not_before: null,
          expires_at: null,
          mention_sender: true,
        };
      }
      if (path.endsWith("/participation-events")) {
        return { items: [runtimeEvent], next_cursor: null };
      }
      if (path.endsWith("/history")) {
        return versionHistoryPage;
      }
      if (path.endsWith("/memory-items")) {
        return { items: [], next_cursor: null };
      }
      return {};
    });
  });

  it("blocks the control surface until a backend-verified group is selected", async () => {
    renderPage({ selected: false });

    expect(screen.getByRole("heading", { name: "群参与与行为" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "先选择一个已验证群聊" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存群策略" })).not.toBeInTheDocument();
    await waitFor(() => expect(apiVersionedMock).toHaveBeenCalled());
    expect(
      apiVersionedMock.mock.calls.some(([, path]) => path.includes("/tenants//")),
    ).toBe(false);

    const results = await axe.run(document.body, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it("renders API float artifacts as compact probability values", async () => {
    const baseImplementation = apiVersionedMock.getMockImplementation();
    expect(baseImplementation).toBeDefined();
    apiVersionedMock.mockImplementation(async (...args) => {
      const [, path, options] = args;
      if (path.endsWith("/participation-policy") && !options?.method) {
        return {
          value: {
            ...policyDocument,
            policy: {
              ...policyDocument.policy,
              max_bot_ratio_last_40: 0.15000000596046448,
            },
            voice_profile: policyDocument.voice_profile
              ? {
                  ...policyDocument.voice_profile,
                  emoji_frequency: 0.05000000074505806,
                }
              : null,
          },
          etag: '"3"',
        };
      }
      return baseImplementation!(...args);
    });

    renderPage();

    const botRatio = await screen.findByRole("spinbutton", {
      name: "最近 40 条最大机器人占比",
    });
    const emojiFrequency = screen.getByRole("spinbutton", {
      name: "表情符号频率（0–0.15）",
    });
    expect(botRatio).toHaveValue(0.15);
    expect(botRatio).toHaveAttribute("value", "0.15");
    expect(emojiFrequency).toHaveValue(0.05);
    expect(emojiFrequency).toHaveAttribute("value", "0.05");
  });

  it("loads and saves the group policy with ETag, If-Match and an idempotency key", async () => {
    const user = userEvent.setup();
    renderPage();

    const groupSwitch = await screen.findByRole("checkbox", { name: /群开关/ });
    const save = screen.getByRole("button", { name: "保存群策略" });
    expect(save).toBeDisabled();

    const results = await axe.run(document.body, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations).toEqual([]);

    await user.click(groupSwitch);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "群内 @发送者策略" }),
      "reply_or_ambiguous",
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "提示上下文保留秒数" }), {
      target: { value: "7200" },
    });
    expect(screen.getByText("有未保存修改")).toBeInTheDocument();
    await user.click(save);

    await waitFor(() =>
      expect(apiVersionedMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/groups/room%40chatroom/participation-policy",
        expect.objectContaining({
          method: "PUT",
          ifMatch: '"3"',
          idempotencyKey: expect.stringMatching(/^agent-console:/),
          body: expect.objectContaining({
            policy: expect.objectContaining({
              mention_sender_strategy: "reply_or_ambiguous",
              prompt_context_retention_seconds: 7200,
            }),
          }),
        }),
      ),
    );
    expect(await screen.findByText(/群参与策略已保存/)).toBeInTheDocument();
    expect(screen.getAllByText("v4").length).toBeGreaterThanOrEqual(1);
  });

  it("renders the group behavior menu as a unified control deck", async () => {
    renderPage();

    const tablist = await screen.findByRole("tablist", { name: "群参与控制" });
    expect(tablist).toHaveClass("tabs-list");
    expect(tablist.closest(".group-behavior-tabs")).not.toBeNull();
    expect(screen.getByRole("tab", { name: "参与策略" })).toHaveTextContent("01参与策略发布、预算与表达边界");
    expect(screen.getByRole("tab", { name: "成员隐私" })).toHaveTextContent("04成员隐私记忆、提及与删除控制");
    expect(await screen.findByLabelText("全局发布控制")).toHaveClass("release-control-card");
    expect(screen.getByText("分层发布")).toBeInTheDocument();
  });

  it("explains every blocking layer and the safe current-group activation order", async () => {
    const baseImplementation = apiVersionedMock.getMockImplementation();
    expect(baseImplementation).toBeDefined();
    apiVersionedMock.mockImplementation(async (...args) => {
      const [, path, options] = args;
      if (path.endsWith("/participation-policy") && !options?.method) {
        return {
          value: {
            ...policyDocument,
            version: 0,
            kill_switches: {
              global_enabled: false,
              tenant_enabled: false,
              group_enabled: false,
            },
            effective_enabled: false,
          },
          etag: '"0"',
        };
      }
      return baseImplementation!(...args);
    });

    renderPage();

    expect(await screen.findByText("当前群尚未实际参与")).toBeInTheDocument();
    expect(screen.getByText(/当前群开关、租户 default 发布控制、全局发布控制/)).toBeInTheDocument();
    expect(screen.getByText(/未配置的其他群默认保持关闭/)).toBeInTheDocument();
  });

  it("governs VoiceProfile source, current-group authorization and validity without chat text", async () => {
    const user = userEvent.setup();
    renderPage();

    const enabled = await screen.findByRole("checkbox", { name: "启用表达风格" });
    expect(enabled).toBeChecked();
    expect(screen.getAllByText("当前生效").length).toBeGreaterThan(0);
    expect(screen.queryByLabelText(/聊天正文|样本正文/)).not.toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "样本来源" }), "authorized_group_samples");
    expect(screen.getByRole("combobox", { name: "样本范围" })).toHaveValue("current_group");
    expect(screen.getByRole("textbox", { name: "授权群" })).toHaveValue("room@chatroom");
    expect(screen.getByRole("textbox", { name: "授权群" })).toHaveAttribute("readonly");

    const reference = screen.getByRole("textbox", { name: "授权引用" });
    await user.clear(reference);
    expect(screen.getByText("使用当前群授权样本时必须填写授权引用。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存群策略" })).toBeDisabled();
    await user.type(reference, "approval-2026-voice-7");

    const validFrom = screen.getByLabelText("生效时间");
    const expiresAt = screen.getByLabelText("到期时间");
    fireEvent.change(validFrom, { target: { value: "2020-01-01T09:00" } });
    fireEvent.change(expiresAt, { target: { value: "2021-01-01T09:00" } });
    expect(screen.getAllByText("已到期").length).toBeGreaterThan(0);
    fireEvent.change(validFrom, { target: { value: "2099-01-01T09:00" } });
    fireEvent.change(expiresAt, { target: { value: "2098-01-01T09:00" } });
    expect(screen.getByText("到期时间必须晚于生效时间。")).toBeInTheDocument();
    fireEvent.change(expiresAt, { target: { value: "2100-01-01T09:00" } });
    expect(screen.getAllByText("等待生效").length).toBeGreaterThan(0);

    const voicePanel = screen.getByRole("heading", { name: "群聊表达风格" }).closest("section");
    expect(voicePanel).not.toBeNull();
    const results = await axe.run(voicePanel as HTMLElement, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations).toEqual([]);

    await user.click(screen.getByRole("button", { name: "保存群策略" }));
    await waitFor(() => {
      const mutation = apiVersionedMock.mock.calls.find(([, path, options]) => (
        path.endsWith("/participation-policy") && options?.method === "PUT"
      ));
      const body = mutation?.[2]?.body as {
        voice_profile?: GroupParticipationPolicyDocument["voice_profile"];
      };
      expect(body.voice_profile).toMatchObject({
        enabled: true,
        sample_source: "authorized_group_samples",
        sample_scope: "current_group",
        authorized_sample_session_ids: ["room@chatroom"],
        authorization_reference: "approval-2026-voice-7",
        valid_from: new Date("2099-01-01T09:00").toISOString(),
        expires_at: new Date("2100-01-01T09:00").toISOString(),
      });
      expect(body.voice_profile).not.toHaveProperty("chat_text");
      expect(body.voice_profile).not.toHaveProperty("sample_text");
    });

    await user.click(enabled);
    expect(screen.getByRole("combobox", { name: "样本来源" })).toBeInTheDocument();
    expect(screen.getAllByText("已停用").length).toBeGreaterThan(0);
  }, 15000);

  it("previews the unsaved VoiceProfile through the no-send style endpoint", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("checkbox", { name: "启用表达风格" });
    await user.selectOptions(screen.getByRole("combobox", { name: "身份说明" }), "always");
    const emojiFrequency = screen.getByRole("spinbutton", {
      name: "表情符号频率（0–0.15）",
    });
    expect(emojiFrequency).toHaveAttribute("max", "0.15");

    await user.click(screen.getByRole("button", { name: "预览表达效果" }));

    expect(await screen.findByText("我是 AI 助手。这个思路可以继续看。")).toBeInTheDocument();
    const previewCall = apiRequestMock.mock.calls.find(([, path]) => (
      path.endsWith("/voice-profile/preview")
    ));
    expect(previewCall?.[1]).toBe(
      "/v1/admin/tenants/default/groups/room%40chatroom/voice-profile/preview",
    );
    const requestBody = JSON.parse(String(previewCall?.[2]?.init?.body || "{}")) as {
      voice_profile: GroupParticipationPolicyDocument["voice_profile"];
      reply_text: string;
    };
    expect(requestBody.voice_profile).toMatchObject({
      identity_disclosure: "always",
      emoji_frequency: 0.05,
    });
    expect(requestBody.reply_text).toContain("这个思路可以继续看");
    expect(requestBody).not.toHaveProperty("chat_text");
    expect(requestBody).not.toHaveProperty("sample_text");
    expect(screen.getByText(/瞬时处理，不进入策略历史或参与事件/)).toBeInTheDocument();

    fireEvent.change(emojiFrequency, { target: { value: "0.2" } });
    expect(screen.getByText("表情符号频率必须在 0 到 0.15 之间。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "预览表达效果" })).toBeDisabled();
  });

  it("runs a locally assisted structured preview, lists events, and edits member privacy", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("checkbox", { name: /群开关/ });

    await user.click(screen.getByRole("tab", { name: "历史与决策模拟" }));
    const localOnlyMarker = "local-history-must-not-upload-7291";
    fireEvent.change(
      screen.getByRole("textbox", { name: "自然语言历史（仅本地解析）" }),
      {
        target: {
          value: `[23:50] 张三：@机器人 你是谁？ ${localOnlyMarker}`,
        },
      },
    );
    await user.click(screen.getByRole("button", { name: "辅助提取结构化信号" }));
    expect(screen.getByRole("checkbox", { name: /明确 @ 机器人/ })).toBeChecked();
    expect(screen.queryByLabelText(/聊天原文/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "运行结构化模拟" }));
    expect(await screen.findByText("必须回复")).toBeInTheDocument();
    expect(apiRequestMock).toHaveBeenCalledWith(
      expect.anything(),
      "/v1/admin/tenants/default/groups/room%40chatroom/participation-preview",
      expect.objectContaining({ init: expect.objectContaining({ method: "POST" }) }),
    );
    const participationPreviewCall = apiRequestMock.mock.calls.find(([, path]) =>
      path.endsWith("/participation-preview"),
    );
    const structuredBody = JSON.parse(
      String(participationPreviewCall?.[2]?.init?.body || "{}"),
    ) as Record<string, unknown>;
    expect(structuredBody).toMatchObject({
      mentioned_me: true,
      explicit_question_to_bot: true,
      now: "2020-01-02T15:50:00.000Z",
    });
    expect(JSON.stringify(structuredBody)).not.toContain(localOnlyMarker);
    expect(structuredBody).not.toHaveProperty("chat_text");
    expect(structuredBody).not.toHaveProperty("natural_history");

    await user.click(screen.getByRole("tab", { name: "决策事件" }));
    const table = screen.getByRole("table", { name: "当前群最近 50 条参与决策事件" });
    expect(within(table).getByText("实际运行")).toBeInTheDocument();
    expect(within(table).getByText("仅观察")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "成员隐私" }));
    await user.type(screen.getByLabelText("成员微信标识"), "wxid_member");
    await user.click(screen.getByRole("button", { name: "读取成员策略" }));
    const memoryToggle = await screen.findByRole("checkbox", { name: /允许持久记忆/ });
    expect(screen.getByRole("heading", { name: "没有可见记忆" })).toBeInTheDocument();
    await user.click(memoryToggle);
    await user.click(screen.getByRole("button", { name: "保存成员策略" }));

    await waitFor(() =>
      expect(apiVersionedMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/groups/room%40chatroom/members/wxid_member/privacy-policy",
        expect.objectContaining({
          method: "PUT",
          ifMatch: '"2"',
          idempotencyKey: expect.stringMatching(/^agent-console:/),
        }),
      ),
    );

    await user.selectOptions(screen.getByRole("combobox", { name: "回滚到成员策略版本" }), "2");
    await user.click(screen.getByRole("button", { name: "回滚成员策略" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "确认回滚成员策略" })).getByRole("button", {
        name: "确认回滚",
      }),
    );
    await waitFor(() =>
      expect(apiVersionedMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/groups/room%40chatroom/members/wxid_member/privacy-policy",
        expect.objectContaining({
          method: "PUT",
          body: { rollback_to_version: 2, change_reason: "" },
          ifMatch: '"3"',
          idempotencyKey: expect.stringMatching(/^agent-console:/),
        }),
      ),
    );
  }, 15000);

  it("keeps the local draft and surfaces a version conflict", async () => {
    const user = userEvent.setup();
    renderPage();

    const threshold = await screen.findByRole("spinbutton", { name: "柔性回复阈值" });
    apiVersionedMock.mockRejectedValueOnce(
      new VersionConflictError("409 resource_version_conflict", { detail: "version_conflict" }, '"4"'),
    );
    await user.clear(threshold);
    await user.type(threshold, "75");
    await user.click(screen.getByRole("button", { name: "保存群策略" }));

    expect(await screen.findByText("策略版本冲突")).toBeInTheDocument();
    expect(threshold).toHaveValue(75);
    expect(screen.getByRole("button", { name: "保存群策略" })).not.toBeDisabled();
  });

  it("reuses the same idempotency key when an unchanged save is retried", async () => {
    const user = userEvent.setup();
    renderPage();

    const threshold = await screen.findByRole("spinbutton", { name: "柔性回复阈值" });
    apiVersionedMock
      .mockRejectedValueOnce(new Error("temporary network error"))
      .mockResolvedValueOnce({ value: { ...policyDocument, version: 4 }, etag: '"4"' });
    await user.clear(threshold);
    await user.type(threshold, "75");
    await user.click(screen.getByRole("button", { name: "保存群策略" }));
    expect(
      await screen.findByText("网络请求未完成，请检查连接后重试；未保存的草稿仍会保留。"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("群策略技术详情 JSON"),
    ).toHaveTextContent("temporary network error");
    await user.click(screen.getByRole("button", { name: "保存群策略" }));
    expect(await screen.findByText(/群参与策略已保存/)).toBeInTheDocument();

    const putCalls = apiVersionedMock.mock.calls.filter(([, , options]) => options?.method === "PUT");
    expect(putCalls).toHaveLength(2);
    expect(putCalls[0][2]?.idempotencyKey).toBe(putCalls[1][2]?.idempotencyKey);
  });

  it("rolls a known policy version back through a confirmed, versioned mutation", async () => {
    const user = userEvent.setup();
    renderPage();

    const groupVersionSection = (await screen.findByRole("heading", {
      name: "群策略版本与回滚",
    })).closest("section");
    expect(groupVersionSection).not.toBeNull();
    await user.selectOptions(
      within(groupVersionSection as HTMLElement).getByRole("combobox", {
        name: "回滚到群策略版本",
      }),
      "2",
    );
    await user.type(
      within(groupVersionSection as HTMLElement).getByRole("textbox", {
        name: "回滚原因（进入审计记录）",
      }),
      "恢复稳定策略",
    );
    await user.click(
      within(groupVersionSection as HTMLElement).getByRole("button", { name: "回滚群策略" }),
    );

    const dialog = screen.getByRole("dialog", { name: "确认回滚群策略" });
    await user.click(within(dialog).getByRole("button", { name: "确认回滚" }));

    await waitFor(() =>
      expect(apiVersionedMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/groups/room%40chatroom/participation-policy",
        expect.objectContaining({
          method: "PUT",
          body: { rollback_to_version: 2, change_reason: "恢复稳定策略" },
          ifMatch: '"3"',
          idempotencyKey: expect.stringMatching(/^agent-console:/),
        }),
      ),
    );
    expect(await screen.findByText(/已回滚到 v2 的内容/)).toBeInTheDocument();
  });

  it("keeps tenant release and tenant-wide member erasure as independent versioned resources", async () => {
    const user = userEvent.setup();
    renderPage();

    const tenantRelease = await screen.findByRole("checkbox", { name: /允许本租户发布/ });
    await user.click(tenantRelease);
    await user.type(
      screen.getAllByRole("textbox", { name: "变更原因（进入审计）" })[1],
      "租户灰度暂停",
    );
    await user.click(screen.getByRole("button", { name: "保存租户发布控制" }));
    await waitFor(() =>
      expect(apiVersionedMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/participation-control",
        expect.objectContaining({
          method: "PUT",
          ifMatch: '"2"',
          idempotencyKey: expect.stringMatching(/^agent-console:/),
          body: expect.objectContaining({
            control: expect.objectContaining({ enabled: false }),
            change_reason: "租户灰度暂停",
          }),
        }),
      ),
    );

    await user.click(screen.getByRole("tab", { name: "成员隐私" }));
    await user.type(screen.getByLabelText("成员微信标识"), "wxid_member");
    await user.click(screen.getByRole("button", { name: "读取成员策略" }));
    await screen.findByRole("heading", { name: "跨群退出与数据删除" });
    await user.type(
      screen.getByRole("textbox", { name: "变更原因（进入审计）" }),
      "成员要求清除",
    );
    await user.click(screen.getByRole("button", { name: "删除该成员全部记忆" }));
    await user.click(
      within(screen.getByRole("dialog", { name: "确认提交跨群记忆删除" })).getByRole(
        "button",
        { name: "提交删除请求" },
      ),
    );

    await waitFor(() =>
      expect(apiVersionedMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/members/wxid_member/control",
        expect.objectContaining({
          method: "PUT",
          ifMatch: '"4"',
          idempotencyKey: expect.stringMatching(/^agent-console:/),
          body: {
            control: {
              memory_opt_out: true,
              participation_opt_out: false,
              no_group_mentions: false,
            },
            request_memory_deletion: true,
            change_reason: "成员要求清除",
          },
        }),
      ),
    );
    expect(await screen.findByText(/删除请求已持久化/)).toBeInTheDocument();
  });

  it("edits the configured group member policy while a stricter tenant policy is effective", async () => {
    const user = userEvent.setup();
    const baseImplementation = apiVersionedMock.getMockImplementation();
    expect(baseImplementation).toBeDefined();
    const configuredPolicy = {
      ...memberDocument.policy,
      memory_enabled: true,
      allow_group_recall: true,
      retention_days: 45,
    };
    apiVersionedMock.mockImplementation(async (...args) => {
      const [, path, options] = args;
      if (path.endsWith("/members/wxid_member/privacy-policy") && !options?.method) {
        return {
          value: {
            ...memberDocument,
            configured_policy: configuredPolicy,
            policy: memberDocument.policy,
          },
          etag: '"2"',
        };
      }
      if (path.endsWith("/members/wxid_member/control") && !options?.method) {
        return {
          value: {
            ...tenantMemberControlDocument,
            control: {
              ...tenantMemberControlDocument.control,
              memory_opt_out: true,
            },
          },
          etag: '"4"',
        };
      }
      return baseImplementation!(...args);
    });
    renderPage();

    await screen.findByRole("checkbox", { name: /当前群开关/ });
    await user.click(screen.getByRole("tab", { name: "成员隐私" }));
    await user.type(screen.getByLabelText("成员微信标识"), "wxid_member");
    await user.click(screen.getByRole("button", { name: "读取成员策略" }));

    expect(
      await screen.findByText("租户级退出正在覆盖当前群配置"),
    ).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /允许持久记忆/ })).toBeChecked();
    const retention = screen.getByRole("spinbutton", { name: /保留天数/ });
    await user.clear(retention);
    await user.type(retention, "60");
    await user.click(screen.getByRole("button", { name: "保存成员策略" }));

    await waitFor(() =>
      expect(apiVersionedMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/groups/room%40chatroom/members/wxid_member/privacy-policy",
        expect.objectContaining({
          method: "PUT",
          body: expect.objectContaining({
            policy: expect.objectContaining({
              memory_enabled: true,
              allow_group_recall: true,
              retention_days: 60,
            }),
          }),
        }),
      ),
    );
  });

  it("paginates and filters decision events on the server with a stable cursor", async () => {
    const user = userEvent.setup();
    const olderEvent: ParticipationEventDocument = {
      ...runtimeEvent,
      event_id: "event-older",
      event_kind: "preview",
      status: "must_reply",
      policy_version: 2,
      created_at: "2026-07-16T09:30:00Z",
    };
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      if (path.endsWith("/participation-events")) {
        if (options?.query?.source === "preview") {
          return { items: [olderEvent], next_cursor: null };
        }
        return options?.query?.cursor
          ? { items: [olderEvent], next_cursor: null }
          : { items: [runtimeEvent], next_cursor: "opaque-event-cursor" };
      }
      if (path.endsWith("/history")) {
        return { items: [], next_cursor: null };
      }
      return {};
    });
    renderPage();
    await screen.findByRole("checkbox", { name: /群开关/ });

    await user.click(screen.getByRole("tab", { name: "决策事件" }));
    await user.click(await screen.findByRole("button", { name: "加载更早事件" }));
    await waitFor(() =>
      expect(apiRequestMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/groups/room%40chatroom/participation-events",
        expect.objectContaining({
          query: expect.objectContaining({ limit: 50, cursor: "opaque-event-cursor" }),
        }),
      ),
    );

    await user.selectOptions(screen.getByRole("combobox", { name: "事件来源" }), "preview");
    await waitFor(() =>
      expect(apiRequestMock).toHaveBeenCalledWith(
        expect.anything(),
        "/v1/admin/tenants/default/groups/room%40chatroom/participation-events",
        expect.objectContaining({
          query: expect.objectContaining({ source: "preview", cursor: undefined }),
        }),
      ),
    );
    const table = screen.getByRole("table", { name: "当前群最近 50 条参与决策事件" });
    expect(await within(table).findByText("控制台模拟")).toBeInTheDocument();
    expect(within(table).queryByText("实际运行")).not.toBeInTheDocument();
    expect(screen.getByText("当前已加载 1 条")).toBeInTheDocument();
  });

  it("shows human-readable outcomes while keeping complete payloads in collapsed technical details", async () => {
    const user = userEvent.setup();
    renderPage();

    await screen.findByRole("checkbox", { name: /群开关/ });
    const policySummary = screen.getByText("查看策略技术详情");
    const policyDetails = policySummary.closest("details");
    expect(policyDetails).not.toBeNull();
    expect(policyDetails).not.toHaveAttribute("open");
    expect(
      within(policyDetails as HTMLElement).getByLabelText("群策略技术详情 JSON"),
    ).toHaveTextContent('"etag": "\\"3\\""');
    expect(screen.queryByText(/^ETag\s/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "决策事件" }));
    const table = screen.getByRole("table", { name: "当前群最近 50 条参与决策事件" });
    expect(within(table).getByText("首次决策 / 不适用")).toBeInTheDocument();
    expect(within(table).queryByText("decision / not_applicable")).not.toBeInTheDocument();
    const eventDetails = within(table).getByText("查看完整记录").closest("details");
    expect(eventDetails).not.toBeNull();
    expect(eventDetails).not.toHaveAttribute("open");
    expect(
      within(eventDetails as HTMLElement).getByLabelText(/完整 JSON/),
    ).toHaveTextContent('"event_id": "event-1"');
  });

  it("uses the accessible discard dialog before replacing a dirty draft", async () => {
    const user = userEvent.setup();
    renderPage();

    const threshold = await screen.findByRole("spinbutton", { name: "柔性回复阈值" });
    await user.clear(threshold);
    await user.type(threshold, "75");
    const hero = screen.getByRole("heading", { name: "群参与与行为" }).closest("section");
    expect(hero).not.toBeNull();
    const reload = within(hero as HTMLElement).getByRole("button", { name: "重新读取" });
    await user.click(reload);

    const dialog = screen.getByRole("dialog", { name: "重新读取群策略？" });
    expect(within(dialog).getByText("未保存内容将丢失")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "继续编辑" }));
    expect(threshold).toHaveValue(75);

    await user.click(reload);
    await user.click(
      within(screen.getByRole("dialog", { name: "重新读取群策略？" })).getByRole("button", {
        name: "放弃并重新读取",
      }),
    );
    await waitFor(() => expect(threshold).toHaveValue(60));
  });
});

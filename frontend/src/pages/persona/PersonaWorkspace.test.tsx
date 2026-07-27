import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../../lib/api";
import {
  personaJobDurationLabel,
  personaJobRetryLabel,
  personaJobStageLabel,
  type PersonaJob,
} from "./model";
import { PersonaWorkspace } from "./PersonaWorkspace";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, apiRequest: vi.fn() };
});

vi.mock("../../state/console-config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../state/console-config")>();
  const consoleConfig = {
    apiBaseUrl: "http://localhost",
    adminToken: "test-token",
    tenantId: "default",
    sessionId: "room@chatroom",
    userId: "",
  };
  const verifiedGroupIds = new Set(["room@chatroom"]);
  return {
    ...actual,
    useConsoleConfig: () => ({
      config: consoleConfig,
      verifiedGroupIds,
    }),
  };
});

const apiRequestMock = vi.mocked(apiRequest);

const runningJob: PersonaJob = {
  id: 7,
  tenant_id: "default",
  session_id: "room@chatroom",
  session_name: "产品群",
  target_user_id: "wxid-zhang",
  target_name: "张三",
  status: "running",
  current_stage: "map_chunks",
  attempt_count: 2,
  max_attempts: 3,
  created_at: "2026-07-21T00:00:00Z",
  started_at: "2026-07-21T00:00:10Z",
};

function renderWorkspace() {
  return render(
    <MemoryRouter>
      <PersonaWorkspace />
    </MemoryRouter>,
  );
}

function installBaseApi(getJobs: () => PersonaJob[]) {
  apiRequestMock.mockImplementation(async (_config, path) => {
    if (path === "/plugins/wxbot/admin/roster/groups") {
      return { sessions: [{ session_id: "room@chatroom", session_name: "产品群", kind: "group" }] };
    }
    if (path.includes("/plugins/wxbot/admin/roster/groups/") && path.endsWith("/members")) {
      return {
        candidates: [{ wxid: "wxid-zhang", name: "张三", msg_count: 120, has_history: true }],
      };
    }
    if (path === "/plugins/persona_extract/jobs") return { items: getJobs() };
    if (path === "/plugins/persona_extract/profiles") return { items: [] };
    return {};
  });
}

describe("persona job presentation", () => {
  it("localizes chunk progress and reports elapsed time and retries", () => {
    expect(personaJobStageLabel("map_chunks", {
      progress: { completed_chunks: 3, total_chunks: 8 },
    })).toBe("提取分段特征（3/8）");
    expect(personaJobStageLabel("extract_chunk_4_of_9")).toBe("提取分段 4/9");
    expect(personaJobDurationLabel({
      ...runningJob,
      completed_at: "2026-07-21T00:02:15Z",
    })).toBe("2 分 5 秒");
    expect(personaJobRetryLabel(runningJob)).toBe("第 2/3 次（已重试 1 次）");
  });
});

describe("PersonaWorkspace asynchronous jobs", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  });

  it("polls only the active job one request at a time and preserves dirty artifact text", async () => {
    const setTimeoutSpy = vi.spyOn(window, "setTimeout");
    let resolvePoll: ((job: PersonaJob) => void) | undefined;
    installBaseApi(() => [runningJob]);
    apiRequestMock.mockImplementation(async (_config, path) => {
      if (path === "/plugins/wxbot/admin/roster/groups") {
        return { sessions: [{ session_id: "room@chatroom", session_name: "产品群", kind: "group" }] };
      }
      if (path.includes("/plugins/wxbot/admin/roster/groups/") && path.endsWith("/members")) {
        return { candidates: [{ wxid: "wxid-zhang", name: "张三", has_history: true }] };
      }
      if (path === "/plugins/persona_extract/jobs") return { items: [runningJob] };
      if (path === "/plugins/persona_extract/profiles") return { items: [] };
      if (path === "/plugins/persona_extract/jobs/7") {
        return new Promise<PersonaJob>((resolve) => { resolvePoll = resolve; });
      }
      return {};
    });

    renderWorkspace();
    await screen.findByRole("button", { name: "7" });
    const editor = await screen.findByLabelText("技能提示词正文（运行时注入）");
    fireEvent.change(editor, { target: { value: "我的手工草稿" } });

    await waitFor(() => {
      expect(setTimeoutSpy.mock.calls.some(([, delay]) => delay === 2_000)).toBe(true);
    });
    const pollTimers = setTimeoutSpy.mock.calls.filter(([, delay]) => delay === 2_000);
    const pollTimer = pollTimers[pollTimers.length - 1]?.[0] as (() => void);
    const listCountBeforePoll = apiRequestMock.mock.calls.filter(([, path]) => path === "/plugins/persona_extract/jobs").length;
    await act(async () => {
      pollTimer();
      pollTimer();
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(apiRequestMock.mock.calls.filter(([, path]) => path === "/plugins/persona_extract/jobs/7")).toHaveLength(1);
    });
    expect(apiRequestMock.mock.calls.filter(([, path]) => path === "/plugins/persona_extract/jobs")).toHaveLength(listCountBeforePoll);

    await act(async () => {
      resolvePoll?.({
        ...runningJob,
        status: "completed",
        current_stage: "completed",
        completed_at: "2026-07-21T00:02:15Z",
        artifact: { files: { skill_prompt: "服务端新产物" } },
      });
      await Promise.resolve();
    });

    expect(screen.getByLabelText("技能提示词正文（运行时注入）")).toHaveValue("我的手工草稿");
  });

  it("pauses while hidden and aborts an in-flight poll during cleanup", async () => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    let pollSignal: AbortSignal | undefined;
    installBaseApi(() => [runningJob]);
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      if (path === "/plugins/wxbot/admin/roster/groups") {
        return { sessions: [{ session_id: "room@chatroom", session_name: "产品群", kind: "group" }] };
      }
      if (path.includes("/plugins/wxbot/admin/roster/groups/") && path.endsWith("/members")) {
        return { candidates: [{ wxid: "wxid-zhang", name: "张三", has_history: true }] };
      }
      if (path === "/plugins/persona_extract/jobs") return { items: [runningJob] };
      if (path === "/plugins/persona_extract/profiles") return { items: [] };
      if (path === "/plugins/persona_extract/jobs/7") {
        pollSignal = options?.init?.signal || undefined;
        return new Promise<PersonaJob>(() => undefined);
      }
      return {};
    });

    const view = renderWorkspace();
    await screen.findByRole("button", { name: "7" });
    expect(apiRequestMock.mock.calls.some(([, path]) => path === "/plugins/persona_extract/jobs/7")).toBe(false);

    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    await waitFor(() => expect(pollSignal).toBeDefined());
    view.unmount();
    expect(pollSignal?.aborted).toBe(true);
  });

  it("cancels an active job through the asynchronous cancel endpoint", async () => {
    installBaseApi(() => [runningJob]);
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      if (path === "/plugins/wxbot/admin/roster/groups") {
        return { sessions: [{ session_id: "room@chatroom", session_name: "产品群", kind: "group" }] };
      }
      if (path.includes("/plugins/wxbot/admin/roster/groups/") && path.endsWith("/members")) {
        return { candidates: [{ wxid: "wxid-zhang", name: "张三", has_history: true }] };
      }
      if (path === "/plugins/persona_extract/jobs") return { items: [runningJob] };
      if (path === "/plugins/persona_extract/profiles") return { items: [] };
      if (path === "/plugins/persona_extract/jobs/7/cancel") {
        expect(options?.init?.method).toBe("POST");
        expect(new Headers(options?.init?.headers).get("Idempotency-Key")).toMatch(/^agent-console:/);
        return {
          job_id: 7,
          status: "cancelled",
          cancel_requested: true,
          job: { ...runningJob, status: "cancelled", current_stage: "cancelled", cancel_requested: true },
        };
      }
      return {};
    });

    renderWorkspace();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "7" });
    await user.click(screen.getByRole("button", { name: "取消" }));
    await user.click(screen.getByRole("button", { name: "确认取消" }));

    await screen.findByText("任务 #7 已取消。");
    expect(apiRequestMock.mock.calls.some(([, path]) => path === "/plugins/persona_extract/jobs/7/cancel")).toBe(true);
  });

  it("reconciles an interrupted create by client_request_id instead of submitting again", async () => {
    let submitted = false;
    let recoveredJob: PersonaJob | null = null;
    let submittedClientRequestId = "";
    installBaseApi(() => (recoveredJob ? [recoveredJob] : []));
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      if (path === "/plugins/wxbot/admin/roster/groups") {
        return { sessions: [{ session_id: "room@chatroom", session_name: "产品群", kind: "group" }] };
      }
      if (path.includes("/plugins/wxbot/admin/roster/groups/") && path.endsWith("/members")) {
        return { candidates: [{ wxid: "wxid-zhang", name: "张三", has_history: true }] };
      }
      if (path === "/plugins/persona_extract/profiles") return { items: [] };
      if (path === "/plugins/persona_extract/jobs" && options?.init?.method === "POST") {
        submitted = true;
        const headers = new Headers(options.init.headers);
        const body = JSON.parse(String(options.init.body)) as { client_request_id: string };
        submittedClientRequestId = body.client_request_id;
        expect(body.client_request_id).toBe(headers.get("Idempotency-Key"));
        recoveredJob = {
          ...runningJob,
          status: "pending",
          current_stage: "queued",
          client_request_id: body.client_request_id,
        };
        throw new TypeError("Failed to fetch");
      }
      if (path === "/plugins/persona_extract/jobs") {
        return { items: submitted && recoveredJob ? [recoveredJob] : [] };
      }
      return {};
    });

    renderWorkspace();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "张三" }));
    await user.click(screen.getByRole("button", { name: "创建并执行蒸馏" }));
    await user.click(screen.getByRole("button", { name: "确认创建任务" }));

    await screen.findByText("提交响应中断，但已按请求标识核对到任务；无需重复创建。页面将继续跟踪该任务。");
    expect(submittedClientRequestId).toMatch(/^agent-console:/);
    expect(apiRequestMock.mock.calls.filter(([, path, options]) => (
      path === "/plugins/persona_extract/jobs" && options?.init?.method === "POST"
    ))).toHaveLength(1);
  });

  it("saves an existing persona even when its source member is no longer in the group roster", async () => {
    const xiaohaiProfile = {
      id: 12,
      session_id: "room@chatroom",
      channel: "wechat",
      source_key: "wxbot",
      source_label: "产品群",
      profile_name: "xiaohai",
      target_user_id: "wxid-xiaohai",
      target_name: "小海",
      skill_slug: "xiaohai",
      prompt_text: "# 小海\n\n说话直接、会接梗。",
      enabled: true,
      artifact: {
        slug: "xiaohai",
        target: { user_id: "wxid-xiaohai", name: "小海" },
        files: { skill_prompt: "# 小海\n\n说话直接、会接梗。" },
      },
    };
    let savedBody: Record<string, unknown> | null = null;
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      if (path === "/plugins/wxbot/admin/roster/groups") {
        return { sessions: [{ session_id: "room@chatroom", session_name: "产品群", kind: "group" }] };
      }
      if (path.includes("/plugins/wxbot/admin/roster/groups/") && path.endsWith("/members")) {
        return { candidates: [{ wxid: "wxid-zhang", name: "张三", has_history: true }] };
      }
      if (path === "/plugins/persona_extract/jobs") return { items: [] };
      if (path === "/plugins/persona_extract/profiles" && options?.init?.method === "POST") {
        savedBody = JSON.parse(String(options.init.body)) as Record<string, unknown>;
        return { ...xiaohaiProfile, ...savedBody };
      }
      if (path === "/plugins/persona_extract/profiles") return { items: [xiaohaiProfile] };
      return {};
    });

    renderWorkspace();
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("当前风格技能"), "12");
    const saveButton = screen.getByRole("button", { name: "保存风格技能" });
    await waitFor(() => expect(saveButton).toBeEnabled());
    await user.click(saveButton);
    await user.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => expect(savedBody).not.toBeNull());
    expect(savedBody).toMatchObject({
      session_id: "room@chatroom",
      profile_name: "xiaohai",
      target_user_id: "wxid-xiaohai",
      target_name: "小海",
      skill_slug: "xiaohai",
      enabled: true,
    });
  });

  it("applies a completed persona job after the source member has left the current roster", async () => {
    const completedJob: PersonaJob = {
      ...runningJob,
      id: 19,
      status: "completed",
      current_stage: "completed",
      target_user_id: "wxid-xiaohai",
      target_name: "小海",
      output_slug: "xiaohai",
      result_text: "# 小海\n\n说话自然，会接梗。",
      artifact: {
        slug: "xiaohai",
        target: { user_id: "wxid-xiaohai", name: "小海" },
        files: { skill_prompt: "# 小海\n\n说话自然，会接梗。" },
      },
    };
    let appliedBody: Record<string, unknown> | null = null;
    apiRequestMock.mockImplementation(async (_config, path, options) => {
      if (path === "/plugins/wxbot/admin/roster/groups") {
        return { sessions: [{ session_id: "room@chatroom", session_name: "产品群", kind: "group" }] };
      }
      if (path.includes("/plugins/wxbot/admin/roster/groups/") && path.endsWith("/members")) {
        return { candidates: [{ wxid: "wxid-zhang", name: "张三", has_history: true }] };
      }
      if (path === "/plugins/persona_extract/jobs") return { items: [completedJob] };
      if (path === "/plugins/persona_extract/profiles/apply-job") {
        appliedBody = JSON.parse(String(options?.init?.body)) as Record<string, unknown>;
        return {
          id: 19,
          session_id: "room@chatroom",
          profile_name: "小海",
          target_user_id: "wxid-xiaohai",
          target_name: "小海",
          skill_slug: "xiaohai",
          prompt_text: completedJob.result_text,
          artifact: completedJob.artifact,
          enabled: true,
        };
      }
      if (path === "/plugins/persona_extract/profiles") return { items: [] };
      return {};
    });

    renderWorkspace();
    const user = userEvent.setup();
    const applyButton = await screen.findByRole("button", { name: "从任务应用" });
    await waitFor(() => expect(applyButton).toBeEnabled());
    await user.click(applyButton);
    await user.click(screen.getByRole("button", { name: "确认应用" }));

    await waitFor(() => expect(appliedBody).not.toBeNull());
    expect(appliedBody).toMatchObject({
      session_id: "room@chatroom",
      job_id: 19,
      profile_name: "小海",
      enabled: true,
    });
  });
});

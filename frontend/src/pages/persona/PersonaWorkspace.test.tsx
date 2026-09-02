import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "../../lib/api";
import {
  portraitConfidenceLabel,
  portraitCoverageLabel,
  portraitFreshness,
  portraitFreshnessHint,
  portraitJobDurationLabel,
  portraitJobModeLabel,
  portraitJobStatusLabel,
  type PortraitJob,
  type PortraitRecord,
} from "./model";
import { PersonaWorkspace } from "./PersonaWorkspace";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, apiBlobRequest: vi.fn(), apiRequest: vi.fn() };
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

const portraitRecord: PortraitRecord = {
  id: 3,
  tenant_id: "default",
  speaker_id: "wxid-zhang",
  display_name: "张三",
  session_id: "room@chatroom",
  status: "ready",
  pending_messages: 2,
  hot_update_enabled: true,
  updated_at: "2026-08-28T05:00:00Z",
  portrait: {
    summary: "爱吃烤鱼的打工人",
    likes: [{ text: "烤鱼", count: 12 }],
    voice: [{ text: "短句连发", count: 6 }],
    confidence: 0.82,
    coverage: { lines_total: 500, lines_read: 500, complete: true },
  },
};

const queuedJob: PortraitJob = {
  id: 11,
  tenant_id: "default",
  session_id: "room@chatroom",
  speaker_id: "wxid-zhang",
  speaker_name: "张三",
  status: "queued",
  mode: "full",
  created_at: "2026-08-28T05:00:00Z",
};

type ApiHandler = (path: string, options?: Record<string, unknown>) => unknown;

function installApi(overrides: Record<string, ApiHandler> = {}, options?: { withPortrait?: boolean }) {
  apiRequestMock.mockImplementation(async (_config, path, requestOptions) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path === prefix || path.startsWith(prefix)) {
        return handler(path, requestOptions as Record<string, unknown>);
      }
    }
    if (path === "/plugins/wxbot/admin/roster/groups") {
      return { sessions: [{ session_id: "room@chatroom", session_name: "产品群", kind: "group" }] };
    }
    if (path.includes("/plugins/wxbot/admin/roster/groups/") && path.endsWith("/members")) {
      return { candidates: [{ wxid: "wxid-zhang", name: "张三", msg_count: 120, has_history: true }] };
    }
    if (path === "/plugins/speaker_portrait/jobs") return { items: [] };
    if (path === "/plugins/persona_extract/profiles") return { items: [] };
    if (path.startsWith("/plugins/speaker_portrait/portraits/")) {
      if (options?.withPortrait) return portraitRecord;
      throw new Error("portrait_not_found");
    }
    return {};
  });
}

function renderWorkspace() {
  return render(
    <MemoryRouter>
      <PersonaWorkspace />
    </MemoryRouter>,
  );
}

describe("portrait presentation helpers", () => {
  it("labels status, mode, duration, confidence and coverage", () => {
    expect(portraitJobStatusLabel("queued")).toBe("已排队");
    expect(portraitJobModeLabel("incremental")).toBe("增量热更新");
    expect(
      portraitJobDurationLabel({
        id: 1,
        status: "completed",
        started_at: "2026-08-28T05:00:00Z",
        finished_at: "2026-08-28T05:02:05Z",
      }),
    ).toBe("2 分 5 秒");
    expect(portraitConfidenceLabel({ confidence: 0.82 })).toBe("82%");
    expect(
      portraitCoverageLabel({ coverage: { lines_total: 500, lines_read: 500, complete: true } }),
    ).toBe("500/500（完整）");
  });

  it("compares last-distillation coverage against the live roster count", () => {
    const record: PortraitRecord = {
      pending_messages: 40,
      portrait: {
        confidence: 0.89,
        coverage: { lines_total: 5656, lines_read: 5656, complete: true },
      },
    };
    const freshness = portraitFreshness(record, 6120);
    expect(freshness).toMatchObject({
      distilledRead: 5656,
      liveTotal: 6120,
      sourceCount: 6120,
      pendingCount: 40,
      behind: true,
      complete: false,
    });
    expect(portraitCoverageLabel(record.portrait, freshness)).toBe("5656/6120（部分）");
    expect(portraitConfidenceLabel(record.portrait, freshness)).toBe("82%");
    expect(portraitFreshnessHint(freshness)).toContain("画像尚未跟上最新聊天记录");
  });
});

describe("PersonaWorkspace portrait flow", () => {
  beforeEach(() => {
    apiRequestMock.mockReset();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("loads roster, jobs and profiles for the verified group", async () => {
    installApi();
    renderWorkspace();

    await waitFor(() => {
      expect(screen.getAllByText("wxid-zhang").length).toBeGreaterThan(0);
    });
    const calledPaths = apiRequestMock.mock.calls.map(([, path]) => path);
    expect(calledPaths).toContain("/plugins/speaker_portrait/jobs");
    expect(calledPaths).toContain("/plugins/persona_extract/profiles");
  });

  it("creates a portrait job for the selected roster member", async () => {
    let createdBody: Record<string, unknown> | null = null;
    installApi({
      "/plugins/speaker_portrait/jobs": (_path, requestOptions) => {
        const init = (requestOptions as { init?: RequestInit })?.init;
        if (init?.method === "POST") {
          createdBody = JSON.parse(String(init.body || "{}"));
          return { status: "queued", job: queuedJob };
        }
        return { items: [] };
      },
    });
    renderWorkspace();

    await waitFor(() => {
      expect(screen.getAllByText("wxid-zhang").length).toBeGreaterThan(0);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "创建画像任务" }));
    });

    await waitFor(() => {
      expect(createdBody).not.toBeNull();
    });
    expect(createdBody).toMatchObject({
      tenant_id: "default",
      session_id: "room@chatroom",
      speaker_id: "wxid-zhang",
      speaker_name: "张三",
      mode: "full",
    });
    expect(await screen.findByText("已排队")).toBeInTheDocument();
  });

  it("renders portrait summary and claims when the member has a portrait", async () => {
    installApi({}, { withPortrait: true });
    renderWorkspace();

    expect(await screen.findByText("爱吃烤鱼的打工人")).toBeInTheDocument();
    expect(screen.getByText(/烤鱼（12 次）/)).toBeInTheDocument();
    expect(screen.getByText(/82%/)).toBeInTheDocument();
  });

  it("previews and applies the compiled reply style", async () => {
    let applyBody: Record<string, unknown> | null = null;
    installApi(
      {
        "/plugins/speaker_portrait/portraits/wxid-zhang/style": () => ({
          status: "ok",
          name: "张三",
          prompt: "你就是张三。",
          prompt_chars: 6,
        }),
        "/plugins/speaker_portrait/portraits/wxid-zhang/apply-style": (_path, requestOptions) => {
          const init = (requestOptions as { init?: RequestInit })?.init;
          applyBody = JSON.parse(String(init?.body || "{}"));
          return { status: "applied", profile_id: 21, name: "张三", prompt: "你就是张三。", prompt_chars: 6 };
        },
      },
      { withPortrait: true },
    );
    renderWorkspace();

    expect(await screen.findByText("爱吃烤鱼的打工人")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "预览风格提示词" }));
    });
    expect(await screen.findByDisplayValue("你就是张三。")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "应用为本群回复风格" }));
    });
    await waitFor(() => {
      expect(applyBody).not.toBeNull();
    });
    expect(applyBody).toMatchObject({
      tenant_id: "default",
      session_id: "room@chatroom",
      channel: "wechat",
      source_key: "wxbot",
      enabled: true,
    });
  });

  it("activates a saved reply-style profile", async () => {
    let activated = false;
    installApi({
      "/plugins/persona_extract/profiles/21/activate": () => {
        activated = true;
        return { id: 21, enabled: true, session_id: "room@chatroom" };
      },
      "/plugins/persona_extract/profiles": (_path, requestOptions) => {
        const init = (requestOptions as { init?: RequestInit })?.init;
        if (init?.method) return {};
        return {
          items: [
            {
              id: 21,
              session_id: "room@chatroom",
              profile_name: "张三",
              skill_slug: "portrait-wxid-zhang",
              enabled: false,
              updated_at: "2026-08-28T05:00:00Z",
            },
          ],
        };
      },
    });
    renderWorkspace();

    expect(await screen.findByText("portrait-wxid-zhang")).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "启用" }));
    });
    await waitFor(() => {
      expect(activated).toBe(true);
    });
  });

  it("polls the active portrait job and refreshes portrait and profiles on completion", async () => {
    vi.useFakeTimers();
    let pollCount = 0;
    installApi(
      {
        "/plugins/speaker_portrait/jobs/11": () => {
          pollCount += 1;
          return { ...queuedJob, status: pollCount >= 2 ? "completed" : "running" };
        },
        "/plugins/speaker_portrait/jobs": (_path, requestOptions) => {
          const init = (requestOptions as { init?: RequestInit })?.init;
          if (init?.method === "POST") return { status: "queued", job: queuedJob };
          return { items: [queuedJob] };
        },
      },
      { withPortrait: true },
    );
    renderWorkspace();

    // Flush the mount-time roster/job/profile loads so the polling effect
    // can see the active job and schedule its first tick.
    await act(async () => {});
    await act(async () => {});

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_100);
    });
    expect(pollCount).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_100);
    });
    expect(pollCount).toBe(2);

    const portraitFetches = apiRequestMock.mock.calls.filter(([, path]) =>
      String(path).startsWith("/plugins/speaker_portrait/portraits/wxid-zhang"),
    );
    expect(portraitFetches.length).toBeGreaterThanOrEqual(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(pollCount).toBe(2);
    vi.useRealTimers();
  });
});

import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { VersionConflictError, apiVersionedResource } from "../lib/api";
import { LlmConfigPage } from "./LlmConfigPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, apiVersionedResource: vi.fn() };
});

const consoleConfig = {
  apiBaseUrl: "http://localhost",
  adminToken: "test-admin-token",
  tenantId: "",
  sessionId: "",
  userId: "operator",
};

vi.mock("../state/console-config", () => ({
  useConsoleConfig: () => ({ config: consoleConfig }),
}));

const apiVersionedMock = vi.mocked(apiVersionedResource);

const editableSources = {
  llm_provider: "persisted_override",
  openai_base_url: "dotenv_or_default",
  openai_api_mode: "dotenv_or_default",
  openai_web_search_enabled: "dotenv_or_default",
  openai_web_search_tool: "dotenv_or_default",
  openai_web_search_live_enabled: "dotenv_or_default",
  llm_embed_provider: "dotenv_or_default",
  knowledge_features_enabled: "dotenv_or_default",
  customer_service_prompt_enabled: "dotenv_or_default",
  llm_model_tier1: "persisted_override",
  llm_model_tier2: "environment",
  llm_model_tier3: "dotenv_or_default",
  llm_embed_model: "dotenv_or_default",
} as const;

function runtimeConfig(overrides: Record<string, unknown> = {}) {
  return {
    loaded: true,
    version: 4,
    llm_provider: "openai",
    openai_base_url: "https://api.openai.com/v1",
    openai_api_mode: "responses",
    openai_web_search_enabled: false,
    openai_web_search_tool: "web_search",
    openai_web_search_live_enabled: true,
    llm_embed_provider: "fake",
    knowledge_features_enabled: false,
    customer_service_prompt_enabled: false,
    llm_model_tier1: "model-one",
    llm_model_tier2: "environment-model",
    llm_model_tier3: "model-three",
    llm_embed_model: "embed-model",
    field_sources: editableSources,
    secret_provider_status: {
      openai_api_key: {
        configured: true,
        source: "secret_provider",
        mutable: false,
      },
    },
    validation_errors: [],
    restart_required: false,
    apply_status: "no_persisted_change",
    affected_roles: ["api", "inbound", "scheduler"],
    updated_at: null,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <main>
        <a href="/other">离开配置页</a>
        <LlmConfigPage />
      </main>
    </MemoryRouter>,
  );
}

describe("LlmConfigPage versioned secret-safe draft", () => {
  beforeEach(() => {
    apiVersionedMock.mockReset();
    apiVersionedMock.mockResolvedValue({
      value: runtimeConfig(),
      etag: '"runtime-llm-config-4"',
    });
  });

  it("shows only provider status, saves a non-secret diff with If-Match, and passes axe", async () => {
    const user = userEvent.setup();
    apiVersionedMock
      .mockResolvedValueOnce({
        value: runtimeConfig(),
        etag: '"runtime-llm-config-4"',
      })
      .mockResolvedValueOnce({
        value: runtimeConfig({
          version: 5,
          llm_model_tier1: "model-next",
          llm_model_tier2: "environment-model-next",
          restart_required: true,
        }),
        etag: '"runtime-llm-config-5"',
      });
    renderPage();

    expect(await screen.findByText("已配置（不回显）")).toBeInTheDocument();
    expect(screen.queryByLabelText(/API Key/)).not.toBeInTheDocument();
    expect(screen.queryByText(/清空 OpenAI Key/)).not.toBeInTheDocument();
    const tierTwo = screen.getByRole("textbox", { name: /^第 2 档模型/ });
    expect(tierTwo).toBeEnabled();

    const tierOne = screen.getByRole("textbox", { name: /^第 1 档模型/ });
    await user.clear(tierOne);
    await user.type(tierOne, "model-next");
    await user.clear(tierTwo);
    await user.type(tierTwo, "environment-model-next");
    await user.click(screen.getByRole("button", { name: "保存为新版本" }));

    await waitFor(() => expect(apiVersionedMock).toHaveBeenLastCalledWith(
      expect.anything(),
      "/v1/admin/runtime/llm-config",
      {
        auth: true,
        method: "POST",
        ifMatch: '"runtime-llm-config-4"',
        idempotencyKey: expect.stringMatching(/^agent-console:/),
        body: {
          llm_model_tier1: "model-next",
          llm_model_tier2: "environment-model-next",
        },
      },
    ));
    expect(JSON.stringify(apiVersionedMock.mock.calls)).not.toContain("openai_api_key");
    expect(await screen.findByText(/尚未热应用/)).toBeInTheDocument();

    const results = await axe.run(document.body, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it("preserves the draft on conflict and protects navigation", async () => {
    const user = userEvent.setup();
    apiVersionedMock
      .mockResolvedValueOnce({
        value: runtimeConfig(),
        etag: '"runtime-llm-config-4"',
      })
      .mockRejectedValueOnce(
        new VersionConflictError(
          "409 version conflict",
          { detail: { code: "version_conflict", current_version: 5 } },
          '"runtime-llm-config-5"',
        ),
      );
    renderPage();

    const tierOne = await screen.findByRole("textbox", { name: /^第 1 档模型/ });
    await user.clear(tierOne);
    await user.type(tierOne, "local-draft");
    await user.click(screen.getByText("离开配置页"));
    expect(await screen.findByRole("dialog", { name: "放弃未保存的修改？" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续编辑" }));

    await user.click(screen.getByRole("button", { name: "保存为新版本" }));
    expect(await screen.findByText(/服务器配置已被其他管理员更新/)).toBeInTheDocument();
    expect(tierOne).toHaveValue("local-draft");
    expect(screen.getByText("查看服务器版本令牌").closest("details")).not.toHaveAttribute("open");
  });

  it("does not create or enable a default draft after a failed read", async () => {
    apiVersionedMock.mockRejectedValueOnce(new Error("database unavailable"));
    renderPage();

    expect(await screen.findByText("尚未取得服务器配置")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("模型配置读取失败，请稍后重试");
    expect(screen.queryByRole("textbox", { name: /^OpenAI 接口地址/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存为新版本" })).toBeDisabled();
  });

  it("reuses the stable idempotency key after a lost response and clears it after success", async () => {
    const user = userEvent.setup();
    apiVersionedMock
      .mockResolvedValueOnce({
        value: runtimeConfig(),
        etag: '"runtime-llm-config-4"',
      })
      .mockRejectedValueOnce(new Error("network response lost"))
      .mockResolvedValueOnce({
        value: runtimeConfig({ version: 5, llm_model_tier1: "retry-model", restart_required: true }),
        etag: '"runtime-llm-config-5"',
      });
    renderPage();

    const tierOne = await screen.findByRole("textbox", { name: /^第 1 档模型/ });
    await user.clear(tierOne);
    await user.type(tierOne, "retry-model");
    await user.click(screen.getByRole("button", { name: "保存为新版本" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络请求未完成");
    await user.click(screen.getByRole("button", { name: "保存为新版本" }));

    await waitFor(() => expect(apiVersionedMock).toHaveBeenCalledTimes(3));
    const firstKey = apiVersionedMock.mock.calls[1]?.[2]?.idempotencyKey;
    const retryKey = apiVersionedMock.mock.calls[2]?.[2]?.idempotencyKey;
    expect(firstKey).toMatch(/^agent-console:/);
    expect(retryKey).toBe(firstKey);
    expect(await screen.findByText("v5")).toBeInTheDocument();
  });
});

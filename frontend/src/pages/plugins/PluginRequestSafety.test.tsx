import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PluginsPage } from "../PluginsPage";
import { PLUGIN_RUNTIME_TIMEOUT_MS, queryPluginRuntime } from "./runtimeQuery";

const mocks = vi.hoisted(() => ({
  config: { apiBaseUrl: "http://localhost", adminToken: "test", tenantId: "tenant-a", sessionId: "a@chatroom", userId: "" },
  request: vi.fn(), versioned: vi.fn(), register: vi.fn(),
}));
vi.mock("../../state/console-config", () => ({
  useConsoleConfig: () => ({ config: mocks.config, registerVerifiedGroups: mocks.register, selectVerifiedGroup: vi.fn() }),
}));
vi.mock("../../lib/api", async (original) => ({
  ...await original<object>(), apiRequest: mocks.request, apiVersionedResource: mocks.versioned,
}));
const plugin = { name: "credits", version: "1", description: "credits", enabled: true, system: false, status: "active", restart_required: false, admin_ui: { scope: "group" } };
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const view = () => <MemoryRouter><PluginsPage /></MemoryRouter>;
function defaults(_config: unknown, path: string) {
  if (path.endsWith("/summary") && path.includes("plugins")) return Promise.resolve({ plugins: [plugin] });
  if (path.endsWith("/installed")) return Promise.resolve({ plugins: [plugin] });
  if (path.endsWith("/scopes")) return Promise.resolve({ items: [] });
  if (path.includes("plugins/credits/runtime")) return Promise.resolve({ runtime_status: { enabled: true, credit_name: "current" } });
  return Promise.resolve({});
}

beforeEach(() => {
  mocks.config = { apiBaseUrl: "http://localhost", adminToken: "test", tenantId: "tenant-a", sessionId: "a@chatroom", userId: "" };
  mocks.request.mockReset().mockImplementation(defaults);
  mocks.versioned.mockReset().mockResolvedValue({ value: {} });
  mocks.register.mockReset();
});
afterEach(() => { vi.useRealTimers(); });

describe("plugin state request safety", () => {
  it("shows unknown and disables lifecycle actions when installed state fails", async () => {
    mocks.request.mockImplementation((config, path) => path.endsWith("/installed")
      ? Promise.reject(new Error("unavailable")) : defaults(config, path));
    render(view());
    await screen.findByText("插件状态查询失败，当前启用状态未知，请刷新重试。");
    const card = document.getElementById("plugin-card-credits")!;
    expect(within(card).getAllByText("状态未知").length).toBeGreaterThan(0);
    expect(within(card).queryByText("已启用")).not.toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "全局启用" })).toBeDisabled();
    expect(mocks.request.mock.calls.some(([, path]) => path.includes("plugins/credits/runtime"))).toBe(false);
  });

  it("treats a successful empty installed list as empty, not an active summary fallback", async () => {
    mocks.request.mockImplementation((config, path) => path.endsWith("/installed")
      ? Promise.resolve({ plugins: [] }) : defaults(config, path));
    render(view());
    await screen.findByText("暂无已安装插件");
    expect(document.getElementById("plugin-card-credits")).toBeNull();
  });

  it("isolates runtime errors and renders successful plugins without waiting for a stalled probe", async () => {
    const slow = deferred<object>();
    mocks.request.mockImplementation((config, path) => {
      if (path.endsWith("/installed")) return Promise.resolve({ plugins: [plugin, { ...plugin, name: "memory" }, { ...plugin, name: "wxbot" }] });
      if (path.includes("plugins/memory/runtime")) return Promise.reject(new Error("memory diagnostic failed"));
      if (path.includes("plugins/wxbot/runtime")) return slow.promise;
      return defaults(config, path);
    });
    const page = render(view());
    expect(await screen.findByText("current")).toBeInTheDocument();
    expect(await screen.findByText("memory diagnostic failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新插件摘要" })).toBeEnabled();
    page.unmount();
    await act(async () => slow.resolve({ runtime_status: {} }));
  });

  it.each(["tenantId", "sessionId", "apiBaseUrl", "adminToken"] as const)("ignores old successes and failures after %s changes", async (key) => {
    const oldScope = deferred<object>();
    const oldRuntime = deferred<object>();
    let old = true;
    mocks.request.mockImplementation((config, path) => {
      if (old && path.endsWith("/scopes")) return oldScope.promise;
      if (old && path.includes("plugins/credits/runtime")) return oldRuntime.promise;
      if (path.endsWith("/scopes")) return Promise.resolve({ items: [{ plugin_name: "credits", enabled: false, version: 7 }] });
      return defaults(config, path);
    });
    const page = render(view());
    await waitFor(() => expect(mocks.request.mock.calls.some(([, path]) => path.endsWith("/scopes"))).toBe(true));
    old = false;
    mocks.config = { ...mocks.config, [key]: key === "sessionId" ? "b@chatroom" : "new-value" };
    page.rerender(view());
    const panel = screen.getByRole("heading", { name: "群级插件开关" }).closest("section")!;
    await within(panel).findByText("已关闭");
    await act(async () => {
      oldScope.resolve({ items: [{ plugin_name: "credits", enabled: true, version: 99 }] });
      oldRuntime.reject(new Error("stale diagnostic error"));
    });
    expect(within(panel).getByText("已关闭")).toBeInTheDocument();
    expect(screen.queryByText("stale diagnostic error")).not.toBeInTheDocument();
    expect(screen.getByText("current")).toBeInTheDocument();
  });

  it("ignores a write acknowledgement and its refresh after switching sessions", async () => {
    const write = deferred<{ value: object }>();
    mocks.versioned.mockImplementation((_config, path) => path.endsWith("/scopes") ? write.promise : Promise.resolve({ value: {} }));
    const page = render(view());
    const scope = () => screen.getByRole("heading", { name: "群级插件开关" }).closest("section")!;
    fireEvent.click(await within(scope()).findByRole("button", { name: "关闭" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认关闭" }));
    await waitFor(() => expect(mocks.versioned.mock.calls.some(([, path]) => path.endsWith("/scopes"))).toBe(true));
    mocks.config = { ...mocks.config, sessionId: "b@chatroom" };
    page.rerender(view());
    await within(scope()).findByText("已开启");
    const calls = mocks.request.mock.calls.length;
    await act(async () => write.resolve({ value: { scope_state: { enabled: false, version: 5 } } }));
    expect(within(scope()).getByText("已开启")).toBeInTheDocument();
    expect(mocks.request.mock.calls.length).toBe(calls);
  });

  it("bounds runtime requests even if the transport ignores abort", async () => {
    vi.useFakeTimers();
    mocks.request.mockReturnValue(new Promise(() => undefined));
    const request = queryPluginRuntime(mocks.config, "credits");
    const rejected = expect(request).rejects.toThrow("运行状态查询超时");
    await vi.advanceTimersByTimeAsync(PLUGIN_RUNTIME_TIMEOUT_MS);
    await rejected;
    expect(mocks.request.mock.calls[0][2].init.signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
});

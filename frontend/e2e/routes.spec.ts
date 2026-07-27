import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const protectedRoutes = [
  "/",
  "/llm",
  "/group-behavior",
  "/plugins",
  "/plugins/marketplace",
  "/amap",
  "/commands",
  "/channels",
  "/playground",
  "/queues",
  "/knowledge",
  "/memory",
  "/relationship-graph",
  "/credits",
  "/moderation",
  "/persona",
  "/repeater",
  "/wxbot",
  "/dlq",
] as const;

const navigation = protectedRoutes.map((path) => ({
  path,
  capability_id: path === "/" ? "core.overview" : `e2e.${path.slice(1).replaceAll("/", ".")}`,
  required_permission: "admin:read",
  visible: true,
  reason: "visible",
}));

type ApiFixtureOptions = {
  visiblePaths?: readonly (typeof protectedRoutes)[number][];
  role?: "platform_admin" | "platform_operator" | "platform_reader";
  permissions?: string[];
  runtimeLlmConflict?: boolean;
  requests?: {
    method: string;
    path: string;
    ifMatch?: string;
    idempotencyKey?: string;
  }[];
};

const runtimeLlmConfig = {
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
  field_sources: {
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
  },
  secret_provider_status: {
    openai_api_key: { configured: true, source: "secret_provider", mutable: false },
  },
  validation_errors: [],
  restart_required: false,
  apply_status: "no_persisted_change",
  affected_roles: ["api", "inbound", "scheduler"],
  updated_at: null,
};

async function installApiFixture(page: Page, options: ApiFixtureOptions = {}) {
  const visiblePaths = options.visiblePaths || protectedRoutes;
  const fixtureNavigation = navigation.filter((item) => visiblePaths.includes(item.path));
  const permissions = options.permissions || ["admin:read", "admin:write", "admin:danger"];
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    const method = route.request().method();
    const requestHeaders = route.request().headers();
    options.requests?.push({
      method,
      path,
      ifMatch: requestHeaders["if-match"],
      idempotencyKey: requestHeaders["idempotency-key"],
    });
    const commonHeaders = { "content-type": "application/json" };
    if (path === "/v1/admin/auth/session") {
      await route.fulfill({ status: 200, headers: commonHeaders, body: JSON.stringify({ authenticated: true }) });
      return;
    }
    if (path === "/v1/admin/auth/me") {
      await route.fulfill({
        status: 200,
        headers: commonHeaders,
        body: JSON.stringify({
          authenticated: true,
          subject: "e2e-operator",
          roles: [options.role || "platform_admin"],
          tenant_ids: ["default"],
          group_ids: ["*"],
          default_tenant_id: "default",
          access_scope: "tenant",
          auth_kind: "session",
        }),
      });
      return;
    }
    if (path.endsWith("/capabilities")) {
      await route.fulfill({
        status: 200,
        headers: commonHeaders,
        body: JSON.stringify({
          schema_version: "1.0",
          tenant_id: "default",
          state: "ready",
          access: { subject: "e2e-operator", roles: [options.role || "platform_admin"], tenant_ids: ["default"], permissions },
          capabilities: fixtureNavigation.map((item) => ({
            id: item.capability_id,
            label: item.path,
            category: "e2e",
            enabled: true,
            available: true,
            health: "ready",
            status_reason: "e2e_fixture",
            dependencies: [],
            recovery_actions: [],
            source: "e2e",
            permissions: ["admin:read"],
            entry_route: item.path,
          })),
          navigation: fixtureNavigation,
          onboarding: { state: "ready", steps: [] },
          summary: { total: fixtureNavigation.length, ready: fixtureNavigation.length, attention: 0, visible_navigation: fixtureNavigation.length },
        }),
      });
      return;
    }
    if (path === "/v1/admin/runtime/llm-config") {
      if (method === "POST" && options.runtimeLlmConflict) {
        await route.fulfill({
          status: 409,
          headers: { ...commonHeaders, etag: '"runtime-llm-config-5"' },
          body: JSON.stringify({ detail: { code: "version_conflict", current_version: 5 } }),
        });
      } else {
        await route.fulfill({
          status: 200,
          headers: { ...commonHeaders, etag: '"runtime-llm-config-4"' },
          body: JSON.stringify(runtimeLlmConfig),
        });
      }
      return;
    }
    if (path === "/healthz") {
      await route.fulfill({ status: 200, headers: commonHeaders, body: JSON.stringify({ status: "ok" }) });
      return;
    }
    if (path === "/readyz") {
      await route.fulfill({ status: 200, headers: commonHeaders, body: JSON.stringify({ status: "ready", checks: {} }) });
      return;
    }
    if (path === "/openapi.json") {
      await route.fulfill({ status: 200, headers: commonHeaders, body: JSON.stringify({ paths: {} }) });
      return;
    }
    await route.fulfill({ status: 200, headers: commonHeaders, body: JSON.stringify({ items: [], messages: [], sessions: [], groups: [], plugins: [] }) });
  });
}

async function replaceApiFixture(page: Page, options: ApiFixtureOptions) {
  await page.unroute("**/api/**");
  await installApiFixture(page, options);
}

test.beforeEach(async ({ page }) => {
  await installApiFixture(page);
});

for (const path of protectedRoutes) {
  test(`direct refresh renders ${path}`, async ({ page }) => {
    await page.goto(path);
    await page.reload();
    await expect(page.locator("main")).toBeVisible();
    await expect(page).toHaveTitle(/智能体控制台/);
    await expect(
      page.getByRole("heading", { name: "页面不存在", exact: true }),
    ).toHaveCount(0);
    const horizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(horizontalOverflow).toBeLessThanOrEqual(1);
    const axe = await new AxeBuilder({ page }).include("main").analyze();
    expect(
      axe.violations.filter((item) => ["serious", "critical"].includes(item.impact || "")),
    ).toEqual([]);
  });
}

test("unknown route exposes a recoverable 404", async ({ page }) => {
  await page.goto("/not-a-real-route");
  await expect(
    page.getByRole("heading", { name: "页面不存在", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /返回/ })).toBeVisible();
});

test("route change announces and moves focus to main content", async ({ page }) => {
  test.skip((await page.viewportSize())?.width !== 1440, "desktop project only");
  await page.goto("/");
  const queuesLink = page.getByRole("link", { name: /消息队列/ }).first();
  await queuesLink.click();
  await expect(page).toHaveURL(/\/queues$/);
  await expect(page.locator("main")).toBeFocused();
  await expect(page.locator(".route-announcer")).toContainText(/消息队列/);
});

test("mobile navigation opens, traps a usable focus target, and closes with Escape", async ({ page }) => {
  test.skip((await page.viewportSize())?.width !== 390, "mobile project only");
  const menu = page.getByRole("button", { name: /菜单|导航/ }).first();
  await page.goto("/");
  await menu.click();
  await expect(page.locator("nav")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(menu).toBeFocused();
});

test("reader navigation and direct routes follow the server capability decision", async ({ page }) => {
  test.skip((await page.viewportSize())?.width !== 1440, "desktop project only");
  await replaceApiFixture(page, {
    visiblePaths: ["/", "/queues"],
    role: "platform_reader",
    permissions: ["admin:read"],
  });

  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "控制台页面" });
  await expect(nav.getByRole("link")).toHaveCount(2);
  await expect(nav.getByRole("link", { name: /概览与上线/ })).toBeVisible();
  await expect(nav.getByRole("link", { name: /消息队列/ })).toBeVisible();
  await expect(nav.getByRole("link", { name: /插件管理/ })).toHaveCount(0);

  await page.goto("/plugins");
  await expect(page.getByRole("heading", { name: "当前入口不可用" })).toBeVisible();
});

test("a keyboard-only operator can open a key task and receives focus feedback", async ({ page }) => {
  test.skip((await page.viewportSize())?.width !== 1440, "desktop project only");
  await page.goto("/");
  await expect(page.locator("main")).toBeFocused();
  const queuesLink = page.getByRole("link", { name: /消息队列/ }).first();

  let reachedQueues = false;
  for (let step = 0; step < 40; step += 1) {
    await page.keyboard.press("Shift+Tab");
    if (await queuesLink.evaluate((element) => document.activeElement === element)) {
      reachedQueues = true;
      break;
    }
  }
  expect(reachedQueues).toBe(true);
  await expect(queuesLink).toHaveCSS("outline-style", "solid");
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/\/queues$/);
  await expect(page.locator("main")).toBeFocused();
  await expect(page.locator(".route-announcer")).toContainText(/消息队列/);
});

test("group writes stay unavailable until a verified group is selected", async ({ page }) => {
  test.skip((await page.viewportSize())?.width !== 1440, "desktop project only");
  const requests: NonNullable<ApiFixtureOptions["requests"]> = [];
  await replaceApiFixture(page, { requests });

  await page.goto("/group-behavior");
  await expect(page.getByText("尚未选择群聊", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "先选择一个已验证群聊" })).toBeVisible();
  await expect(page.getByRole("button", { name: "保存群策略" })).toHaveCount(0);
  expect(
    requests.some(({ method, path }) => method !== "GET" && path.includes("/social/")),
  ).toBe(false);
});

test("a version conflict preserves the local model draft and blocks overwrite", async ({ page }) => {
  test.skip((await page.viewportSize())?.width !== 1440, "desktop project only");
  const requests: NonNullable<ApiFixtureOptions["requests"]> = [];
  await replaceApiFixture(page, { runtimeLlmConflict: true, requests });

  await page.goto("/llm");
  const tierOne = page.getByRole("textbox", { name: /^第 1 档模型/ });
  await expect(tierOne).toHaveValue("model-one");
  await tierOne.fill("local-browser-draft");
  await page.getByRole("button", { name: "保存为新版本" }).click();

  await expect(page.getByText(/服务器配置已被其他管理员更新/)).toBeVisible();
  await expect(tierOne).toHaveValue("local-browser-draft");
  await expect(page.getByRole("button", { name: "保存为新版本" })).toBeDisabled();
  const writes = requests.filter(
    ({ method, path }) => method === "POST" && path === "/v1/admin/runtime/llm-config",
  );
  expect(writes).toHaveLength(1);
  expect(writes[0]?.ifMatch).toBe('"runtime-llm-config-4"');
  expect(writes[0]?.idempotencyKey).toMatch(/^agent-console:/);
});

test("dangerous queue replay requires explicit confirmation and restores focus", async ({ page }) => {
  test.skip((await page.viewportSize())?.width !== 1440, "desktop project only");
  const requests: NonNullable<ApiFixtureOptions["requests"]> = [];
  await replaceApiFixture(page, { requests });

  await page.goto("/dlq");
  await page.getByRole("textbox", { name: "消息标识", exact: true }).fill("entry-42");
  const replay = page.getByRole("button", { name: "重放消息" });
  await replay.click();
  const dialog = page.getByRole("dialog", { name: "确认重放死信消息" });
  await expect(dialog).toContainText("entry-42");
  await expect(dialog).toContainText("从死信队列移除原记录");
  expect(requests.some(({ method }) => method === "POST")).toBe(false);

  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(replay).toBeFocused();
  await replay.click();
  await page.getByRole("button", { name: "确认重放" }).click();
  await expect(page.getByRole("dialog", { name: "确认重放死信消息" })).toHaveCount(0);

  const writes = requests.filter(
    ({ method, path }) => method === "POST" && path.endsWith("/dlq/messages/entry-42/replay"),
  );
  expect(writes).toHaveLength(1);
  expect(writes[0]?.idempotencyKey).toMatch(/^agent-console:/);
});

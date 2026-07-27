import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ConsoleConfig } from "../state/console-config";
import {
  AUTH_INVALID_EVENT,
  COOKIE_SESSION_MARKER,
} from "../state/console-config";
import {
  apiBlobRequest,
  apiBrowserPath,
  apiDocumentUrl,
  apiRequest,
  apiVersionedResource,
  editVersionedResource,
  markVersionedResourceLoaded,
  VersionConflictError,
} from "./api";

const baseConfig: ConsoleConfig = {
  apiBaseUrl: "http://console.test",
  adminToken: "",
  tenantId: "default",
  sessionId: "",
  userId: "",
};

describe("browser API prefix", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
  });

  it("normalizes every backend path under /api without double-prefixing", () => {
    expect(apiBrowserPath("/plugins/wxbot/admin/sessions")).toBe(
      "/api/plugins/wxbot/admin/sessions",
    );
    expect(apiBrowserPath("/v1/admin/auth/session?fresh=true")).toBe(
      "/api/v1/admin/auth/session?fresh=true",
    );
    expect(apiBrowserPath("/api/healthz")).toBe("/api/healthz");
    expect(apiDocumentUrl(baseConfig, "/docs")).toBe("http://console.test/api/docs");
  });

  it("includes cookie credentials and keeps a real bearer token in memory only", async () => {
    const fetchMock = vi.mocked(fetch);
    await apiRequest(
      { ...baseConfig, adminToken: "ephemeral-token" },
      "/v1/admin/plugins/summary",
      { auth: true },
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "http://console.test/api/v1/admin/plugins/summary",
      expect.objectContaining({ credentials: "include" }),
    );
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("Authorization")).toBe(
      "Bearer ephemeral-token",
    );
  });

  it("uses the cookie session marker without sending it as a bearer credential", async () => {
    const fetchMock = vi.mocked(fetch);
    await apiBlobRequest(
      { ...baseConfig, adminToken: COOKIE_SESSION_MARKER },
      "/plugins/wxbot/admin/images/mid1.payload.signature",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "http://console.test/api/plugins/wxbot/admin/images/mid1.payload.signature",
      expect.objectContaining({ credentials: "include" }),
    );
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).has("Authorization")).toBe(false);
  });

  it("invalidates the global session only for an authentication 401, not an authorization 403", async () => {
    const onInvalid = vi.fn();
    window.addEventListener(AUTH_INVALID_EVENT, onInvalid);
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "admin access required" }), {
          status: 403,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: "invalid_or_expired_admin_session" }),
          {
            status: 401,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );

    try {
      await expect(
        apiRequest(baseConfig, "/plugins/wxbot/admin/sessions", { auth: true }),
      ).rejects.toMatchObject({ status: 403 });
      expect(onInvalid).not.toHaveBeenCalled();

      await expect(
        apiRequest(baseConfig, "/v1/admin/auth/session", { auth: true }),
      ).rejects.toMatchObject({ status: 401 });
      expect(onInvalid).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(AUTH_INVALID_EVENT, onInvalid);
    }
  });

  it("preserves ETags and sends optimistic-concurrency and idempotency headers", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ participation: 0.25 }), {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          ETag: '"policy-v7"',
        },
      }),
    );

    const result = await apiVersionedResource<
      { participation: number },
      { participation: number }
    >(baseConfig, "/v1/admin/tenants/default/groups/room/participation-policy", {
      auth: true,
      method: "PUT",
      body: { participation: 0.25 },
      ifMatch: '"policy-v6"',
      idempotencyKey: "policy-save-001",
    });

    expect(result).toEqual({
      value: { participation: 0.25 },
      etag: '"policy-v7"',
    });
    const request = vi.mocked(fetch).mock.calls[0]?.[1] as RequestInit;
    const headers = new Headers(request.headers);
    expect(headers.get("If-Match")).toBe('"policy-v6"');
    expect(headers.get("Idempotency-Key")).toBe("policy-save-001");
    expect(headers.get("Content-Type")).toBe("application/json");

    const loaded = markVersionedResourceLoaded(result.value, result.etag);
    expect(editVersionedResource(loaded, { participation: 0.4 })).toMatchObject({
      status: "loaded",
      dirty: true,
      etag: '"policy-v7"',
    });
  });

  it("surfaces a 409 as a typed version conflict without discarding the server ETag", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "resource_version_conflict" }), {
        status: 409,
        statusText: "Conflict",
        headers: {
          "Content-Type": "application/json",
          ETag: '"policy-v8"',
        },
      }),
    );

    const request = apiVersionedResource(baseConfig, "/v1/admin/versioned", {
      auth: true,
      method: "PATCH",
      body: { enabled: true },
      ifMatch: '"policy-v7"',
    });

    await expect(request).rejects.toMatchObject({
      name: "VersionConflictError",
      status: 409,
      serverEtag: '"policy-v8"',
    } satisfies Partial<VersionConflictError>);
  });

  it("keeps non-version 409 responses as ordinary API errors", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        detail: { code: "channel_connection_must_be_disabled_before_delete" },
      }), {
        status: 409,
        statusText: "Conflict",
        headers: { "Content-Type": "application/json" },
      }),
    );

    const request = apiVersionedResource(baseConfig, "/v1/admin/channel-connections/c1", {
      auth: true,
      method: "DELETE",
      ifMatch: '"4"',
    });

    await expect(request).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      message: "409 channel_connection_must_be_disabled_before_delete",
    });
  });

  it("renders structured API errors without object coercion", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        new Response(JSON.stringify({
          detail: { code: "channel_connection_operation_failed" },
        }), {
          status: 500,
          statusText: "Internal Server Error",
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({
          detail: { message: "connector is unavailable" },
        }), {
          status: 503,
          statusText: "Service Unavailable",
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({
          detail: { code: "export_failed" },
        }), {
          status: 500,
          statusText: "Internal Server Error",
          headers: { "Content-Type": "application/json" },
        }),
      );

    await expect(
      apiVersionedResource(baseConfig, "/v1/admin/channel-connections/c1/probe", {
        method: "POST",
      }),
    ).rejects.toMatchObject({
      message: "500 channel_connection_operation_failed",
    });
    await expect(
      apiRequest(baseConfig, "/v1/admin/channel-connections/c1"),
    ).rejects.toMatchObject({
      message: "503 connector is unavailable",
    });
    await expect(
      apiBlobRequest(baseConfig, "/v1/admin/export"),
    ).rejects.toMatchObject({
      message: "500 export_failed",
    });
  });
});

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiBlobRequest } from "../lib/api";
import {
  AuthenticatedImage,
  mediaStableKey,
  resetAuthenticatedImageCache,
  sdkImageDisplayPath,
  sdkImageProxyPath,
} from "./AuthenticatedImage";

const { mockConfig } = vi.hoisted(() => ({
  mockConfig: {
    apiBaseUrl: "http://localhost",
    adminToken: "test-admin-token",
    tenantId: "default",
    sessionId: "",
    userId: "operator",
  },
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, apiBlobRequest: vi.fn() };
});

vi.mock("../state/console-config", () => ({
  COOKIE_SESSION_MARKER: "__agent_console_cookie_session__",
  useConsoleConfig: () => ({ config: mockConfig }),
}));

const apiBlobRequestMock = vi.mocked(apiBlobRequest);

function encodeMid1(payload: Record<string, unknown>, signature = "sig") {
  const encoded = btoa(JSON.stringify(payload))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  return `mid1.${encoded}.${signature}`;
}

function signedImage(locator: string, expiresAt: number, signature = "sig") {
  return encodeMid1(
    {
      e: expiresAt,
      k: "sdk_path",
      l: locator,
      r: "image",
      t: "default",
      v: 1,
    },
    signature,
  );
}

function futureExpiry(offsetSeconds = 300) {
  return Math.floor(Date.now() / 1000) + offsetSeconds;
}

describe("AuthenticatedImage media identifiers", () => {
  const mediaId = "mid1.payload.signature";

  it("builds a proxy URL only from a signed media identifier", () => {
    expect(sdkImageProxyPath(mediaId)).toBe(
      "/plugins/wxbot/admin/images/mid1.payload.signature",
    );
    expect(sdkImageProxyPath(`media:${mediaId}`)).toBe(
      "/plugins/wxbot/admin/images/mid1.payload.signature",
    );
    expect(sdkImageDisplayPath(mediaId)).toBe("受保护媒体");
  });

  it("does not turn server paths or remote URLs into browser-loadable sources", () => {
    expect(sdkImageProxyPath("/images/generated/example.png")).toBe("");
    expect(sdkImageProxyPath("http://127.0.0.1:5080/images/example.png")).toBe("");
    expect(sdkImageProxyPath("../../etc/passwd")).toBe("");
  });

  it("keeps a stable locator key when only the signed expiry changes", () => {
    const first = signedImage("generated/example.png", 1_700_000_100);
    const second = signedImage("generated/example.png", 1_700_000_105, "other");
    expect(mediaStableKey(first)).toBe(mediaStableKey(`media:${second}`));
    expect(mediaStableKey(first)).toBe("mid1:default:sdk_path:image:generated/example.png");
    expect(mediaStableKey(signedImage("generated/other.png", 1_700_000_100))).not.toBe(
      mediaStableKey(first),
    );
  });
});

describe("AuthenticatedImage loading", () => {
  let nextObjectUrl = 0;

  beforeEach(() => {
    resetAuthenticatedImageCache();
    apiBlobRequestMock.mockReset();
    Object.assign(mockConfig, {
      apiBaseUrl: "http://localhost",
      adminToken: "test-admin-token",
      tenantId: "default",
      sessionId: "",
      userId: "operator",
    });
    nextObjectUrl = 0;
    URL.createObjectURL = vi.fn(() => `blob:test-image-${++nextObjectUrl}`);
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    resetAuthenticatedImageCache();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("deduplicates an in-flight request when only the signed media id rotates", async () => {
    let resolveBlob: ((blob: Blob) => void) | undefined;
    apiBlobRequestMock.mockImplementation(
      () =>
        new Promise<Blob>((resolve) => {
          resolveBlob = resolve;
        }),
    );

    const first = signedImage("generated/example.png", futureExpiry());
    const { rerender } = render(
      <AuthenticatedImage source={first} alt="消息图片" className="queue-image-preview" />,
    );
    expect(screen.getByText("加载中")).toBeInTheDocument();

    rerender(
      <AuthenticatedImage
        source={signedImage("generated/example.png", futureExpiry(600), "rotated")}
        alt="消息图片"
        className="queue-image-preview"
      />,
    );

    expect(apiBlobRequestMock).toHaveBeenCalledTimes(1);
    resolveBlob?.(new Blob(["image-bytes"], { type: "image/png" }));
    expect(await screen.findByRole("img", { name: "消息图片" })).toHaveAttribute(
      "src",
      "blob:test-image-1",
    );
  });

  it("reuses a fresh cached blob across remounts of the same locator", async () => {
    apiBlobRequestMock.mockResolvedValue(new Blob(["image-bytes"], { type: "image/png" }));
    const source = signedImage("generated/example.png", futureExpiry());
    const { unmount } = render(
      <AuthenticatedImage source={source} alt="消息图片" className="queue-image-preview" />,
    );
    await screen.findByRole("img", { name: "消息图片" });
    unmount();

    render(
      <AuthenticatedImage
        source={signedImage("generated/example.png", futureExpiry(600), "later")}
        alt="消息图片"
        className="queue-image-preview"
      />,
    );
    expect(await screen.findByRole("img", { name: "消息图片" })).toHaveAttribute(
      "src",
      "blob:test-image-1",
    );
    expect(apiBlobRequestMock).toHaveBeenCalledTimes(1);
  });

  it("isolates cache entries by backend, admin credential, tenant, and user", async () => {
    apiBlobRequestMock.mockResolvedValue(new Blob(["image-bytes"], { type: "image/png" }));
    const source = signedImage("generated/scoped.png", futureExpiry());
    const { rerender } = render(
      <AuthenticatedImage source={source} alt="隔离图片" className="queue-image-preview" />,
    );
    await screen.findByRole("img", { name: "隔离图片" });

    mockConfig.apiBaseUrl = "https://other-backend.example";
    rerender(<AuthenticatedImage source={source} alt="隔离图片" className="queue-image-preview" />);
    await waitFor(() => expect(screen.getByRole("img", { name: "隔离图片" })).toHaveAttribute("src", "blob:test-image-2"));

    mockConfig.adminToken = "other-admin-token";
    rerender(<AuthenticatedImage source={source} alt="隔离图片" className="queue-image-preview" />);
    await waitFor(() => expect(screen.getByRole("img", { name: "隔离图片" })).toHaveAttribute("src", "blob:test-image-3"));

    mockConfig.tenantId = "other-tenant";
    rerender(<AuthenticatedImage source={source} alt="隔离图片" className="queue-image-preview" />);
    await waitFor(() => expect(screen.getByRole("img", { name: "隔离图片" })).toHaveAttribute("src", "blob:test-image-4"));

    mockConfig.userId = "other-operator";
    rerender(<AuthenticatedImage source={source} alt="隔离图片" className="queue-image-preview" />);
    await waitFor(() => expect(screen.getByRole("img", { name: "隔离图片" })).toHaveAttribute("src", "blob:test-image-5"));
    expect(apiBlobRequestMock).toHaveBeenCalledTimes(5);
  });

  it("does not retain opaque cookie-session blobs after the last image unmounts", async () => {
    mockConfig.adminToken = "__agent_console_cookie_session__";
    apiBlobRequestMock.mockResolvedValue(new Blob(["image-bytes"], { type: "image/png" }));
    const source = signedImage("generated/cookie-session.png", futureExpiry());
    const first = render(
      <AuthenticatedImage source={source} alt="会话图片" className="queue-image-preview" />,
    );
    await screen.findByRole("img", { name: "会话图片" });

    first.unmount();
    await Promise.resolve();

    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test-image-1");
    render(<AuthenticatedImage source={source} alt="会话图片" className="queue-image-preview" />);
    await waitFor(() => expect(screen.getByRole("img", { name: "会话图片" })).toHaveAttribute("src", "blob:test-image-2"));
    expect(apiBlobRequestMock).toHaveBeenCalledTimes(2);
  });

  it("revalidates after the signed id expires and revokes the stale object URL", async () => {
    const nowMs = 1_900_000_000_000;
    const dateNow = vi.spyOn(Date, "now").mockReturnValue(nowMs);
    apiBlobRequestMock.mockResolvedValue(new Blob(["image-bytes"], { type: "image/png" }));
    const first = signedImage("generated/expiring.png", Math.floor(nowMs / 1000) + 30);
    const { unmount } = render(
      <AuthenticatedImage source={first} alt="过期图片" className="queue-image-preview" />,
    );
    await screen.findByRole("img", { name: "过期图片" });
    unmount();

    dateNow.mockReturnValue(nowMs + 31_000);
    render(
      <AuthenticatedImage
        source={signedImage("generated/expiring.png", Math.floor(nowMs / 1000) + 300, "rotated")}
        alt="过期图片"
        className="queue-image-preview"
      />,
    );

    await waitFor(() => expect(screen.getByRole("img", { name: "过期图片" })).toHaveAttribute("src", "blob:test-image-2"));
    expect(apiBlobRequestMock).toHaveBeenCalledTimes(2);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test-image-1");
  });

  it("periodically revalidates a still-mounted image instead of retaining it indefinitely", async () => {
    const nowMs = 1_900_000_000_000;
    vi.useFakeTimers();
    vi.setSystemTime(nowMs);
    apiBlobRequestMock.mockResolvedValue(new Blob(["image-bytes"], { type: "image/png" }));
    render(
      <AuthenticatedImage
        source={signedImage("generated/revalidate.png", Math.floor(nowMs / 1000) + 300)}
        alt="重验图片"
        className="queue-image-preview"
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole("img", { name: "重验图片" })).toHaveAttribute(
      "src",
      "blob:test-image-1",
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(apiBlobRequestMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("img", { name: "重验图片" })).toHaveAttribute(
      "src",
      "blob:test-image-2",
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test-image-1");
  });

  it("bounds the cache and revokes the least-recently-used object URL", async () => {
    apiBlobRequestMock.mockResolvedValue(new Blob(["image-bytes"], { type: "image/png" }));
    render(
      <>
        {Array.from({ length: 65 }, (_, index) => (
          <AuthenticatedImage
            key={index}
            source={signedImage(`generated/cache-${index}.png`, futureExpiry())}
            alt={`缓存图片 ${index}`}
            className="queue-image-preview"
          />
        ))}
      </>,
    );

    await waitFor(() => expect(apiBlobRequestMock).toHaveBeenCalledTimes(65));
    await screen.findByRole("img", { name: "缓存图片 64" });
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test-image-1");
  });

  it("revokes every cached object URL when the cache is reset", async () => {
    apiBlobRequestMock.mockResolvedValue(new Blob(["image-bytes"], { type: "image/png" }));
    render(
      <AuthenticatedImage
        source={signedImage("generated/reset.png", futureExpiry())}
        alt="重置图片"
        className="queue-image-preview"
      />,
    );
    await screen.findByRole("img", { name: "重置图片" });

    resetAuthenticatedImageCache();

    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test-image-1");
  });

  it("replaces a broken img element with the failure placeholder", async () => {
    apiBlobRequestMock.mockResolvedValue(new Blob(["broken-image"], { type: "image/png" }));
    render(
      <AuthenticatedImage
        source={signedImage("generated/broken.png", futureExpiry())}
        alt="损坏图片"
        className="queue-image-preview"
      />,
    );
    const image = await screen.findByRole("img", { name: "损坏图片" });

    fireEvent.error(image);

    expect(screen.queryByRole("img", { name: "损坏图片" })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("预览失败");
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test-image-1");
  });

  it("shows a failure state when the proxy cannot load the image", async () => {
    apiBlobRequestMock.mockRejectedValue(new Error("proxy failed"));
    render(
      <AuthenticatedImage
        source={signedImage("generated/missing.png", futureExpiry())}
        alt="消息图片"
        className="queue-image-preview"
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("预览失败");
  });
});

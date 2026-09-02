import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiBlobRequest } from "../lib/api";
import {
  AuthenticatedImage,
  mediaStableKey,
  resetAuthenticatedImageCache,
  sdkImageDisplayPath,
  sdkImageProxyPath,
} from "./AuthenticatedImage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, apiBlobRequest: vi.fn() };
});

vi.mock("../state/console-config", () => ({
  useConsoleConfig: () => ({
    config: {
      apiBaseUrl: "http://localhost",
      adminToken: "test-admin-token",
      tenantId: "default",
      sessionId: "",
      userId: "operator",
    },
  }),
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

  it("keeps a stable cache key when only the signed expiry changes", () => {
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
  beforeEach(() => {
    resetAuthenticatedImageCache();
    apiBlobRequestMock.mockReset();
    URL.createObjectURL = vi.fn(() => "blob:test-image");
  });

  afterEach(() => {
    resetAuthenticatedImageCache();
  });

  it("does not refetch when a refresh only rotates the signed media id", async () => {
    let resolveBlob: ((blob: Blob) => void) | undefined;
    apiBlobRequestMock.mockImplementation(
      () =>
        new Promise<Blob>((resolve) => {
          resolveBlob = resolve;
        }),
    );

    const first = signedImage("generated/example.png", 1_700_000_100);
    const { rerender } = render(
      <AuthenticatedImage source={first} alt="消息图片" className="queue-image-preview" />,
    );
    expect(screen.getByText("加载中")).toBeInTheDocument();

    rerender(
      <AuthenticatedImage
        source={signedImage("generated/example.png", 1_700_000_105, "rotated")}
        alt="消息图片"
        className="queue-image-preview"
      />,
    );

    expect(apiBlobRequestMock).toHaveBeenCalledTimes(1);
    resolveBlob?.(new Blob(["image-bytes"], { type: "image/png" }));
    expect(await screen.findByRole("img", { name: "消息图片" })).toHaveAttribute(
      "src",
      "blob:test-image",
    );
    expect(screen.queryByText("加载中")).not.toBeInTheDocument();
  });

  it("reuses a cached blob across remounts of the same locator", async () => {
    apiBlobRequestMock.mockResolvedValue(new Blob(["image-bytes"], { type: "image/png" }));
    const source = signedImage("generated/example.png", 1_700_000_100);
    const { unmount } = render(
      <AuthenticatedImage source={source} alt="消息图片" className="queue-image-preview" />,
    );
    await screen.findByRole("img", { name: "消息图片" });
    unmount();

    render(
      <AuthenticatedImage
        source={signedImage("generated/example.png", 1_700_000_200, "later")}
        alt="消息图片"
        className="queue-image-preview"
      />,
    );
    expect(screen.getByRole("img", { name: "消息图片" })).toHaveAttribute("src", "blob:test-image");
    expect(apiBlobRequestMock).toHaveBeenCalledTimes(1);
  });

  it("shows a failure state when the proxy cannot load the image", async () => {
    apiBlobRequestMock.mockRejectedValue(new Error("proxy failed"));
    render(
      <AuthenticatedImage
        source={signedImage("generated/missing.png", 1_700_000_100)}
        alt="消息图片"
        className="queue-image-preview"
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("预览失败");
  });
});

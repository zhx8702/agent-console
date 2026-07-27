import { describe, expect, it } from "vitest";

import { sdkImageDisplayPath, sdkImageProxyPath } from "./AuthenticatedImage";

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
});

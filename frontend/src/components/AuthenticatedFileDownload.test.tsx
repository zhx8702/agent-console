import { describe, expect, it } from "vitest";

import { sdkFileProxyPath } from "./AuthenticatedFileDownload";

describe("sdkFileProxyPath", () => {
  it("builds the authenticated wxbot file proxy path", () => {
    const mediaId = "mid1.payload.signature";
    expect(sdkFileProxyPath(mediaId)).toBe(
      "/plugins/wxbot/admin/files/mid1.payload.signature",
    );
    expect(sdkFileProxyPath(`file-media:${mediaId}`)).toBe(
      "/plugins/wxbot/admin/files/mid1.payload.signature",
    );
  });

  it("rejects raw SDK paths and URLs", () => {
    expect(sdkFileProxyPath("/files/incoming/report.pdf")).toBe("");
    expect(sdkFileProxyPath("E:\\wxbot-files\\report.pdf")).toBe("");
  });
});

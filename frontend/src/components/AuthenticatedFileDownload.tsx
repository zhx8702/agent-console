import { useState } from "react";

import { apiBlobRequest } from "../lib/api";
import { useConsoleConfig } from "../state/console-config";

type AuthenticatedFileDownloadProps = {
  source: string;
  fileName: string;
};

function fileMediaId(source: string) {
  const value = source.trim();
  const mediaId = value.startsWith("file-media:")
    ? value.slice("file-media:".length)
    : value;
  return /^mid1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(mediaId) ? mediaId : "";
}

export function sdkFileProxyPath(source: string) {
  const mediaId = fileMediaId(source);
  return mediaId
    ? `/plugins/wxbot/admin/files/${encodeURIComponent(mediaId)}`
    : "";
}

export function AuthenticatedFileDownload({
  source,
  fileName,
}: AuthenticatedFileDownloadProps) {
  const { config } = useConsoleConfig();
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const proxyPath = sdkFileProxyPath(source);

  const download = async () => {
    if (!proxyPath || downloading) {
      return;
    }
    setDownloading(true);
    setError("");
    try {
      const blob = await apiBlobRequest(config, proxyPath);
      const objectUrl = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement("a");
        anchor.href = objectUrl;
        anchor.download = fileName.trim() || "download";
        anchor.rel = "noopener";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      } finally {
        URL.revokeObjectURL(objectUrl);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "文件下载失败");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <span>
      <button
        className="button button-secondary"
        type="button"
        disabled={!proxyPath || downloading}
        onClick={() => void download()}
      >
        {downloading ? "下载中…" : "下载文件"}
      </button>
      {error && (
        <span className="muted-copy" role="alert">
          {error}
        </span>
      )}
    </span>
  );
}

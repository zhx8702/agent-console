import { useEffect, useMemo, useRef, useState } from "react";

import { apiBlobRequest } from "../lib/api";
import { useConsoleConfig } from "../state/console-config";

type AuthenticatedImageProps = {
  source: string;
  alt: string;
  className: string;
  loading?: "eager" | "lazy";
};

function mediaIdFromLocator(source: string) {
  const value = source.trim();
  if (!value) {
    return "";
  }
  const mediaId = value.startsWith("media:") ? value.slice("media:".length) : value;
  return /^mid1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(mediaId) ? mediaId : "";
}

export function sdkImageProxyPath(source: string) {
  const mediaId = mediaIdFromLocator(source);
  if (!mediaId) {
    return "";
  }
  return `/plugins/wxbot/admin/images/${encodeURIComponent(mediaId)}`;
}

export function sdkImageDisplayPath(source: string) {
  return mediaIdFromLocator(source) ? "受保护媒体" : source;
}

export function AuthenticatedImage({ source, alt, className, loading = "eager" }: AuthenticatedImageProps) {
  const { config } = useConsoleConfig();
  const hostRef = useRef<HTMLSpanElement>(null);
  const proxyPath = useMemo(() => sdkImageProxyPath(source), [source]);
  const [visible, setVisible] = useState(loading !== "lazy" || !proxyPath);
  const [blobUrl, setBlobUrl] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setVisible(loading !== "lazy" || !proxyPath);
    if (loading !== "lazy" || !proxyPath || !hostRef.current || !("IntersectionObserver" in window)) {
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "240px" },
    );
    observer.observe(hostRef.current);
    return () => observer.disconnect();
  }, [loading, proxyPath]);

  useEffect(() => {
    setBlobUrl("");
    setFailed(Boolean(source.trim()) && !proxyPath);
    if (!proxyPath || !visible) {
      return;
    }
    const controller = new AbortController();
    let objectUrl = "";
    void apiBlobRequest(config, proxyPath, { signal: controller.signal })
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setFailed(true);
        }
      });
    return () => {
      controller.abort();
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [config.apiBaseUrl, config.adminToken, proxyPath, source, visible]);

  const displaySource = proxyPath ? blobUrl : "";
  return (
    <span
      ref={hostRef}
      className={`authenticated-image-shell ${className}-shell${failed ? " is-error" : ""}`}
    >
      {displaySource ? (
        <img className={className} src={displaySource} alt={alt} loading={loading} />
      ) : (
        <span className="authenticated-image-placeholder" role={failed ? "alert" : undefined}>
          {failed ? "预览失败" : "加载中"}
        </span>
      )}
    </span>
  );
}

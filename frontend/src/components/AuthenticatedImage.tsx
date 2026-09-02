import { useEffect, useMemo, useRef, useState } from "react";

import { apiBlobRequest } from "../lib/api";
import { useConsoleConfig } from "../state/console-config";

type AuthenticatedImageProps = {
  source: string;
  alt: string;
  className: string;
  loading?: "eager" | "lazy";
};

const blobUrlCache = new Map<string, string>();
const inflight = new Map<string, Promise<string>>();

function mediaIdFromLocator(source: string) {
  const value = source.trim();
  if (!value) {
    return "";
  }
  const mediaId = value.startsWith("media:") ? value.slice("media:".length) : value;
  return /^mid1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(mediaId) ? mediaId : "";
}

function decodeMid1Payload(mediaId: string): Record<string, unknown> | null {
  const parts = mediaId.split(".");
  if (parts.length < 3 || parts[0] !== "mid1") {
    return null;
  }
  try {
    const padded = parts[1] + "=".repeat((4 - (parts[1].length % 4)) % 4);
    const json = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
    const payload = JSON.parse(json) as unknown;
    if (!payload || typeof payload !== "object") {
      return null;
    }
    return payload as Record<string, unknown>;
  } catch {
    return null;
  }
}

export function mediaStableKey(source: string) {
  const trimmed = source.trim();
  const mediaId = mediaIdFromLocator(trimmed);
  if (!mediaId) {
    return trimmed;
  }
  const payload = decodeMid1Payload(mediaId);
  if (!payload || typeof payload.l !== "string" || !payload.l) {
    return mediaId;
  }
  const tenant = typeof payload.t === "string" ? payload.t : "";
  const kind = typeof payload.k === "string" ? payload.k : "";
  const role = typeof payload.r === "string" ? payload.r : "image";
  return `mid1:${tenant}:${kind}:${role}:${payload.l}`;
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

export function resetAuthenticatedImageCache() {
  blobUrlCache.clear();
  inflight.clear();
}

function loadAuthenticatedBlob(cacheKey: string, proxyPath: string, config: ReturnType<typeof useConsoleConfig>["config"]) {
  const pending = inflight.get(cacheKey);
  if (pending) {
    return pending;
  }
  const request = apiBlobRequest(config, proxyPath)
    .then((blob) => {
      const objectUrl = URL.createObjectURL(blob);
      blobUrlCache.set(cacheKey, objectUrl);
      return objectUrl;
    })
    .finally(() => {
      inflight.delete(cacheKey);
    });
  inflight.set(cacheKey, request);
  return request;
}

export function AuthenticatedImage({ source, alt, className, loading = "eager" }: AuthenticatedImageProps) {
  const { config } = useConsoleConfig();
  const hostRef = useRef<HTMLSpanElement>(null);
  const cacheKey = useMemo(() => mediaStableKey(source), [source]);
  const proxyPath = useMemo(() => sdkImageProxyPath(source), [source]);
  const proxyPathRef = useRef(proxyPath);
  const configRef = useRef(config);
  proxyPathRef.current = proxyPath;
  configRef.current = config;
  const [visible, setVisible] = useState(loading !== "lazy" || !cacheKey);
  const [blobUrl, setBlobUrl] = useState(() => blobUrlCache.get(cacheKey) || "");
  const [failed, setFailed] = useState(Boolean(source.trim()) && !proxyPath);

  useEffect(() => {
    if (loading !== "lazy" || !cacheKey) {
      setVisible(true);
      return;
    }
    if (!hostRef.current || !("IntersectionObserver" in window)) {
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
  }, [loading, cacheKey]);

  useEffect(() => {
    if (!cacheKey) {
      setBlobUrl("");
      setFailed(false);
      return;
    }
    if (!proxyPathRef.current) {
      setBlobUrl("");
      setFailed(true);
      return;
    }
    const cached = blobUrlCache.get(cacheKey);
    if (cached) {
      setBlobUrl(cached);
      setFailed(false);
      return;
    }
    if (!visible) {
      return;
    }
    if (!inflight.has(cacheKey)) {
      setBlobUrl("");
    }
    setFailed(false);
    let cancelled = false;
    void loadAuthenticatedBlob(cacheKey, proxyPathRef.current, configRef.current)
      .then((objectUrl) => {
        if (!cancelled) {
          setBlobUrl(objectUrl);
          setFailed(false);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setFailed(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [cacheKey, config.adminToken, config.apiBaseUrl, visible]);

  const displaySource = proxyPath ? blobUrl : "";
  return (
    <span
      ref={hostRef}
      className={`authenticated-image-shell ${className}-shell${failed ? " is-error" : ""}`}
    >
      {displaySource ? (
        <img
          className={className}
          src={displaySource}
          alt={alt}
          loading={loading}
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="authenticated-image-placeholder" role={failed ? "alert" : undefined}>
          {failed ? "预览失败" : "加载中"}
        </span>
      )}
    </span>
  );
}

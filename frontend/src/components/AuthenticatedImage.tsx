import { useEffect, useMemo, useRef, useState } from "react";

import { apiBlobRequest } from "../lib/api";
import {
  COOKIE_SESSION_MARKER,
  useConsoleConfig,
  type ConsoleConfig,
} from "../state/console-config";

type AuthenticatedImageProps = {
  source: string;
  alt: string;
  className: string;
  loading?: "eager" | "lazy";
};

type CachedBlob = {
  objectUrl: string;
  expiresAtMs: number;
};

type DisplayedBlob = CachedBlob & {
  cacheKey: string;
};

const MAX_BLOB_CACHE_ENTRIES = 64;
const MAX_BLOB_CACHE_AGE_MS = 60_000;
const blobUrlCache = new Map<string, CachedBlob>();
const inflight = new Map<string, Promise<CachedBlob>>();
let cacheGeneration = 0;
let cookieSessionConsumers = 0;
let cookieSessionCleanupVersion = 0;

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

function mediaExpiryMs(source: string) {
  const mediaId = mediaIdFromLocator(source);
  const payload = mediaId ? decodeMid1Payload(mediaId) : null;
  const expiresAtSeconds = payload?.e;
  if (
    typeof expiresAtSeconds !== "number" ||
    !Number.isSafeInteger(expiresAtSeconds) ||
    expiresAtSeconds <= 0
  ) {
    return null;
  }
  return expiresAtSeconds * 1000;
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

function normalizedApiBaseUrl(value: string) {
  const trimmed = value.trim();
  try {
    return new URL(trimmed).origin;
  } catch {
    return trimmed;
  }
}

function scopedCacheKey(stableKey: string, config: ConsoleConfig) {
  if (!stableKey) {
    return "";
  }
  return JSON.stringify([
    normalizedApiBaseUrl(config.apiBaseUrl),
    config.adminToken.trim(),
    config.tenantId.trim(),
    config.userId.trim(),
    stableKey,
  ]);
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

function revokeObjectUrl(objectUrl: string) {
  URL.revokeObjectURL(objectUrl);
}

function removeCachedBlob(cacheKey: string, expectedObjectUrl?: string) {
  const cached = blobUrlCache.get(cacheKey);
  if (!cached || (expectedObjectUrl && cached.objectUrl !== expectedObjectUrl)) {
    return;
  }
  blobUrlCache.delete(cacheKey);
  revokeObjectUrl(cached.objectUrl);
}

function getCachedBlob(cacheKey: string, source: string) {
  const cached = blobUrlCache.get(cacheKey);
  if (!cached) {
    return null;
  }
  const sourceExpiryMs = mediaExpiryMs(source);
  if (
    cached.expiresAtMs <= Date.now() ||
    (sourceExpiryMs !== null && sourceExpiryMs <= Date.now())
  ) {
    removeCachedBlob(cacheKey, cached.objectUrl);
    return null;
  }
  // Map insertion order is the LRU order. A hit promotes the entry.
  blobUrlCache.delete(cacheKey);
  blobUrlCache.set(cacheKey, cached);
  return cached;
}

function cacheBlob(cacheKey: string, cached: CachedBlob) {
  removeCachedBlob(cacheKey);
  blobUrlCache.set(cacheKey, cached);
  while (blobUrlCache.size > MAX_BLOB_CACHE_ENTRIES) {
    const oldestKey = blobUrlCache.keys().next().value as string | undefined;
    if (!oldestKey) {
      break;
    }
    removeCachedBlob(oldestKey);
  }
}

export function resetAuthenticatedImageCache() {
  cacheGeneration += 1;
  for (const cached of blobUrlCache.values()) {
    revokeObjectUrl(cached.objectUrl);
  }
  blobUrlCache.clear();
  inflight.clear();
}

function loadAuthenticatedBlob(
  cacheKey: string,
  proxyPath: string,
  source: string,
  config: ConsoleConfig,
) {
  const pending = inflight.get(cacheKey);
  if (pending) {
    return pending;
  }
  const generation = cacheGeneration;
  const request = apiBlobRequest(config, proxyPath)
    .then((blob) => {
      if (generation !== cacheGeneration) {
        throw new DOMException("Authenticated image cache was reset", "AbortError");
      }
      const now = Date.now();
      const signedExpiryMs = mediaExpiryMs(source);
      const expiresAtMs = Math.min(
        now + MAX_BLOB_CACHE_AGE_MS,
        signedExpiryMs ?? Number.POSITIVE_INFINITY,
      );
      if (expiresAtMs <= now) {
        throw new Error("Signed media identifier has expired");
      }
      const cached = { objectUrl: URL.createObjectURL(blob), expiresAtMs };
      cacheBlob(cacheKey, cached);
      return cached;
    })
    .finally(() => {
      if (inflight.get(cacheKey) === request) {
        inflight.delete(cacheKey);
      }
    });
  inflight.set(cacheKey, request);
  return request;
}

export function AuthenticatedImage({ source, alt, className, loading = "eager" }: AuthenticatedImageProps) {
  const { config } = useConsoleConfig();
  const hostRef = useRef<HTMLSpanElement>(null);
  const stableKey = useMemo(() => mediaStableKey(source), [source]);
  const cacheKey = useMemo(
    () => scopedCacheKey(stableKey, config),
    [stableKey, config.adminToken, config.apiBaseUrl, config.tenantId, config.userId],
  );
  const proxyPath = useMemo(() => sdkImageProxyPath(source), [source]);
  const [visible, setVisible] = useState(loading !== "lazy" || !stableKey);
  const [displayedBlob, setDisplayedBlob] = useState<DisplayedBlob | null>(null);
  const [failedCacheKey, setFailedCacheKey] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!cacheKey || config.adminToken.trim() !== COOKIE_SESSION_MARKER) {
      return;
    }
    cookieSessionConsumers += 1;
    cookieSessionCleanupVersion += 1;
    return () => {
      cookieSessionConsumers -= 1;
      const cleanupVersion = ++cookieSessionCleanupVersion;
      queueMicrotask(() => {
        if (cookieSessionConsumers === 0 && cleanupVersion === cookieSessionCleanupVersion) {
          // The cookie is HttpOnly, so its administrator identity is intentionally opaque.
          // Do not let cache entries survive the last image consumer (for example, logout).
          resetAuthenticatedImageCache();
        }
      });
    };
  }, [cacheKey, config.adminToken]);

  useEffect(() => {
    if (loading !== "lazy" || !stableKey) {
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
  }, [loading, stableKey]);

  useEffect(() => {
    if (!cacheKey) {
      setDisplayedBlob(null);
      setFailedCacheKey(null);
      return;
    }
    if (!proxyPath) {
      setDisplayedBlob(null);
      setFailedCacheKey(cacheKey);
      return;
    }
    const cached = getCachedBlob(cacheKey, source);
    if (cached) {
      setDisplayedBlob({ cacheKey, ...cached });
      setFailedCacheKey(null);
      return;
    }
    if (!visible) {
      return;
    }
    setDisplayedBlob(null);
    setFailedCacheKey(null);
    let cancelled = false;
    void loadAuthenticatedBlob(cacheKey, proxyPath, source, config)
      .then((loaded) => {
        if (!cancelled) {
          setDisplayedBlob({ cacheKey, ...loaded });
          setFailedCacheKey(null);
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          setDisplayedBlob(null);
          setFailedCacheKey(cacheKey);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [cacheKey, proxyPath, reloadKey, source, visible]);

  const currentBlob = displayedBlob?.cacheKey === cacheKey ? displayedBlob : null;

  useEffect(() => {
    if (!currentBlob) {
      return;
    }
    const expire = () => {
      removeCachedBlob(cacheKey, currentBlob.objectUrl);
      setDisplayedBlob((current) =>
        current?.cacheKey === cacheKey && current.objectUrl === currentBlob.objectUrl ? null : current,
      );
      setReloadKey((current) => current + 1);
    };
    const remainingMs = currentBlob.expiresAtMs - Date.now();
    if (remainingMs <= 0) {
      expire();
      return;
    }
    const timer = window.setTimeout(expire, remainingMs);
    return () => window.clearTimeout(timer);
  }, [cacheKey, currentBlob]);

  const failed = failedCacheKey === cacheKey;
  const displaySource = proxyPath && !failed ? currentBlob?.objectUrl || "" : "";
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
          onError={() => {
            removeCachedBlob(cacheKey, displaySource);
            setDisplayedBlob(null);
            setFailedCacheKey(cacheKey);
          }}
        />
      ) : (
        <span className="authenticated-image-placeholder" role={failed ? "alert" : undefined}>
          {failed ? "预览失败" : "加载中"}
        </span>
      )}
    </span>
  );
}

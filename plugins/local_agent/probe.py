"""Cached sidecar probe for grok / Codex."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from plugins.local_agent.client import LocalAgentClient, LocalAgentClientError
from plugins.local_agent.sidecar.backends import BACKENDS


@dataclass(frozen=True)
class BackendStatus:
    name: str
    ok: bool
    executable: str = ""
    version: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "executable": self.executable,
            "version": self.version,
            "error": self.error,
        }


@dataclass(frozen=True)
class ProbeSnapshot:
    ok: bool
    configured: bool
    error: str
    backends: dict[str, BackendStatus]
    probed_at: float

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "configured": self.configured,
            "error": self.error,
            "backends": {name: item.as_dict() for name, item in self.backends.items()},
            "probed_at": self.probed_at,
        }

    def backend(self, name: str) -> BackendStatus:
        key = str(name or "").strip().lower()
        return self.backends.get(
            key,
            BackendStatus(name=key, ok=False, error="unknown_backend"),
        )


class LocalAgentProbe:
    def __init__(self, client: LocalAgentClient, settings: Any) -> None:
        self._client = client
        self._ttl = max(
            1.0,
            float(getattr(settings, "local_agent_probe_cache_seconds", 15.0) or 15.0),
        )
        self._cached: ProbeSnapshot | None = None

    def _empty(self, *, configured: bool, error: str) -> ProbeSnapshot:
        return ProbeSnapshot(
            ok=False,
            configured=configured,
            error=error,
            backends={
                name: BackendStatus(name=name, ok=False, error=error)
                for name in BACKENDS
            },
            probed_at=time.time(),
        )

    async def snapshot(self, *, force: bool = False) -> ProbeSnapshot:
        now = time.time()
        if (
            not force
            and self._cached is not None
            and now - self._cached.probed_at < self._ttl
        ):
            return self._cached
        if not self._client.configured:
            snapshot = self._empty(configured=False, error="not_configured")
            self._cached = snapshot
            return snapshot
        try:
            payload = await self._client.backends()
        except LocalAgentClientError as exc:
            snapshot = self._empty(configured=True, error=exc.code)
            self._cached = snapshot
            return snapshot
        except Exception as exc:
            snapshot = self._empty(configured=True, error=exc.__class__.__name__)
            self._cached = snapshot
            return snapshot
        raw_backends = payload.get("backends") if isinstance(payload, dict) else {}
        backends: dict[str, BackendStatus] = {}
        for name in BACKENDS:
            item = raw_backends.get(name) if isinstance(raw_backends, dict) else None
            record = item if isinstance(item, dict) else {}
            backends[name] = BackendStatus(
                name=name,
                ok=bool(record.get("ok")),
                executable=str(record.get("executable") or ""),
                version=str(record.get("version") or ""),
                error=str(record.get("error") or ""),
            )
        snapshot = ProbeSnapshot(
            ok=any(item.ok for item in backends.values()),
            configured=True,
            error="",
            backends=backends,
            probed_at=now,
        )
        self._cached = snapshot
        return snapshot

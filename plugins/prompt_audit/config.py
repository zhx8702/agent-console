"""Immutable configuration and compare-and-swap snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from plugins.prompt_audit.contracts import AuditMode


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    id: str
    base_url: str
    model: str = "sileader/qwen3guard:0.6b"
    api_key: str = field(default="", repr=False)
    enabled: bool = True
    priority: int = 100
    timeout_seconds: float = 8.0
    input_limit: int = 4096
    allowed_hosts: tuple[str, ...] = ()
    allowed_private_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("id", "base_url", "model", "api_key"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"prompt-audit endpoint {name} must be a string")
        if type(self.enabled) is not bool:
            raise TypeError("prompt-audit endpoint enabled must be a boolean")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise TypeError("prompt-audit endpoint priority must be an integer")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds,
            (int, float),
        ):
            raise TypeError("prompt-audit endpoint timeout_seconds must be numeric")
        if isinstance(self.input_limit, bool) or not isinstance(self.input_limit, int):
            raise TypeError("prompt-audit endpoint input_limit must be an integer")
        if any(not isinstance(value, str) for value in self.allowed_hosts):
            raise TypeError("prompt-audit allowed_hosts values must be strings")
        if any(not isinstance(value, str) for value in self.allowed_private_origins):
            raise TypeError("prompt-audit allowed_private_origins values must be strings")
        object.__setattr__(self, "allowed_hosts", tuple(self.allowed_hosts))
        object.__setattr__(
            self,
            "allowed_private_origins",
            tuple(self.allowed_private_origins),
        )
        if not str(self.id or "").strip():
            raise ValueError("prompt-audit endpoint id cannot be empty")
        if not str(self.base_url or "").strip():
            raise ValueError("prompt-audit endpoint base_url cannot be empty")
        if not str(self.model or "").strip():
            raise ValueError("prompt-audit endpoint model cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("prompt-audit endpoint timeout_seconds must be positive")
        if self.input_limit <= 0:
            raise ValueError("prompt-audit endpoint input_limit must be positive")


@dataclass(frozen=True, slots=True)
class PromptAuditConfig:
    enabled: bool = False
    mode: AuditMode = AuditMode.OFF
    version: int = 1
    endpoints: tuple[EndpointConfig, ...] = ()
    total_timeout_seconds: float = 12.0
    observe_enqueue_timeout_seconds: float = 0.5
    event_record_timeout_seconds: float = 0.5
    preview_chars: int = 240
    max_input_chars: int = 65_536
    max_prior_segments: int = 32
    max_chunks: int = 256
    store_pass_events: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("prompt-audit enabled must be a boolean")
        if not isinstance(self.mode, AuditMode):
            raise TypeError("prompt-audit mode must be an AuditMode")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("prompt-audit config version must be an integer")
        if any(not isinstance(endpoint, EndpointConfig) for endpoint in self.endpoints):
            raise TypeError("prompt-audit endpoints must contain EndpointConfig values")
        object.__setattr__(self, "endpoints", tuple(self.endpoints))
        for name in (
            "total_timeout_seconds",
            "observe_enqueue_timeout_seconds",
            "event_record_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"prompt-audit {name} must be numeric")
            if value <= 0:
                raise ValueError(f"prompt-audit {name} must be positive")
        for name in ("preview_chars", "max_input_chars", "max_prior_segments", "max_chunks"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"prompt-audit {name} must be an integer")
        if type(self.store_pass_events) is not bool:
            raise TypeError("prompt-audit store_pass_events must be a boolean")
        if self.version < 1:
            raise ValueError("prompt-audit config version must be positive")
        if self.preview_chars < 0:
            raise ValueError("prompt-audit preview_chars cannot be negative")
        if self.max_input_chars < 1 or self.max_prior_segments < 0 or self.max_chunks < 1:
            raise ValueError("prompt-audit input and chunk limits are invalid")
        if self.mode != AuditMode.OFF and not self.enabled:
            raise ValueError("prompt-audit observe/blocking mode requires enabled=true")

    @property
    def effective_mode(self) -> AuditMode:
        return self.mode if self.enabled else AuditMode.OFF

    @property
    def active_endpoints(self) -> tuple[EndpointConfig, ...]:
        return tuple(
            sorted(
                (endpoint for endpoint in self.endpoints if endpoint.enabled),
                key=lambda endpoint: (endpoint.priority, endpoint.id),
            )
        )


class ConfigConflictError(RuntimeError):
    pass


class ConfigSnapshot:
    """Small in-memory CAS store; persistence is deliberately an adapter concern."""

    def __init__(self, initial: PromptAuditConfig | None = None) -> None:
        self._lock = RLock()
        self._value = initial or PromptAuditConfig()
        self._history = {self._value.version: self._value}

    def get(self, version: int | None = None) -> PromptAuditConfig:
        with self._lock:
            if version is None:
                return self._value
            try:
                return self._history[version]
            except KeyError as exc:
                raise ConfigVersionUnavailable("prompt_audit_config_version_unavailable") from exc

    def replace(
        self,
        config: PromptAuditConfig,
        *,
        expected_version: int | None = None,
    ) -> PromptAuditConfig:
        with self._lock:
            if expected_version is not None and self._value.version != expected_version:
                raise ConfigConflictError("prompt_audit_config_conflict")
            if config.version <= self._value.version:
                raise ValueError("replacement config version must increase")
            self._value = config
            self._history[config.version] = config
            return self._value


class ConfigVersionUnavailable(LookupError):
    pass

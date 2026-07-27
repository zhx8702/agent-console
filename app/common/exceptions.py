from __future__ import annotations


class CSError(Exception):
    """Base class for all customer service system errors."""

    code: str = "cs_error"


class ValidationError(CSError):
    code = "validation_error"


class SignatureError(CSError):
    code = "signature_error"


class RateLimited(CSError):
    code = "rate_limited"


class IdempotencyReplay(CSError):
    code = "idempotency_replay"


class UpstreamUnavailable(CSError):
    code = "upstream_unavailable"


class SessionLockLost(CSError):
    """The worker no longer owns the distributed session lease."""

    code = "session_lock_lost"


class ConfigError(CSError):
    code = "config_error"


class CapabilityError(CSError):
    code = "capability_error"


class SafetyBlocked(CSError):
    code = "safety_blocked"


class ToolError(CSError):
    code = "tool_error"


class QuotaExceeded(CSError):
    code = "quota_exceeded"

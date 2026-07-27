from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, cast

import structlog

from app.common.config import get_settings
from app.common.context import get_tenant_id, get_trace_id

_REDACTED = "[redacted]"
_MAX_REDACTION_DEPTH = 12
_SENSITIVE_LOG_KEYS = frozenset(
    {
        "activation_code",
        "address",
        "auth",
        "authorization",
        "command",
        "content",
        "cookie",
        "customer",
        "decrypted_dir",
        "delivery",
        "detail",
        "details",
        "disk_serial",
        "envelope",
        "error",
        "errors",
        "exception",
        "exc_info",
        "file_name",
        "filename",
        "fingerprint",
        "headers",
        "hostname",
        "identity",
        "image_path",
        "machine_guid",
        "media_path",
        "message",
        "msg_text",
        "my_names",
        "name",
        "normalized",
        "original",
        "password",
        "path",
        "payload",
        "prompt",
        "reply_text",
        "runtime",
        "secret",
        "self_wxid",
        "sender_name",
        "sender_wxid",
        "session",
        "session_id",
        "session_name",
        "source_message",
        "stack",
        "stack_info",
        "system_root",
        "text",
        "token",
        "uri",
        "url",
        "user_wxid",
        "wechat_data_dir",
        "wxid",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "_address",
    "_cookie",
    "_headers",
    "_name",
    "_names",
    "_password",
    "_path",
    "_payload",
    "_prompt",
    "_secret",
    "_text",
    "_token",
    "_uri",
    "_url",
    "_wxid",
)
_INLINE_PRIVATE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[^\s,;]+"),
    re.compile(r"(?i)\b(?:token|secret|password|activation_code)=([^&\s]+)"),
    re.compile(r"(?i)https?://[^/@\s:]+:[^/@\s]+@"),
    re.compile(r"(?i)\bwxid_[a-z0-9_-]+\b"),
    re.compile(r"\b\d{5,}@chatroom\b", re.IGNORECASE),
    re.compile(r"(?<![\w.])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.])"),
    re.compile(r"(?<!\w)1[3-9]\d{9}(?!\w)"),
    re.compile(r"(?<!\w)[A-Za-z]:\\[^\r\n]*"),
)


def _is_sensitive_log_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_LOG_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _redact_inline_private_values(value: str) -> str:
    redacted = value
    for pattern in _INLINE_PRIVATE_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def _redact_log_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= _MAX_REDACTION_DEPTH:
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(key): (
                _REDACTED
                if _is_sensitive_log_key(key)
                else _redact_log_value(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_log_value(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item, depth=depth + 1) for item in value)
    if isinstance(value, str):
        return _redact_inline_private_values(value)
    return value


def redact_log_event(
    _logger: Any,
    _name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Remove private values from a structured event immediately before rendering."""

    return cast(MutableMapping[str, Any], _redact_log_value(event_dict))


def _inject_context(
    _logger: Any,
    _name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    tid = get_trace_id()
    tenant = get_tenant_id()
    if tid:
        event_dict.setdefault("trace_id", tid)
    if tenant:
        event_dict.setdefault("tenant_id", tenant)
    return event_dict


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.app_log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_log_event,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))

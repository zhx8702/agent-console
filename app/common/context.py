from __future__ import annotations

from contextvars import ContextVar

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)


def set_trace_id(value: str | None) -> None:
    _trace_id.set(value)


def get_trace_id() -> str | None:
    return _trace_id.get()


def set_tenant_id(value: str | None) -> None:
    _tenant_id.set(value)


def get_tenant_id() -> str | None:
    return _tenant_id.get()


def set_session_id(value: str | None) -> None:
    _session_id.set(value)


def get_session_id() -> str | None:
    return _session_id.get()


def clear_context() -> None:
    _trace_id.set(None)
    _tenant_id.set(None)
    _session_id.set(None)

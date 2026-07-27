"""
Runtime capability guard.

Delegates to client.runtime_guard.RuntimeAuthGuard which validates
capabilities against the remote auth server's signed session.
All sealed_core modules call require_capability() before executing
protected operations.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_guard = None


class CapabilityError(RuntimeError):
    pass


def install_guard(guard) -> None:
    global _guard
    with _lock:
        _guard = guard


def require_capability(name: str) -> None:
    with _lock:
        g = _guard
    if g is None:
        raise CapabilityError("no active session — authenticate first")
    try:
        g.require(name)
    except Exception as e:
        raise CapabilityError(str(e)) from e


def is_active() -> bool:
    with _lock:
        g = _guard
    if g is None:
        return False
    snap = g.status_snapshot()
    return snap.get("has_session", False)


def has_capability(name: str) -> bool:
    with _lock:
        g = _guard
    if g is None:
        return False
    try:
        g.require(name)
        return True
    except Exception:
        return False

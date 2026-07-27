"""
Session state machine helpers.

Defines the legal transitions between SessionState values. The orchestrator and
SessionManager rely on :func:`assert_can_transition` before persisting a state
change so we never silently corrupt session lifecycle semantics.
"""
from __future__ import annotations

from app.common.types import SessionState

# Legal transitions. CLOSED is terminal — re-engagement should create a new
# session (caller's responsibility) rather than reopening an existing one.
ALLOWED_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.IDLE: {SessionState.CHATTING, SessionState.ESCALATED, SessionState.CLOSED},
    SessionState.CHATTING: {
        SessionState.AWAITING_INFO,
        SessionState.ESCALATED,
        SessionState.CLOSED,
    },
    SessionState.AWAITING_INFO: {
        SessionState.CHATTING,
        SessionState.ESCALATED,
        SessionState.CLOSED,
    },
    SessionState.ESCALATED: {SessionState.CHATTING, SessionState.CLOSED},
    SessionState.CLOSED: set(),
}


def can_transition(current: SessionState, new: SessionState) -> bool:
    """Return True if transitioning from ``current`` to ``new`` is legal.

    Self-transitions (``current == new``) are always allowed as no-ops.
    """
    if current == new:
        return True
    return new in ALLOWED_TRANSITIONS.get(current, set())


def assert_can_transition(current: SessionState, new: SessionState) -> None:
    """Raise :class:`ValueError` if the state transition is not permitted."""
    if not can_transition(current, new):
        raise ValueError(
            f"illegal session state transition: {current.value} -> {new.value}"
        )

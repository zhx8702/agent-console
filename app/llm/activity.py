"""Activity-aware deadlines for internally streamed LLM requests.

Callers still receive one complete response.  Providers record each upstream
stream event so outer orchestration deadlines measure inactivity instead of
blind wall-clock time while the model is demonstrably making progress.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TypeVar

_T = TypeVar("_T")


@dataclass(slots=True)
class LLMStreamActivity:
    sequence: int = 0
    last_event_at: float = 0.0
    idle_timeout_seconds: float = 0.0

    def record(self, *, idle_timeout_seconds: float = 0.0) -> None:
        self.sequence += 1
        self.last_event_at = time.monotonic()
        self.idle_timeout_seconds = max(
            self.idle_timeout_seconds,
            max(0.0, float(idle_timeout_seconds or 0.0)),
        )


_current_activity: ContextVar[LLMStreamActivity | None] = ContextVar(
    "llm_stream_activity",
    default=None,
)


def record_llm_stream_event(*, idle_timeout_seconds: float = 0.0) -> None:
    """Record one parsed upstream event for the current LLM operation."""

    activity = _current_activity.get()
    if activity is not None:
        activity.record(idle_timeout_seconds=idle_timeout_seconds)


async def wait_for_llm_activity(
    awaitable: Awaitable[_T],
    *,
    timeout: float,
    wait_for_cancellation: bool = True,
) -> _T:
    """Wait with a deadline that advances while an LLM stream stays active.

    Before the first event this behaves like ``asyncio.wait_for``.  Every
    parsed stream event moves the deadline to at least one idle-timeout window
    beyond that event.  Nested orchestration deadlines share the same mutable
    tracker through ``ContextVar`` task inheritance.
    """

    timeout_seconds = float(timeout or 0.0)
    if timeout_seconds <= 0:
        return await awaitable

    activity = _current_activity.get()
    token: Token[LLMStreamActivity | None] | None = None
    if activity is None:
        activity = LLMStreamActivity()
        token = _current_activity.set(activity)

    task = asyncio.ensure_future(awaitable)
    cancellation_detached = False
    deadline = time.monotonic() + timeout_seconds
    seen_sequence = activity.sequence
    try:
        while True:
            if activity.sequence != seen_sequence:
                seen_sequence = activity.sequence
                grace = max(timeout_seconds, activity.idle_timeout_seconds)
                deadline = max(deadline, activity.last_event_at + grace)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                task.cancel()
                if wait_for_cancellation:
                    await asyncio.gather(task, return_exceptions=True)
                else:
                    cancellation_detached = True
                    task.add_done_callback(_consume_task_result)
                raise TimeoutError

            done, _ = await asyncio.wait(
                {task},
                timeout=min(remaining, 0.1),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done:
                return task.result()
    finally:
        if not task.done() and not cancellation_detached:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if token is not None:
            _current_activity.reset(token)


def current_llm_stream_activity() -> LLMStreamActivity | None:
    """Expose the current tracker for diagnostics and focused tests."""

    return _current_activity.get()


def _consume_task_result(task: asyncio.Future[object]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        return

"""
Pipeline hooks for plugins.

Hooks let plugins intercept the orchestrator pipeline at well-defined
points without modifying the core engine code.  Each hook receives the
current :class:`PipelineContext` and can read/modify it or short-circuit
by raising :class:`HookAbort`.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.orchestrator.outcome import RetryableProcessingError
from app.orchestrator.owner_gate import (
    DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    OwnerExecutionGate,
    evaluate_owner_execution,
    owner_gate_failure_is_retryable,
)
from app.orchestrator.pipeline import PipelineContext

DEFAULT_HOOK_TIMEOUT_SECONDS = 5.0
MAX_HOOK_TIMEOUT_SECONDS = 30.0
HOOK_TRACE_SCRATCH_KEY = "plugin_hook_trace.v1"
HOOK_ERROR_POLICIES = frozenset({"fail_closed", "fail_open"})
RESULT_PRODUCER_OWNER_KEY = "result_producer_owner"
_RESULT_PRODUCER_OWNER_SCRATCH_KEY = "orchestrator.result_producer_owner.v1"
_DELEGATED_RESULT_OWNER_HOOKS = frozenset({("commands", "commands.center")})


class HookPoint(str, Enum):
    """Where in the pipeline a hook fires."""

    BEFORE_PREPROCESS = "before_preprocess"
    AFTER_PREPROCESS = "after_preprocess"
    BEFORE_ROUTE = "before_route"
    AFTER_ROUTE = "after_route"
    BEFORE_CAPABILITY = "before_capability"
    AFTER_CAPABILITY = "after_capability"
    BEFORE_POSTPROCESS = "before_postprocess"
    AFTER_POSTPROCESS = "after_postprocess"


class HookAbort(Exception):
    """Raise from a hook to short-circuit the pipeline with a canned reply."""

    def __init__(self, reply_text: str, reason: str = "hook_abort"):
        self.reply_text = reply_text
        self.reason = reason
        self._result_producer_owner = ""
        super().__init__(reply_text)

    @property
    def result_producer_owner(self) -> str:
        """Return provenance bound by a trusted nested dispatcher, if any."""

        return self._result_producer_owner

    def bind_result_producer_owner(self, owner: str) -> None:
        """Bind a nested registered handler owner before the abort is raised.

        HookRunner accepts delegated provenance only from explicitly trusted
        aggregator hooks. Ordinary hooks are always rebound to their actual
        registration owner, so exception payloads cannot claim kernel identity.
        """

        self._result_producer_owner = str(owner or "").strip()


class HookExecutionError(RuntimeError):
    """Low-sensitivity failure raised by a fail-closed plugin hook."""

    def __init__(self, entry: HookEntry, *, code: str) -> None:
        self.owner = entry.owner
        self.hook_name = entry.name
        self.point = entry.point
        self.code = code
        super().__init__(
            f"plugin_hook_{code}:{entry.owner or 'compat'}:{entry.point.value}:{entry.name}"
        )


class PipelineHook(Protocol):
    """Interface a single hook must implement."""

    name: str
    point: HookPoint
    priority: int  # lower runs first

    async def run(self, ctx: PipelineContext) -> None: ...


@dataclass
class HookEntry:
    owner: str
    name: str
    point: HookPoint
    priority: int
    timeout_seconds: float
    error_policy: str
    hook: PipelineHook


class HookRunner:
    """Collects hooks from all plugins and dispatches them by point."""

    def __init__(
        self,
        *,
        owner_gate: OwnerExecutionGate | None = None,
        owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
        default_hook_timeout_seconds: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
    ) -> None:
        self._hooks: dict[HookPoint, list[HookEntry]] = {p: [] for p in HookPoint}
        self._owner_gate = owner_gate
        self._owner_gate_timeout_seconds = _bounded_timeout(
            owner_gate_timeout_seconds,
            default=DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
        )
        self._default_hook_timeout_seconds = _bounded_timeout(
            default_hook_timeout_seconds,
            default=DEFAULT_HOOK_TIMEOUT_SECONDS,
        )

    def register(self, hook: PipelineHook, *, owner: str = "") -> None:
        error_policy = str(getattr(hook, "error_policy", "fail_closed") or "fail_closed")
        if error_policy not in HOOK_ERROR_POLICIES:
            raise ValueError(f"invalid hook error policy: {error_policy}")
        entry = HookEntry(
            owner=str(owner or "").strip(),
            name=str(hook.name or "").strip(),
            point=hook.point,
            priority=int(hook.priority),
            timeout_seconds=_bounded_timeout(
                getattr(hook, "timeout_seconds", self._default_hook_timeout_seconds),
                default=self._default_hook_timeout_seconds,
            ),
            error_policy=error_policy,
            hook=hook,
        )
        if not entry.name:
            raise ValueError("hook name cannot be empty")
        entries = self._hooks[hook.point]
        entries[:] = [
            existing
            for existing in entries
            if not (existing.owner == entry.owner and existing.name == entry.name)
        ]
        entries.append(entry)
        entries.sort(key=lambda e: (e.priority, e.owner, e.name))

    def unregister_owner(self, owner: str) -> int:
        owner = str(owner or "").strip()
        if not owner:
            return 0
        removed = 0
        for point, entries in self._hooks.items():
            next_entries = [entry for entry in entries if entry.owner != owner]
            removed += len(entries) - len(next_entries)
            self._hooks[point] = next_entries
        return removed

    def owner_summary(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for entries in self._hooks.values():
            for entry in entries:
                if entry.owner:
                    result.setdefault(entry.owner, []).append(entry.name)
        return result

    async def run(self, point: HookPoint, ctx: PipelineContext) -> None:
        for entry in self._hooks[point]:
            started = time.perf_counter()
            decision = await evaluate_owner_execution(
                self._owner_gate,
                entry.owner,
                ctx,
                timeout_seconds=self._owner_gate_timeout_seconds,
            )
            if not decision.allowed:
                _append_hook_trace(
                    ctx,
                    entry,
                    status=(
                        "owner_gate_retryable"
                        if owner_gate_failure_is_retryable(decision.reason)
                        else "owner_skipped"
                    ),
                    reason=decision.reason,
                    started=started,
                )
                if owner_gate_failure_is_retryable(decision.reason):
                    raise RetryableProcessingError(
                        decision.reason,
                        error_type="PluginOwnerGateUnavailable",
                    )
                continue
            try:
                await asyncio.wait_for(
                    entry.hook.run(ctx),
                    timeout=entry.timeout_seconds,
                )
            except HookAbort as exc:
                producer_owner = _hook_abort_producer_owner(entry, exc)
                denied_reason = await self._fresh_result_owner_denial(
                    entry,
                    ctx,
                    producer_owner=producer_owner,
                )
                if denied_reason:
                    _append_hook_trace(
                        ctx,
                        entry,
                        status=(
                            "owner_gate_retryable"
                            if owner_gate_failure_is_retryable(denied_reason)
                            else "owner_skipped"
                        ),
                        reason=denied_reason,
                        started=started,
                    )
                    if owner_gate_failure_is_retryable(denied_reason):
                        raise RetryableProcessingError(
                            denied_reason,
                            error_type="PluginOwnerGateUnavailable",
                        ) from None
                    continue
                bind_result_producer_owner(ctx, producer_owner)
                exc.bind_result_producer_owner(producer_owner)
                _append_hook_trace(ctx, entry, status="aborted", started=started)
                raise
            except TimeoutError as exc:
                status = "timeout_open" if entry.error_policy == "fail_open" else "timeout"
                _append_hook_trace(ctx, entry, status=status, started=started)
                if entry.error_policy == "fail_closed":
                    raise HookExecutionError(entry, code="timeout") from exc
            except asyncio.CancelledError:
                raise
            except RetryableProcessingError:
                raise
            except Exception as exc:
                status = "error_open" if entry.error_policy == "fail_open" else "error"
                _append_hook_trace(ctx, entry, status=status, started=started)
                if entry.error_policy == "fail_closed":
                    raise HookExecutionError(entry, code="failed") from exc
            else:
                denied_reason = await self._fresh_result_owner_denial(
                    entry,
                    ctx,
                    producer_owner=entry.owner,
                )
                if denied_reason:
                    _append_hook_trace(
                        ctx,
                        entry,
                        status=(
                            "owner_gate_retryable"
                            if owner_gate_failure_is_retryable(denied_reason)
                            else "owner_skipped"
                        ),
                        reason=denied_reason,
                        started=started,
                    )
                    if owner_gate_failure_is_retryable(denied_reason):
                        raise RetryableProcessingError(
                            denied_reason,
                            error_type="PluginOwnerGateUnavailable",
                        )
                    continue
                _append_hook_trace(ctx, entry, status="ok", started=started)

    async def _fresh_result_owner_denial(
        self,
        entry: HookEntry,
        ctx: PipelineContext,
        *,
        producer_owner: str,
    ) -> str:
        owners = tuple(dict.fromkeys((str(entry.owner or "").strip(), producer_owner)))
        for owner in owners:
            decision = await evaluate_owner_execution(
                self._owner_gate,
                owner,
                ctx,
                timeout_seconds=self._owner_gate_timeout_seconds,
            )
            if not decision.allowed:
                return decision.reason
        return ""

    @property
    def owner_gate(self) -> OwnerExecutionGate | None:
        return self._owner_gate

    @property
    def owner_gate_timeout_seconds(self) -> float:
        return self._owner_gate_timeout_seconds

    def set_owner_gate(
        self,
        gate: OwnerExecutionGate | None,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """Replace the process policy callback used for subsequent contexts."""

        self._owner_gate = gate
        if timeout_seconds is not None:
            self._owner_gate_timeout_seconds = _bounded_timeout(
                timeout_seconds,
                default=DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
            )

    def has_hooks(self, point: HookPoint) -> bool:
        return bool(self._hooks[point])

    @property
    def summary(self) -> dict[str, list[str]]:
        return {p.value: [e.name for e in entries] for p, entries in self._hooks.items() if entries}


def bind_result_producer_owner(ctx: PipelineContext, owner: str) -> str:
    """Record result provenance established by a trusted execution boundary."""

    normalized = str(owner or "").strip() or "core"
    ctx.scratch[_RESULT_PRODUCER_OWNER_SCRATCH_KEY] = normalized
    # Keep a low-sensitivity compatibility/debug view in extras. Consumers use
    # the scratch binding and never accept this public value as authority.
    ctx.extras[RESULT_PRODUCER_OWNER_KEY] = normalized
    return normalized


def trusted_result_producer_owner(ctx: PipelineContext) -> str:
    """Return only provenance previously bound by a trusted core boundary."""

    return str(ctx.scratch.get(_RESULT_PRODUCER_OWNER_SCRATCH_KEY) or "").strip()


def _hook_abort_producer_owner(entry: HookEntry, exc: HookAbort) -> str:
    registered_owner = str(entry.owner or "").strip() or "core"
    if (entry.owner, entry.name) not in _DELEGATED_RESULT_OWNER_HOOKS:
        return registered_owner
    return str(exc.result_producer_owner or "").strip() or registered_owner


def _bounded_timeout(value: object, *, default: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = default
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = default
    return min(timeout, MAX_HOOK_TIMEOUT_SECONDS)


def _append_hook_trace(
    ctx: PipelineContext,
    entry: HookEntry,
    *,
    status: str,
    started: float,
    reason: str = "",
) -> None:
    trace = ctx.scratch.setdefault(HOOK_TRACE_SCRATCH_KEY, [])
    if not isinstance(trace, list):
        trace = []
        ctx.scratch[HOOK_TRACE_SCRATCH_KEY] = trace
    item: dict[str, object] = {
        "owner": entry.owner or "compat",
        "name": entry.name,
        "point": entry.point.value,
        "priority": entry.priority,
        "status": status,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    if reason:
        item["reason"] = str(reason)[:64]
    trace.append(item)

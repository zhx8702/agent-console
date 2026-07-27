"""Scope-aware execution gate shared by plugin runtime boundaries.

The gate is deliberately a small callable contract.  Runtime policy providers
may consult durable plugin state, while consumers get one stable decision per
owner and pipeline context.  Core contributions and runtimes without a gate
remain fully backwards compatible.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeAlias

from app.orchestrator.pipeline import PipelineContext

DEFAULT_OWNER_GATE_TIMEOUT_SECONDS = 1.0
OWNER_GATE_CACHE_KEY = "owner_execution_gate.v1"
_REASON_CODE_RE = re.compile(r"[^a-z0-9_.:-]+")


@dataclass(frozen=True, slots=True)
class OwnerExecutionDecision:
    """Low-sensitivity decision returned by an owner policy provider."""

    allowed: bool
    reason: str = ""


OwnerExecutionGateResult: TypeAlias = OwnerExecutionDecision | bool
OwnerExecutionGate: TypeAlias = Callable[
    [str, PipelineContext],
    Awaitable[OwnerExecutionGateResult],
]


async def evaluate_owner_execution(
    gate: OwnerExecutionGate | None,
    owner: str,
    ctx: PipelineContext,
    *,
    timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
) -> OwnerExecutionDecision:
    """Return a cached, bounded execution decision for ``owner``.

    Missing gates default to allow.  ``core`` and the empty compatibility
    owner always bypass policy so enabling the feature cannot disable kernel
    execution.  Once a plugin owner is evaluated, the result is cached in the
    request-local ``scratch`` map to prevent policy drift mid-pipeline.
    """

    normalized_owner = str(owner or "").strip()
    if not normalized_owner or normalized_owner == "core" or gate is None:
        return OwnerExecutionDecision(allowed=True, reason="core_compatible")

    cache = _decision_cache(ctx)
    cached = cache.get(normalized_owner)
    if isinstance(cached, OwnerExecutionDecision) and not cached.allowed:
        return cached

    timeout = _bounded_timeout(timeout_seconds)
    try:
        raw = await asyncio.wait_for(gate(normalized_owner, ctx), timeout=timeout)
    except TimeoutError:
        decision = OwnerExecutionDecision(False, "owner_gate_timeout")
    except asyncio.CancelledError:
        raise
    except Exception:
        decision = OwnerExecutionDecision(False, "owner_gate_error")
    else:
        decision = _normalize_decision(raw)
    # A durable disable must take effect at the next side-effect boundary in
    # the same pipeline. Cache only denials; positive decisions are rechecked.
    if not decision.allowed:
        cache[normalized_owner] = decision
    return decision


def owner_gate_failure_is_retryable(reason: str) -> bool:
    return str(reason or "").strip() in {
        "owner_gate_timeout",
        "owner_gate_error",
        "owner_gate_invalid_result",
    }


def _decision_cache(ctx: PipelineContext) -> dict[str, OwnerExecutionDecision]:
    cached = ctx.scratch.get(OWNER_GATE_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    result: dict[str, OwnerExecutionDecision] = {}
    ctx.scratch[OWNER_GATE_CACHE_KEY] = result
    return result


def _normalize_decision(raw: OwnerExecutionGateResult) -> OwnerExecutionDecision:
    if isinstance(raw, OwnerExecutionDecision):
        allowed = bool(raw.allowed)
        reason = _reason_code(raw.reason)
    elif isinstance(raw, bool):
        allowed = raw
        reason = ""
    else:
        return OwnerExecutionDecision(False, "owner_gate_invalid_result")
    if allowed:
        return OwnerExecutionDecision(True, reason or "owner_allowed")
    return OwnerExecutionDecision(False, reason or "owner_execution_denied")


def _reason_code(value: object) -> str:
    normalized = _REASON_CODE_RE.sub("_", str(value or "").strip().lower())
    return normalized.strip("_")[:64]


def _bounded_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_OWNER_GATE_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        return DEFAULT_OWNER_GATE_TIMEOUT_SECONDS
    return min(timeout, 30.0)


__all__ = [
    "DEFAULT_OWNER_GATE_TIMEOUT_SECONDS",
    "OWNER_GATE_CACHE_KEY",
    "OwnerExecutionDecision",
    "OwnerExecutionGate",
    "OwnerExecutionGateResult",
    "evaluate_owner_execution",
    "owner_gate_failure_is_retryable",
]

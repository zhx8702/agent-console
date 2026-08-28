"""Overflow long or failed LLM turns onto host grok / Codex."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.common.logging import get_logger
from app.common.types import RouteType
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint
from plugins.local_agent.hooks import enqueue_local_agent_job, estimate_prompt_chars
from plugins.local_agent.probe import LocalAgentProbe, ProbeSnapshot
from plugins.local_agent.sidecar.backends import BACKENDS
from plugins.local_agent.store import LocalAgentStore
from plugins.local_agent.worker import ACCEPTED_OVERFLOW_TEXT

logger = get_logger(__name__)

OVERFLOW_ROUTES = frozenset({RouteType.LLM, RouteType.RAG, RouteType.AGENT})
OVERFLOW_ERROR_MARKERS = (
    "context_length",
    "context window",
    "maximum context",
    "prompt is too long",
    "prompt too long",
    "prompt too large",
    "too many tokens",
    "token limit",
    "string too long",
    "413",
    "request too large",
)
DEFAULT_MIN_CHARS = 24000
SOFT_FAIL_MIN_CHARS = 8000


def overflow_settings(settings: Any) -> dict[str, Any]:
    enabled = bool(getattr(settings, "local_agent_overflow_enabled", True))
    min_chars = int(getattr(settings, "local_agent_overflow_min_chars", DEFAULT_MIN_CHARS) or DEFAULT_MIN_CHARS)
    min_chars = max(1000, min(min_chars, 200_000))
    backend = str(getattr(settings, "local_agent_overflow_backend", "auto") or "auto").strip().lower()
    if backend not in {"auto", *BACKENDS}:
        backend = "auto"
    return {
        "enabled": enabled,
        "min_chars": min_chars,
        "backend": backend,
    }


def pick_backend(snapshot: ProbeSnapshot, preference: str) -> str:
    preferred = str(preference or "auto").strip().lower()
    if preferred in BACKENDS and snapshot.backend(preferred).ok:
        return preferred
    for name in ("codex", "grok"):
        if snapshot.backend(name).ok:
            return name
    return ""


def _route_type(ctx: PipelineContext) -> RouteType | None:
    route = ctx.route
    if route is None:
        return None
    value = getattr(route, "type", None)
    if isinstance(value, RouteType):
        return value
    try:
        return RouteType(str(value or ""))
    except ValueError:
        return None


def _current_text(ctx: PipelineContext) -> str:
    pre = ctx.pre
    if pre is not None:
        text = str(getattr(pre, "cleaned_text", "") or getattr(pre, "original_text", "") or "").strip()
        if text:
            return text
    return str(getattr(ctx.event.message, "content", "") or "").strip()


def _result_blob(ctx: PipelineContext) -> str:
    result = ctx.result
    if result is None:
        return ""
    metadata = dict(getattr(result, "metadata", None) or {})
    parts = [
        str(getattr(result, "reply_text", "") or ""),
        str(metadata.get("degradation_reason") or ""),
        str(metadata.get("error") or ""),
        str(metadata.get("error_message") or ""),
        str(ctx.extras.get("capability_error") or ""),
    ]
    return " ".join(parts).lower()


def looks_like_context_overflow(ctx: PipelineContext) -> bool:
    blob = _result_blob(ctx)
    if any(marker in blob for marker in OVERFLOW_ERROR_MARKERS):
        return True
    result = ctx.result
    if result is None:
        return False
    metadata = dict(getattr(result, "metadata", None) or {})
    reason = str(metadata.get("degradation_reason") or "")
    return reason == "capability_failed:llm" and estimate_prompt_chars(ctx) >= SOFT_FAIL_MIN_CHARS


def should_overflow_before(ctx: PipelineContext, *, min_chars: int) -> bool:
    if ctx.extras.get("_local_agent_overflow"):
        return False
    if ctx.extras.get("_command_token") or ctx.extras.get("_command_canonical"):
        return False
    route = _route_type(ctx)
    if route not in OVERFLOW_ROUTES:
        return False
    return estimate_prompt_chars(ctx) >= min_chars


def should_overflow_after(ctx: PipelineContext, *, min_chars: int) -> bool:
    if ctx.extras.get("_local_agent_overflow"):
        return False
    if ctx.extras.get("_command_token") or ctx.extras.get("_command_canonical"):
        return False
    route = _route_type(ctx)
    if route not in OVERFLOW_ROUTES:
        return False
    if looks_like_context_overflow(ctx):
        return True
    return False


async def overflow_to_local_agent(
    *,
    store: LocalAgentStore,
    probe: LocalAgentProbe,
    ctx: PipelineContext,
    settings: Any,
    reason: str,
) -> None:
    cfg = overflow_settings(settings)
    snapshot = await probe.snapshot()
    backend = pick_backend(snapshot, str(cfg["backend"]))
    if not backend:
        logger.info(
            "local_agent.overflow_skipped_no_backend",
            reason=reason,
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
        )
        return
    job = await enqueue_local_agent_job(
        store=store,
        probe=probe,
        ctx=ctx,
        backend=backend,
        user_text=_current_text(ctx),
    )
    ctx.extras["_local_agent_overflow"] = {
        "job_id": job.job_id,
        "backend": backend,
        "reason": reason,
        "chars": estimate_prompt_chars(ctx),
    }
    logger.info(
        "local_agent.overflow_queued",
        job_id=job.job_id,
        backend=backend,
        reason=reason,
        tenant_id=ctx.event.tenant_id,
        session_id=ctx.event.session_id,
    )
    raise HookAbort(
        ACCEPTED_OVERFLOW_TEXT.format(backend=backend),
        reason="local_agent_overflow",
    )


@dataclass
class LocalAgentOverflowHook:
    store: LocalAgentStore
    probe: LocalAgentProbe
    settings: Any
    name: str = "local_agent.overflow"
    point: HookPoint = HookPoint.BEFORE_CAPABILITY
    priority: int = 80
    timeout_seconds: float = 8.0
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> None:
        cfg = overflow_settings(self.settings)
        if not cfg["enabled"]:
            return
        if not should_overflow_before(ctx, min_chars=int(cfg["min_chars"])):
            return
        await overflow_to_local_agent(
            store=self.store,
            probe=self.probe,
            ctx=ctx,
            settings=self.settings,
            reason="prompt_too_long",
        )


@dataclass
class LocalAgentOverflowRetryHook:
    store: LocalAgentStore
    probe: LocalAgentProbe
    settings: Any
    name: str = "local_agent.overflow_retry"
    point: HookPoint = HookPoint.AFTER_CAPABILITY
    priority: int = 80
    timeout_seconds: float = 8.0
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> None:
        cfg = overflow_settings(self.settings)
        if not cfg["enabled"]:
            return
        if not should_overflow_after(ctx, min_chars=int(cfg["min_chars"])):
            return
        await overflow_to_local_agent(
            store=self.store,
            probe=self.probe,
            ctx=ctx,
            settings=self.settings,
            reason="llm_context_overflow",
        )

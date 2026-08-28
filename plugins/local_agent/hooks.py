"""Slash commands that hand long work to the host sidecar."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from app.channel import ChannelRegistry, ChannelTarget
from app.commands import CommandDefinition
from app.common.logging import get_logger
from app.common.types import MessageType, Role
from app.orchestrator.pipeline import PipelineContext
from plugins.local_agent.probe import LocalAgentProbe
from plugins.local_agent.sidecar.backends import BACKENDS
from plugins.local_agent.store import LocalAgentJob, LocalAgentStore
from plugins.local_agent.worker import ACCEPTED_TEXT, UNAVAILABLE_TEXT

logger = get_logger(__name__)

HELP_TEXT = {
    "grok": "用法: /grok 任务内容",
    "codex": "用法: /codex 任务内容",
}
HISTORY_LIMIT = 20


def current_user_text(ctx: PipelineContext) -> str:
    pre = ctx.pre
    if pre is not None:
        text = str(getattr(pre, "cleaned_text", "") or getattr(pre, "original_text", "") or "").strip()
        if text:
            return text
    return str(getattr(ctx.event.message, "content", "") or "").strip()


def _history_prompt(ctx: PipelineContext, current: str) -> str:
    chunks: list[str] = []
    session = ctx.session
    if session is not None:
        recent = list(session.turns or [])[-HISTORY_LIMIT:]
        for turn in recent:
            if turn.role not in (Role.USER, Role.ASSISTANT):
                continue
            text = str(turn.content or "").strip()
            if not text:
                continue
            label = "用户" if turn.role == Role.USER else "助手"
            chunks.append(f"{label}: {text}")
    chunks.append(f"用户: {current.strip()}")
    return "\n".join(chunks).strip()


def estimate_prompt_chars(ctx: PipelineContext) -> int:
    return len(_history_prompt(ctx, current_user_text(ctx)))


async def enqueue_local_agent_job(
    *,
    store: LocalAgentStore,
    probe: LocalAgentProbe,
    ctx: PipelineContext,
    backend: str,
    user_text: str,
) -> LocalAgentJob:
    _ = probe
    event = ctx.event
    target = ChannelTarget.from_event(event)
    job = await store.create_job(
        backend=backend,
        prompt=_history_prompt(ctx, user_text),
        tenant_id=event.tenant_id,
        channel=str(getattr(event.channel, "value", event.channel) or ""),
        session_id=event.session_id,
        user_id=event.user_id,
        adapter_id=event.adapter_id,
        connection_id=event.connection_id,
        request_id=str(ctx.trace_id or event.trace_id or event.message_id or ""),
        trace_id=str(ctx.trace_id or event.trace_id or ""),
        original_message_id=str(event.message_id or ""),
        callback_target=asdict(target),
        source_message=event.model_dump(mode="json"),
    )
    logger.info(
        "local_agent.job_queued",
        job_id=job.job_id,
        backend=backend,
        tenant_id=event.tenant_id,
        session_id=event.session_id,
    )
    return job


async def handle_local_agent_command(
    *,
    store: LocalAgentStore,
    probe: LocalAgentProbe,
    channel_registry: ChannelRegistry | None,
    backend: str,
    ctx: PipelineContext,
    args: list[str],
) -> str:
    _ = channel_registry
    name = str(backend or "").strip().lower()
    if name not in BACKENDS:
        raise ValueError("unknown_backend")
    event = ctx.event
    if event.message.type != MessageType.TEXT:
        raise ValueError("本机命令当前仅支持文本消息")
    prompt = " ".join(str(item or "").strip() for item in args).strip()
    if not prompt:
        raise ValueError(HELP_TEXT[name])
    snapshot = await probe.snapshot()
    status = snapshot.backend(name)
    if not status.ok:
        return UNAVAILABLE_TEXT[name]
    await enqueue_local_agent_job(
        store=store,
        probe=probe,
        ctx=ctx,
        backend=name,
        user_text=prompt,
    )
    return ACCEPTED_TEXT[name]


def build_local_agent_command_definitions(
    store: LocalAgentStore,
    probe: LocalAgentProbe,
    channel_registry: ChannelRegistry | None,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> list[CommandDefinition]:
    _ = scope_execution_allowed

    async def _grok(ctx: PipelineContext, args: list[str]) -> str:
        return await handle_local_agent_command(
            store=store,
            probe=probe,
            channel_registry=channel_registry,
            backend="grok",
            ctx=ctx,
            args=args,
        )

    async def _codex(ctx: PipelineContext, args: list[str]) -> str:
        return await handle_local_agent_command(
            store=store,
            probe=probe,
            channel_registry=channel_registry,
            backend="codex",
            ctx=ctx,
            args=args,
        )

    return [
        CommandDefinition(
            plugin_name="local_agent",
            command="/grok",
            aliases=("/g",),
            description="把复杂任务交给本机 grok",
            usage=HELP_TEXT["grok"],
            handler=_grok,
        ),
        CommandDefinition(
            plugin_name="local_agent",
            command="/codex",
            aliases=("/c",),
            description="把复杂任务交给本机 Codex",
            usage=HELP_TEXT["codex"],
            handler=_codex,
        ),
    ]

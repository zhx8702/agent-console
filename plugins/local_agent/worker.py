"""Claim queued local-agent jobs, talk to the sidecar, and send replies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.channel import ChannelSendOptions, ChannelTarget, ChannelRegistry
from app.common.logging import get_logger
from plugins.local_agent.client import LocalAgentClient, LocalAgentClientError
from plugins.local_agent.store import LocalAgentJob, LocalAgentStore

logger = get_logger(__name__)

ACCEPTED_TEXT = {
    "grok": "已交给本机 grok，完成后回这里。",
    "codex": "已交给本机 Codex，完成后回这里。",
}
ACCEPTED_OVERFLOW_TEXT = "内容较长，已转本机 {backend} 处理，完成后回这里。"
UNAVAILABLE_TEXT = {
    "grok": "本机 grok 当前不可用。",
    "codex": "本机 Codex 当前不可用。",
}
FAILED_TEXT = "本机任务失败：{error}"


def _target_from_job(job: LocalAgentJob) -> ChannelTarget:
    payload = dict(job.callback_target or {})
    return ChannelTarget(
        tenant_id=str(payload.get("tenant_id") or job.tenant_id),
        channel=str(payload.get("channel") or job.channel),
        session_id=str(payload.get("session_id") or job.session_id),
        adapter_id=str(payload.get("adapter_id") or job.adapter_id),
        connection_id=str(payload.get("connection_id") or job.connection_id),
        external_conversation_id=str(payload.get("external_conversation_id") or ""),
        canonical_conversation_id=str(payload.get("canonical_conversation_id") or ""),
        external_participant_id=str(payload.get("external_participant_id") or ""),
        canonical_participant_id=str(payload.get("canonical_participant_id") or ""),
        user_id=str(payload.get("user_id") or job.user_id),
        sender_id=str(payload.get("sender_id") or job.user_id),
        sender_name=str(payload.get("sender_name") or ""),
        reply_to_message_id=str(
            payload.get("reply_to_message_id") or job.original_message_id
        ),
        metadata=dict(payload.get("metadata") or {}),
    )


def _guard_text(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return "本机任务没有返回文本。"
    if len(cleaned) > 4000:
        return cleaned[:3990].rstrip() + "…"
    return cleaned


async def _send_callback(
    *,
    store: LocalAgentStore,
    channel_registry: ChannelRegistry,
    job: LocalAgentJob,
    text: str,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None,
) -> None:
    if job.callback_sent:
        return
    target_payload = dict(job.callback_target or {})
    if not str(target_payload.get("session_id") or job.session_id or "").strip() or str(job.channel or "") in {"", "web"}:
        await store.mark_callback_sent(job.job_id)
        return
    if scope_execution_allowed is not None:
        allowed = await scope_execution_allowed(job.tenant_id, job.session_id)
        if allowed is not True:
            await store.mark_callback_error(job.job_id, "scope_execution_denied")
            return
    target = _target_from_job(job)
    try:
        outbound = channel_registry.require_outbound_for_target(target)
    except Exception as exc:
        await store.mark_callback_error(job.job_id, str(exc)[:500])
        return
    command_id = (
        f"channel-reply:{job.tenant_id}:{job.original_message_id or job.request_id}"
        f":local-agent:{job.job_id}"
    )
    await outbound.send_text(
        target,
        _guard_text(text),
        ChannelSendOptions(
            trace_id=job.trace_id,
            source_message=dict(job.source_message or {}),
            idempotency_key=command_id,
            delivery_metadata={"command_id": command_id, "idempotency_key": command_id},
        ),
    )
    await store.mark_callback_sent(job.job_id)


async def process_job(
    *,
    store: LocalAgentStore,
    client: LocalAgentClient,
    channel_registry: ChannelRegistry,
    job: LocalAgentJob,
    settings: Any,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> None:
    timeout_seconds = float(
        getattr(settings, "local_agent_task_timeout_seconds", 600.0) or 600.0
    )
    if not job.sidecar_task_id:
        try:
            task = await client.create_task(
                backend=job.backend,
                prompt=job.prompt,
                timeout_seconds=timeout_seconds,
            )
        except LocalAgentClientError as exc:
            await store.mark_failed(job.job_id, exc.code, str(exc))
            latest = await store.get_job(job.job_id)
            if latest is not None:
                await _send_callback(
                    store=store,
                    channel_registry=channel_registry,
                    job=latest,
                    text=FAILED_TEXT.format(error=exc.code),
                    scope_execution_allowed=scope_execution_allowed,
                )
            return
        await store.mark_submitted(job.job_id, task.task_id)
        job.sidecar_task_id = task.task_id
        job.status = "submitted"

    try:
        task = await client.get_task(job.sidecar_task_id)
    except LocalAgentClientError as exc:
        logger.warning(
            "local_agent.poll_failed",
            job_id=job.job_id,
            sidecar_task_id=job.sidecar_task_id,
            error=exc.code,
        )
        await store.release_lock(job.job_id)
        return

    if task.status in {"queued", "running", "submitted"}:
        if task.status == "running" and job.status != "running":
            await store.mark_running(job.job_id)
        await store.release_lock(job.job_id)
        return
    if task.status == "succeeded":
        await store.mark_succeeded(job.job_id, task.result_text)
        latest = await store.get_job(job.job_id)
        if latest is not None:
            await _send_callback(
                store=store,
                channel_registry=channel_registry,
                job=latest,
                text=task.result_text,
                scope_execution_allowed=scope_execution_allowed,
            )
        return
    error = task.error or task.status or "sidecar_failed"
    await store.mark_failed(job.job_id, "sidecar_failed", error)
    latest = await store.get_job(job.job_id)
    if latest is not None:
        await _send_callback(
            store=store,
            channel_registry=channel_registry,
            job=latest,
            text=FAILED_TEXT.format(error=error[:200]),
            scope_execution_allowed=scope_execution_allowed,
        )


async def drain_queued_jobs(
    *,
    store: LocalAgentStore,
    client: LocalAgentClient,
    channel_registry: ChannelRegistry,
    worker_id: str,
    settings: Any,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> dict[str, int]:
    batch_size = int(getattr(settings, "local_agent_job_batch_size", 3) or 3)
    lock_ttl = float(getattr(settings, "local_agent_job_lock_ttl_seconds", 120.0) or 120.0)
    claimed = await store.claim_due_jobs(
        limit=batch_size,
        lock_ttl_seconds=lock_ttl,
        worker_id=worker_id,
    )
    processed = 0
    failed = 0
    for job in claimed:
        if job.callback_sent and job.status in {"succeeded", "failed"}:
            continue
        try:
            await process_job(
                store=store,
                client=client,
                channel_registry=channel_registry,
                job=job,
                settings=settings,
                scope_execution_allowed=scope_execution_allowed,
            )
            processed += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "local_agent.job_failed",
                job_id=job.job_id,
                error_type=exc.__class__.__name__,
            )
            latest = await store.get_job(job.job_id)
            if latest is None or latest.status not in {"succeeded", "failed"}:
                await store.mark_failed(job.job_id, "worker_exception", str(exc)[:500])
    return {"claimed": len(claimed), "processed": processed, "failed": failed}

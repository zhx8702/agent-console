from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import urljoin
from uuid import uuid4

from app.billing import BillingCoordinator, BillingReservation
from app.channel import ChannelMedia, ChannelRegistry, ChannelSendOptions, ChannelTarget
from app.commands import CommandDefinition
from app.common.logging import get_logger
from app.common.quote_images import quote_image_source_from_metadata
from app.common.types import Channel, MessageType, ReplyType, channel_id_value
from app.orchestrator.effects import (
    EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
    EffectCommitRecord,
)
from app.orchestrator.flow import MessageEffect, StepResult
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookPoint
from plugins.draw.avatar import resolve_prompt_avatar_reference
from plugins.draw.store import (
    DRAW_DEFAULT_QUALITY,
    DRAW_QUALITY_ERROR_TEXT,
    DRAW_QUALITY_SIZES,
    DRAW_TASK_INTERRUPTED_ERROR_CODE,
    DRAW_TASK_INTERRUPTED_ERROR_MESSAGE,
    DrawApiError,
    DrawConfigError,
    DrawStore,
    DrawTaskCreate,
    DrawTaskRecord,
    normalize_draw_quality,
)

logger = get_logger(__name__)


class _DrawScopeExecutionDenied(RuntimeError):
    """The task scope was disabled between two durable job boundaries."""

DRAW_SUCCESS_TEXT = "画好了"
DRAW_ACCEPTED_TEXT = "收到, 正在画。"
REDRAW_ACCEPTED_TEXT = "收到, 正在重绘。"
DRAW_CONFIG_ERROR_TEXT = "画图失败：未配置画图服务。"
DRAW_API_ERROR_TEXT = "画图失败：上游请求失败。"
DRAW_TIMEOUT_ERROR_TEXT = "画图失败：上游超时。"
DRAW_EMPTY_RESPONSE_ERROR_TEXT = "画图失败：上游返回无图片。"
DRAW_IMAGE_ID_ERROR_TEXT = "画图失败：图片ID无效/找不到。"
DRAW_PARAM_ERROR_TEXT = "画图失败：参数错误。"
DRAW_CRASH_ERROR_TEXT = "画图失败：内部异常。"
DRAW_HELP_TEXT = "用法: /draw [quality=low|medium|high] 提示词 或 /画图 提示词"
REDRAW_HELP_TEXT = "用法: /redraw [quality=low|medium|high] 图片ID 提示词, 或引用图片后发送 /重绘 提示词"
QUOTE_TEXT_PROMPT_LIMIT = 1200
DRAW_INTERRUPTED_TEXT = "画图任务中断，请重试。"
DRAW_TASK_RECOVERY_CALLBACK_TEXT = DRAW_TASK_INTERRUPTED_ERROR_MESSAGE
DRAW_TASK_INLINE_CLAIM_GRACE_SECONDS = 30.0
NON_RETRYABLE_DRAW_FAILURE_CATEGORIES = {
    "config_missing",
    "invalid_image_id",
    "invalid_params",
}
_DELIVERY_CONTRACT_METADATA_KEY = "_wxbot_delivery_contract"
_T = TypeVar("_T")


async def _resolve_persistent_operation(
    operation: Awaitable[_T],
) -> tuple[_T, bool]:
    """Finish a durable/remote critical section despite repeated cancellation."""

    operation_task = asyncio.ensure_future(operation)
    cancellation_requested = False
    while not operation_task.done():
        try:
            await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            cancellation_requested = True
    return operation_task.result(), cancellation_requested


async def _finish_persistent_operation(operation: Awaitable[_T]) -> _T:
    result, _cancellation_requested = await _resolve_persistent_operation(operation)
    return result


def _draw_success_text(image_id: str) -> str:
    image_id = str(image_id or "").strip()
    if not image_id:
        return DRAW_SUCCESS_TEXT
    return f"{DRAW_SUCCESS_TEXT}, 图片ID: {image_id}"


def _resolve_draw_public_url(
    store: DrawStore, *, public_path: str = "", source_url: str = ""
) -> str:
    base_url = str(getattr(store.settings, "wxbot_media_base_url", "") or "").strip().rstrip("/")
    if base_url and public_path.strip():
        return urljoin(f"{base_url}/", public_path.lstrip("/"))
    if source_url.strip():
        return source_url.strip()
    return ""


@dataclass(frozen=True)
class DrawCommandArgs:
    quality: str
    args: list[str]


@dataclass(frozen=True)
class DrawFailure:
    category: str
    text: str


@dataclass(frozen=True)
class DrawTaskContext:
    task_id: str
    request_id: str
    command_type: str
    original_message_id: str
    requester_user_id: str
    requester_display_name: str
    prompt: str
    quality: str
    created_at: str
    trace_id: str
    target: ChannelTarget
    source_message: dict[str, Any]
    source_image_id: str = ""
    source_image_url: str = ""
    source_image_path: str = ""
    source_label: str = ""

    @property
    def source(self) -> str:
        return (
            self.source_image_id
            or self.source_label
            or self.source_image_url
            or self.source_image_path
            or "prompt"
        )


def _pipeline_delivery_contract(ctx: PipelineContext) -> dict[str, Any]:
    event = ctx.event
    if event.channel is not Channel.WECHAT or not str(event.session_id).endswith(
        "@chatroom"
    ):
        return {}
    source_message_id = str(
        event.message_id or event.metadata.get("msg_svr_id") or ctx.trace_id or ""
    ).strip()
    policy_value = ctx.extras.get("wxbot_reply_policy")
    policy_state = policy_value if isinstance(policy_value, dict) else {}
    contract: dict[str, Any] = {
        "participation_status": "must_reply",
        "source_message_id": source_message_id,
        "response_kind": "tool_result",
        "speech_output_kind": "ordinary",
        "speech_class": "obligation",
        "participation_reason_codes": ["direct_tool_request"],
    }
    for key in (
        "participation_policy_version",
        "send_revalidation_enabled",
        "participation_policy_source",
        "humanization_stage",
        "humanization_cohort",
        "speech_budget_enabled",
        "duplicate_guard_enabled",
    ):
        if key in policy_state:
            contract[key] = policy_state[key]
    return contract


def _task_result_delivery(task_ctx: DrawTaskContext) -> dict[str, Any]:
    target = task_ctx.target
    if target.channel != Channel.WECHAT.value or not target.session_id.endswith(
        "@chatroom"
    ):
        return {}
    captured = task_ctx.source_message.get(_DELIVERY_CONTRACT_METADATA_KEY)
    delivery = dict(captured) if isinstance(captured, dict) else {}
    delivery.update(
        {
            "participation_status": "must_reply",
            "source_message_id": str(
                delivery.get("source_message_id")
                or task_ctx.original_message_id
                or target.reply_to_message_id
                or ""
            ).strip(),
            "response_kind": "tool_result",
            "speech_output_kind": "ordinary",
            "speech_class": "obligation",
            "participation_reason_codes": ["direct_tool_request"],
        }
    )
    return delivery


async def _bind_channel_delivery_contract(
    channel_outbound: object,
    task_ctx: DrawTaskContext,
) -> None:
    if (
        task_ctx.target.channel != Channel.WECHAT.value
        or not task_ctx.target.session_id.endswith("@chatroom")
    ):
        return
    captured = task_ctx.source_message.get(_DELIVERY_CONTRACT_METADATA_KEY)
    if isinstance(captured, dict) and _is_complete_delivery_contract(captured):
        return
    capture = getattr(channel_outbound, "capture_group_delivery_contract", None)
    if not callable(capture):
        return
    contract = await capture(
        task_ctx.target,
        source_message_id=(
            task_ctx.original_message_id or task_ctx.target.reply_to_message_id
        ),
        response_kind="tool_result",
    )
    if not isinstance(contract, dict) or not _is_complete_delivery_contract(contract):
        raise RuntimeError("draw_async_delivery_contract_unavailable")
    task_ctx.source_message[_DELIVERY_CONTRACT_METADATA_KEY] = dict(contract)


def _is_complete_delivery_contract(contract: dict[str, Any]) -> bool:
    if str(contract.get("participation_status") or "") != "must_reply":
        return False
    if not str(contract.get("source_message_id") or "").strip():
        return False
    version = contract.get("participation_policy_version")
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        return False
    try:
        int(version)
    except (TypeError, ValueError):
        return False
    return isinstance(contract.get("send_revalidation_enabled"), bool)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _chain_class_names(exc: BaseException) -> str:
    return " ".join(item.__class__.__name__ for item in _exception_chain(exc)).lower()


def _draw_failure_from_exception(exc: BaseException) -> DrawFailure:
    if isinstance(exc, DrawConfigError):
        return DrawFailure("config_missing", DRAW_CONFIG_ERROR_TEXT)

    if isinstance(exc, DrawApiError):
        message = str(exc or "").lower()
        class_names = _chain_class_names(exc)
        if "找不到这个图片 id" in message or "图片 id" in message or "原图文件不存在" in message:
            return DrawFailure("invalid_image_id", DRAW_IMAGE_ID_ERROR_TEXT)
        if "提示词不能为空" in message:
            return DrawFailure("invalid_params", DRAW_PARAM_ERROR_TEXT)
        if "timeout" in message or "timed out" in message or "超时" in message or "timeout" in class_names:
            return DrawFailure("upstream_timeout", DRAW_TIMEOUT_ERROR_TEXT)
        if "httpstatuserror" in class_names or "接口返回 " in message:
            return DrawFailure("upstream_http_status", DRAW_API_ERROR_TEXT)
        if (
            "未找到图片数据" in message
            or "未返回可解析" in message
            or "空图片" in message
            or "未返回图片内容" in message
            or "base64 图片数据无效" in message
            or "响应中未找到图片数据" in message
        ):
            return DrawFailure("upstream_empty_response", DRAW_EMPTY_RESPONSE_ERROR_TEXT)
        if "引用图片不可读取" in message or "读取引用图片失败" in message:
            return DrawFailure("invalid_params", DRAW_PARAM_ERROR_TEXT)
        return DrawFailure("upstream_request_failed", DRAW_API_ERROR_TEXT)

    return DrawFailure("internal_exception", DRAW_CRASH_ERROR_TEXT)


def _is_retryable_draw_failure(record: DrawTaskRecord) -> bool:
    error_code = str(record.error_code or "").strip()
    if error_code in NON_RETRYABLE_DRAW_FAILURE_CATEGORIES:
        return False
    error_text = f"{error_code} {record.error_message or ''}".lower()
    non_retryable_markers = (
        "draw_storage_dir",
        "不可写",
        "not writable",
        "permission denied",
        "configuration",
        "未配置",
        "图片id无效",
        "图片 id",
        "invalid image",
        "parameter",
        "validation",
        "提示词不能为空",
    )
    return not any(marker in error_text for marker in non_retryable_markers)


def _retry_next_run_at(retry_count: int, retry_backoff_seconds: float) -> str:
    try:
        base_backoff = float(retry_backoff_seconds or 0.0)
    except (TypeError, ValueError):
        base_backoff = 0.0
    base_backoff = max(0.0, base_backoff)
    attempt = max(1, int(retry_count or 1))
    delay = base_backoff * attempt
    if delay <= 0:
        return datetime.now(UTC).isoformat()
    return (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()


def _build_draw_task_context(
    ctx: PipelineContext,
    *,
    prompt: str,
    quality: str,
    command: str,
    source_image_id: str = "",
    source_image_url: str = "",
    source_image_path: str = "",
    source_label: str = "",
) -> DrawTaskContext:
    event = ctx.event
    target = ChannelTarget.from_event(event)
    trace_id = f"{ctx.trace_id}:draw"
    task_id = ""
    if event.channel == Channel.DISCORD:
        task_id = _draw_task_id_for_source_message(
            tenant_id=event.tenant_id,
            channel=channel_id_value(event.channel),
            session_id=event.session_id,
            message_id=event.message_id,
            command=command,
        )
    source_message = event.model_dump(mode="json")
    delivery_contract = _pipeline_delivery_contract(ctx)
    if delivery_contract:
        source_message[_DELIVERY_CONTRACT_METADATA_KEY] = delivery_contract
    return DrawTaskContext(
        task_id=task_id,
        request_id=str(ctx.trace_id or event.trace_id or event.message_id or trace_id),
        command_type=command,
        original_message_id=str(event.message_id or ""),
        requester_user_id=str(event.user_id or ""),
        requester_display_name=str(event.metadata.get("sender_name") or ""),
        prompt=prompt,
        quality=quality,
        created_at=datetime.now(UTC).isoformat(),
        trace_id=trace_id,
        target=target,
        source_message=source_message,
        source_image_id=source_image_id,
        source_image_url=source_image_url,
        source_image_path=source_image_path,
        source_label=source_label,
    )


def _draw_task_id_for_source_message(
    *,
    tenant_id: str,
    channel: str,
    session_id: str,
    message_id: str,
    command: str,
) -> str:
    message_id = str(message_id or "").strip()
    if not message_id:
        return ""
    raw = "\x1f".join(
        [
            str(tenant_id or "").strip(),
            str(channel or "").strip(),
            str(session_id or "").strip(),
            message_id,
            str(command or "").strip().lower(),
        ]
    )
    return f"drawtask_msg_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def _task_log_fields(task_ctx: DrawTaskContext) -> dict[str, object]:
    return {
        "task_id": task_ctx.task_id,
        "request_id": task_ctx.request_id,
        "command": task_ctx.command_type,
        "original_message_id": task_ctx.original_message_id,
        "requester_user_id": task_ctx.requester_user_id,
        "requester_display_name": task_ctx.requester_display_name,
        "session_id": task_ctx.target.session_id,
        "callback_channel": task_ctx.target.channel,
        "callback_session_id": task_ctx.target.session_id,
        "callback_reply_to_message_id": task_ctx.target.reply_to_message_id,
        "trace_id": task_ctx.trace_id,
        "quality": task_ctx.quality,
        "created_at": task_ctx.created_at,
    }


def _task_ctx_with_id(task_ctx: DrawTaskContext, task_id: str) -> DrawTaskContext:
    return DrawTaskContext(
        task_id=task_id,
        request_id=task_ctx.request_id,
        command_type=task_ctx.command_type,
        original_message_id=task_ctx.original_message_id,
        requester_user_id=task_ctx.requester_user_id,
        requester_display_name=task_ctx.requester_display_name,
        prompt=task_ctx.prompt,
        quality=task_ctx.quality,
        created_at=task_ctx.created_at,
        trace_id=task_ctx.trace_id,
        target=task_ctx.target,
        source_message=dict(task_ctx.source_message),
        source_image_id=task_ctx.source_image_id,
        source_image_url=task_ctx.source_image_url,
        source_image_path=task_ctx.source_image_path,
        source_label=task_ctx.source_label,
    )


def _channel_target_json(target: ChannelTarget) -> dict[str, Any]:
    return {
        "tenant_id": target.tenant_id,
        "channel": target.channel,
        "session_id": target.session_id,
        "session_name": target.session_name,
        "session_kind": target.session_kind,
        "user_id": target.user_id,
        "sender_id": target.sender_id,
        "sender_name": target.sender_name,
        "reply_to_message_id": target.reply_to_message_id,
        "metadata": dict(target.metadata or {}),
    }


def _draw_task_command_type(command: str) -> str:
    value = str(command or "").strip().lower()
    if "redraw" in value or "重绘" in value:
        return "redraw"
    return "draw"


def _draw_task_create_from_context(task_ctx: DrawTaskContext) -> DrawTaskCreate:
    target = task_ctx.target
    return DrawTaskCreate(
        task_id=task_ctx.task_id,
        request_id=task_ctx.request_id,
        trace_id=task_ctx.trace_id,
        command_type=_draw_task_command_type(task_ctx.command_type),
        status="queued",
        tenant_id=target.tenant_id,
        channel=target.channel,
        source_key=task_ctx.source,
        chat_id=target.session_id,
        session_id=target.session_id,
        group_id=target.session_id if target.session_kind == "group" else "",
        user_id=target.user_id,
        requester=task_ctx.requester_user_id,
        requester_display_name=task_ctx.requester_display_name,
        original_message_id=task_ctx.original_message_id,
        callback_target=_channel_target_json(target),
        callback_reply_to_message_id=target.reply_to_message_id,
        source_message=dict(task_ctx.source_message),
        prompt=task_ctx.prompt,
        quality=task_ctx.quality,
        size=DRAW_QUALITY_SIZES.get(task_ctx.quality, ""),
        source_image={
            "image_id": task_ctx.source_image_id,
            "image_url": task_ctx.source_image_url,
            "image_path": task_ctx.source_image_path,
            "source_label": task_ctx.source_label,
        },
        next_run_at=(
            datetime.now(UTC) + timedelta(seconds=DRAW_TASK_INLINE_CLAIM_GRACE_SECONDS)
        ).isoformat(),
        created_at=task_ctx.created_at,
    )


def _channel_target_from_task_record(record: DrawTaskRecord) -> ChannelTarget:
    target = dict(record.callback_target or {})
    metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    return ChannelTarget(
        tenant_id=str(target.get("tenant_id") or record.tenant_id or ""),
        channel=str(target.get("channel") or record.channel or ""),
        session_id=str(target.get("session_id") or record.session_id or record.chat_id or ""),
        session_name=str(target.get("session_name") or ""),
        session_kind=str(target.get("session_kind") or ""),
        user_id=str(target.get("user_id") or record.user_id or ""),
        sender_id=str(target.get("sender_id") or record.requester or record.user_id or ""),
        sender_name=str(target.get("sender_name") or record.requester_display_name or ""),
        reply_to_message_id=str(
            target.get("reply_to_message_id")
            or record.callback_reply_to_message_id
            or record.original_message_id
            or ""
        ),
        metadata=dict(metadata),
    )


def _draw_task_context_from_record(record: DrawTaskRecord) -> DrawTaskContext:
    source_image = dict(record.source_image or {})
    return DrawTaskContext(
        task_id=record.task_id,
        request_id=record.request_id,
        command_type=record.command_type,
        original_message_id=record.original_message_id,
        requester_user_id=record.requester,
        requester_display_name=record.requester_display_name,
        prompt=record.prompt,
        quality=record.quality,
        created_at=record.created_at,
        trace_id=record.trace_id,
        target=_channel_target_from_task_record(record),
        source_message=dict(record.source_message or {}),
        source_image_id=str(source_image.get("image_id") or ""),
        source_image_url=str(source_image.get("image_url") or ""),
        source_image_path=str(source_image.get("image_path") or ""),
        source_label=str(source_image.get("source_label") or record.source_key or ""),
    )


async def recover_stale_draw_tasks(
    *,
    store: DrawStore,
    channel_registry: ChannelRegistry,
    stale_seconds: float,
    limit: int = 50,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> dict[str, int]:
    if scope_execution_allowed is None:
        recover = getattr(store, "recover_stale_tasks", None)
        if not callable(recover):
            return {"recovered": 0, "callbacks_sent": 0, "callback_failed": 0}
        records = await recover(
            stale_seconds=stale_seconds,
            limit=limit,
            status="interrupted",
            error_code=DRAW_TASK_INTERRUPTED_ERROR_CODE,
            error_message=DRAW_TASK_INTERRUPTED_ERROR_MESSAGE,
        )
    else:
        list_stale = getattr(store, "list_stale_draw_tasks", None)
        recover_one = getattr(store, "recover_stale_draw_task", None)
        if not callable(list_stale) or not callable(recover_one):
            logger.error("draw.stale_recovery_scope_primitives_missing")
            return {"recovered": 0, "callbacks_sent": 0, "callback_failed": 0}
        candidates = await list_stale(
            stale_seconds=stale_seconds,
            limit=limit,
        )
        records = []
        for candidate in candidates:
            if not await _draw_record_scope_allowed(
                scope_execution_allowed,
                candidate,
            ):
                defer = getattr(store, "defer_stale_draw_task", None)
                if callable(defer):
                    await defer(
                        candidate.task_id,
                        stale_seconds=stale_seconds,
                    )
                else:
                    logger.error(
                        "draw.stale_recovery_scope_defer_unavailable",
                        task_id=candidate.task_id,
                    )
                continue
            recovered_record = await recover_one(
                candidate.task_id,
                stale_seconds=stale_seconds,
                status="interrupted",
                error_code=DRAW_TASK_INTERRUPTED_ERROR_CODE,
                error_message=DRAW_TASK_INTERRUPTED_ERROR_MESSAGE,
            )
            if recovered_record is not None:
                records.append(recovered_record)
    recovered = 0
    callbacks_sent = 0
    callback_failed = 0
    for record in records:
        recovered += 1
        task_ctx = _draw_task_context_from_record(record)
        try:
            if record.status == "completed":
                resend_result = await resend_draw_task_callback(
                    store=store,
                    channel_registry=channel_registry,
                    task=record,
                    scope_execution_allowed=scope_execution_allowed,
                )
                sent = bool(resend_result.get("sent"))
                if resend_result.get("error"):
                    raise RuntimeError(str(resend_result["error"]))
            else:
                sent = await _send_task_callback_once(
                    store=store,
                    channel_registry=channel_registry,
                    task_ctx=task_ctx,
                    text=DRAW_TASK_RECOVERY_CALLBACK_TEXT,
                    scope_execution_allowed=scope_execution_allowed,
                )
        except Exception:
            callback_failed += 1
            logger.warning(
                "draw.stale_recovery_callback_failed",
                **_task_log_fields(task_ctx),
                exc_info=True,
            )
            continue
        if sent:
            callbacks_sent += 1
    if recovered:
        logger.warning(
            "draw.stale_tasks_recovered",
            recovered=recovered,
            callbacks_sent=callbacks_sent,
            callback_failed=callback_failed,
        )
    return {
        "recovered": recovered,
        "callbacks_sent": callbacks_sent,
        "callback_failed": callback_failed,
    }


async def retry_draw_task_once(
    *,
    store: DrawStore,
    channel_registry: ChannelRegistry,
    billing: BillingCoordinator | None,
    task: DrawTaskRecord,
    max_retries: int,
    retry_backoff_seconds: float = 0.0,
    register_background_task: Callable[[asyncio.Task[None]], Awaitable[None] | None] | None = None,
) -> dict[str, object]:
    _ = (channel_registry, billing, register_background_task)
    if task.status not in {"failed", "interrupted"}:
        return {
            "retry_queued": False,
            "error": "draw task is not failed or interrupted",
            "status": task.status,
        }
    max_retries = max(0, int(max_retries or 0))
    if task.retry_count >= max_retries:
        return {
            "retry_queued": False,
            "error": "retry budget exhausted",
            "retry_count": task.retry_count,
            "max_retries": max_retries,
        }
    reserver = getattr(store, "reserve_draw_task_retry", None)
    if callable(reserver):
        reserved = await reserver(task.task_id, max_retries=max_retries)
    else:
        reserved = task
    if reserved is None:
        latest = await store.get_draw_task(task.task_id)
        return {
            "retry_queued": False,
            "error": (
                "retry budget exhausted"
                if latest and latest.retry_count >= max_retries
                else "draw task is not retryable"
            ),
            "retry_count": getattr(latest, "retry_count", task.retry_count),
            "max_retries": max_retries,
        }
    creator = getattr(store, "create_retry_draw_task", None)
    if not callable(creator):
        return {
            "retry_queued": False,
            "needs_worker_support": True,
            "message": "draw retry is eligible but this store cannot create a retry task",
            "retry_count": reserved.retry_count,
            "max_retries": max_retries,
        }
    next_run_at = _retry_next_run_at(reserved.retry_count, retry_backoff_seconds)
    try:
        retry_record = await creator(
            reserved,
            retry_count=reserved.retry_count,
            next_run_at=next_run_at,
        )
    except TypeError:
        retry_record = await creator(reserved, retry_count=reserved.retry_count)
    logger.info(
        "draw.manual_retry_queued",
        task_id=task.task_id,
        retry_task_id=retry_record.task_id,
        retry_count=reserved.retry_count,
        max_retries=max_retries,
        trace_id=retry_record.trace_id,
        status=retry_record.status,
    )
    return {
        "retry_queued": True,
        "task_id": task.task_id,
        "retry_task_id": retry_record.task_id,
        "retry_count": reserved.retry_count,
        "max_retries": max_retries,
        "backoff_seconds": retry_backoff_seconds,
        "next_run_at": retry_record.next_run_at or next_run_at,
    }


async def _maybe_enqueue_auto_retry(
    *,
    store: DrawStore,
    task: DrawTaskRecord,
    max_retries: int,
    retry_backoff_seconds: float,
) -> DrawTaskRecord | None:
    if task.status not in {"failed", "interrupted"}:
        return None
    if not _is_retryable_draw_failure(task):
        logger.info(
            "draw.auto_retry_skipped_non_retryable",
            task_id=task.task_id,
            retry_count=task.retry_count,
            error_code=task.error_code,
        )
        return None
    reserver = getattr(store, "reserve_draw_task_retry", None)
    creator = getattr(store, "create_retry_draw_task", None)
    if not callable(reserver) or not callable(creator):
        return None
    reserved = await reserver(task.task_id, max_retries=max_retries)
    if reserved is None:
        logger.info(
            "draw.auto_retry_budget_exhausted",
            task_id=task.task_id,
            retry_count=task.retry_count,
            max_retries=max_retries,
        )
        return None
    next_run_at = _retry_next_run_at(reserved.retry_count, retry_backoff_seconds)
    try:
        retry_record = await creator(
            reserved,
            retry_count=reserved.retry_count,
            next_run_at=next_run_at,
        )
    except TypeError:
        retry_record = await creator(reserved, retry_count=reserved.retry_count)
    logger.info(
        "draw.auto_retry_queued",
        task_id=task.task_id,
        retry_task_id=retry_record.task_id,
        retry_count=reserved.retry_count,
        max_retries=max_retries,
        next_run_at=retry_record.next_run_at or next_run_at,
    )
    return retry_record


async def drain_queued_draw_tasks(
    *,
    store: DrawStore,
    channel_registry: ChannelRegistry,
    billing: BillingCoordinator | None,
    worker_id: str = "",
    batch_size: int = 5,
    lock_ttl_seconds: float = 900.0,
    auto_retry_enabled: bool = False,
    max_retries: int = 3,
    retry_backoff_seconds: float = 0.0,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> dict[str, int]:
    claimer = getattr(store, "claim_due_draw_tasks", None)
    if not callable(claimer):
        return {"claimed": 0, "completed": 0, "failed": 0, "auto_retried": 0}
    claimed = await claimer(
        limit=batch_size,
        lock_ttl_seconds=lock_ttl_seconds,
        worker_id=worker_id,
    )
    completed = 0
    failed = 0
    auto_retried = 0
    eligible_claimed = 0
    for record in claimed:
        if scope_execution_allowed is not None and not await _draw_record_scope_allowed(
            scope_execution_allowed,
            record,
        ):
            defer = getattr(store, "defer_draw_task_claim", None)
            if callable(defer):
                await defer(
                    record.task_id,
                    worker_id=worker_id,
                    defer_seconds=max(1.0, float(retry_backoff_seconds or 30.0)),
                )
            else:
                logger.error(
                    "draw.queue_scope_defer_unavailable",
                    task_id=record.task_id,
                )
            continue
        eligible_claimed += 1
        await _run_async_draw_job(
            store=store,
            channel_registry=channel_registry,
            billing=billing,
            task_ctx=_draw_task_context_from_record(record),
            reservation=None,
            worker_id=worker_id,
            scope_execution_allowed=scope_execution_allowed,
        )
        latest = await store.get_draw_task(record.task_id)
        if latest is None:
            continue
        if latest.status == "completed":
            completed += 1
            continue
        if latest.status in {"failed", "interrupted"}:
            failed += 1
            if auto_retry_enabled:
                retry_record = await _maybe_enqueue_auto_retry(
                    store=store,
                    task=latest,
                    max_retries=max_retries,
                    retry_backoff_seconds=retry_backoff_seconds,
                )
                if retry_record is not None:
                    auto_retried += 1
    if claimed:
        logger.info(
            "draw.queue_drained",
            worker_id=worker_id,
            claimed=eligible_claimed,
            scope_deferred=len(claimed) - eligible_claimed,
            completed=completed,
            failed=failed,
            auto_retried=auto_retried,
        )
    return {
        "claimed": eligible_claimed,
        "completed": completed,
        "failed": failed,
        "auto_retried": auto_retried,
    }


async def resend_draw_task_callback(
    *,
    store: DrawStore,
    channel_registry: ChannelRegistry,
    task: DrawTaskRecord,
    force: bool = False,
    idempotency_suffix: str = "",
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> dict[str, object]:
    if task.status not in {"completed", "failed", "interrupted"}:
        return {
            "sent": False,
            "error": "draw task is not in a callbackable terminal state",
            "status": task.status,
        }
    if task.callback_sent and not force:
        return {
            "sent": False,
            "skipped": True,
            "reason": "callback already sent",
            "task_id": task.task_id,
        }
    task_ctx = _draw_task_context_from_record(task)
    text = DRAW_TASK_RECOVERY_CALLBACK_TEXT
    image_path = ""
    image_url = ""
    if task.status == "completed":
        text = _draw_success_text(task.result_image_id)
        image_path = task.result_local_path
        image_url = _resolve_draw_public_url(
            store,
            public_path=task.result_public_path,
            source_url=task.result_source_url,
        )
    elif task.status == "failed":
        text = _draw_failure_from_exception(DrawApiError(task.error_message or task.error_code)).text

    error = ""
    try:
        sent = await _send_task_callback_once(
            store=store,
            channel_registry=channel_registry,
            task_ctx=task_ctx,
            text=text,
            image_path=image_path,
            image_url=image_url,
            force=force,
            idempotency_suffix=idempotency_suffix,
            scope_execution_allowed=scope_execution_allowed,
        )
    except Exception:
        logger.warning(
            "draw.manual_callback_resend_failed",
            task_id=task.task_id,
            trace_id=task.trace_id,
            status=task.status,
            exc_info=True,
        )
        sent = False
        error = "callback send failed"
    return {
        "sent": sent,
        "task_id": task.task_id,
        "status": task.status,
        "force": force,
        "error": error,
    }


def _parse_draw_command_args(args: list[str]) -> DrawCommandArgs:
    quality = DRAW_DEFAULT_QUALITY
    remaining: list[str] = []
    index = 0
    while index < len(args):
        item = str(args[index] or "").strip()
        if not item:
            index += 1
            continue
        lower = item.lower()
        if lower in {"--quality", "-q"}:
            if index + 1 >= len(args):
                raise ValueError(DRAW_QUALITY_ERROR_TEXT)
            quality = normalize_draw_quality(args[index + 1])
            index += 2
            continue
        if lower.startswith("--quality="):
            quality = normalize_draw_quality(item.split("=", 1)[1])
            index += 1
            continue
        if lower.startswith("quality="):
            quality = normalize_draw_quality(item.split("=", 1)[1])
            index += 1
            continue
        remaining.append(item)
        index += 1
    return DrawCommandArgs(quality=quality, args=remaining)


def draw_command_billing_metadata(ctx: PipelineContext, args: list[str]) -> dict[str, str]:
    _ = ctx
    parsed = _parse_draw_command_args(args)
    return {"quality": parsed.quality}


def _should_handle_draw_command(ctx: PipelineContext) -> bool:
    event = ctx.event
    return (
        event.message.type == MessageType.TEXT
        and not bool(event.metadata.get("image_url"))
    )


async def _release_draw_reservation_after_cancel(
    billing: BillingCoordinator | None,
    ctx: PipelineContext,
) -> None:
    if ctx.extras.get("_draw_billing_handed_off"):
        return
    reservation = ctx.extras.get("_billing_command_reservation")
    if (
        billing is None
        or not isinstance(reservation, BillingReservation)
        or reservation.amount <= 0
    ):
        return
    await _finish_persistent_operation(billing.release(reservation))
    ctx.extras["_billing_command_settlement"] = "released"


def _ensure_draw_storage_ready(store: DrawStore) -> None:
    ensure_storage_dir = getattr(store, "_ensure_storage_dir", None)
    if not callable(ensure_storage_dir):
        return
    ensure_storage_dir()


async def _send_channel_draw_messages(
    channel_registry: ChannelRegistry,
    task_ctx: DrawTaskContext,
    *,
    text: str,
    image_path: str = "",
    image_url: str = "",
    idempotency_suffix: str = "",
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> None:
    target = task_ctx.target
    outbound = channel_registry.require_outbound_for_target(target)
    source_message = dict(task_ctx.source_message)
    delivery_contract = _task_result_delivery(task_ctx)

    if text.strip():
        if scope_execution_allowed is not None and not await _draw_context_scope_allowed(
            scope_execution_allowed,
            task_ctx,
        ):
            raise _DrawScopeExecutionDenied("draw callback text scope denied")
        command_id = f"channel-reply:{target.tenant_id}:{task_ctx.original_message_id or task_ctx.request_id}:draw-text"
        command_id = f"{command_id}:{idempotency_suffix}" if idempotency_suffix else command_id
        await outbound.send_text(
            target,
            text.strip(),
            ChannelSendOptions(
                trace_id=task_ctx.trace_id,
                source_message=source_message,
                idempotency_key=command_id,
                delivery_metadata={
                    "command_id": command_id,
                    "idempotency_key": command_id,
                    **delivery_contract,
                },
            ),
        )
    if image_path.strip() or image_url.strip():
        if scope_execution_allowed is not None and not await _draw_context_scope_allowed(
            scope_execution_allowed,
            task_ctx,
        ):
            raise _DrawScopeExecutionDenied("draw callback image scope denied")
        command_id = f"channel-reply:{target.tenant_id}:{task_ctx.original_message_id or task_ctx.request_id}:draw-image"
        command_id = f"{command_id}:{idempotency_suffix}" if idempotency_suffix else command_id
        clean_image_url = image_url.strip()
        await outbound.send_image(
            target,
            ChannelMedia(
                image_path="" if clean_image_url else image_path.strip(),
                image_url=clean_image_url,
            ),
            ChannelSendOptions(
                trace_id=task_ctx.trace_id,
                source_message=source_message,
                idempotency_key=command_id,
                delivery_metadata={
                    "command_id": command_id,
                    "idempotency_key": command_id,
                    **delivery_contract,
                },
            ),
        )


async def _send_task_callback_once(
    *,
    store: DrawStore,
    channel_registry: ChannelRegistry,
    task_ctx: DrawTaskContext,
    text: str,
    image_path: str = "",
    image_url: str = "",
    force: bool = False,
    idempotency_suffix: str = "",
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> bool:
    task_id = str(task_ctx.task_id or "").strip()
    stable_suffix = str(idempotency_suffix or "").strip()
    if force and not stable_suffix:
        raise ValueError("forced callback resend requires a stable idempotency suffix")

    if scope_execution_allowed is not None and not await _draw_context_scope_allowed(
        scope_execution_allowed,
        task_ctx,
    ):
        await _defer_draw_callback_claim(
            store,
            task_id,
            claimed=False,
            force=force,
        )
        return False

    async def _claim_send_and_ack() -> bool:
        claimed_callback = False
        if task_id and not force:
            claim = getattr(store, "claim_draw_task_callback", None)
            if callable(claim) and not await claim(task_id):
                logger.info(
                    "draw.callback_skipped",
                    task_id=task_id,
                    trace_id=task_ctx.trace_id,
                    status="already_sent",
                )
                return False
            claimed_callback = callable(claim)
        if scope_execution_allowed is not None and not await _draw_context_scope_allowed(
            scope_execution_allowed,
            task_ctx,
        ):
            await _defer_draw_callback_claim(
                store,
                task_id,
                claimed=claimed_callback,
                force=force,
            )
            return False
        try:
            await _send_channel_draw_messages(
                channel_registry,
                task_ctx,
                text=text,
                image_path=image_path,
                image_url=image_url,
                idempotency_suffix=stable_suffix if force else "",
                scope_execution_allowed=scope_execution_allowed,
            )
        except _DrawScopeExecutionDenied:
            await _defer_draw_callback_claim(
                store,
                task_id,
                claimed=claimed_callback,
                force=force,
            )
            return False
        except (Exception, asyncio.CancelledError) as exc:
            recorder = getattr(store, "mark_draw_task_callback_error", None)
            if task_id and callable(recorder):
                await recorder(task_id, callback_error=str(exc), force=force)
            raise
        marker = getattr(store, "mark_draw_task_callback_sent", None)
        if task_id and callable(marker):
            await marker(task_id)
        return True

    sent, cancellation_requested = await _resolve_persistent_operation(
        _claim_send_and_ack()
    )
    if cancellation_requested:
        raise asyncio.CancelledError()
    return sent


async def _defer_draw_callback_claim(
    store: DrawStore,
    task_id: str,
    *,
    claimed: bool,
    force: bool,
) -> None:
    if not task_id or force:
        return
    if claimed:
        release = getattr(store, "release_draw_task_callback_claim", None)
        if callable(release) and await release(
            task_id,
            reason="scope_execution_denied",
        ):
            return
    recorder = getattr(store, "mark_draw_task_callback_error", None)
    if callable(recorder):
        await recorder(
            task_id,
            callback_error="scope_execution_denied",
            force=False,
        )


async def _draw_context_scope_allowed(
    gate: Callable[[str, str], Awaitable[bool]],
    task_ctx: DrawTaskContext,
) -> bool:
    try:
        return await gate(
            str(task_ctx.target.tenant_id or ""),
            str(task_ctx.target.session_id or ""),
        ) is True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "draw.callback_scope_gate_failed",
            task_id=task_ctx.task_id,
            tenant_id=task_ctx.target.tenant_id,
            session_id=task_ctx.target.session_id,
            error_type=exc.__class__.__name__,
        )
        return False


async def _run_async_draw_job(
    *,
    store: DrawStore,
    channel_registry: ChannelRegistry,
    billing: BillingCoordinator | None,
    task_ctx: DrawTaskContext,
    reservation: BillingReservation | None,
    worker_id: str = "draw-inline-runner",
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> None:
    log_fields = _task_log_fields(task_ctx)
    completion_committed = False
    cancellation_requested = False
    try:
        if task_ctx.task_id:
            claimer = getattr(store, "claim_draw_task_for_execution", None)
            if callable(claimer):
                claimed = await claimer(
                    task_ctx.task_id,
                    worker_id=worker_id,
                    lock_ttl_seconds=getattr(
                        store.settings,
                        "draw_task_lock_ttl_seconds",
                        900.0,
                    ),
                )
                if claimed is not None and claimed.status in {"completed", "failed", "interrupted"}:
                    logger.info(
                        "draw.async_skipped_terminal_task",
                        **log_fields,
                        current_status=claimed.status,
                    )
                    return
                if claimed is not None and (
                    claimed.status != "running"
                    or (claimed.locked_by and claimed.locked_by != worker_id)
                ):
                    logger.info(
                        "draw.async_skipped_locked_task",
                        **log_fields,
                        current_status=claimed.status,
                        locked_by=claimed.locked_by,
                    )
                    return
            else:
                marker = getattr(store, "mark_draw_task_running", None)
                if callable(marker):
                    await marker(task_ctx.task_id)
        if scope_execution_allowed is not None and not await _draw_context_scope_allowed(
            scope_execution_allowed,
            task_ctx,
        ):
            await _defer_running_draw_job(
                store=store,
                billing=billing,
                reservation=reservation,
                task_ctx=task_ctx,
                worker_id=worker_id,
            )
            return
        if task_ctx.source_image_url or task_ctx.source_image_path:
            result = await store.edit_reference_image(
                image_url=task_ctx.source_image_url,
                image_path=task_ctx.source_image_path,
                prompt=task_ctx.prompt,
                trace_id=task_ctx.trace_id,
                quality=task_ctx.quality,
                source_label=task_ctx.source_label or "reference",
            )
        elif task_ctx.source_image_id:
            result = await store.edit_image(
                task_ctx.source_image_id,
                task_ctx.prompt,
                trace_id=task_ctx.trace_id,
                quality=task_ctx.quality,
            )
        else:
            result = await store.generate_image(
                task_ctx.prompt,
                trace_id=task_ctx.trace_id,
                quality=task_ctx.quality,
            )
        image_url = _resolve_draw_public_url(
            store,
            public_path=result.public_path,
            source_url=result.source_url,
        )
        if scope_execution_allowed is not None and not await _draw_context_scope_allowed(
            scope_execution_allowed,
            task_ctx,
        ):
            await _defer_running_draw_job(
                store=store,
                billing=billing,
                reservation=reservation,
                task_ctx=task_ctx,
                worker_id=worker_id,
            )
            return
        completer = getattr(store, "complete_draw_task", None)
        async def _capture_and_complete() -> DrawTaskRecord | None:
            if billing is not None and reservation is not None:
                await billing.capture(reservation)
            if task_ctx.task_id and callable(completer):
                return await completer(task_ctx.task_id, result)
            return None

        completed_record, settlement_cancelled = await _resolve_persistent_operation(
            _capture_and_complete()
        )
        cancellation_requested = cancellation_requested or settlement_cancelled
        if (
            completed_record is not None
            and getattr(completed_record, "status", "") != "completed"
        ):
            logger.warning(
                "draw.async_completion_skipped_for_terminal_task",
                **log_fields,
                current_status=getattr(completed_record, "status", ""),
            )
            return
        completion_committed = True
        try:
            await _send_task_callback_once(
                store=store,
                channel_registry=channel_registry,
                task_ctx=task_ctx,
                text=_draw_success_text(result.image_id),
                image_path=result.local_path,
                image_url=image_url,
                scope_execution_allowed=scope_execution_allowed,
            )
        except asyncio.CancelledError:
            cancellation_requested = True
        except Exception:
            logger.warning("draw.async_callback_failed", **log_fields, exc_info=True)
        if cancellation_requested:
            raise asyncio.CancelledError()
        logger.info(
            "draw.async_completed",
            **log_fields,
            image_id=result.image_id,
            source_image_id=result.source_image_id
            or task_ctx.source_image_id
            or task_ctx.source_label,
            file_name=result.file_name,
            status="completed",
        )
    except asyncio.CancelledError:
        if not completion_committed:
            marker = getattr(store, "fail_draw_task", None)

            async def _release_and_interrupt() -> None:
                if billing is not None and reservation is not None:
                    await billing.release(reservation)
                if task_ctx.task_id and callable(marker):
                    await marker(
                        task_ctx.task_id,
                        status="interrupted",
                        error_code="cancelled",
                        error_message="draw task cancelled",
                    )

            await _finish_persistent_operation(_release_and_interrupt())
            try:
                await _send_task_callback_once(
                    store=store,
                    channel_registry=channel_registry,
                    task_ctx=task_ctx,
                    text=DRAW_INTERRUPTED_TEXT,
                    scope_execution_allowed=scope_execution_allowed,
                )
            except asyncio.CancelledError:
                # The callback helper only re-raises after its durable ack.
                pass
            except Exception:
                logger.warning("draw.async_cancel_callback_failed", **log_fields, exc_info=True)
            logger.warning("draw.async_interrupted", **log_fields, status="interrupted")
        else:
            logger.info(
                "draw.async_cancelled_after_completion",
                **log_fields,
                status="completed",
            )
        raise
    except (DrawConfigError, DrawApiError) as exc:
        failure = _draw_failure_from_exception(exc)
        failure_error = str(exc)
        marker = getattr(store, "fail_draw_task", None)

        async def _release_and_fail() -> DrawTaskRecord | None:
            if billing is not None and reservation is not None:
                await billing.release(reservation)
            if task_ctx.task_id and callable(marker):
                return await marker(
                    task_ctx.task_id,
                    status="failed",
                    error_code=failure.category,
                    error_message=failure_error,
                )
            return None

        failed_record, cancellation_requested = await _resolve_persistent_operation(
            _release_and_fail()
        )
        if failed_record is not None and getattr(failed_record, "status", "") != "failed":
            logger.warning(
                "draw.async_failure_skipped_for_terminal_task",
                **log_fields,
                current_status=getattr(failed_record, "status", ""),
            )
            if cancellation_requested:
                raise asyncio.CancelledError() from None
            return
        try:
            await _send_task_callback_once(
                store=store,
                channel_registry=channel_registry,
                task_ctx=task_ctx,
                text=failure.text,
                scope_execution_allowed=scope_execution_allowed,
            )
        except asyncio.CancelledError:
            cancellation_requested = True
        except Exception:
            logger.warning("draw.async_failure_callback_failed", **log_fields, exc_info=True)
        logger.warning(
            "draw.async_failed",
            **log_fields,
            failure_category=failure.category,
            error=failure_error,
            status="failed",
        )
        if cancellation_requested:
            raise asyncio.CancelledError() from None
    except Exception as exc:
        failure = _draw_failure_from_exception(exc)
        failure_error = str(exc)
        marker = getattr(store, "fail_draw_task", None)

        async def _release_and_fail_crash() -> DrawTaskRecord | None:
            if billing is not None and reservation is not None:
                await billing.release(reservation)
            if task_ctx.task_id and callable(marker):
                return await marker(
                    task_ctx.task_id,
                    status="failed",
                    error_code=failure.category,
                    error_message=failure_error,
                )
            return None

        failed_record, cancellation_requested = await _resolve_persistent_operation(
            _release_and_fail_crash()
        )
        if failed_record is not None and getattr(failed_record, "status", "") != "failed":
            logger.warning(
                "draw.async_crash_skipped_for_terminal_task",
                **log_fields,
                current_status=getattr(failed_record, "status", ""),
            )
            if cancellation_requested:
                raise asyncio.CancelledError() from None
            return
        try:
            await _send_task_callback_once(
                store=store,
                channel_registry=channel_registry,
                task_ctx=task_ctx,
                text=failure.text,
                scope_execution_allowed=scope_execution_allowed,
            )
        except asyncio.CancelledError:
            cancellation_requested = True
        except Exception:
            logger.warning("draw.async_crash_callback_failed", **log_fields, exc_info=True)
        logger.exception(
            "draw.async_crashed",
            **log_fields,
            failure_category=failure.category,
            status="failed",
        )
        if cancellation_requested:
            raise asyncio.CancelledError() from None


async def _defer_running_draw_job(
    *,
    store: DrawStore,
    billing: BillingCoordinator | None,
    reservation: BillingReservation | None,
    task_ctx: DrawTaskContext,
    worker_id: str,
) -> None:
    if billing is not None and reservation is not None:
        await _finish_persistent_operation(billing.release(reservation))
    defer = getattr(store, "defer_draw_task_claim", None)
    if task_ctx.task_id and callable(defer):
        await _finish_persistent_operation(
            defer(
                task_ctx.task_id,
                worker_id=worker_id,
                defer_seconds=30.0,
            )
        )
    logger.info(
        "draw.async_scope_deferred",
        **_task_log_fields(task_ctx),
        worker_id=worker_id,
    )


async def _handle_draw_command(
    store: DrawStore,
    billing: BillingCoordinator | None,
    channel_registry: ChannelRegistry | None,
    register_background_task: Callable[[asyncio.Task[None]], Awaitable[None] | None] | None,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None,
    ctx: PipelineContext,
    args: list[str],
) -> str:
    event = ctx.event
    if event.message.type != MessageType.TEXT:
        raise ValueError("绘图命令当前仅支持文本消息")
    if event.metadata.get("image_url"):
        raise ValueError("当前消息已包含图片, 不能同时作为绘图命令")

    parsed_args = _parse_draw_command_args(args)
    quality = parsed_args.quality
    prompt = " ".join(str(item or "").strip() for item in parsed_args.args).strip()
    quote_text = _quoted_text(ctx)
    if not prompt and not quote_text:
        raise ValueError(DRAW_HELP_TEXT)
    prompt = _compose_draw_prompt(prompt, quote_text)

    command = str(
        ctx.extras.get("_command_canonical") or ctx.extras.get("_command_token") or "/draw"
    )
    try:
        _ensure_draw_storage_ready(store)
    except DrawConfigError as exc:
        raise ValueError(DRAW_CONFIG_ERROR_TEXT) from exc
    try:
        avatar_ref = await resolve_prompt_avatar_reference(
            store,
            session_id=str(event.session_id or ""),
            prompt=prompt,
            trace_id=ctx.trace_id,
        )
    except asyncio.CancelledError:
        await _release_draw_reservation_after_cancel(billing, ctx)
        raise
    return await _handle_draw_request(
        store,
        billing,
        channel_registry,
        register_background_task,
        scope_execution_allowed,
        ctx,
        prompt=prompt,
        quality=quality,
        command=command,
        accepted_text=DRAW_ACCEPTED_TEXT,
        source_image_url=(
            ""
            if avatar_ref is not None and avatar_ref.image_path
            else avatar_ref.avatar_url if avatar_ref is not None else ""
        ),
        source_image_path=avatar_ref.image_path if avatar_ref is not None else "",
        source_label=avatar_ref.source_label if avatar_ref is not None else "",
    )


def _quoted_text(ctx: PipelineContext) -> str:
    metadata = dict(ctx.event.metadata or {})
    text = str(metadata.get("quote_text") or "").strip()
    if text:
        return text[:QUOTE_TEXT_PROMPT_LIMIT]
    return _quote_text_from_record(_record(metadata.get("quote")))[:QUOTE_TEXT_PROMPT_LIMIT]


def _quote_text_from_record(record: dict) -> str:
    candidates = [
        record,
        _record(record.get("message")),
        _record(record.get("quoted_message")),
        _record(record.get("quoted")),
        _record(record.get("raw")),
    ]
    for item in candidates:
        text = _first_quote_str(
            item.get("text"),
            item.get("content"),
            item.get("message_text"),
            item.get("msg"),
            item.get("body"),
            item.get("caption"),
        )
        if text and text not in {"[图片]", "[image]"}:
            return text
    return ""


def _compose_draw_prompt(prompt: str, quote_text: str) -> str:
    prompt = str(prompt or "").strip()
    quote_text = str(quote_text or "").strip()
    if not quote_text:
        return prompt
    if not prompt:
        return quote_text
    if quote_text in prompt:
        return prompt
    return f"{prompt}\n\n引用文本：{quote_text}"


def _quoted_image_source(ctx: PipelineContext) -> tuple[str, str, str]:
    source = quote_image_source_from_metadata(
        dict(ctx.event.metadata or {}),
        session=ctx.session,
    )
    if not source.found:
        return "", "", ""
    return source.image_url, source.image_path, source.label


def _record(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _first_quote_str(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _is_explicit_draw_image_id(store: DrawStore, image_id: str) -> bool:
    image_id = str(image_id or "").strip()
    if not image_id:
        return False
    if image_id.startswith("img_"):
        return True
    resolver = getattr(store, "resolve_image_id", None)
    if not callable(resolver):
        return False
    try:
        return resolver(image_id) is not None
    except Exception:
        return False


async def _handle_redraw_command(
    store: DrawStore,
    billing: BillingCoordinator | None,
    channel_registry: ChannelRegistry | None,
    register_background_task: Callable[[asyncio.Task[None]], Awaitable[None] | None] | None,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None,
    ctx: PipelineContext,
    args: list[str],
) -> str:
    event = ctx.event
    if event.message.type != MessageType.TEXT:
        raise ValueError("重绘命令当前仅支持文本消息")
    if event.metadata.get("image_url"):
        raise ValueError("当前消息已包含图片, 不能同时作为重绘命令")

    quote_image_url, quote_image_path, quote_label = _quoted_image_source(ctx)
    parsed_args = _parse_draw_command_args(args)
    quality = parsed_args.quality
    args = parsed_args.args
    image_id = ""
    source_image_url = ""
    source_image_path = ""
    source_label = ""
    if quote_image_url or quote_image_path:
        first_arg = str(args[0] if args else "").strip()
        if first_arg and len(args) >= 2 and _is_explicit_draw_image_id(store, first_arg):
            image_id = first_arg
            prompt = " ".join(str(item or "").strip() for item in args[1:]).strip()
        else:
            prompt = " ".join(str(item or "").strip() for item in args).strip()
            source_image_url = quote_image_url
            source_image_path = quote_image_path
            source_label = quote_label
    else:
        image_id = str(args[0] if args else "").strip()
        prompt = " ".join(str(item or "").strip() for item in args[1:]).strip()

    if not prompt or (not image_id and not source_image_url and not source_image_path):
        raise ValueError(REDRAW_HELP_TEXT)
    try:
        _ensure_draw_storage_ready(store)
    except DrawConfigError as exc:
        raise ValueError(DRAW_CONFIG_ERROR_TEXT) from exc
    if image_id:
        resolver = getattr(store, "resolve_image_id", None)
        if callable(resolver):
            try:
                found = resolver(image_id)
            except DrawConfigError as exc:
                logger.warning(
                    "draw.redraw_image_id_validation_failed",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    command="/redraw",
                    image_id=image_id,
                    failure_category="config_missing",
                    error=str(exc),
                )
                raise ValueError(DRAW_CONFIG_ERROR_TEXT) from exc
            except Exception as exc:
                logger.warning(
                    "draw.redraw_image_id_validation_failed",
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                    command="/redraw",
                    image_id=image_id,
                    error=str(exc),
                )
                raise ValueError(DRAW_IMAGE_ID_ERROR_TEXT) from exc
            if found is None:
                raise ValueError(DRAW_IMAGE_ID_ERROR_TEXT)

    command = str(
        ctx.extras.get("_command_canonical") or ctx.extras.get("_command_token") or "/redraw"
    )
    return await _handle_draw_request(
        store,
        billing,
        channel_registry,
        register_background_task,
        scope_execution_allowed,
        ctx,
        prompt=prompt,
        quality=quality,
        command=command,
        accepted_text=REDRAW_ACCEPTED_TEXT,
        source_image_id=image_id,
        source_image_url=source_image_url,
        source_image_path=source_image_path,
        source_label=source_label,
    )


async def _handle_draw_request(
    store: DrawStore,
    billing: BillingCoordinator | None,
    channel_registry: ChannelRegistry | None,
    register_background_task: Callable[[asyncio.Task[None]], Awaitable[None] | None] | None,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None,
    ctx: PipelineContext,
    *,
    prompt: str,
    quality: str,
    command: str,
    accepted_text: str,
    source_image_id: str = "",
    source_image_url: str = "",
    source_image_path: str = "",
    source_label: str = "",
) -> str:
    event = ctx.event
    logger.info(
        "draw.command_triggered",
        session_id=event.session_id,
        trace_id=event.trace_id,
        command=command,
        quality=quality,
    )

    reservation = ctx.extras.get("_billing_command_reservation")
    if not isinstance(reservation, BillingReservation):
        reservation = None
    task_ctx = _build_draw_task_context(
        ctx,
        prompt=prompt,
        quality=quality,
        command=command,
        source_image_id=source_image_id,
        source_image_url=source_image_url,
        source_image_path=source_image_path,
        source_label=source_label,
    )

    target = ChannelTarget.from_event(event)
    channel_outbound = (
        channel_registry.outbound_for(target.channel)
        if channel_registry is not None
        else None
    )
    if channel_registry is not None and channel_outbound is not None:
        cancellation_requested = False
        try:
            _, operation_cancelled = await _resolve_persistent_operation(
                _bind_channel_delivery_contract(channel_outbound, task_ctx)
            )
            cancellation_requested = cancellation_requested or operation_cancelled
            inline_worker_id = f"draw-inline-runner-{uuid4().hex}"
            creator = getattr(store, "create_draw_task", None)
            if callable(creator):
                record, operation_cancelled = await _resolve_persistent_operation(
                    creator(_draw_task_create_from_context(task_ctx))
                )
                cancellation_requested = (
                    cancellation_requested or operation_cancelled
                )
                if (
                    task_ctx.task_id
                    and record.task_id == task_ctx.task_id
                    and (
                        record.status != "queued"
                        or record.started_at
                        or record.locked_by
                        or record.finished_at
                    )
                ):
                    ctx.extras["draw_task_id"] = record.task_id
                    ctx.extras["_billing_command_force_release"] = True
                    logger.info(
                        "draw.duplicate_command_skipped",
                        **_task_log_fields(_task_ctx_with_id(task_ctx, record.task_id)),
                        current_status=record.status,
                        locked_by=record.locked_by,
                    )
                    if cancellation_requested:
                        raise asyncio.CancelledError()
                    return accepted_text
                task_ctx = _task_ctx_with_id(task_ctx, record.task_id)
                claimer = getattr(store, "claim_draw_task_for_execution", None)
                if callable(claimer):
                    claimed, operation_cancelled = await _resolve_persistent_operation(
                        claimer(
                            task_ctx.task_id,
                            worker_id=inline_worker_id,
                            lock_ttl_seconds=getattr(
                                store.settings,
                                "draw_task_lock_ttl_seconds",
                                900.0,
                            ),
                        )
                    )
                    cancellation_requested = (
                        cancellation_requested or operation_cancelled
                    )
                    if (
                        claimed is None
                        or claimed.status != "running"
                        or claimed.locked_by != inline_worker_id
                    ):
                        ctx.extras["draw_task_id"] = task_ctx.task_id
                        ctx.extras["_billing_command_force_release"] = True
                        logger.info(
                            "draw.duplicate_command_claim_skipped",
                            **_task_log_fields(task_ctx),
                            current_status=getattr(claimed, "status", ""),
                            locked_by=getattr(claimed, "locked_by", ""),
                        )
                        if cancellation_requested:
                            raise asyncio.CancelledError()
                        return accepted_text
            ctx.extras["_billing_command_deferred"] = True
            ctx.extras["draw_task_id"] = task_ctx.task_id
            ctx.extras["_draw_billing_handed_off"] = True
            task = asyncio.create_task(
                _run_async_draw_job(
                    store=store,
                    channel_registry=channel_registry,
                    billing=billing,
                    task_ctx=task_ctx,
                    reservation=reservation,
                    worker_id=inline_worker_id,
                    scope_execution_allowed=scope_execution_allowed,
                )
            )
            if register_background_task is not None:
                maybe_awaitable = register_background_task(task)
                if maybe_awaitable is not None:
                    _, operation_cancelled = await _resolve_persistent_operation(
                        maybe_awaitable
                    )
                    cancellation_requested = (
                        cancellation_requested or operation_cancelled
                    )
            if cancellation_requested:
                raise asyncio.CancelledError()
            logger.info(
                "draw.async_accepted",
                **_task_log_fields(task_ctx),
                status="queued",
            )
            return accepted_text
        except asyncio.CancelledError:
            await _release_draw_reservation_after_cancel(billing, ctx)
            raise

    try:
        if source_image_url or source_image_path:
            result = await store.edit_reference_image(
                image_url=source_image_url,
                image_path=source_image_path,
                prompt=prompt,
                trace_id=ctx.trace_id,
                quality=quality,
                source_label=source_label or "reference",
            )
        elif source_image_id:
            result = await store.edit_image(
                source_image_id,
                prompt,
                trace_id=ctx.trace_id,
                quality=quality,
            )
        else:
            result = await store.generate_image(prompt, trace_id=ctx.trace_id, quality=quality)
    except asyncio.CancelledError:
        await _release_draw_reservation_after_cancel(billing, ctx)
        raise
    except (DrawConfigError, DrawApiError) as exc:
        failure = _draw_failure_from_exception(exc)
        logger.warning(
            "draw.command_failed",
            session_id=event.session_id,
            trace_id=event.trace_id,
            command=command,
            request_id=task_ctx.request_id,
            failure_category=failure.category,
            error=str(exc),
        )
        raise ValueError(failure.text) from exc

    ctx.extras["draw_result"] = {
        "image_id": result.image_id,
        "prompt": prompt,
        "quality": quality,
        "local_path": result.local_path,
        "file_name": result.file_name,
        "media_type": result.media_type,
        "public_path": result.public_path,
        "source_url": result.source_url,
        "source_image_id": result.source_image_id,
        "image_url": _resolve_draw_public_url(
            store,
            public_path=result.public_path,
            source_url=result.source_url,
        ),
        "text": _draw_success_text(result.image_id),
    }
    return _draw_success_text(result.image_id)


def build_draw_command_definitions(
    store: DrawStore,
    billing: BillingCoordinator | None = None,
    channel_registry: ChannelRegistry | None = None,
    register_background_task: Callable[[asyncio.Task[None]], Awaitable[None] | None] | None = None,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> list[CommandDefinition]:
    async def _command(ctx: PipelineContext, args: list[str]) -> str:
        return await _handle_draw_command(
            store,
            billing,
            channel_registry,
            register_background_task,
            scope_execution_allowed,
            ctx,
            args,
        )

    async def _redraw_command(ctx: PipelineContext, args: list[str]) -> str:
        return await _handle_redraw_command(
            store,
            billing,
            channel_registry,
            register_background_task,
            scope_execution_allowed,
            ctx,
            args,
        )

    return [
        CommandDefinition(
            plugin_name="draw",
            command="/draw",
            aliases=("/画图",),
            description="根据提示词生成图片并回传到当前会话",
            usage=DRAW_HELP_TEXT,
            handler=_command,
            billing_metadata=draw_command_billing_metadata,
            should_handle=_should_handle_draw_command,
        ),
        CommandDefinition(
            plugin_name="draw",
            command="/redraw",
            aliases=("/重绘",),
            description="按图片 ID 或引用图片带入提示词重绘",
            usage=REDRAW_HELP_TEXT,
            handler=_redraw_command,
            billing_metadata=draw_command_billing_metadata,
            should_handle=_should_handle_draw_command,
        ),
    ]


@dataclass
class DrawReplyHook:
    name: str = "draw.reply"
    point: HookPoint = HookPoint.BEFORE_POSTPROCESS
    priority: int = 10

    async def run(self, ctx: PipelineContext) -> None:
        draw_result = ctx.extras.get("draw_result")
        if not isinstance(draw_result, dict):
            return
        if ctx.result is None:
            return
        image_path = str(draw_result.get("local_path") or "").strip()
        if not image_path:
            return

        metadata = dict(ctx.result.metadata or {})
        metadata["draw"] = {
            "image_id": draw_result.get("image_id", ""),
            "prompt": draw_result.get("prompt", ""),
            "file_name": draw_result.get("file_name", ""),
            "local_path": draw_result.get("local_path", ""),
            "public_path": draw_result.get("public_path", ""),
            "media_type": draw_result.get("media_type", ""),
            "source_url": draw_result.get("source_url", ""),
            "source_image_id": draw_result.get("source_image_id", ""),
            "image_url": draw_result.get("image_url", ""),
        }
        metadata["reply_segments"] = [
            {
                "type": ReplyType.TEXT.value,
                "content": str(draw_result.get("text") or DRAW_SUCCESS_TEXT),
            },
            {
                "type": ReplyType.TEXT.value,
                "content": "",
                "metadata": {
                    "wxbot_msg_type": "image",
                    "image_path": image_path,
                    "image_url": str(draw_result.get("image_url") or ""),
                    "public_path": str(draw_result.get("public_path") or ""),
                    "file_name": str(draw_result.get("file_name") or ""),
                    "image_id": str(draw_result.get("image_id") or ""),
                    "source_image_id": str(draw_result.get("source_image_id") or ""),
                    "media_type": str(draw_result.get("media_type") or ""),
                },
            },
        ]
        ctx.result.reply_text = str(draw_result.get("text") or DRAW_SUCCESS_TEXT)
        ctx.result.metadata = metadata


def _sync_draw_signal(ctx: PipelineContext) -> dict[str, object]:
    draw_result = ctx.extras.get("draw_result")
    payload = dict(draw_result) if isinstance(draw_result, dict) else {}
    ctx.signals.setdefault("draw", {})["result"] = payload
    return payload


def _publish_media_effect(ctx: PipelineContext, draw_result: dict[str, object]) -> MessageEffect:
    return MessageEffect(
        type="publish_media",
        owner="draw",
        payload={
            "commit_semantics": EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
            "tenant_id": ctx.event.tenant_id,
            "session_id": ctx.event.session_id,
            "user_id": ctx.event.user_id,
            "channel": channel_id_value(ctx.event.channel),
            "image_url": str(draw_result.get("image_url") or ""),
            "image_path": str(
                draw_result.get("image_path") or draw_result.get("local_path") or ""
            ),
            "image_id": str(draw_result.get("image_id") or ""),
            "file_name": str(draw_result.get("file_name") or ""),
            "media_type": str(draw_result.get("media_type") or ""),
            "trace_id": ctx.event.trace_id,
        },
        idempotency_key=(
            "draw:publish_media:"
            f"{ctx.event.tenant_id}:{channel_id_value(ctx.event.channel)}:"
            f"{ctx.event.session_id}:{ctx.event.trace_id}"
        ),
    )


@dataclass
class DrawPublishMediaEffectHandler:
    """Record draw media publication effects without re-sending channel replies."""

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        payload = dict(effect.payload)
        ctx.signals.setdefault("effects", {}).setdefault("draw", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "idempotency_key": effect.idempotency_key,
                "channel": str(
                    payload.get("channel") or channel_id_value(ctx.event.channel)
                ),
                "session_id": str(payload.get("session_id") or ctx.event.session_id),
                "image_id": str(payload.get("image_id") or ""),
                "image_path": str(payload.get("image_path") or ""),
                "image_url": str(payload.get("image_url") or ""),
                "status": "audited",
            }
        )


def _draw_channel_reply_effects(
    ctx: PipelineContext,
    draw_result: dict[str, object],
) -> list[MessageEffect]:
    image_path = str(draw_result.get("image_path") or draw_result.get("local_path") or "").strip()
    image_url = str(draw_result.get("image_url") or "").strip()
    if not image_path and not image_url:
        return []
    target = ChannelTarget.from_event(ctx.event)
    text = str(draw_result.get("text") or DRAW_SUCCESS_TEXT)
    source_message: dict[str, Any] = {
        "plugin": "draw",
        "trace_id": ctx.event.trace_id,
        "session_id": ctx.event.session_id,
        "image_id": str(draw_result.get("image_id") or ""),
    }
    delivery_contract = _pipeline_delivery_contract(ctx)
    if delivery_contract:
        source_message[_DELIVERY_CONTRACT_METADATA_KEY] = dict(delivery_contract)
    owner = "wxbot" if target.channel == "wechat" else target.channel
    text_command_id = (
        f"channel-reply:{ctx.event.tenant_id}:{ctx.event.message_id or ctx.trace_id}:"
        "draw-text"
    )
    image_command_id = (
        f"channel-reply:{ctx.event.tenant_id}:{ctx.event.message_id or ctx.trace_id}:"
        "draw-image"
    )
    base_payload = {
        "tenant_id": target.tenant_id,
        "channel": target.channel,
        "session_id": target.session_id,
        "session_name": target.session_name,
        "session_kind": target.session_kind,
        "user_id": target.user_id,
        "sender_id": target.sender_id,
        "sender_name": target.sender_name,
        "reply_to_message_id": target.reply_to_message_id,
        "trace_id": ctx.event.trace_id,
        "source_message": source_message,
    }
    return [
        MessageEffect(
            type="enqueue_channel_reply",
            owner=owner,
            payload={
                **base_payload,
                "body": {"type": "text", "text": text},
                "delivery": {
                    "command_id": text_command_id,
                    "idempotency_key": text_command_id,
                    **delivery_contract,
                },
                "command_id": text_command_id,
            },
            idempotency_key=text_command_id,
        ),
        MessageEffect(
            type="enqueue_channel_reply",
            owner=owner,
            payload={
                **base_payload,
                "media": {
                    "image_path": image_path,
                    "image_url": image_url,
                },
                "delivery": {
                    "command_id": image_command_id,
                    "idempotency_key": image_command_id,
                    **delivery_contract,
                },
                "command_id": image_command_id,
            },
            idempotency_key=image_command_id,
        ),
    ]


async def _draw_record_scope_allowed(
    gate: Callable[[str, str], Awaitable[bool]],
    record: DrawTaskRecord,
) -> bool:
    tenant_id = str(record.tenant_id or "")
    session_id = str(record.session_id or record.chat_id or "")
    try:
        return await gate(tenant_id, session_id) is True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "draw.task_scope_gate_failed",
            task_id=record.task_id,
            tenant_id=tenant_id,
            session_id=session_id,
            error_type=exc.__class__.__name__,
        )
        return False


@dataclass
class DrawPostprocessResultStep:
    channel_reply_effects_enabled: bool = False
    kind: str = "plugin.draw.postprocess_result"
    owner: str = "draw"
    name: str = "Postprocess draw result"
    permissions: list[str] = field(default_factory=lambda: ["storage:plugin"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "result"})
    outputs: set[str] = field(
        default_factory=lambda: {
            "signals.draw.result",
            "effects.enqueue_channel_reply",
            "effects.publish_media",
        }
    )
    timeout_seconds: float = 1.0
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        draw_result = _sync_draw_signal(ctx)
        if not draw_result:
            return StepResult(reason="no_draw_result")
        before = ctx.result.reply_text if ctx.result is not None else ""
        await DrawReplyHook().run(ctx)
        if ctx.result is None:
            return StepResult(reason="no_result")
        effects = [_publish_media_effect(ctx, draw_result)]
        if self.channel_reply_effects_enabled:
            channel_effects = _draw_channel_reply_effects(ctx, draw_result)
            if channel_effects:
                ctx.extras["suppress_outbound"] = True
                ctx.extras["skip_assistant_turn"] = True
                effects = [*effects, *channel_effects]
        return StepResult(
            reason="postprocessed" if ctx.result.reply_text != before else "unchanged",
            result=ctx.result,
            publish_outbound=False
            if ctx.extras.get("suppress_outbound")
            else None,
            append_assistant_turn=False
            if ctx.extras.get("skip_assistant_turn")
            else None,
            effects=effects,
        )

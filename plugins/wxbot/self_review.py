from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.common.logging import get_logger
from app.common.types import ChatMessage, ChatRequest, Role
from app.llm.activity import wait_for_llm_activity
from plugins.wxbot.reports import WxbotReportService
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)
_DEFAULT_TZ = "Asia/Shanghai"
_SELF_REVIEW_MAX_CHARS_PER_CHUNK = 20_000
_INLINE_AT_RE = re.compile(r"@\S+")


class SelfReviewPublishError(RuntimeError):
    """Base class for manual self-review publication failures."""


class SelfReviewJobNotFound(SelfReviewPublishError):
    """Raised when a job does not exist in the requested tenant."""


class SelfReviewJobNotReady(SelfReviewPublishError):
    """Raised when a job has no completed draft that can be reviewed."""


class SelfReviewPublishFailed(SelfReviewPublishError):
    """Raised when the approved draft could not be persisted to the KB."""


def _safe_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or _DEFAULT_TZ))
    except Exception:
        return ZoneInfo(_DEFAULT_TZ)


def _coerce_hour(value: Any, default: int = 23) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(hour, 0), 23)


def resolve_self_review_preview_period(*, date: str = "", tz: str = _DEFAULT_TZ) -> tuple[str, str]:
    period_key = str(date or "").strip() or (datetime.now(_safe_tz(tz)).date() - timedelta(days=1)).strftime("%Y-%m-%d")
    return period_key, period_key


def resolve_self_review_due_period(
    subscription: dict[str, Any],
    now: datetime | None = None,
) -> tuple[str, str] | None:
    if not bool(subscription.get("enabled")):
        return None
    tz = _safe_tz(subscription.get("tz"))
    now_local = (now or datetime.now(tz)).astimezone(tz)
    fire_time = now_local.replace(hour=_coerce_hour(subscription.get("daily_hour")), minute=0, second=0, microsecond=0)
    if now_local < fire_time:
        return None
    period_key = (now_local.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    return period_key, period_key


def seconds_to_next_self_review_fire(
    subscriptions: list[dict[str, Any]],
    now: datetime | None = None,
) -> float:
    if not subscriptions:
        return 3600.0
    now_dt = now or datetime.now(ZoneInfo(_DEFAULT_TZ))
    fire_times: list[float] = []
    for sub in subscriptions:
        if not bool(sub.get("enabled")):
            continue
        tz = _safe_tz(sub.get("tz"))
        now_local = now_dt.astimezone(tz)
        target = now_local.replace(hour=_coerce_hour(sub.get("daily_hour")), minute=0, second=0, microsecond=0)
        if target <= now_local:
            target += timedelta(days=1)
        fire_times.append(target.timestamp())
    if not fire_times:
        return 3600.0
    return max(10.0, min(fire_times) - now_dt.timestamp())


def _message_text(item: dict[str, Any]) -> str:
    msg_type = str(item.get("msg_type") or "text").strip().lower()
    text = str(item.get("text") or "").strip()
    if text:
        return text
    placeholders = {
        "image": "[图片]",
        "audio": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "event": "[事件]",
    }
    return placeholders.get(msg_type, f"[{msg_type or '消息'}]")


def _message_line(item: dict[str, Any]) -> str:
    timestamp = str(item.get("timestamp") or "").strip()
    sender_name = str(item.get("sender_name") or item.get("sender_wxid") or "未知成员").strip() or "未知成员"
    if bool(item.get("is_self_sent")):
        sender_name = f"{sender_name}(机器人)"
    text = _message_text(item)
    if timestamp:
        return f"[{timestamp}] {sender_name}: {text}"
    return f"{sender_name}: {text}"


def _looks_like_bot_trigger(item: dict[str, Any]) -> bool:
    text = _message_text(item).strip()
    if not text:
        return False
    if text.startswith("/"):
        return True
    if text.startswith("@"):
        return True
    return bool(_INLINE_AT_RE.search(text[:24]))


def _build_focus_windows(messages: list[dict[str, Any]]) -> tuple[list[list[dict[str, Any]]], dict[str, int]]:
    selected_indices: set[int] = set()
    bot_reply_count = 0
    trigger_count = 0
    for index, item in enumerate(messages):
        is_bot = bool(item.get("is_self_sent"))
        is_trigger = _looks_like_bot_trigger(item)
        if not is_bot and not is_trigger:
            continue
        if is_bot:
            bot_reply_count += 1
        if is_trigger and not is_bot:
            trigger_count += 1
        start = max(0, index - 3)
        end = min(len(messages), index + 2)
        selected_indices.update(range(start, end))

    if not selected_indices:
        fallback_start = max(0, len(messages) - 30)
        selected_indices.update(range(fallback_start, len(messages)))

    windows: list[list[dict[str, Any]]] = []
    current_indices: list[int] = []
    previous_index: int | None = None
    for index in sorted(selected_indices):
        if previous_index is not None and index - previous_index > 1 and current_indices:
            windows.append([messages[item_index] for item_index in current_indices])
            current_indices = []
        current_indices.append(index)
        previous_index = index
    if current_indices:
        windows.append([messages[item_index] for item_index in current_indices])

    return windows, {
        "focused_message_count": len(selected_indices),
        "focused_thread_count": len(windows),
        "bot_message_count": bot_reply_count,
        "trigger_message_count": trigger_count,
    }


def _window_blocks(windows: list[list[dict[str, Any]]]) -> list[str]:
    blocks: list[str] = []
    for index, window in enumerate(windows, start=1):
        lines = [_message_line(item) for item in window]
        if not lines:
            continue
        blocks.append(f"## 交互片段 {index}\n" + "\n".join(lines))
    return blocks


def _chunk_blocks(blocks: list[str], max_chars: int = _SELF_REVIEW_MAX_CHARS_PER_CHUNK) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in blocks:
        block_len = len(block) + 2
        if current and current_len + block_len > max_chars:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = block_len
            continue
        current.append(block)
        current_len += block_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


class WxbotSelfReviewService:
    def __init__(
        self,
        store: WxbotStore,
        container: Any,
        *,
        bridge: Any | None = None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._container = container
        self._scope_gate = scope_execution_allowed
        self._report_service = WxbotReportService(
            store,
            container,
            bridge=bridge,
            scope_execution_allowed=scope_execution_allowed,
        )
        self._publish_locks: dict[int, asyncio.Lock] = {}

    async def _scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        if not callable(self._scope_gate):
            logger.error(
                "wxbot.self_review_scope_execution_gate_missing",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            return await self._scope_gate(tenant_id, session_id) is True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "wxbot.self_review_scope_execution_gate_error",
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False

    async def scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        """Expose the fail-closed lifecycle gate to route orchestration."""

        return await self._scope_execution_allowed(tenant_id, session_id)

    async def fetch_messages_payload(
        self,
        session_id: str,
        *,
        session_name: str,
        period_key: str,
    ) -> dict[str, Any]:
        return await self._report_service.fetch_report_messages_payload(
            session_id,
            session_name=session_name,
            report_type="daily",
            date=period_key,
        )

    async def _call_llm(self, *, trace_id: str, system: str, user: str, max_tokens: int) -> str:
        timeout = float(getattr(self._store.settings, "wxbot_report_stage_timeout_seconds", 240.0) or 240.0)
        backend = str(
            getattr(self._store.settings, "wxbot_self_review_llm_backend", "")
            or getattr(self._store.settings, "wxbot_report_llm_backend", "http")
            or "http"
        )
        from plugins.local_agent.complete import complete_chat, resolve_local_backend

        if resolve_local_backend(backend):
            result = await complete_chat(
                self._store.settings,
                backend=backend,
                system=system,
                user=user,
                timeout_seconds=timeout,
            )
            return result.content
        llm_service = getattr(self._container, "llm_service", None)
        if llm_service is None:
            raise RuntimeError("LLM service not available")
        request = ChatRequest(
            tenant_id=str(getattr(self._store.settings, "wxbot_default_tenant_id", "default") or "default"),
            trace_id=trace_id,
            model_tier="tier-2",
            messages=[ChatMessage(role=Role.USER, content=user)],
            system=system,
            max_tokens=max_tokens,
            temperature=0.2,
            metadata={"disable_openai_fallback": True, "wxbot_self_review_job": True},
        )
        response = await wait_for_llm_activity(
            llm_service.chat(request),
            timeout=timeout,
        )
        return str(response.content or "").strip()

    async def _write_kb_document(
        self,
        *,
        tenant_id: str,
        session_id: str,
        title: str,
        content: str,
        metadata: dict[str, Any],
    ) -> int:
        kb_service = getattr(self._container, "_kb_service", None) or getattr(self._container, "kb_service", None)
        if kb_service is None:
            raise RuntimeError("KB service not available")
        return await kb_service.add_document(
            tenant_id=tenant_id,
            session_id=session_id,
            title=title,
            content=content,
            source="wxbot_self_review",
            metadata=metadata,
        )

    async def publish_self_review_job(
        self,
        job_id: int,
        *,
        tenant_id: str,
        actor: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Publish one completed draft after an explicit administrator action."""

        lock = self._publish_locks.setdefault(int(job_id), asyncio.Lock())
        async with lock:
            job = await self._store.get_self_review_job(int(job_id))
            if not job or str(job.get("tenant_id") or "") != str(tenant_id or ""):
                raise SelfReviewJobNotFound("self review job not found")
            review_payload = dict(job.get("review_payload") or {})
            run_attempt = int(job.get("run_attempt") or 0)
            existing_doc_id = job.get("kb_doc_id") or review_payload.get("kb_doc_id")
            title = str(job.get("kb_doc_title") or "").strip() or self._review_title(job)
            if existing_doc_id is not None:
                return {
                    "job_id": int(job_id),
                    "tenant_id": tenant_id,
                    "kb_doc_id": int(existing_doc_id),
                    "kb_doc_title": title,
                    "kb_publish_status": "published",
                    "idempotent": True,
                }

            if not await self._scope_execution_allowed(
                str(tenant_id or ""),
                str(job.get("session_id") or ""),
            ):
                raise SelfReviewJobNotReady("self review scope execution denied")

            result_text = str(job.get("result_text") or "").strip()
            if str(job.get("status") or "") != "completed" or not result_text:
                raise SelfReviewJobNotReady(
                    "self review job must be completed and contain result_text"
                )

            normalized_actor = str(actor or "admin").strip() or "admin"
            normalized_request_id = str(request_id or "").strip()
            published_at = datetime.now(_safe_tz("UTC")).isoformat()
            metadata = {
                "category": "wxbot_self_review",
                "period": str(job.get("period_key") or job.get("period_label") or ""),
                "session_id": str(job.get("session_id") or ""),
                "session_name": str(job.get("session_name") or ""),
                "focus_mode": str(review_payload.get("focus_mode") or "bot_interactions"),
                "message_count": int(job.get("msg_count") or 0),
                "focused_message_count": int(
                    review_payload.get("focused_message_count") or 0
                ),
                "focused_thread_count": int(
                    review_payload.get("focused_thread_count") or 0
                ),
                "self_review_job_id": int(job_id),
                "reviewed": True,
                "reviewed_by": normalized_actor,
                "reviewed_request_id": normalized_request_id,
                "reviewed_at": published_at,
                "published_by": normalized_actor,
                "published_request_id": normalized_request_id,
                "published_at": published_at,
            }

            try:
                kb_doc_id = int(
                    await self._write_kb_document(
                        tenant_id=tenant_id,
                        session_id=str(job.get("session_id") or ""),
                        title=title,
                        content=result_text,
                        metadata=metadata,
                    )
                )
                if kb_doc_id <= 0:
                    raise RuntimeError("KB service returned an invalid document id")
            except Exception as exc:
                error = str(exc).strip() or "knowledge document publication failed"
                failed_payload = {
                    **review_payload,
                    "auto_create_kb_doc": False,
                    "kb_doc_id": None,
                    "kb_doc_title": title,
                    "kb_doc_error": error,
                    "kb_publish_status": "pending_review",
                }
                try:
                    await self._store.update_self_review_job(
                        int(job_id),
                        status="completed",
                        current_stage="completed",
                        review_payload=failed_payload,
                        kb_doc_title=title,
                        error="",
                        expected_run_attempt=run_attempt,
                        expected_status="completed",
                    )
                except Exception:
                    logger.exception(
                        "wxbot.self_review_publish_failure_state_write_failed",
                        job_id=job_id,
                        tenant_id=tenant_id,
                    )
                logger.warning(
                    "wxbot.self_review_publish_failed",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    error=error,
                )
                raise SelfReviewPublishFailed(
                    "self review knowledge publication failed"
                ) from exc

            published_payload = {
                **review_payload,
                "auto_create_kb_doc": False,
                "kb_doc_id": kb_doc_id,
                "kb_doc_title": title,
                "kb_doc_error": "",
                "kb_publish_status": "published",
                "reviewed_by": normalized_actor,
                "reviewed_request_id": normalized_request_id,
                "reviewed_at": published_at,
                "published_by": normalized_actor,
                "published_request_id": normalized_request_id,
                "published_at": published_at,
            }
            updated = await self._store.update_self_review_job(
                int(job_id),
                status="completed",
                current_stage="completed",
                result_text=result_text,
                review_payload=published_payload,
                kb_doc_id=kb_doc_id,
                kb_doc_title=title,
                error="",
                expected_run_attempt=run_attempt,
                expected_status="completed",
            )
            if not updated:
                raise SelfReviewPublishFailed(
                    "self review changed while knowledge publication was in progress"
                )
            return {
                "job_id": int(job_id),
                "tenant_id": tenant_id,
                "kb_doc_id": kb_doc_id,
                "kb_doc_title": title,
                "kb_publish_status": "published",
                "idempotent": False,
            }

    @staticmethod
    def _review_title(job: dict[str, Any]) -> str:
        session_id = str(job.get("session_id") or "")
        session_name = str(job.get("session_name") or session_id)
        period = str(job.get("period_key") or job.get("period_label") or "")
        return f"[{session_name}] 自我迭代复盘 · {period}"

    async def _update_self_review_job_for_attempt(
        self,
        job_id: int,
        run_attempt: int,
        **changes: Any,
    ) -> bool:
        return bool(
            await self._store.update_self_review_job(
                job_id,
                expected_run_attempt=run_attempt,
                expected_status="running",
                **changes,
            )
        )

    async def _scope_allowed_or_defer_review(
        self,
        *,
        job_id: int,
        run_attempt: int,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        if await self._scope_execution_allowed(tenant_id, session_id):
            return True
        await self._update_self_review_job_for_attempt(
            job_id,
            run_attempt,
            status="pending",
            current_stage="scope_execution_denied",
            error="",
        )
        return False

    async def run_self_review_job(self, job_id: int) -> None:
        job = await self._store.get_self_review_job(job_id)
        if not job:
            return
        tenant_id = str(
            job.get("tenant_id")
            or getattr(
                self._store.settings,
                "wxbot_default_tenant_id",
                "default",
            )
            or "default"
        )
        session_id = str(job.get("session_id") or "")
        if not await self._scope_execution_allowed(tenant_id, session_id):
            return
        run_attempt = await self._store.try_start_self_review_job(job_id)
        if run_attempt is None:
            return
        run_attempt = int(run_attempt)
        job = await self._store.get_self_review_job(job_id)
        if not job:
            return

        session_name = str(job.get("session_name") or session_id)
        period_key = str(job.get("period_key") or "")
        review_title = f"[{session_name}] 自我迭代复盘 · {period_key}"

        try:
            if not await self._scope_allowed_or_defer_review(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            subscription = await self._store.get_self_review_subscription(tenant_id, session_id) or {}
            focus_mode = str(subscription.get("focus_mode") or "bot_interactions")

            payload = await self.fetch_messages_payload(
                session_id,
                session_name=session_name,
                period_key=period_key,
            )
            if not await self._scope_allowed_or_defer_review(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            raw_messages = payload.get("messages") or []
            if not isinstance(raw_messages, list):
                raise RuntimeError("self review messages payload missing messages list")
            messages = [item for item in raw_messages if isinstance(item, dict)]

            windows, focus_stats = _build_focus_windows(messages)
            blocks = _window_blocks(windows)
            chunks = _chunk_blocks(blocks)
            msg_count = len(messages)
            focused_count = int(focus_stats.get("focused_message_count") or 0)
            if not blocks:
                result_text = (
                    f"# {session_name} 自我迭代复盘 · {period_key}\n\n"
                    "## 概览\n"
                    "- 该周期没有可用于分析的重点交互片段。\n\n"
                    "## 发现的问题\n"
                    "- 暂无。\n\n"
                    "## 待人工确认\n"
                    "- 是否该周期没有机器人回复，或原始消息尚未完整入库。"
                )
                review_payload = {
                    "session_id": session_id,
                    "session_name": session_name,
                    "period": period_key,
                    "count": msg_count,
                    "focused_message_count": focused_count,
                    "focused_thread_count": int(focus_stats.get("focused_thread_count") or 0),
                    "bot_message_count": int(focus_stats.get("bot_message_count") or 0),
                    "trigger_message_count": int(focus_stats.get("trigger_message_count") or 0),
                    "focus_mode": focus_mode,
                    "cached": False,
                    "auto_create_kb_doc": False,
                    "kb_doc_id": None,
                    "kb_doc_title": review_title,
                    "kb_doc_error": "",
                    "kb_publish_status": "pending_review",
                }
                if not await self._scope_allowed_or_defer_review(
                    job_id=job_id,
                    run_attempt=run_attempt,
                    tenant_id=tenant_id,
                    session_id=session_id,
                ):
                    return
                await self._update_self_review_job_for_attempt(
                    job_id,
                    run_attempt,
                    status="completed",
                    current_stage="completed",
                    msg_count=msg_count,
                    result_text=result_text,
                    review_payload=review_payload,
                    kb_doc_title=review_title,
                    error="",
                )
                return

            partials: list[str] = []
            for index, chunk in enumerate(chunks, start=1):
                if not await self._scope_allowed_or_defer_review(
                    job_id=job_id,
                    run_attempt=run_attempt,
                    tenant_id=tenant_id,
                    session_id=session_id,
                ):
                    return
                if not await self._update_self_review_job_for_attempt(
                    job_id,
                    run_attempt,
                    status="running",
                    current_stage=f"analyze_chunk_{index}",
                    msg_count=msg_count,
                    error="",
                ):
                    return
                partial = await self._call_llm(
                    trace_id=f"wxbot_self_review_{job_id}_chunk_{index}",
                    system=(
                        "你是微信群机器人质量复盘助手。"
                        "只根据对话证据指出机器人回复、触发、上下文、风格、工具命中和异常兜底方面的问题或亮点。"
                        "不要编造不存在的现象。"
                    ),
                    user=(
                        "下面是同一周期里和机器人相关的群聊交互片段。\n"
                        "请输出四段，且只输出这四段：\n"
                        "[问题]\n"
                        "- ...\n"
                        "[亮点]\n"
                        "- ...\n"
                        "[待确认]\n"
                        "- ...\n"
                        "[建议]\n"
                        "- ...\n\n"
                        "要求：\n"
                        "1. 重点看误触发、漏触发、上下文串台、答非所问、风格失真、命令/FAQ/工具走错、异常兜底不自然。\n"
                        "2. 证据必须来自给定片段，不要脑补。\n"
                        "3. 如果这一段没有明显问题，可以只写亮点和待确认。\n\n"
                        f"交互片段：\n{chunk}"
                    ),
                    max_tokens=1200,
                )
                if not await self._scope_allowed_or_defer_review(
                    job_id=job_id,
                    run_attempt=run_attempt,
                    tenant_id=tenant_id,
                    session_id=session_id,
                ):
                    return
                partials.append(partial)

            if not await self._scope_allowed_or_defer_review(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            if not await self._update_self_review_job_for_attempt(
                job_id,
                run_attempt,
                status="running",
                current_stage="finalize",
                msg_count=msg_count,
                error="",
            ):
                return
            final_text = await self._call_llm(
                trace_id=f"wxbot_self_review_{job_id}_final",
                system=(
                    "你是资深机器人产品与对话质量工程师。"
                    "请把分块复盘结果合并成一份面向开发修复的中文 Markdown 文档。"
                    "文档要尖锐、可执行、基于证据，不要写空话。"
                ),
                user=(
                    f"请基于下面的分块复盘摘要，输出最终《自我迭代问题文档》。\n"
                    "文档格式固定如下：\n"
                    f"# {session_name} 自我迭代复盘 · {period_key}\n"
                    "## 概览\n"
                    "- 该周期总消息数、机器人消息数、重点交互片段数、总体判断\n"
                    "## 发现的问题\n"
                    "### 1. [问题类型] 标题\n"
                    "- 严重度：高/中/低\n"
                    "- 现象：\n"
                    "- 证据：\n"
                    "- 可能原因：\n"
                    "- 修复建议：\n"
                    "- 是否建议进入 Codex 自动修复：是/否\n"
                    "## 表现良好的点\n"
                    "- ...\n"
                    "## 待人工确认\n"
                    "- ...\n"
                    "## 建议的后续动作\n"
                    "- ...\n\n"
                    "约束：\n"
                    "1. 至少给出 2 个高价值问题；如果证据不足，可以把第二个放到待人工确认。\n"
                    "2. 必须覆盖触发策略、上下文理解、回复风格/长度、工具或 FAQ 命中四类中的至少三类。\n"
                    "3. 结论要明确，能直接进入排查或修复。\n"
                    "4. 不要写“继续观察”“整体不错”这种空话。\n\n"
                    f"统计信息：\n"
                    f"- 该周期总消息数: {msg_count}\n"
                    f"- 机器人消息数: {int(focus_stats.get('bot_message_count') or 0)}\n"
                    f"- 重点交互消息数: {focused_count}\n"
                    f"- 重点交互片段数: {int(focus_stats.get('focused_thread_count') or 0)}\n"
                    f"- 触发疑似消息数: {int(focus_stats.get('trigger_message_count') or 0)}\n"
                    f"- focus_mode: {focus_mode}\n\n"
                    "分块复盘摘要：\n" + "\n\n".join(partials)
                ),
                max_tokens=2200,
            )
            if not await self._scope_allowed_or_defer_review(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return

            review_payload = {
                "session_id": session_id,
                "session_name": session_name,
                "period": period_key,
                "count": msg_count,
                "focused_message_count": focused_count,
                "focused_thread_count": int(focus_stats.get("focused_thread_count") or 0),
                "bot_message_count": int(focus_stats.get("bot_message_count") or 0),
                "trigger_message_count": int(focus_stats.get("trigger_message_count") or 0),
                "focus_mode": focus_mode,
                "chunk_count": len(chunks),
                "cached": False,
                "auto_create_kb_doc": False,
                "kb_doc_id": None,
                "kb_doc_title": review_title,
                "kb_doc_error": "",
                "kb_publish_status": "pending_review",
            }
            if not await self._scope_allowed_or_defer_review(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            await self._update_self_review_job_for_attempt(
                job_id,
                run_attempt,
                status="completed",
                current_stage="completed",
                msg_count=msg_count,
                result_text=final_text,
                review_payload=review_payload,
                kb_doc_title=review_title,
                error="",
            )
        except asyncio.CancelledError:
            await self._store.update_self_review_job(
                job_id,
                status="failed",
                current_stage="cancelled",
                error="self review job cancelled during shutdown",
                expected_run_attempt=run_attempt,
                expected_status="running",
            )
            raise
        except Exception as exc:
            current = await self._store.get_self_review_job(job_id)
            updated = await self._store.update_self_review_job(
                job_id,
                status="failed",
                current_stage=str((current or {}).get("current_stage") or "unknown"),
                msg_count=int((current or {}).get("msg_count") or 0),
                error=str(exc).strip() or "self review generation failed",
                expected_run_attempt=run_attempt,
                expected_status="running",
            )
            if not updated:
                return
            logger.warning("wxbot.self_review_job_failed", job_id=job_id, error=str(exc))

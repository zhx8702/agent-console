from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.channel.identity import (
    LEGACY_WXBOT_CONNECTION_ID,
    canonical_conversation_id,
    canonical_message_id,
    require_legacy_wxbot_history_scope,
)
from app.common.config import Settings
from app.common.logging import get_logger
from app.common.safe_url import configure_http_client, safe_get
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request
from plugins.credits.store import CreditStore
from plugins.moderation.store import ModerationStore
from plugins.repeater.store import RepeaterStore
from plugins.wxbot.agent_analysis import (
    _MENTION_NAME_RE,
    _build_research_summary,
    _coerce_int,
    _extract_research_keywords,
    _extract_research_solutions,
    _normalize_research_question,
    _profile_aliases_from_name,
    _profile_candidate_url_platform,
    _profile_evidence_id,
    _profile_extract_terms,
    _profile_facet,
    _profile_normalize_text,
    _profile_redact_text,
    _profile_score_external_candidate,
    _score_research_message,
)
from plugins.wxbot.file_artifacts import (
    SUPPORTED_FILE_FORMATS,
    convert_file_bytes,
    infer_file_format,
    normalize_file_format,
    stage_outbound_artifact,
)
from plugins.wxbot.file_intent import classify_file_intent
from plugins.wxbot.group_file_policy import (
    GroupFilePolicyReader,
    require_group_file_send_enabled,
)
from plugins.wxbot.message_exports import (
    build_message_export_summary,
    cleanup_message_exports,
    stage_message_export,
)
from plugins.wxbot.message_reader import WxbotMessageReader
from plugins.wxbot.reports import WxbotReportService
from plugins.wxbot.store import WxbotStore

_OwnedReadResult = TypeVar("_OwnedReadResult")
_DELIVERY_CONTRACT_METADATA_KEY = "_wxbot_delivery_contract"
_MANAGED_MESSAGE_EXPORT_MAX_RECORDS = 10_000
logger = get_logger(__name__)
WxbotDataOwnerScopeExecutionAllowed = Callable[
    [str, str, str],
    Awaitable[bool],
]
WxbotDataOwnersScopeExecutionAllowed = Callable[
    [tuple[str, ...], str, str],
    Awaitable[bool],
]


async def _gather_cancel_on_error(
    *awaitables: Awaitable[Any],
) -> tuple[Any, ...]:
    """Gather concurrent reads without leaving siblings running after failure."""

    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class WxbotAgentToolService:
    def __init__(
        self,
        settings: Settings,
        *,
        wxbot_store: WxbotStore | None = None,
        credits_store: CreditStore | None = None,
        moderation_store: ModerationStore | None = None,
        repeater_store: RepeaterStore | None = None,
        report_service: WxbotReportService | None = None,
        effect_reply_enabled: bool = False,
        message_export_root: str | Path | None = None,
        message_export_max_bytes: int | None = None,
        data_owner_scope_execution_allowed: (
            WxbotDataOwnerScopeExecutionAllowed | None
        ) = None,
        data_owners_scope_execution_allowed: (
            WxbotDataOwnersScopeExecutionAllowed | None
        ) = None,
        social_policy_store: GroupFilePolicyReader | None = None,
    ) -> None:
        self._settings = settings
        self._wxbot_store = wxbot_store or WxbotStore(settings)
        self._credits_store = credits_store or CreditStore(settings)
        self._moderation_store = moderation_store or ModerationStore(settings)
        self._repeater_store = repeater_store or RepeaterStore(settings)
        self._report_service = report_service
        self._effect_reply_enabled = bool(effect_reply_enabled)
        self._message_export_root = str(
            message_export_root
            or getattr(settings, "wxbot_outbound_file_dir", "/data/wxbot-outbound")
            or "/data/wxbot-outbound"
        )
        configured_max_bytes = (
            message_export_max_bytes
            if message_export_max_bytes is not None
            else getattr(
                settings,
                "wxbot_outbound_file_max_bytes",
                10 * 1024 * 1024,
            )
        )
        self._message_export_max_bytes = int(configured_max_bytes)
        self._data_owner_scope_execution_allowed = data_owner_scope_execution_allowed
        self._data_owners_scope_execution_allowed = data_owners_scope_execution_allowed
        self._social_policy_store = social_policy_store
        self._sdk_scope: ContextVar[dict[str, str] | None] = ContextVar(
            f"wxbot_agent_sdk_scope_{id(self)}",
            default=None,
        )

    async def export_current_messages_file(
        self,
        session: Any,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Export current-conversation records and queue the selected file format."""

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_id = self._external_session_id(session).strip()
        if not session_id:
            raise ValueError("当前会话标识不可用")
        await self._cleanup_message_export_artifacts()
        latest_turn, latest_metadata = self._latest_user_context(session)
        session_metadata = dict(getattr(session, "metadata", {}) or {})
        session_name = str(
            latest_metadata.get("session_name")
            or session_metadata.get("session_name")
            or session_id
        ).strip() or session_id

        report_type = str(arguments.get("report_type") or "daily").strip().lower()
        if report_type not in {"daily", "monthly"}:
            raise ValueError("report_type 仅支持 daily 或 monthly")
        export_format = normalize_file_format(arguments.get("format") or "txt")
        request_intent = classify_file_intent(
            str(getattr(latest_turn, "content", "") or ""),
        )
        if request_intent.requested_format:
            if request_intent.requested_format not in {"txt", "md", "csv", "json"}:
                raise ValueError(
                    f"当前版本不支持 {request_intent.requested_format} 导出，请选择 txt、md、csv 或 json"
                )
            if export_format != request_intent.requested_format:
                raise ValueError("工具格式与用户明确要求的文件格式不一致")

        date, year_month = self._message_export_period_arguments(
            report_type,
            date=str(arguments.get("date") or "").strip(),
            year_month=str(arguments.get("year_month") or "").strip(),
        )
        session_kind = self._message_export_session_kind(
            session,
            latest_metadata=latest_metadata,
            session_id=session_id,
        )
        await self._require_session_group_file_send_enabled(
            tenant_id=tenant_id,
            session_id=session_id,
            session_kind=session_kind,
        )
        if session_kind == "group":
            connection_id = (
                str(getattr(session, "connection_id", "") or "").strip()
                or LEGACY_WXBOT_CONNECTION_ID
            )
            period = date if report_type == "daily" else year_month
            if connection_id == LEGACY_WXBOT_CONNECTION_ID:
                require_legacy_wxbot_history_scope(
                    self._settings,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                )
                report_service = self._report_service
                if report_service is None:
                    raise RuntimeError("wxbot report service is unavailable")
                messages_payload = await report_service.fetch_report_messages_payload(
                    session_id,
                    session_name=session_name,
                    report_type=report_type,
                    date=date if report_type == "daily" else "",
                    year_month=year_month if report_type == "monthly" else "",
                )
                if messages_payload.get("ok") is False:
                    raise RuntimeError(
                        str(messages_payload.get("error") or "wxbot report messages failed")
                    )
                raw_messages = messages_payload.get("messages")
                if not isinstance(raw_messages, list):
                    raise RuntimeError("wxbot report messages payload missing messages list")
                messages = [item for item in raw_messages if isinstance(item, dict)]
                period = str(messages_payload.get("period") or period).strip()
            else:
                messages = await self._managed_group_message_records(
                    session,
                    tenant_id=tenant_id,
                    external_session_id=session_id,
                    connection_id=connection_id,
                    report_type=report_type,
                    period=period,
                )
        else:
            period = date if report_type == "daily" else year_month
            messages = self._current_private_message_records(
                session,
                report_type=report_type,
                period=period,
            )
        summary_text = build_message_export_summary(
            session_name,
            period,
            messages,
            report_type=report_type,
        )

        source_message_id = self._message_export_source_id(
            session,
            latest_turn=latest_turn,
            latest_metadata=latest_metadata,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        request_digest = hashlib.sha256(
            "\0".join(
                (
                    tenant_id,
                    session_id,
                    source_message_id,
                    report_type,
                    period,
                    export_format,
                )
            ).encode("utf-8")
        ).hexdigest()[:32]
        request_id = f"wxbot-message-export-{request_digest}"
        artifact = stage_message_export(
            self._message_export_root,
            tenant_id,
            session_id,
            request_id,
            session_name,
            period,
            summary_text,
            messages,
            max_bytes=self._message_export_max_bytes,
            export_format=export_format,
        )

        delivery_contract = self._message_export_delivery_contract(
            session,
            latest_metadata=latest_metadata,
            source_message_id=source_message_id,
        )
        effects = self._message_export_reply_effects(
            session,
            latest_metadata=latest_metadata,
            tenant_id=tenant_id,
            session_id=session_id,
            session_name=session_name,
            source_message_id=source_message_id,
            request_id=request_id,
            request_digest=request_digest,
            period=period,
            artifact=artifact,
            delivery_contract=delivery_contract,
        )
        if self._effect_reply_enabled:
            channel_reply_effects = effects
        else:
            await self._enqueue_message_export_replies(effects)
            channel_reply_effects = []

        return {
            "ok": True,
            "report_type": report_type,
            "period": period,
            "format": export_format,
            "message_count": int(artifact.get("message_count") or 0),
            "file_name": str(artifact.get("file_name") or ""),
            "sent_to_current_session": True,
            "delivery_status": "queued",
            "delivery_acknowledged": False,
            "self_enqueued_reply": True,
            "suppress_final_reply": True,
            "channel_reply_effects": channel_reply_effects,
            "message": "消息记录已整理并排队发送到当前会话。",
        }

    async def inspect_current_file(
        self,
        session: Any,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Read only the recent file attached by the current requester.

        The returned text is explicitly marked as untrusted so prompt-like
        strings inside a document cannot be mistaken for agent instructions.
        """

        _ = arguments
        latest_turn, _ = self._latest_user_context(session)
        if latest_turn is None:
            raise ValueError("当前轮次没有收到文件")
        file_turn, file_metadata = self._latest_file_context(session)
        if file_turn is None:
            raise ValueError("当前会话没有可用的文件附件")
        downloaded = await self._download_current_file(file_metadata, session=session)
        file_name = str(downloaded.get("file_name") or "当前文件").strip()
        content_type = str(downloaded.get("content_type") or "")
        source_format = infer_file_format(file_name, content_type)
        descriptor = {
            key: downloaded.get(key)
            for key in (
                "file_name",
                "file_size",
                "file_sha256",
                "content_type",
                "download_status",
            )
        }
        if source_format not in SUPPORTED_FILE_FORMATS:
            return {
                "ok": True,
                "file": descriptor,
                "extractable": False,
                "reason": "当前版本只支持读取 txt、md、csv、json 文本文件；可以先让用户转换格式。",
            }
        preview = convert_file_bytes(
            bytes(downloaded["content"]),
            source_name=file_name,
            target_format="txt",
            source_content_type=str(downloaded.get("content_type") or ""),
        ).decode("utf-8", errors="replace")
        preview = preview[:12_000]
        return {
            "ok": True,
            "file": descriptor,
            "extractable": True,
            "content": (
                "[UNTRUSTED_FILE_CONTENT_BEGIN]\n"
                + preview
                + "\n[UNTRUSTED_FILE_CONTENT_END]"
            ),
            "truncated": len(preview) >= 12_000,
        }

    async def convert_current_file(
        self,
        session: Any,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert the current inbound text file and send it only when asked."""

        target_format = normalize_file_format(arguments.get("format") or "txt")
        latest_turn, latest_metadata = self._latest_user_context(session)
        if latest_turn is None:
            raise ValueError("当前轮次没有收到文件")
        file_turn, file_metadata = self._latest_file_context(session)
        if file_turn is None:
            raise ValueError("当前会话没有可用的文件附件")
        intent = classify_file_intent(
            str(getattr(latest_turn, "content", "") or ""),
            has_attachment=True,
        )
        if intent.operation != "convert":
            raise ValueError("未检测到明确的文件转换意图")
        if not intent.delivery_required:
            return {
                "ok": False,
                "needs_confirmation": True,
                "delivery_not_requested": True,
                "message": "请先确认目标格式以及是否需要把转换后的文件发送给你。",
            }
        if not intent.requested_format:
            return {
                "ok": False,
                "needs_confirmation": True,
                "format_required": True,
                "message": "请先指定要转换成 txt、md、csv 还是 json，再发送文件。",
            }
        if intent.requested_format:
            if intent.requested_format not in {"txt", "md", "csv", "json"}:
                raise ValueError(
                    f"当前版本不支持转换为 {intent.requested_format}，请使用 txt、md、csv 或 json"
                )
            if target_format != intent.requested_format:
                raise ValueError("工具格式与用户明确要求的目标格式不一致")
        tenant_id = str(getattr(session, "tenant_id", "") or "default").strip() or "default"
        session_id = self._external_session_id(session).strip()
        session_kind = self._message_export_session_kind(
            session,
            latest_metadata=latest_metadata,
            session_id=session_id,
        )
        await self._require_session_group_file_send_enabled(
            tenant_id=tenant_id,
            session_id=session_id,
            session_kind=session_kind,
        )
        downloaded = await self._download_current_file(file_metadata, session=session)
        file_name = str(downloaded.get("file_name") or "当前文件").strip()
        source_format = infer_file_format(
            file_name,
            str(downloaded.get("content_type") or ""),
        )
        if source_format not in SUPPORTED_FILE_FORMATS:
            raise ValueError(
                "当前版本只支持转换 txt、md、csv、json；PDF、Word、Excel 等格式请先转成文本文件"
            )
        content = convert_file_bytes(
            bytes(downloaded["content"]),
            source_name=file_name,
            target_format=target_format,
            source_content_type=str(downloaded.get("content_type") or ""),
        )
        source_id = self._message_export_source_id(
            session,
            latest_turn=latest_turn,
            latest_metadata=latest_metadata,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        digest = hashlib.sha256(
            "\0".join((tenant_id, session_id, source_id, target_format, file_name)).encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        request_id = f"wxbot-file-convert-{digest}"
        artifact = stage_outbound_artifact(
            self._message_export_root,
            tenant_id=tenant_id,
            session_id=session_id,
            request_id=request_id,
            file_name=Path(file_name).stem or "转换文件",
            content=content,
            file_format=target_format,
            max_bytes=self._message_export_max_bytes,
        )
        delivery_contract = self._message_export_delivery_contract(
            session,
            latest_metadata=latest_metadata,
            source_message_id=source_id,
        )
        delivery_session_id = self._canonical_delivery_session_id(
            session,
            external_session_id=session_id,
        )
        reply_to_message_id = self._external_reply_to_message_id(
            session,
            latest_metadata=latest_metadata,
            source_message_id=source_id,
        )
        channel = str(getattr(getattr(session, "channel", "wechat"), "value", "wechat"))
        sender_id = str(
            latest_metadata.get("sender_id")
            or latest_metadata.get("sender_wxid")
            or getattr(session, "external_participant_id", "")
            or getattr(session, "user_id", "")
            or ""
        ).strip()
        command_id = f"channel-reply:{tenant_id}:wxbot-file-convert:{digest}"
        payload = {
            "tenant_id": tenant_id,
            "channel": channel,
            "adapter_id": str(getattr(session, "adapter_id", "") or ""),
            "connection_id": str(getattr(session, "connection_id", "") or ""),
            "session_id": delivery_session_id,
            "external_conversation_id": session_id,
            "canonical_conversation_id": delivery_session_id,
            "session_name": str(latest_metadata.get("session_name") or session_id),
            "session_kind": session_kind,
            "user_id": str(getattr(session, "user_id", "") or ""),
            "sender_id": sender_id,
            "sender_name": str(latest_metadata.get("sender_name") or ""),
            "reply_to_message_id": reply_to_message_id,
            "trace_id": str(latest_metadata.get("trace_id") or source_id)[:64],
            "source_message": {
                "agent_tool": "convert_current_file",
                "message_id": source_id,
                "external_message_id": reply_to_message_id,
                "session_id": delivery_session_id,
                "external_conversation_id": session_id,
            },
            "file": {
                "file_path": artifact["file_path"],
                "file_name": artifact["file_name"],
                "file_size": artifact["file_size"],
                "file_md5": artifact["file_md5"],
                "file_sha256": artifact["file_sha256"],
            },
            "delivery": {
                "channel": channel,
                "adapter_id": str(getattr(session, "adapter_id", "") or ""),
                "connection_id": str(getattr(session, "connection_id", "") or ""),
                "tenant_id": tenant_id,
                "session_id": delivery_session_id,
                "external_conversation_id": session_id,
                "canonical_conversation_id": delivery_session_id,
                "session_kind": session_kind,
                "sender_wxid": sender_id,
                "command_id": command_id,
                "idempotency_key": command_id,
                **delivery_contract,
                "must_deliver_file": True,
            },
            "command_id": command_id,
        }
        effect = {
            "type": "enqueue_channel_reply",
            "owner": "wxbot" if channel == "wechat" else channel,
            "idempotency_key": command_id,
            "payload": payload,
        }
        if self._effect_reply_enabled:
            channel_reply_effects = [effect]
        else:
            await self._enqueue_message_export_replies([effect])
            channel_reply_effects = []
        return {
            "ok": True,
            "file_name": artifact["file_name"],
            "format": target_format,
            "file_size": artifact["file_size"],
            "sent_to_current_session": True,
            "delivery_status": "queued",
            "delivery_acknowledged": False,
            "self_enqueued_reply": True,
            "suppress_final_reply": True,
            "channel_reply_effects": channel_reply_effects,
            "message": "文件已转换并排队发送到当前会话。",
        }

    async def generate_text_file(
        self,
        session: Any,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Stage explicitly requested answer content as a file and queue it."""

        target_format = normalize_file_format(arguments.get("format") or "txt")
        latest_turn, latest_metadata = self._latest_user_context(session)
        if latest_turn is None:
            raise ValueError("当前轮次没有可生成文件的请求")
        intent = classify_file_intent(str(getattr(latest_turn, "content", "") or ""))
        if intent.operation != "generate":
            raise ValueError("未检测到明确的文件生成意图")
        if not intent.delivery_required:
            return {
                "ok": False,
                "needs_confirmation": True,
                "delivery_not_requested": True,
                "message": "请先确认是否需要把整理后的内容生成文件并发送给你。",
            }
        if intent.requested_format:
            if intent.requested_format not in SUPPORTED_FILE_FORMATS:
                raise ValueError(
                    f"当前版本不支持生成 {intent.requested_format}，请使用 txt、md、csv 或 json"
                )
            if target_format != intent.requested_format:
                raise ValueError("工具格式与用户明确要求的文件格式不一致")
        raw_content = str(arguments.get("content") or "").strip()
        if not raw_content:
            return {
                "ok": False,
                "needs_content": True,
                "message": "请先整理出要写入文件的正文内容，再生成文件。",
            }
        tenant_id = str(getattr(session, "tenant_id", "") or "default").strip() or "default"
        session_id = self._external_session_id(session).strip()
        if not session_id:
            raise ValueError("当前会话标识不可用")
        session_kind = self._message_export_session_kind(
            session,
            latest_metadata=latest_metadata,
            session_id=session_id,
        )
        await self._require_session_group_file_send_enabled(
            tenant_id=tenant_id,
            session_id=session_id,
            session_kind=session_kind,
        )
        source_id = self._message_export_source_id(
            session,
            latest_turn=latest_turn,
            latest_metadata=latest_metadata,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        digest = hashlib.sha256(
            "\0".join((tenant_id, session_id, source_id, target_format, raw_content)).encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        content = convert_file_bytes(
            raw_content.encode("utf-8"),
            source_name="generated.json" if target_format == "json" else "generated.txt",
            target_format=target_format,
            source_content_type=(
                "application/json" if target_format == "json" else "text/plain"
            ),
        )
        artifact = stage_outbound_artifact(
            self._message_export_root,
            tenant_id=tenant_id,
            session_id=session_id,
            request_id=f"wxbot-file-generate-{digest}",
            file_name=str(arguments.get("file_name") or "整理内容").strip(),
            content=content,
            file_format=target_format,
            max_bytes=self._message_export_max_bytes,
        )
        delivery_contract = self._message_export_delivery_contract(
            session,
            latest_metadata=latest_metadata,
            source_message_id=source_id,
        )
        delivery_session_id = self._canonical_delivery_session_id(
            session,
            external_session_id=session_id,
        )
        reply_to_message_id = self._external_reply_to_message_id(
            session,
            latest_metadata=latest_metadata,
            source_message_id=source_id,
        )
        channel = str(getattr(getattr(session, "channel", "wechat"), "value", "wechat"))
        sender_id = str(
            latest_metadata.get("sender_id")
            or latest_metadata.get("sender_wxid")
            or getattr(session, "external_participant_id", "")
            or getattr(session, "user_id", "")
            or ""
        ).strip()
        command_id = f"channel-reply:{tenant_id}:wxbot-file-generate:{digest}"
        payload = {
            "tenant_id": tenant_id,
            "channel": channel,
            "adapter_id": str(getattr(session, "adapter_id", "") or ""),
            "connection_id": str(getattr(session, "connection_id", "") or ""),
            "session_id": delivery_session_id,
            "external_conversation_id": session_id,
            "canonical_conversation_id": delivery_session_id,
            "session_name": str(latest_metadata.get("session_name") or session_id),
            "session_kind": session_kind,
            "user_id": str(getattr(session, "user_id", "") or ""),
            "sender_id": sender_id,
            "sender_name": str(latest_metadata.get("sender_name") or ""),
            "reply_to_message_id": reply_to_message_id,
            "trace_id": str(latest_metadata.get("trace_id") or source_id)[:64],
            "source_message": {
                "agent_tool": "generate_text_file",
                "message_id": source_id,
                "external_message_id": reply_to_message_id,
                "session_id": delivery_session_id,
                "external_conversation_id": session_id,
            },
            "file": {
                "file_path": artifact["file_path"],
                "file_name": artifact["file_name"],
                "file_size": artifact["file_size"],
                "file_md5": artifact["file_md5"],
                "file_sha256": artifact["file_sha256"],
            },
            "delivery": {
                "channel": channel,
                "adapter_id": str(getattr(session, "adapter_id", "") or ""),
                "connection_id": str(getattr(session, "connection_id", "") or ""),
                "tenant_id": tenant_id,
                "session_id": delivery_session_id,
                "external_conversation_id": session_id,
                "canonical_conversation_id": delivery_session_id,
                "session_kind": session_kind,
                "sender_wxid": sender_id,
                "command_id": command_id,
                "idempotency_key": command_id,
                **delivery_contract,
                "must_deliver_file": True,
            },
            "command_id": command_id,
        }
        effect = {
            "type": "enqueue_channel_reply",
            "owner": "wxbot" if channel == "wechat" else channel,
            "idempotency_key": command_id,
            "payload": payload,
        }
        if self._effect_reply_enabled:
            effects = [effect]
        else:
            await self._enqueue_message_export_replies([effect])
            effects = []
        return {
            "ok": True,
            "file_name": artifact["file_name"],
            "format": target_format,
            "file_size": artifact["file_size"],
            "sent_to_current_session": True,
            "delivery_status": "queued",
            "delivery_acknowledged": False,
            "self_enqueued_reply": True,
            "suppress_final_reply": True,
            "channel_reply_effects": effects,
            "message": "文件已生成并排队发送到当前会话。",
        }

    @staticmethod
    def _metadata_has_file(metadata: dict[str, Any]) -> bool:
        media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
        attachment = (
            metadata.get("file_attachment")
            if isinstance(metadata.get("file_attachment"), dict)
            else {}
        )
        return bool(
            str(metadata.get("msg_type") or "").strip().lower() == "file"
            or str(attachment.get("type") or "").strip().lower() == "file"
            or str(
                metadata.get("file_name")
                or metadata.get("file_url")
                or media.get("file_name")
                or media.get("file_url")
                or ""
            ).strip()
        )

    @classmethod
    def _latest_file_context(cls, session: Any) -> tuple[Any | None, dict[str, Any]]:
        session_id = str(getattr(session, "external_conversation_id", "") or getattr(session, "session_id", "") or "")
        is_group = session_id.endswith("@chatroom") or str(
            dict(getattr(session, "metadata", {}) or {}).get("session_kind") or ""
        ).strip().lower() == "group"
        _request_turn, request_metadata = cls._latest_user_context(session)
        requester_id = str(
            request_metadata.get("sender_wxid")
            or request_metadata.get("sender_id")
            or getattr(session, "external_participant_id", "")
            or getattr(session, "user_id", "")
            or ""
        ).strip()
        for turn in reversed(list(getattr(session, "turns", []) or [])):
            raw_role = getattr(turn, "role", "")
            role = str(getattr(raw_role, "value", raw_role) or "").strip().lower()
            if role != "user":
                continue
            metadata = dict(getattr(turn, "metadata", {}) or {})
            if is_group and requester_id:
                file_sender = str(
                    metadata.get("sender_wxid") or metadata.get("sender_id") or ""
                ).strip()
                if file_sender and file_sender != requester_id:
                    continue
            if cls._metadata_has_file(metadata):
                return turn, metadata
        return None, {}

    async def _enrich_media_ready_metadata(
        self,
        metadata: dict[str, Any],
        *,
        session: Any | None,
    ) -> dict[str, Any]:
        """Recover a deferred SDK URL without trusting arbitrary user paths."""

        media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
        attachment = (
            metadata.get("file_attachment")
            if isinstance(metadata.get("file_attachment"), dict)
            else {}
        )
        if str(
            metadata.get("file_url") or media.get("file_url") or attachment.get("url") or ""
        ).strip():
            return metadata
        getter = getattr(self._wxbot_store, "get_media_ready_event", None)
        if not callable(getter) or session is None:
            return metadata
        tenant_id = str(getattr(session, "tenant_id", "") or "default").strip() or "default"
        connection_id = str(
            metadata.get("connection_id")
            or getattr(session, "connection_id", "")
            or getattr(self._settings, "channel_connection_id", "")
            or ""
        ).strip()
        message_ids = []
        for value in (
            metadata.get("msg_svr_id"),
            metadata.get("external_message_id"),
            metadata.get("message_id"),
        ):
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in message_ids:
                message_ids.append(cleaned)
        for message_id in message_ids:
            try:
                row = await getter(
                    tenant_id,
                    message_id=message_id,
                    connection_id=connection_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "wxbot.file_media_ready_lookup_failed",
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    message_id=message_id,
                )
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            ready_message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            ready_media = payload.get("media") if isinstance(payload.get("media"), dict) else {}
            merged = dict(metadata)
            merged_media = {**media, **ready_media}
            file_url = str(
                ready_message.get("file_url")
                or ready_media.get("file_url")
                or row.get("media_url")
                or ""
            ).strip()
            if file_url:
                merged["file_url"] = file_url
            for key in (
                "file_name",
                "file_ext",
                "file_size",
                "file_md5",
                "file_sha256",
                "file_download_status",
                "file_failure_reason",
                "media_status",
            ):
                value = ready_message.get(key) or ready_media.get(key)
                if value in (None, "") and key == "media_status":
                    value = ready_media.get("status") or row.get("event_type")
                if value not in (None, ""):
                    merged[key] = value
            merged["media"] = merged_media
            if file_url:
                return merged
        return metadata

    async def _download_current_file(
        self,
        metadata: dict[str, Any],
        *,
        session: Any | None = None,
    ) -> dict[str, Any]:
        metadata = await self._enrich_media_ready_metadata(metadata, session=session)
        connection_id = str(
            metadata.get("connection_id")
            or getattr(session, "connection_id", "")
            or getattr(self._settings, "channel_connection_id", "")
            or ""
        ).strip()
        configured_connection_id = str(
            getattr(self._settings, "channel_connection_id", "") or ""
        ).strip()
        if connection_id and connection_id not in {
            LEGACY_WXBOT_CONNECTION_ID,
            configured_connection_id,
        }:
            raise ValueError("当前文件所属连接未配置受控下载通道")
        media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
        attachment = (
            metadata.get("file_attachment")
            if isinstance(metadata.get("file_attachment"), dict)
            else {}
        )
        file_url = str(
            metadata.get("file_url") or media.get("file_url") or attachment.get("url") or ""
        ).strip()
        if not file_url:
            raise ValueError("当前文件尚未下载完成，请稍后再试")
        base_url = str(getattr(self._settings, "wxbot_sdk_url", "") or "").rstrip("/")
        parsed = urlsplit(file_url)
        if not base_url or parsed.scheme not in {"http", "https"}:
            raise ValueError("当前文件地址不可用")
        base_origin = urlsplit(base_url)
        if (parsed.scheme.lower(), parsed.netloc.lower()) != (
            base_origin.scheme.lower(),
            base_origin.netloc.lower(),
        ):
            raise ValueError("当前文件地址不是受信任的 wxbot SDK")
        decoded_path = unquote(parsed.path)
        if (
            not decoded_path.startswith("/files/")
            or ".." in decoded_path.replace("\\", "/").split("/")
            or parsed.fragment
        ):
            raise ValueError("当前文件地址格式不安全")
        request_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        configured_limit = int(
            getattr(self._settings, "wxbot_file_analysis_max_bytes", 8 * 1024 * 1024)
            or 8 * 1024 * 1024
        )
        sdk_download_limit = int(
            getattr(self._settings, "wxbot_file_download_max_bytes", 2 * 1024 * 1024 * 1024)
            or 2 * 1024 * 1024 * 1024
        )
        max_bytes = max(
            1024 * 1024,
            min(configured_limit, sdk_download_limit, 100 * 1024 * 1024),
        )
        async with httpx.AsyncClient(timeout=None, trust_env=False, follow_redirects=False) as client:
            response = await safe_trusted_service_request(
                client,
                "GET",
                base_url,
                request_path,
                headers=wxbot_sdk_headers(self._settings),
                timeout_seconds=30.0,
                max_response_bytes=max_bytes,
                allowed_response_content_types=(
                    "text/",
                    "application/json",
                    "application/octet-stream",
                    "binary/octet-stream",
                    "application/x-download",
                    "application/force-download",
                    "application/pdf",
                    "application/zip",
                    "application/msword",
                    "application/vnd.ms-excel",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            )
        if response.status_code == 404:
            raise ValueError("当前文件在 wxbot SDK 中不存在")
        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError("wxbot SDK 文件下载失败")
        content = bytes(response.content)
        if not content:
            raise ValueError("当前文件为空")
        if len(content) > max_bytes:
            raise ValueError("当前文件超过处理大小限制")
        raw_expected_size = metadata.get("file_size")
        if raw_expected_size in (None, ""):
            raw_expected_size = media.get("file_size", media.get("size", attachment.get("size")))
        if raw_expected_size not in (None, ""):
            try:
                expected_size = int(raw_expected_size)
            except (TypeError, ValueError) as exc:
                raise ValueError("当前文件大小元数据无效") from exc
            if expected_size > 0 and expected_size != len(content):
                raise ValueError("当前文件大小校验失败，已拒绝继续处理")
        expected_sha256 = str(
            metadata.get("file_sha256")
            or media.get("file_sha256")
            or media.get("sha256")
            or attachment.get("sha256")
            or ""
        ).strip().lower()
        if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("当前文件校验值格式无效")
        if expected_sha256:
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError("当前文件校验失败，已拒绝继续处理")
        expected_md5 = str(
            metadata.get("file_md5")
            or media.get("file_md5")
            or media.get("md5")
            or attachment.get("md5")
            or ""
        ).strip().lower()
        if expected_md5 and not re.fullmatch(r"[0-9a-f]{32}", expected_md5):
            raise ValueError("当前文件校验值格式无效")
        if expected_md5:
            if hashlib.md5(content).hexdigest() != expected_md5:
                raise ValueError("当前文件校验失败，已拒绝继续处理")
        file_name = str(
            metadata.get("file_name")
            or media.get("file_name")
            or attachment.get("name")
            or "当前文件"
        ).strip()
        extension = Path(file_name).suffix.lower().lstrip(".")
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].lower()
        binary_types = {
            "application/pdf",
            "application/zip",
            "application/msword",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        binary_magic = (
            content.startswith(b"%PDF-")
            or content.startswith(b"PK\x03\x04")
            or content.startswith(b"\xd0\xcf\x11\xe0")
        )
        if extension in {"txt", "md", "csv", "json"} and (
            content_type in binary_types or binary_magic
        ):
            raise ValueError("当前文件内容类型与扩展名不一致，已拒绝继续处理")
        return {
            "content": content,
            "file_name": file_name,
            "file_size": len(content),
            "file_sha256": hashlib.sha256(content).hexdigest(),
            "content_type": content_type,
            "download_status": "ready",
        }

    async def _cleanup_message_export_artifacts(self) -> None:
        try:
            list_active = getattr(
                self._wxbot_store,
                "list_active_outbound_file_paths",
                None,
            )
            active_paths = await list_active() if callable(list_active) else []
            export_root = Path(self._message_export_root).resolve(strict=False)
            protected_paths = []
            for raw_path in active_paths:
                candidate = Path(str(raw_path or ""))
                if not candidate.is_absolute():
                    continue
                resolved = candidate.resolve(strict=False)
                if resolved.is_relative_to(export_root):
                    protected_paths.append(resolved)
            result = cleanup_message_exports(
                self._message_export_root,
                protected_paths=protected_paths,
                retention_seconds=getattr(
                    self._settings,
                    "wxbot_outbound_file_retention_seconds",
                    24 * 60 * 60,
                ),
                cleanup_grace_seconds=getattr(
                    self._settings,
                    "wxbot_outbound_file_cleanup_grace_seconds",
                    5 * 60,
                ),
            )
            if result.get("errors"):
                logger.warning(
                    "wxbot.message_export_cleanup_partial",
                    error_count=len(result["errors"]),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "wxbot.message_export_cleanup_failed",
                error_type=exc.__class__.__name__,
            )

    def _message_export_period_arguments(
        self,
        report_type: str,
        *,
        date: str,
        year_month: str,
    ) -> tuple[str, str]:
        timezone = self._message_export_timezone()
        now = datetime.now(timezone)
        if report_type == "daily":
            if year_month:
                raise ValueError("daily 导出不能使用 year_month")
            resolved_date = date or now.strftime("%Y-%m-%d")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved_date) is None:
                raise ValueError("date 必须是 YYYY-MM-DD")
            try:
                datetime.strptime(resolved_date, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("date 必须是 YYYY-MM-DD") from exc
            return resolved_date, ""

        if date:
            raise ValueError("monthly 导出不能使用 date")
        resolved_year_month = year_month or now.strftime("%Y-%m")
        if re.fullmatch(r"\d{4}-\d{2}", resolved_year_month) is None:
            raise ValueError("year_month 必须是 YYYY-MM")
        try:
            datetime.strptime(resolved_year_month, "%Y-%m")
        except ValueError as exc:
            raise ValueError("year_month 必须是 YYYY-MM") from exc
        return "", resolved_year_month

    def _message_export_timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(
                str(
                    getattr(self._settings, "timezone", "Asia/Shanghai")
                    or "Asia/Shanghai"
                )
            )
        except Exception:
            return ZoneInfo("Asia/Shanghai")

    def _message_export_period_bounds(
        self,
        *,
        report_type: str,
        period: str,
    ) -> tuple[int, int]:
        timezone = self._message_export_timezone()
        if report_type == "daily":
            start = datetime.strptime(period, "%Y-%m-%d").replace(tzinfo=timezone)
            end = start + timedelta(days=1)
        else:
            parsed = datetime.strptime(period, "%Y-%m")
            start = datetime(parsed.year, parsed.month, 1, tzinfo=timezone)
            if parsed.month == 12:
                end = datetime(parsed.year + 1, 1, 1, tzinfo=timezone)
            else:
                end = datetime(parsed.year, parsed.month + 1, 1, tzinfo=timezone)
        return int(start.timestamp()), int(end.timestamp())

    async def _managed_group_message_records(
        self,
        session: Any,
        *,
        tenant_id: str,
        external_session_id: str,
        connection_id: str,
        report_type: str,
        period: str,
    ) -> list[dict[str, Any]]:
        canonical_session_id = canonical_conversation_id(
            connection_id,
            external_session_id,
        )
        declared_canonical_id = str(
            getattr(session, "canonical_conversation_id", "")
            or getattr(session, "session_id", "")
            or ""
        ).strip()
        if declared_canonical_id and declared_canonical_id != canonical_session_id:
            raise ValueError("managed_wxbot_conversation_scope_mismatch")

        start_ts, end_ts = self._message_export_period_bounds(
            report_type=report_type,
            period=period,
        )
        observations = await self._wxbot_store.list_group_observations_for_period(
            tenant_id,
            canonical_session_id,
            start_occurred_ts=start_ts,
            end_occurred_ts=end_ts,
            limit=_MANAGED_MESSAGE_EXPORT_MAX_RECORDS + 1,
        )
        if len(observations) > _MANAGED_MESSAGE_EXPORT_MAX_RECORDS:
            raise ValueError(
                "当前时间范围内的群消息超过 10000 条，请缩小导出时间范围"
            )
        return [
            self._managed_group_observation_record(item)
            for item in observations
            if isinstance(item, dict)
        ]

    def _managed_group_observation_record(
        self,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            occurred_ts = max(0, int(observation.get("occurred_ts") or 0))
        except (TypeError, ValueError):
            occurred_ts = 0
        timestamp = ""
        if occurred_ts:
            try:
                timestamp = datetime.fromtimestamp(
                    occurred_ts,
                    tz=self._message_export_timezone(),
                ).strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                timestamp = ""

        metadata = observation.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        attachment = metadata.get("file_attachment")
        attachment = attachment if isinstance(attachment, dict) else {}
        record: dict[str, Any] = {
            "timestamp": timestamp,
            "occurred_ts": occurred_ts,
            "sender_wxid": str(observation.get("sender_wxid") or "").strip(),
            "sender_name": str(observation.get("sender_name") or "").strip(),
            "msg_type": str(observation.get("msg_type") or "text").strip().lower()
            or "text",
            "text": str(observation.get("content") or ""),
            "is_self_sent": bool(observation.get("is_self_sent")),
        }
        if record["msg_type"] == "file":
            record["file_attachment"] = {
                "name": str(
                    metadata.get("file_name") or attachment.get("name") or ""
                ).strip(),
                "size": metadata.get("file_size", attachment.get("size")),
                "sha256": str(
                    metadata.get("file_sha256") or attachment.get("sha256") or ""
                )
                .strip()
                .lower(),
                "download_status": str(
                    metadata.get("file_download_status")
                    or attachment.get("download_status")
                    or metadata.get("media_status")
                    or ""
                )
                .strip()
                .lower(),
            }
        return record

    async def _require_session_group_file_send_enabled(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_kind: str,
    ) -> None:
        if session_kind != "group":
            return
        await require_group_file_send_enabled(
            self._social_policy_store,
            tenant_id=tenant_id,
            session_id=session_id,
        )

    @staticmethod
    def _message_export_session_kind(
        session: Any,
        *,
        latest_metadata: dict[str, Any],
        session_id: str,
    ) -> str:
        declared = str(
            latest_metadata.get("session_kind")
            or dict(getattr(session, "metadata", {}) or {}).get("session_kind")
            or ""
        ).strip().lower()
        if declared in {"group", "private"}:
            return declared
        return "group" if session_id.endswith("@chatroom") else "private"

    def _current_private_message_records(
        self,
        session: Any,
        *,
        report_type: str,
        period: str,
    ) -> list[dict[str, Any]]:
        timezone = self._message_export_timezone()
        fallback_created_at = getattr(session, "last_active_at", None)
        if not isinstance(fallback_created_at, datetime):
            fallback_created_at = datetime.now(timezone)
        records: list[dict[str, Any]] = []
        for turn in list(getattr(session, "turns", []) or []):
            raw_role = getattr(turn, "role", "")
            role = str(getattr(raw_role, "value", raw_role) or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            created_at = getattr(turn, "created_at", None)
            if not isinstance(created_at, datetime):
                created_at = fallback_created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            local_created_at = created_at.astimezone(timezone)
            local_period = (
                local_created_at.strftime("%Y-%m-%d")
                if report_type == "daily"
                else local_created_at.strftime("%Y-%m")
            )
            if local_period != period:
                continue
            metadata = dict(getattr(turn, "metadata", {}) or {})
            if role == "assistant":
                sender_wxid = "assistant"
                sender_name = "助手"
            else:
                sender_wxid = str(
                    metadata.get("sender_wxid")
                    or metadata.get("sender_id")
                    or getattr(session, "user_id", "")
                    or ""
                ).strip()
                sender_name = str(
                    metadata.get("sender_name") or sender_wxid or "当前用户"
                ).strip()
            msg_type = str(metadata.get("msg_type") or "text").strip().lower() or "text"
            turn_text = str(getattr(turn, "content", "") or "")
            if msg_type == "file" and not turn_text:
                turn_text = f"[文件] {str(metadata.get('file_name') or '').strip()}".rstrip()
            attachment = (
                metadata.get("file_attachment")
                if isinstance(metadata.get("file_attachment"), dict)
                else {}
            )
            file_name = str(
                metadata.get("file_name") or attachment.get("name") or ""
            ).strip()
            file_size = metadata.get("file_size", attachment.get("size"))
            try:
                file_size = max(0, int(file_size or 0))
            except (TypeError, ValueError):
                file_size = 0
            file_sha256 = str(
                metadata.get("file_sha256") or attachment.get("sha256") or ""
            ).strip().lower()
            file_status = str(
                metadata.get("file_download_status")
                or attachment.get("download_status")
                or metadata.get("media_status")
                or ""
            ).strip().lower()
            records.append(
                {
                    "ts": int(local_created_at.timestamp()),
                    "timestamp": local_created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "sender_wxid": sender_wxid,
                    "sender_name": sender_name,
                    "msg_type": msg_type,
                    "text": turn_text,
                    "file_name": file_name,
                    "file_size": file_size,
                    "file_sha256": file_sha256,
                    "file_status": file_status,
                }
            )
        records.sort(key=lambda item: (int(item.get("ts") or 0), str(item["sender_wxid"])))
        return records

    @staticmethod
    def _latest_user_context(session: Any) -> tuple[Any | None, dict[str, Any]]:
        for turn in reversed(list(getattr(session, "turns", []) or [])):
            raw_role = getattr(turn, "role", "")
            role = str(getattr(raw_role, "value", raw_role) or "").strip().lower()
            if role == "user":
                return turn, dict(getattr(turn, "metadata", {}) or {})
        return None, {}

    @classmethod
    def _message_export_source_id(
        cls,
        session: Any,
        *,
        latest_turn: Any | None,
        latest_metadata: dict[str, Any],
        tenant_id: str,
        session_id: str,
    ) -> str:
        session_metadata = dict(getattr(session, "metadata", {}) or {})
        captured = latest_metadata.get(_DELIVERY_CONTRACT_METADATA_KEY)
        if not isinstance(captured, dict):
            captured = session_metadata.get(_DELIVERY_CONTRACT_METADATA_KEY)
        captured_contract = dict(captured) if isinstance(captured, dict) else {}
        connection_id = (
            str(getattr(session, "connection_id", "") or "").strip()
            or LEGACY_WXBOT_CONNECTION_ID
        )
        if connection_id == LEGACY_WXBOT_CONNECTION_ID:
            for value in (
                captured_contract.get("source_message_id"),
                latest_metadata.get("message_id"),
                latest_metadata.get("msg_svr_id"),
                latest_metadata.get("reply_to_message_id"),
                session_metadata.get("message_id"),
                session_metadata.get("msg_svr_id"),
                latest_metadata.get("canonical_message_id"),
                latest_metadata.get("external_message_id"),
                session_metadata.get("canonical_message_id"),
                session_metadata.get("external_message_id"),
            ):
                normalized = str(value or "").strip()
                if normalized:
                    return normalized[:128]
        else:
            for value in (
                captured_contract.get("source_message_id"),
                latest_metadata.get("canonical_message_id"),
                latest_metadata.get("message_id"),
                session_metadata.get("canonical_message_id"),
                session_metadata.get("message_id"),
            ):
                normalized = str(value or "").strip()
                if normalized.startswith("cx1:m:"):
                    return normalized[:128]
            for value in (
                latest_metadata.get("external_message_id"),
                latest_metadata.get("msg_svr_id"),
                latest_metadata.get("reply_to_message_id"),
                session_metadata.get("external_message_id"),
                session_metadata.get("msg_svr_id"),
                session_metadata.get("reply_to_message_id"),
                captured_contract.get("source_message_id"),
                latest_metadata.get("message_id"),
                session_metadata.get("message_id"),
            ):
                external_message_id = str(value or "").strip()
                if external_message_id and not external_message_id.startswith("cx1:"):
                    return canonical_message_id(
                        connection_id,
                        external_message_id,
                    )[:128]

        turn_created_at = str(getattr(latest_turn, "created_at", "") or "")
        turn_content = str(getattr(latest_turn, "content", "") or "")
        fallback = hashlib.sha256(
            "\0".join(
                (
                    tenant_id,
                    session_id,
                    str(getattr(session, "user_id", "") or ""),
                    turn_created_at,
                    turn_content,
                    str(getattr(session, "last_active_at", "") or ""),
                )
            ).encode("utf-8")
        ).hexdigest()[:32]
        return f"message-export-source-{fallback}"

    @staticmethod
    def _message_export_delivery_contract(
        session: Any,
        *,
        latest_metadata: dict[str, Any],
        source_message_id: str,
    ) -> dict[str, Any]:
        captured = latest_metadata.get(_DELIVERY_CONTRACT_METADATA_KEY)
        if not isinstance(captured, dict):
            captured = dict(getattr(session, "metadata", {}) or {}).get(
                _DELIVERY_CONTRACT_METADATA_KEY
            )
        delivery = dict(captured) if isinstance(captured, dict) else {}
        delivery.update(
            {
                "participation_status": "must_reply",
                "source_message_id": source_message_id,
                "response_kind": "tool_result",
                "speech_output_kind": "ordinary",
                "speech_class": "obligation",
                "participation_reason_codes": ["direct_tool_request"],
            }
        )
        return delivery

    @staticmethod
    def _message_export_reply_effects(
        session: Any,
        *,
        latest_metadata: dict[str, Any],
        tenant_id: str,
        session_id: str,
        session_name: str,
        source_message_id: str,
        request_id: str,
        request_digest: str,
        period: str,
        artifact: dict[str, Any],
        delivery_contract: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_channel = getattr(session, "channel", "wechat")
        channel = str(getattr(raw_channel, "value", raw_channel) or "wechat")
        session_kind = WxbotAgentToolService._message_export_session_kind(
            session,
            latest_metadata=latest_metadata,
            session_id=session_id,
        )
        sender_id = str(
            latest_metadata.get("sender_id")
            or latest_metadata.get("sender_wxid")
            or getattr(session, "external_participant_id", "")
            or getattr(session, "user_id", "")
            or ""
        ).strip()
        sender_name = str(latest_metadata.get("sender_name") or "").strip()
        trace_id = str(
            latest_metadata.get("trace_id")
            or dict(getattr(session, "metadata", {}) or {}).get("trace_id")
            or source_message_id
        ).strip()[:64]
        delivery_session_id = WxbotAgentToolService._canonical_delivery_session_id(
            session,
            external_session_id=session_id,
        )
        reply_to_message_id = WxbotAgentToolService._external_reply_to_message_id(
            session,
            latest_metadata=latest_metadata,
            source_message_id=source_message_id,
        )
        source_message = {
            "agent_tool": "export_current_messages_file",
            "request_id": request_id,
            "message_id": source_message_id,
            "external_message_id": reply_to_message_id,
            "session_id": delivery_session_id,
            "external_conversation_id": session_id,
            "adapter_id": str(getattr(session, "adapter_id", "") or ""),
            "connection_id": str(getattr(session, "connection_id", "") or ""),
            _DELIVERY_CONTRACT_METADATA_KEY: dict(delivery_contract),
        }
        delivery_target = {
            "request_id": request_id,
            "channel": channel,
            "adapter_id": str(getattr(session, "adapter_id", "") or ""),
            "connection_id": str(getattr(session, "connection_id", "") or ""),
            "tenant_id": tenant_id,
            "session_id": delivery_session_id,
            "external_conversation_id": session_id,
            "canonical_conversation_id": delivery_session_id,
            "session_name": session_name,
            "session_kind": session_kind,
            "sender_name": sender_name,
            "sender_wxid": sender_id,
        }
        base_payload = {
            "tenant_id": tenant_id,
            "channel": channel,
            "adapter_id": str(getattr(session, "adapter_id", "") or ""),
            "connection_id": str(getattr(session, "connection_id", "") or ""),
            "session_id": delivery_session_id,
            "external_conversation_id": session_id,
            "canonical_conversation_id": delivery_session_id,
            "session_name": session_name,
            "session_kind": session_kind,
            "user_id": str(getattr(session, "user_id", "") or ""),
            "sender_id": sender_id,
            "sender_name": sender_name,
            "reply_to_message_id": reply_to_message_id,
            "trace_id": trace_id,
            "source_message": source_message,
        }
        command_prefix = (
            f"channel-reply:{tenant_id}:wxbot-message-export:{request_digest}"
        )
        confirmation_command = f"{command_prefix}:text"
        file_command = f"{command_prefix}:file"
        message_count = int(artifact.get("message_count") or 0)
        confirmation_text = (
            f"已整理 {period} 的消息记录，共 {message_count} 条，文件已排队发送。"
        )
        # The confirmation reserves the one conversational obligation.  The
        # file is the payload of that same response and must not consume a
        # second group speech-budget slot or be suppressed as a duplicate.
        file_delivery = {
            **delivery_contract,
            "speech_budget_enabled": False,
            "duplicate_guard_enabled": False,
            "must_deliver_file": True,
        }
        owner = "wxbot" if channel == "wechat" else channel
        return [
            {
                "type": "enqueue_channel_reply",
                "owner": owner,
                "idempotency_key": confirmation_command,
                "payload": {
                    **base_payload,
                    "body": {"type": "text", "text": confirmation_text},
                    "delivery": {
                        **delivery_target,
                        "command_id": confirmation_command,
                        "idempotency_key": confirmation_command,
                        **delivery_contract,
                    },
                    "command_id": confirmation_command,
                },
            },
            {
                "type": "enqueue_channel_reply",
                "owner": owner,
                "idempotency_key": file_command,
                "payload": {
                    **base_payload,
                    "file": {
                        "file_path": str(artifact.get("file_path") or ""),
                        "file_name": str(artifact.get("file_name") or ""),
                        "file_size": int(artifact.get("file_size") or 0),
                        "file_md5": str(artifact.get("file_md5") or ""),
                        "file_sha256": str(artifact.get("file_sha256") or ""),
                    },
                    "delivery": {
                        **delivery_target,
                        "command_id": file_command,
                        "idempotency_key": file_command,
                        **file_delivery,
                    },
                    "command_id": file_command,
                },
            },
        ]

    async def _enqueue_message_export_replies(
        self,
        effects: list[dict[str, Any]],
    ) -> None:
        for effect in effects:
            payload = dict(effect.get("payload") or {})
            delivery = dict(payload.get("delivery") or {})
            source_message = dict(payload.get("source_message") or {})
            body = payload.get("body")
            file_payload = payload.get("file")
            is_file = isinstance(file_payload, dict)
            await self._wxbot_store.enqueue_reply(
                tenant_id=str(payload.get("tenant_id") or "default"),
                session_id=str(payload.get("session_id") or ""),
                session_name=str(payload.get("session_name") or ""),
                sender_name=str(payload.get("sender_name") or ""),
                sender_wxid=str(payload.get("sender_id") or ""),
                reply_text=(
                    str(body.get("text") or "")
                    if isinstance(body, dict)
                    else ""
                ),
                trace_id=str(payload.get("trace_id") or ""),
                msg_type="file" if is_file else "text",
                file_path=(
                    str(file_payload.get("file_path") or "") if is_file else ""
                ),
                file_name=(
                    str(file_payload.get("file_name") or "") if is_file else ""
                ),
                file_size=(
                    int(file_payload.get("file_size") or 0) if is_file else None
                ),
                file_md5=(
                    str(file_payload.get("file_md5") or "") if is_file else ""
                ),
                file_sha256=(
                    str(file_payload.get("file_sha256") or "") if is_file else ""
                ),
                mention_sender=False,
                reply_to_msg_svr_id=str(payload.get("reply_to_message_id") or ""),
                session_kind=str(payload.get("session_kind") or ""),
                source_message=source_message,
                delivery=delivery,
                command_id=str(payload.get("command_id") or ""),
            )

    async def _require_data_owner_scope(
        self,
        owner: str,
        tenant_id: str,
        session_id: str,
    ) -> None:
        gate = self._data_owner_scope_execution_allowed
        if not callable(gate):
            raise RuntimeError(f"{owner}_plugin_scope_unavailable")
        try:
            allowed = await gate(
                str(owner or "").strip(),
                tenant_id=str(tenant_id or "").strip(),
                session_id=str(session_id or "").strip(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(f"{owner}_plugin_scope_unavailable") from exc
        if allowed is not True:
            raise RuntimeError(f"{owner}_plugin_runtime_disabled")

    async def _require_data_owners_scope(
        self,
        owners: tuple[str, ...],
        tenant_id: str,
        session_id: str,
    ) -> None:
        normalized_owners = tuple(
            dict.fromkeys(str(owner or "").strip() for owner in owners)
        )
        gate = self._data_owners_scope_execution_allowed
        if not normalized_owners or not callable(gate):
            raise RuntimeError("plugin_owner_snapshot_unavailable")
        try:
            allowed = await gate(
                normalized_owners,
                tenant_id=str(tenant_id or "").strip(),
                session_id=str(session_id or "").strip(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError("plugin_owner_snapshot_unavailable") from exc
        if allowed is not True:
            raise RuntimeError("plugin_owner_snapshot_disabled")

    async def _read_owned_data(
        self,
        owner: str,
        tenant_id: str,
        session_id: str,
        read: Callable[[], Awaitable[_OwnedReadResult]],
    ) -> _OwnedReadResult:
        """Read another plugin's data only inside two fresh owner checks.

        The positive decision is deliberately not cached.  A disable that
        races the store call therefore discards the result before it can be
        returned to the agent runtime and included in model context.
        """

        await self._require_data_owner_scope(owner, tenant_id, session_id)
        result = await read()
        await self._require_data_owner_scope(owner, tenant_id, session_id)
        return result

    async def get_group_reply_policy(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_reply_policy only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_name, _ = await self._load_group_context(session)
        policy = await self._wxbot_store.get_session_policy(tenant_id, session_id)
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "reply_mode": str(policy.get("effective_mode") or "off"),
            "mention_sender": bool(policy.get("effective_mention_sender")),
            "trigger_keywords": list(policy.get("trigger_keywords") or []),
            "raw_policy": policy,
            "source": "wxbot_reply_policy",
        }

    async def get_group_credits_status(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_credits_status only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_name, _ = await self._load_group_context(session)
        limit = _coerce_int(arguments.get("limit"), 5, minimum=1, maximum=20)
        async def read_credits() -> tuple[dict[str, Any], dict[str, Any]]:
            cfg_task = self._credits_store.get_config(tenant_id, session_id)
            members_task = self._credits_store.list_members(
                tenant_id,
                session_id,
                limit=limit,
            )
            cfg, members = await _gather_cancel_on_error(cfg_task, members_task)
            return cfg, members

        cfg, members = await self._read_owned_data(
            "credits",
            tenant_id,
            session_id,
            read_credits,
        )
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "enabled": bool(cfg.get("enabled")),
            "credit_name": str(cfg.get("credit_name") or "积分"),
            "cost_per_chat": int(cfg.get("cost_per_chat") or 0),
            "checkin_mode": int(cfg.get("checkin_mode") or 1),
            "checkin_mode_label": str(cfg.get("checkin_mode_label") or ""),
            "daily_checkin": int(cfg.get("daily_checkin") or 0),
            "streak_bonus": int(cfg.get("streak_bonus") or 0),
            "streak_cap": int(cfg.get("streak_cap") or 0),
            "summary": dict(members.get("summary") or {}),
            "top_members": list(members.get("items") or [])[:limit],
            "source": "credits_plugin",
        }

    async def get_group_credits_member(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_credits_member only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_name, members = await self._load_group_context(session)
        user_id = str(arguments.get("user_id") or "").strip()
        display_name = str(arguments.get("display_name") or arguments.get("query") or "").strip()
        if not user_id and not display_name:
            fallback = self._resolve_current_group_target(session, members)
            user_id = str(fallback.get("user_id") or "").strip()
            display_name = str(fallback.get("display_name") or "").strip()
        if not user_id and not display_name:
            raise ValueError("需要提供 user_id 或 display_name")

        if not user_id and display_name:
            user_id = self._match_group_member_wxid(members, display_name)
        if not user_id:
            raise ValueError("未找到对应群成员，无法查询积分")

        detail = await self._read_owned_data(
            "credits",
            tenant_id,
            session_id,
            lambda: self._credits_store.get_member_detail(
                tenant_id,
                session_id,
                user_id,
            ),
        )
        detail["session_id"] = session_id
        detail["session_name"] = session_name or session_id
        detail["source"] = "credits_plugin"
        return detail

    async def get_group_credits_leaderboard(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_credits_leaderboard only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_name, _ = await self._load_group_context(session)
        limit = _coerce_int(arguments.get("limit"), 10, minimum=1, maximum=50)
        query = str(arguments.get("query") or "").strip()
        checked_in_today_only = bool(arguments.get("checked_in_today_only"))
        member_limit = max(limit * 3, 30) if checked_in_today_only else limit
        data = await self._read_owned_data(
            "credits",
            tenant_id,
            session_id,
            lambda: self._credits_store.list_members(
                tenant_id,
                session_id,
                limit=member_limit,
                query=query,
            ),
        )
        items = list(data.get("items") or [])
        if checked_in_today_only:
            items = [item for item in items if bool(item.get("checked_in_today"))]
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "query": query,
            "checked_in_today_only": checked_in_today_only,
            "count": len(items),
            "summary": dict(data.get("summary") or {}),
            "items": items[:limit],
            "source": "credits_plugin",
        }

    async def get_group_moderation_status(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_moderation_status only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_name, _ = await self._load_group_context(session)
        keyword_limit = _coerce_int(arguments.get("keyword_limit"), 10, minimum=1, maximum=50)
        event_limit = _coerce_int(arguments.get("event_limit"), 5, minimum=1, maximum=20)
        async def read_moderation() -> tuple[
            dict[str, Any],
            list[dict[str, Any]],
            list[dict[str, Any]],
        ]:
            cfg_task = self._moderation_store.get_config(tenant_id, session_id)
            keywords_task = self._moderation_store.get_keywords(
                tenant_id,
                session_id,
                enabled_only=False,
            )
            events_task = self._moderation_store.get_events(
                tenant_id,
                session_id,
                limit=event_limit,
            )
            cfg, keywords, events = await _gather_cancel_on_error(
                cfg_task,
                keywords_task,
                events_task,
            )
            return cfg, keywords, events

        cfg, keywords, events = await self._read_owned_data(
            "moderation",
            tenant_id,
            session_id,
            read_moderation,
        )
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "enabled": bool(cfg.get("enabled")),
            "webhook_enabled": bool(cfg.get("webhook_enabled")),
            "reminder_mode": str(cfg.get("reminder_mode") or "off"),
            "reminder_text": str(cfg.get("reminder_text") or ""),
            "keyword_count": len(keywords),
            "keywords": list(keywords)[:keyword_limit],
            "recent_events": list(events)[:event_limit],
            "source": "moderation_plugin",
        }

    async def get_group_recent_moderation_events(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_recent_moderation_events only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_name, _ = await self._load_group_context(session)
        limit = _coerce_int(arguments.get("limit"), 10, minimum=1, maximum=50)
        keyword = str(arguments.get("keyword") or "").strip()
        action = str(arguments.get("action") or "").strip()
        webhook_status = str(arguments.get("webhook_status") or "").strip()
        events = await self._read_owned_data(
            "moderation",
            tenant_id,
            session_id,
            lambda: self._moderation_store.get_events(
                tenant_id,
                session_id,
                action=action,
                webhook_status=webhook_status,
                keyword=keyword,
                limit=limit,
            ),
        )
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "keyword": keyword,
            "action": action,
            "webhook_status": webhook_status,
            "count": len(events),
            "items": list(events),
            "source": "moderation_plugin",
        }

    async def get_group_repeater_status(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_repeater_status only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_name, _ = await self._load_group_context(session)
        event_limit = _coerce_int(arguments.get("event_limit"), 5, minimum=1, maximum=20)
        async def read_repeater() -> tuple[dict[str, Any], list[dict[str, Any]]]:
            cfg_task = self._repeater_store.get_config(tenant_id, session_id)
            events_task = self._repeater_store.list_events(
                tenant_id,
                session_id,
                limit=event_limit,
            )
            cfg, events = await _gather_cancel_on_error(cfg_task, events_task)
            return cfg, events

        cfg, events = await self._read_owned_data(
            "repeater",
            tenant_id,
            session_id,
            read_repeater,
        )
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "enabled": bool(cfg.get("enabled")),
            "cooldown_seconds": int(cfg.get("cooldown_seconds") or 0),
            "recent_events": list(events)[:event_limit],
            "source": "repeater_plugin",
        }

    async def list_group_roster_members(self, session: Any) -> list[dict[str, Any]]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("list_group_roster_members only supports group chats")
        _session_name, members = await self._load_group_context(session)
        return members

    async def get_group_welcome_status(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_welcome_status only supports group chats")

        session_name, _ = await self._load_group_context(session)
        welcome_cfg = await self._sdk_get_optional(
            f"/group-members/settings/{self._external_session_id(session)}"
        )
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "enabled": bool((welcome_cfg or {}).get("welcome_enabled")),
            "mention": bool((welcome_cfg or {}).get("welcome_mention")),
            "template": str((welcome_cfg or {}).get("welcome_template") or ""),
            "raw_config": welcome_cfg or {},
            "source": "wxbot_welcome",
        }

    async def get_group_report_status(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_report_status only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        session_name, _ = await self._load_group_context(session)
        report = await self._wxbot_store.get_report_subscription(tenant_id, session_id)
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "daily_enabled": bool((report or {}).get("daily_enabled")),
            "monthly_enabled": bool((report or {}).get("monthly_enabled")),
            "daily_hour": int((report or {}).get("daily_hour") or 9),
            "monthly_day": int((report or {}).get("monthly_day") or 1),
            "timezone": str((report or {}).get("tz") or "Asia/Shanghai"),
            "raw_config": report or {},
            "source": "wxbot_reports",
        }

    async def get_group_activity_ranking(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_activity_ranking only supports group chats")

        hours = _coerce_int(arguments.get("hours"), 24, minimum=1, maximum=24 * 14)
        limit = _coerce_int(arguments.get("limit"), 10, minimum=1, maximum=30)
        session_name, members = await self._load_group_context(session)
        member_name_map = {
            str(item.get("wxid") or "").strip(): str(item.get("display_name") or "").strip()
            for item in members
            if str(item.get("wxid") or "").strip()
        }
        messages = await self._load_group_text_messages(
            session_id,
            member_name_map=member_name_map,
            hours=hours,
            limit=max(limit * 30, 200),
        )
        sender_counter = Counter(
            str(item.get("sender_wxid") or item.get("sender_name") or "unknown")
            for item in messages
        )
        latest_by_sender: dict[str, dict[str, Any]] = {}
        for item in messages:
            sender_key = str(item.get("sender_wxid") or item.get("sender_name") or "unknown")
            latest_by_sender.setdefault(sender_key, item)

        ranking: list[dict[str, Any]] = []
        for sender_key, count in sender_counter.most_common(limit):
            latest = latest_by_sender.get(sender_key) or {}
            display_name = member_name_map.get(sender_key) or str(
                latest.get("sender_name") or sender_key
            )
            ranking.append(
                {
                    "display_name": display_name or sender_key,
                    "wxid": sender_key,
                    "message_count": count,
                    "latest_message": str(latest.get("text") or "")[:120],
                    "latest_timestamp": str(latest.get("timestamp") or ""),
                }
            )
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "time_window_hours": hours,
            "member_count": len(members),
            "message_count": len(messages),
            "active_member_count": len(sender_counter),
            "items": ranking,
            "source": "wxbot_activity",
        }

    async def get_group_info(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_info only supports group chats")

        session_name, member_items = await self._load_group_context(session)
        roster_available = bool(member_items)
        return {
            "session_id": session_id,
            "session_name": session_name,
            "member_count": len(member_items),
            "member_count_known": roster_available,
            # Member enumeration is a separately authorized admin tool.  Keep
            # the ordinary group-info result aggregate-only so raw identifiers
            # and avatar metadata are not sent to the model.
            "members_sample": [],
            "roster_available": roster_available,
            "note": (
                ""
                if roster_available
                else "当前没有拿到群成员名册，不能按 0 人处理。请稍后再试或先检查 SDK roster。"
            ),
            "source": "sdk_roster" if roster_available else "sdk_roster_empty",
        }

    async def list_group_members(self, session: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("list_group_members only supports group chats")

        query = str(arguments.get("query") or "").strip().lower()
        try:
            limit = int(arguments.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 50))

        session_name, normalized = await self._load_group_context(session)
        roster_available = bool(normalized)
        if query:
            normalized = [
                item
                for item in normalized
                if query in str(item.get("display_name") or "").lower()
                or query in str(item.get("wxid") or "").lower()
            ]
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "query": query,
            "total": len(normalized),
            "members": normalized[:limit],
            "roster_available": roster_available,
            "note": (
                ""
                if roster_available
                else "当前没有拿到群成员名册，暂时无法列出成员列表。请稍后再试或先检查 SDK roster。"
            ),
            "source": "sdk_roster" if roster_available else "sdk_roster_empty",
        }

    async def get_group_member_avatar(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_member_avatar only supports group chats")

        session_name, members = await self._load_group_context(session)
        user_id = str(arguments.get("user_id") or arguments.get("wxid") or "").strip()
        query = str(arguments.get("display_name") or arguments.get("query") or "").strip()
        if not user_id and not query:
            fallback = self._resolve_current_group_target(session, members)
            user_id = str(fallback.get("user_id") or getattr(session, "user_id", "") or "").strip()
            query = str(fallback.get("display_name") or "").strip()
        if not user_id and not query:
            raise ValueError("需要提供 user_id、display_name 或 query")

        member = self._match_group_member(members, user_id or query)
        if not member and user_id:
            member = {"wxid": user_id, "display_name": user_id}
        if not member:
            raise ValueError("未找到对应群成员，无法查询头像")

        wxid = str(member.get("wxid") or user_id or "").strip()
        avatar = dict(member.get("avatar") or {})
        cache_url = str(avatar.get("cache_url") or "").strip()
        cached = bool(avatar.get("cached")) or bool(cache_url)
        avatar_url = str(avatar.get("avatar_url") or "").strip()
        if not avatar_url and cached and wxid:
            avatar_url = self._sdk_absolute_url(
                cache_url or f"/ext/roster/avatars/{quote(wxid, safe='')}"
            )
        avatar_file_path = await self._cache_avatar_file(
            avatar_url,
            wxid=wxid,
            content_type=str(avatar.get("content_type") or ""),
        )

        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "display_name": str(member.get("display_name") or wxid or query),
            "wxid": wxid,
            "avatar_url": avatar_url,
            "avatar_file_path": avatar_file_path,
            "avatar_cached": cached,
            "avatar_content_type": str(avatar.get("content_type") or ""),
            "avatar_size": _coerce_int(avatar.get("size"), 0, minimum=0, maximum=1024 * 1024 * 20),
            "avatar_update_time": avatar.get("update_time"),
            "small_head_url": str(avatar.get("small_head_url") or ""),
            "big_head_url": str(avatar.get("big_head_url") or ""),
            "source": "sdk_roster_avatar",
            "note": "" if avatar_url else "SDK 名册里没有可用的缓存头像 URL。",
        }

    async def search_group_messages(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("search_group_messages only supports group chats")

        query = str(arguments.get("query") or "").strip()
        sender_name_query = str(arguments.get("sender_name") or "").strip().lower()
        sender_wxid_query = str(arguments.get("sender_wxid") or "").strip().lower()
        hours = _coerce_int(arguments.get("hours"), 24, minimum=1, maximum=24 * 14)
        limit = _coerce_int(arguments.get("limit"), 10, minimum=1, maximum=20)

        session_name, members = await self._load_group_context(session)
        member_name_map = {
            str(item.get("wxid") or "").strip(): str(item.get("display_name") or "").strip()
            for item in members
            if str(item.get("wxid") or "").strip()
        }
        messages = await self._load_group_text_messages(
            session_id,
            member_name_map=member_name_map,
            hours=hours,
            limit=max(limit * 12, 120),
        )

        query_lower = query.lower()
        filtered: list[dict[str, Any]] = []
        for item in messages:
            text = str(item.get("text") or "")
            sender_name = str(item.get("sender_name") or "").lower()
            sender_wxid = str(item.get("sender_wxid") or "").lower()
            if query_lower and query_lower not in text.lower():
                continue
            if sender_name_query and sender_name_query not in sender_name:
                continue
            if sender_wxid_query and sender_wxid_query not in sender_wxid:
                continue
            filtered.append(item)

        sender_counter = Counter(
            str(item.get("sender_name") or item.get("sender_wxid") or "未知成员")
            for item in filtered
        )
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "query": query,
            "sender_name": str(arguments.get("sender_name") or "").strip(),
            "sender_wxid": str(arguments.get("sender_wxid") or "").strip(),
            "time_window_hours": hours,
            "total": len(filtered),
            "matched_senders": [
                {"display_name": name, "message_count": count}
                for name, count in sender_counter.most_common(5)
            ],
            "messages": filtered[:limit],
        }

    async def research_group_messages(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("research_group_messages only supports group chats")

        question = _normalize_research_question(
            arguments.get("question") or arguments.get("query") or ""
        )
        if not question:
            raise ValueError("需要提供要研究的问题")

        hours = _coerce_int(arguments.get("hours"), 24, minimum=1, maximum=24 * 14)
        limit = _coerce_int(arguments.get("limit"), 6, minimum=1, maximum=12)
        session_name, members = await self._load_group_context(session)
        member_name_map = {
            str(item.get("wxid") or "").strip(): str(item.get("display_name") or "").strip()
            for item in members
            if str(item.get("wxid") or "").strip()
        }
        messages = await self._load_group_text_messages(
            session_id,
            member_name_map=member_name_map,
            hours=hours,
            limit=max(limit * 40, 240),
        )

        keywords = _extract_research_keywords(question)
        ranked: list[dict[str, Any]] = []
        for item in messages:
            score, matched_keywords = _score_research_message(question, keywords, item)
            if score <= 0:
                continue
            enriched = dict(item)
            enriched["score"] = score
            enriched["matched_keywords"] = matched_keywords
            ranked.append(enriched)

        ranked.sort(
            key=lambda item: (
                int(item.get("score") or 0),
                int(item.get("ts") or 0),
            ),
            reverse=True,
        )
        sender_counter = Counter(
            str(item.get("sender_name") or item.get("sender_wxid") or "未知成员") for item in ranked
        )
        matched_keywords = list(
            dict.fromkeys(
                keyword
                for item in ranked[:limit]
                for keyword in (item.get("matched_keywords") or [])
                if str(keyword or "").strip()
            )
        )
        top_messages = ranked[:limit]
        found = bool(top_messages)
        solution_hints = _extract_research_solutions(
            question=question,
            top_messages=top_messages,
        )
        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "question": question,
            "time_window_hours": hours,
            "keywords": keywords,
            "matched_keywords": matched_keywords,
            "found": found,
            "total": len(ranked),
            "matched_senders": [
                {"display_name": name, "message_count": count}
                for name, count in sender_counter.most_common(5)
            ],
            "solution_hints": solution_hints,
            "summary": _build_research_summary(
                question=question,
                hours=hours,
                total=len(ranked),
                top_messages=top_messages,
                keywords=keywords,
            ),
            "messages": top_messages,
            "source": "wxbot_group_research",
        }

    async def build_group_member_profile_report(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("build_group_member_profile_report only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        query = str(
            arguments.get("query")
            or arguments.get("display_name")
            or arguments.get("user_id")
            or arguments.get("wxid")
            or ""
        ).strip()
        user_id = str(arguments.get("user_id") or arguments.get("wxid") or "").strip()
        if not query and not user_id:
            raise ValueError("需要提供 query、display_name、user_id 或 wxid")

        hours = _coerce_int(arguments.get("hours"), 168, minimum=1, maximum=24 * 30)
        limit = _coerce_int(arguments.get("limit"), 8, minimum=1, maximum=20)
        session_name, members = await self._load_group_context(session)
        member_name_map = {
            str(item.get("wxid") or "").strip(): str(item.get("display_name") or "").strip()
            for item in members
            if str(item.get("wxid") or "").strip()
        }

        member = self._match_group_member(members, user_id or query)
        if not member and user_id:
            member = {"wxid": user_id, "display_name": user_id}
        if not member:
            candidates = [
                item
                for item in members
                if query.lower() in str(item.get("display_name") or "").lower()
                or query.lower() in str(item.get("wxid") or "").lower()
            ][:limit]
            return {
                "session_id": session_id,
                "session_name": session_name or session_id,
                "query": query,
                "found": False,
                "member_candidates": candidates,
                "profile": {},
                "facets": [],
                "evidence_refs": [],
                "external_candidates": [],
                "review": {
                    "state": "needs_review",
                    "notes": "未唯一匹配到群成员；请提供更明确的 display_name 或 wxid。",
                },
                "source": "wxbot_profile_report_readonly",
            }

        member_wxid = str(member.get("wxid") or user_id or "").strip()
        display_name = str(member.get("display_name") or member_wxid or query).strip()
        aliases = _profile_aliases_from_name(display_name)
        if query and query not in aliases:
            aliases.extend(_profile_aliases_from_name(query))
        aliases = list(dict.fromkeys(aliases))[:10]
        alias_keys = {_profile_normalize_text(item) for item in aliases if item}

        messages = await self._load_group_text_messages(
            session_id,
            member_name_map=member_name_map,
            hours=hours,
            limit=max(limit * 60, 300),
        )
        own_messages = [
            item
            for item in messages
            if member_wxid and str(item.get("sender_wxid") or "") == member_wxid
        ]
        mention_messages: list[dict[str, Any]] = []
        for item in messages:
            if member_wxid and str(item.get("sender_wxid") or "") == member_wxid:
                continue
            text_key = _profile_normalize_text(item.get("text"))
            if any(alias_key and alias_key in text_key for alias_key in alias_keys):
                mention_messages.append(item)

        evidence_refs: list[dict[str, Any]] = []
        for source_label, items in (
            ("member_message", own_messages),
            ("mention_message", mention_messages),
        ):
            for item in items[: max(1, limit // 2)]:
                summary = _profile_redact_text(item.get("text"))[:180]
                if not summary:
                    continue
                evidence_refs.append(
                    {
                        "evidence_id": _profile_evidence_id(
                            source_label,
                            session_id,
                            item.get("sender_wxid"),
                            item.get("ts"),
                            summary,
                        ),
                        "source_type": "group_message",
                        "source_id": f"{session_id}:{item.get('ts') or ''}:{item.get('sender_wxid') or ''}",
                        "timestamp": str(item.get("timestamp") or ""),
                        "summary": summary,
                        "raw_access": "disabled",
                        "matched_fields": ["sender_wxid"]
                        if source_label == "member_message"
                        else ["display_name", "alias"],
                        "confidence": 0.75 if source_label == "member_message" else 0.55,
                    }
                )
        evidence_refs = evidence_refs[:limit]
        evidence_ids = [str(item.get("evidence_id") or "") for item in evidence_refs]

        topic_terms = _profile_extract_terms(
            [str(item.get("text") or "") for item in own_messages + mention_messages],
            set(aliases),
            limit=8,
        )
        facets: list[dict[str, Any]] = [
            _profile_facet(
                facet_type="alias",
                claim="、".join(aliases),
                confidence=0.8,
                evidence_refs=[],
                source_types=["roster"],
            )
        ]
        if topic_terms:
            facets.append(
                _profile_facet(
                    facet_type="topic_interest",
                    claim="群内最近相关话题候选：" + "、".join(topic_terms[:6]),
                    confidence=0.45 if len(evidence_ids) <= 1 else 0.6,
                    evidence_refs=evidence_ids[:4],
                    source_types=["group_message"],
                )
            )
        if own_messages or mention_messages:
            facets.append(
                _profile_facet(
                    facet_type="activity_pattern",
                    claim=f"最近 {hours} 小时内本人发言 {len(own_messages)} 条，被提及 {len(mention_messages)} 条。",
                    confidence=0.7,
                    evidence_refs=evidence_ids[:4],
                    source_types=["group_message"],
                )
            )

        external_input = arguments.get("external_candidates") or []
        if not isinstance(external_input, list):
            external_input = []
        external_candidates: list[dict[str, Any]] = []
        external_evidence_refs: list[dict[str, Any]] = []
        for index, raw_candidate in enumerate(external_input[:limit]):
            if not isinstance(raw_candidate, dict):
                continue
            candidate = dict(raw_candidate)
            candidate_url = _profile_redact_text(candidate.get("url") or "")
            confidence, signals, binding_status = _profile_score_external_candidate(
                candidate,
                display_name=display_name,
                aliases=aliases,
                project_keywords=topic_terms,
            )
            candidate_summary = _profile_redact_text(
                candidate.get("public_summary")
                or candidate.get("summary")
                or candidate.get("description")
                or ""
            )[:240]
            evidence_id = _profile_evidence_id(
                "external",
                session_id,
                display_name,
                candidate.get("url"),
                candidate_summary,
                index,
            )
            external_evidence_refs.append(
                {
                    "evidence_id": evidence_id,
                    "source_type": "web_search",
                    "source_id": str(candidate.get("candidate_id") or f"external:{index + 1}"),
                    "timestamp": str(candidate.get("timestamp") or ""),
                    "summary": candidate_summary,
                    "raw_access": "disabled",
                    "url": candidate_url,
                    "matched_fields": signals,
                    "confidence": confidence,
                }
            )
            external_candidates.append(
                {
                    "candidate_id": str(candidate.get("candidate_id") or f"external:{index + 1}"),
                    "platform": _profile_candidate_url_platform(candidate),
                    "display_name": _profile_redact_text(
                        candidate.get("display_name") or candidate.get("name") or ""
                    ),
                    "url": candidate_url,
                    "public_summary": candidate_summary,
                    "match_signals": signals,
                    "negative_signals": [
                        _profile_redact_text(item)
                        for item in (candidate.get("negative_signals") or [])
                        if str(item or "").strip()
                    ][:6],
                    "confidence": confidence,
                    "binding_status": binding_status,
                    "evidence_refs": [evidence_id],
                }
            )

        if external_candidates:
            facets.append(
                _profile_facet(
                    facet_type="public_identity_candidate",
                    claim=f"发现 {len(external_candidates)} 个公开身份候选；均需人工确认后才能绑定。",
                    confidence=max(item["confidence"] for item in external_candidates),
                    evidence_refs=[
                        ref["evidence_id"]
                        for ref in external_evidence_refs
                        if str(ref.get("evidence_id") or "")
                    ][:limit],
                    source_types=["web_search"],
                )
            )

        evidence_refs.extend(external_evidence_refs[: max(0, limit - len(evidence_refs))])
        external_review_needed = any(
            str(item.get("binding_status") or "") in {"needs_human_review", "matched"}
            for item in external_candidates
        )
        summary_bits = [f"只读画像草案：群成员 {display_name}。"]
        if topic_terms:
            summary_bits.append("群内相关话题候选：" + "、".join(topic_terms[:5]) + "。")
        if external_candidates:
            summary_bits.append("公开候选仅作为待确认线索，不自动绑定。")

        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "query": query,
            "found": True,
            "member": {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "display_name": display_name,
                "wxid": member_wxid,
                "aliases": aliases,
                "match": "wxid" if user_id and user_id == member_wxid else "display_name_or_alias",
            },
            "profile": {
                "profile_id": f"profile:{tenant_id}:wechat:{session_id}:{member_wxid or hashlib.md5(display_name.encode()).hexdigest()[:12]}",
                "tenant_id": tenant_id,
                "channel": "wechat",
                "session_id": session_id,
                "member_id": member_wxid,
                "display_names": aliases,
                "status": "candidate",
                "confidence": 0.6 if evidence_ids else 0.35,
                "summary": _profile_redact_text("".join(summary_bits)),
                "facets": [item["facet_id"] for item in facets],
                "evidence_refs": [item["evidence_id"] for item in evidence_refs],
                "external_candidates": [item["candidate_id"] for item in external_candidates],
                "review": {
                    "state": "needs_review" if external_review_needed else "unreviewed",
                    "notes": "只读报告：不写数据库，不自动 accepted，不自动绑定公开身份。",
                },
            },
            "facets": facets,
            "evidence_refs": evidence_refs,
            "external_candidates": external_candidates,
            "review": {
                "state": "needs_review" if external_review_needed else "unreviewed",
                "binding_policy": "公开搜索候选只能作为 candidate/needs_human_review；除非候选显式带 verified_by_group_evidence，否则不输出 matched。",
                "notes": "请人工确认公开账号与群成员是否同一人；敏感信息已在摘要中脱敏或过滤。",
            },
            "source": "wxbot_profile_report_readonly",
        }

    async def get_group_public_facts(
        self, session: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        session_id = self._external_session_id(session)
        if not session_id.endswith("@chatroom"):
            raise ValueError("get_group_public_facts only supports group chats")

        tenant_id = str(getattr(session, "tenant_id", "") or "").strip() or "default"
        hours = _coerce_int(arguments.get("hours"), 72, minimum=1, maximum=24 * 30)
        session_name, members = await self._load_group_context(session)
        aggregate_owners = ("wxbot", "credits", "moderation", "repeater")
        await self._require_data_owners_scope(
            aggregate_owners,
            tenant_id,
            session_id,
        )
        member_name_map = {
            str(item.get("wxid") or "").strip(): str(item.get("display_name") or "").strip()
            for item in members
            if str(item.get("wxid") or "").strip()
        }

        messages_task = self._load_group_text_messages(
            session_id,
            member_name_map=member_name_map,
            hours=hours,
            limit=300,
        )
        member_events_task = self._wxbot_store.list_member_events(
            tenant_id,
            limit=50,
            connection_id=str(getattr(session, "connection_id", "") or ""),
        )
        policy_task = self._wxbot_store.get_session_policy(tenant_id, session_id)
        report_task = self._wxbot_store.get_report_subscription(tenant_id, session_id)
        credits_task = self._read_owned_data(
            "credits",
            tenant_id,
            session_id,
            lambda: self._credits_store.get_config(tenant_id, session_id),
        )
        moderation_task = self._read_owned_data(
            "moderation",
            tenant_id,
            session_id,
            lambda: self._moderation_store.get_config(tenant_id, session_id),
        )
        repeater_task = self._read_owned_data(
            "repeater",
            tenant_id,
            session_id,
            lambda: self._repeater_store.get_config(tenant_id, session_id),
        )
        welcome_task = self._sdk_get_optional(
            f"/group-members/settings/{self._external_session_id(session)}"
        )

        (
            messages,
            member_events,
            reply_policy,
            report_subscription,
            credits_cfg,
            moderation_cfg,
            repeater_cfg,
            welcome_cfg,
        ) = await _gather_cancel_on_error(
            messages_task,
            member_events_task,
            policy_task,
            report_task,
            credits_task,
            moderation_task,
            repeater_task,
            welcome_task,
        )
        await self._require_data_owners_scope(
            aggregate_owners,
            tenant_id,
            session_id,
        )

        filtered_events = [
            event for event in member_events if str(event.get("session_id") or "") == session_id
        ][:5]

        sender_counter = Counter(
            str(item.get("sender_wxid") or item.get("sender_name") or "unknown")
            for item in messages
        )
        feature_labels: list[str] = []
        if bool(credits_cfg.get("enabled")):
            feature_labels.append("积分")
        if bool(moderation_cfg.get("enabled")):
            feature_labels.append("审核")
        if bool(repeater_cfg.get("enabled")):
            feature_labels.append("复读机")
        if bool((welcome_cfg or {}).get("welcome_enabled")):
            feature_labels.append("欢迎语")
        if bool(
            report_subscription
            and (
                report_subscription.get("daily_enabled")
                or report_subscription.get("monthly_enabled")
            )
        ):
            feature_labels.append("日报月报")

        return {
            "session_id": session_id,
            "session_name": session_name or session_id,
            "time_window_hours": hours,
            "member_count": len(members),
            "recent_message_count": len(messages),
            "active_member_count": len(sender_counter),
            "top_speakers": [],
            "recent_samples": [],
            "recent_member_events": [
                {
                    "event_type": str(item.get("event_type") or ""),
                    "timestamp": str(item.get("created_at") or ""),
                }
                for item in filtered_events
            ],
            "feature_labels": feature_labels,
            "features": {
                "reply_policy": {
                    "mode": str(reply_policy.get("effective_mode") or "off"),
                    "mention_sender": bool(reply_policy.get("effective_mention_sender")),
                    "trigger_keywords": list(reply_policy.get("trigger_keywords") or [])[:10],
                },
                "credits": {
                    "enabled": bool(credits_cfg.get("enabled")),
                    "credit_name": str(credits_cfg.get("credit_name") or "积分"),
                    "cost_per_chat": int(credits_cfg.get("cost_per_chat") or 0),
                    "checkin_mode": int(credits_cfg.get("checkin_mode") or 1),
                    "checkin_mode_label": str(credits_cfg.get("checkin_mode_label") or ""),
                },
                "moderation": {
                    "enabled": bool(moderation_cfg.get("enabled")),
                    "webhook_enabled": bool(moderation_cfg.get("webhook_enabled")),
                    "reminder_mode": str(moderation_cfg.get("reminder_mode") or "off"),
                },
                "repeater": {
                    "enabled": bool(repeater_cfg.get("enabled")),
                    "cooldown_seconds": int(repeater_cfg.get("cooldown_seconds") or 0),
                },
                "welcome": {
                    "enabled": bool((welcome_cfg or {}).get("welcome_enabled")),
                    "mention": bool((welcome_cfg or {}).get("welcome_mention")),
                },
                "reports": {
                    "daily_enabled": bool((report_subscription or {}).get("daily_enabled")),
                    "monthly_enabled": bool((report_subscription or {}).get("monthly_enabled")),
                    "daily_hour": int((report_subscription or {}).get("daily_hour") or 9),
                    "monthly_day": int((report_subscription or {}).get("monthly_day") or 1),
                },
            },
            "source": "wxbot_group_agent",
        }

    async def _sdk_get(self, path: str) -> dict[str, Any]:
        self._require_sdk_boundary_available()
        base_url = str(
            getattr(self._settings, "wxbot_sdk_url", "http://127.0.0.1:5080") or ""
        ).rstrip("/")
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                trust_env=False,
            ) as client:
                resp = await safe_trusted_service_request(
                    client,
                    "GET",
                    base_url,
                    path,
                    headers={
                        "Accept": "application/json",
                        **wxbot_sdk_headers(self._settings),
                    },
                    timeout_seconds=10.0,
                    max_response_bytes=10 * 1024 * 1024,
                    allowed_response_content_types=(
                        "application/json",
                        "application/problem+json",
                        "text/plain",
                    ),
                )
        except httpx.HTTPError as exc:
            raise ValueError("wxbot sdk unavailable") from exc
        if resp.status_code >= 400:
            raise ValueError(f"wxbot sdk returned HTTP {resp.status_code}")
        payload = resp.json()
        return payload if isinstance(payload, dict) else {"items": payload}

    async def _sdk_get_optional(self, path: str) -> dict[str, Any] | None:
        try:
            return await self._sdk_get(path)
        except ValueError as exc:
            detail = str(exc).lower()
            if "404" in detail or "not found" in detail:
                return None
            raise

    async def _sdk_query_rows(
        self,
        *,
        database: str,
        sql: str,
        params: list[Any] | dict[str, Any] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self._require_sdk_boundary_available()
        base_url = str(
            getattr(self._settings, "wxbot_sdk_url", "http://127.0.0.1:5080") or ""
        ).rstrip("/")
        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                trust_env=False,
            ) as client:
                response = await safe_trusted_service_request(
                    client,
                    "POST",
                    base_url,
                    "/ext/query/read",
                    json={
                        "database": database,
                        "sql": sql,
                        "params": params,
                        "limit": max(1, min(int(limit or 200), 500)),
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        **wxbot_sdk_headers(self._settings),
                    },
                    timeout_seconds=20.0,
                    max_response_bytes=10 * 1024 * 1024,
                    allowed_response_content_types=(
                        "application/json",
                        "application/problem+json",
                        "text/plain",
                    ),
                )
        except httpx.HTTPError as exc:
            raise ValueError("wxbot sdk unavailable") from exc
        if response.status_code >= 400:
            raise ValueError(f"wxbot sdk returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise ValueError("wxbot sdk query returned invalid payload")
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    async def _load_group_context(self, session: Any) -> tuple[str, list[dict[str, Any]]]:
        session_id = self._external_session_id(session)
        external_session_id = self._external_session_id(session)
        connection_id = str(getattr(session, "connection_id", "") or "").strip()
        if connection_id and connection_id != LEGACY_WXBOT_CONNECTION_ID:
            # The core process intentionally does not own connector secrets and
            # there is no connection-scoped bridge RPC yet.  Keep canonical
            # store-backed tools available, but never fall through to the
            # legacy/global SDK account for a managed session.
            self._sdk_scope.set(
                {
                    "mode": "managed_unavailable",
                    "connection_id": connection_id,
                    "external_session_id": external_session_id,
                }
            )
            metadata = dict(getattr(session, "metadata", {}) or {})
            session_name = str(metadata.get("session_name") or session_id).strip()
            return session_name or session_id, []
        self._sdk_scope.set(
            {
                "mode": "legacy",
                "connection_id": connection_id or LEGACY_WXBOT_CONNECTION_ID,
                "external_session_id": external_session_id,
            }
        )
        groups_payload, members_payload = await _gather_cancel_on_error(
            self._sdk_get("/ext/roster/groups"),
            self._sdk_get(f"/ext/roster/groups/{external_session_id}/members"),
        )
        groups = list(groups_payload.get("sessions") or groups_payload.get("items") or [])
        group = next(
            (
                item
                for item in groups
                if str(item.get("session_id") or "") == external_session_id
            ),
            None,
        )
        members = list(
            members_payload.get("members")
            or members_payload.get("items")
            or members_payload.get("candidates")
            or []
        )

        session_name = ""
        if isinstance(group, dict):
            session_name = str(group.get("session_name") or group.get("name") or "").strip()
        if not session_name:
            session_name = str(
                members_payload.get("session_name")
                or getattr(session, "metadata", {}).get("session_name")
                or session_id
            ).strip()
        normalized = [self._normalize_member(item) for item in members]
        normalized = [item for item in normalized if item]
        return session_name or session_id, normalized

    async def _load_group_text_messages(
        self,
        session_id: str,
        *,
        member_name_map: dict[str, str],
        hours: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        self._require_sdk_boundary_available()
        scope = self._sdk_scope.get() or {}
        external_session_id = str(scope.get("external_session_id") or session_id)
        reader = WxbotMessageReader(self._settings, query_rows=self._sdk_query_rows)
        return await reader.load_group_text_messages(
            external_session_id,
            member_name_map=member_name_map,
            hours=hours,
            limit=limit,
        )

    def _sdk_absolute_url(self, value: str) -> str:
        if (self._sdk_scope.get() or {}).get("mode") == "managed_unavailable":
            return ""
        url = str(value or "").strip()
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        base_url = str(
            getattr(self._settings, "wxbot_sdk_url", "http://127.0.0.1:5080") or ""
        ).rstrip("/")
        if not url.startswith("/"):
            url = "/" + url
        return f"{base_url}{url}"

    def _normalize_avatar(self, avatar: Any, wxid: str) -> dict[str, Any]:
        if not isinstance(avatar, dict):
            return {}
        normalized = dict(avatar)
        cache_url = str(normalized.get("cache_url") or "").strip()
        cached = bool(normalized.get("cached")) or bool(cache_url)
        normalized["cached"] = cached
        normalized["cache_url"] = cache_url
        normalized["avatar_url"] = self._sdk_absolute_url(cache_url) if cache_url else ""
        if cached and not normalized["avatar_url"] and wxid:
            normalized["avatar_url"] = self._sdk_absolute_url(
                f"/ext/roster/avatars/{quote(wxid, safe='')}"
            )
        normalized["small_head_url"] = str(normalized.get("small_head_url") or "")
        normalized["big_head_url"] = str(normalized.get("big_head_url") or "")
        normalized["content_type"] = str(normalized.get("content_type") or "")
        return normalized

    async def _cache_avatar_file(
        self, avatar_url: str, *, wxid: str, content_type: str = ""
    ) -> str:
        if (self._sdk_scope.get() or {}).get("mode") == "managed_unavailable":
            return ""
        url = str(avatar_url or "").strip()
        if not url.startswith(("http://", "https://")):
            return ""
        try:
            sdk_url = str(getattr(self._settings, "wxbot_sdk_url", "http://127.0.0.1:5080") or "")
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                configure_http_client(
                    client,
                    allowed_private_origins=[sdk_url],
                    origin_headers={
                        sdk_url: wxbot_sdk_headers(self._settings),
                    },
                )
                response = await safe_get(client, url)
                response.raise_for_status()
        except httpx.HTTPError:
            return ""
        media_type = str(
            response.headers.get("content-type") or content_type or "image/jpeg"
        ).split(";", 1)[0]
        if not media_type.startswith("image/") or not response.content:
            return ""
        extension = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(media_type.lower(), ".img")
        cache_dir = Path(self._settings.project_root) / "data" / "wxbot-avatars"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.md5(response.content).hexdigest()[:12]
            safe_wxid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", wxid or "avatar")[:64] or "avatar"
            file_path = cache_dir / f"{safe_wxid}-{digest}{extension}"
            if not file_path.exists():
                file_path.write_bytes(response.content)
        except OSError:
            return ""
        return str(file_path)

    @staticmethod
    def _external_session_id(session: Any) -> str:
        metadata = dict(getattr(session, "metadata", {}) or {})
        return str(
            getattr(session, "external_conversation_id", "")
            or metadata.get("external_conversation_id")
            or getattr(session, "session_id", "")
            or ""
        )

    @staticmethod
    def _canonical_delivery_session_id(
        session: Any,
        *,
        external_session_id: str,
    ) -> str:
        external_id = str(external_session_id or "").strip()
        if not external_id:
            raise ValueError("当前会话标识不可用")
        connection_id = (
            str(getattr(session, "connection_id", "") or "").strip() or LEGACY_WXBOT_CONNECTION_ID
        )
        if connection_id == LEGACY_WXBOT_CONNECTION_ID:
            return external_id

        expected = canonical_conversation_id(connection_id, external_id)
        declared = str(
            getattr(session, "canonical_conversation_id", "")
            or getattr(session, "session_id", "")
            or ""
        ).strip()
        if declared and declared != expected:
            raise ValueError("managed_wxbot_conversation_scope_mismatch")
        return expected

    @staticmethod
    def _external_reply_to_message_id(
        session: Any,
        *,
        latest_metadata: dict[str, Any],
        source_message_id: str,
    ) -> str:
        session_metadata = dict(getattr(session, "metadata", {}) or {})
        candidates = (
            latest_metadata.get("external_message_id"),
            latest_metadata.get("msg_svr_id"),
            session_metadata.get("external_message_id"),
            session_metadata.get("msg_svr_id"),
            latest_metadata.get("reply_to_message_id"),
            session_metadata.get("reply_to_message_id"),
        )
        connection_id = (
            str(getattr(session, "connection_id", "") or "").strip() or LEGACY_WXBOT_CONNECTION_ID
        )
        if connection_id == LEGACY_WXBOT_CONNECTION_ID:
            for value in candidates:
                normalized = str(value or "").strip()
                if normalized:
                    return normalized[:128]
            return str(source_message_id or "").strip()[:128]

        canonical_source_id = str(source_message_id or "").strip()
        for value in candidates:
            external_message_id = str(value or "").strip()
            if not external_message_id or external_message_id.startswith("cx1:"):
                continue
            if (
                canonical_message_id(
                    connection_id,
                    external_message_id,
                )
                == canonical_source_id
            ):
                return external_message_id[:128]
        # Quoting a source message is optional. Sending without a quote is
        # safer than leaking a canonical persistence ID into the SDK boundary.
        return ""

    def _require_sdk_boundary_available(self) -> None:
        if (self._sdk_scope.get() or {}).get("mode") == "managed_unavailable":
            raise ValueError(
                "managed wxbot SDK tool requires a connection-scoped bridge RPC"
            )

    def _normalize_member(self, item: dict[str, Any]) -> dict[str, Any]:
        wxid = str(item.get("wxid") or item.get("user_id") or item.get("member_wxid") or "").strip()
        display_name = str(
            item.get("display_name")
            or item.get("nickname")
            or item.get("nick_name")
            or item.get("remark")
            or item.get("alias")
            or item.get("name")
            or item.get("member_name")
            or ""
        ).strip()
        if not wxid and not display_name:
            return {}
        return {
            "display_name": display_name or wxid,
            "wxid": wxid,
            "avatar": self._normalize_avatar(item.get("avatar"), wxid),
        }

    @staticmethod
    def _match_group_member(members: list[dict[str, Any]], query: str) -> dict[str, Any]:
        lowered = str(query or "").strip().lower()
        if not lowered:
            return {}
        matched = next(
            (
                item
                for item in members
                if lowered == str(item.get("wxid") or "").lower()
                or lowered == str(item.get("display_name") or "").lower()
            ),
            None,
        )
        if matched is None:
            matched = next(
                (
                    item
                    for item in members
                    if lowered in str(item.get("display_name") or "").lower()
                    or lowered in str(item.get("wxid") or "").lower()
                ),
                None,
            )
        return dict(matched or {})

    @classmethod
    def _match_group_member_wxid(cls, members: list[dict[str, Any]], query: str) -> str:
        matched = cls._match_group_member(members, query)
        return str((matched or {}).get("wxid") or "").strip()

    @classmethod
    def _resolve_current_group_target(
        cls, session: Any, members: list[dict[str, Any]]
    ) -> dict[str, str]:
        turns = list(getattr(session, "turns", []) or [])
        current_user_turn = next(
            (turn for turn in reversed(turns) if str(getattr(turn, "role", "") or "") == "user"),
            None,
        )
        if current_user_turn is None:
            return {}

        metadata = dict(getattr(current_user_turn, "metadata", {}) or {})
        mentioned_me = bool(metadata.get("mentioned_me"))
        at_wxids = [
            str(item or "").strip()
            for item in (metadata.get("at_wxids") or [])
            if str(item or "").strip()
        ]
        original = str(
            metadata.get("wxbot_original_content")
            or metadata.get("original_content")
            or getattr(current_user_turn, "content", "")
            or ""
        ).strip()
        mention_names = [
            item.strip() for item in _MENTION_NAME_RE.findall(original) if item.strip()
        ]

        if mention_names:
            candidate_name = ""
            if mentioned_me and len(mention_names) >= 2:
                candidate_name = mention_names[1]
            elif not mentioned_me and len(mention_names) >= 1:
                candidate_name = mention_names[0]
            if candidate_name:
                matched_wxid = cls._match_group_member_wxid(members, candidate_name)
                if matched_wxid:
                    return {"user_id": matched_wxid, "display_name": candidate_name}

        if at_wxids:
            if mentioned_me and len(at_wxids) >= 2:
                return {"user_id": at_wxids[-1]}
            if len(at_wxids) == 1:
                return {"user_id": at_wxids[0]}

        return {}

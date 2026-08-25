from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol, cast

from app.channel import (
    LEGACY_WXBOT_CONNECTION_ID,
    ChannelMedia,
    ChannelSendOptions,
    ChannelSendResult,
    ChannelTarget,
)
from app.channel.models import ChannelFile
from app.social.contracts import GroupParticipationPolicyDocument
from app.social.rollout import resolve_humanization_features
from app.social.speech_ledger import GroupSpeechBudgetExceeded
from plugins.wxbot.group_file_policy import (
    GroupFileSendDenied,
    require_group_file_send_enabled,
)
from plugins.wxbot.store import WxbotStore

_GROUP_DELIVERY_CONTRACT_FIELDS = (
    "participation_status",
    "source_message_id",
    "participation_policy_version",
    "send_revalidation_enabled",
)
_GROUP_REVALIDATED_STATUSES = {"must_reply", "may_reply", "defer"}
_DELIVERY_CONTRACT_METADATA_KEY = "_wxbot_delivery_contract"
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_ENGLISH_CHANNEL_FALLBACK = "I can only reply in English. Please send that again."


class GroupParticipationPolicyReader(Protocol):
    async def get_group_policy(
        self,
        tenant_id: str,
        session_id: str,
    ) -> GroupParticipationPolicyDocument: ...


class ChannelConnectionReader(Protocol):
    async def get(self, tenant_id: str, connection_id: str) -> Any: ...


class WxbotGroupDeliverySuppressed(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason or "group_delivery_suppressed")


def group_policy_delivery_contract(
    document: GroupParticipationPolicyDocument,
    *,
    tenant_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Build the versioned send-time fence shared by delayed group producers."""

    if not document.effective_enabled:
        raise WxbotGroupDeliverySuppressed("participation_disabled")
    features = resolve_humanization_features(
        tenant_id=tenant_id,
        session_id=session_id,
        stage=document.policy.rollout_stage,
        opted_in=document.policy.rollout_opt_in,
        kill_switches=document.kill_switches,
        proactive_percent=document.policy.proactive_rollout_percent,
    )
    return {
        "participation_policy_version": int(document.version),
        "send_revalidation_enabled": bool(features.send_revalidation_enabled),
        "participation_policy_source": "social_policy_store",
        "humanization_stage": features.stage.value,
        "humanization_cohort": features.cohort,
        "speech_budget_enabled": bool(features.speech_budget_enabled),
        "duplicate_guard_enabled": bool(features.duplicate_guard_enabled),
    }


def captured_group_delivery_contract(
    *,
    source_message_id: str,
    policy_state: dict[str, Any] | None,
    response_kind: str,
) -> dict[str, Any]:
    """Capture request-time policy identity for an asynchronous task result."""

    state = policy_state if isinstance(policy_state, dict) else {}
    source_id = str(source_message_id or "").strip()
    contract: dict[str, Any] = {
        "participation_status": "must_reply",
        "source_message_id": source_id,
        "response_kind": str(response_kind or "tool_result").strip() or "tool_result",
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
        if key in state:
            contract[key] = state[key]
    return contract


class WxbotChannelOutbound:
    def __init__(
        self,
        store: WxbotStore,
        *,
        social_policy_store: GroupParticipationPolicyReader | None = None,
        connection_store: ChannelConnectionReader | None = None,
    ) -> None:
        self._store = store
        self._social_policy_store = social_policy_store
        self._connection_store = connection_store

    async def get_session_policy(self, target: ChannelTarget) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._store.get_session_policy(
                target.tenant_id,
                target.external_conversation_id or target.session_id,
            ),
        )

    async def capture_group_delivery_contract(
        self,
        target: ChannelTarget,
        *,
        source_message_id: str,
        response_kind: str = "tool_result",
    ) -> dict[str, Any]:
        """Snapshot the policy fence before a source-bound async task starts."""

        if not (target.session_kind == "group" or target.session_id.endswith("@chatroom")):
            return {}
        source_id = str(source_message_id or "").strip()
        if not source_id:
            raise RuntimeError("wxbot_group_delivery_source_message_id_required")
        if self._social_policy_store is None:
            raise RuntimeError("wxbot_group_delivery_policy_store_required")
        document = await self._social_policy_store.get_group_policy(
            target.tenant_id,
            target.external_conversation_id or target.session_id,
        )
        policy_contract = group_policy_delivery_contract(
            document,
            tenant_id=target.tenant_id,
            session_id=target.external_conversation_id or target.session_id,
        )
        return captured_group_delivery_contract(
            source_message_id=source_id,
            policy_state=policy_contract,
            response_kind=response_kind,
        )

    async def send_text(
        self,
        target: ChannelTarget,
        text: str,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        options = options or ChannelSendOptions()
        try:
            reply_id = await self._enqueue(
                target,
                reply_text=text,
                msg_type="text",
                image_path="",
                image_url="",
                file_path="",
                file_name="",
                file_size=None,
                file_md5="",
                file_sha256="",
                options=options,
            )
        except GroupSpeechBudgetExceeded as exc:
            return ChannelSendResult(
                provider="wxbot",
                metadata={
                    "suppressed": True,
                    "reason": exc.reason,
                    "output_kind": exc.output_kind,
                },
            )
        except WxbotGroupDeliverySuppressed as exc:
            return ChannelSendResult(
                provider="wxbot",
                metadata={"suppressed": True, "reason": exc.reason},
            )
        return ChannelSendResult(
            message_id=str(reply_id),
            provider="wxbot",
            metadata={"reply_queue_id": reply_id},
        )

    async def send_image(
        self,
        target: ChannelTarget,
        media: ChannelMedia,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        options = options or ChannelSendOptions()
        try:
            reply_id = await self._enqueue(
                target,
                reply_text="",
                msg_type="image",
                image_path=media.image_path,
                image_url=media.image_url,
                file_path="",
                file_name="",
                file_size=None,
                file_md5="",
                file_sha256="",
                options=options,
            )
        except GroupSpeechBudgetExceeded as exc:
            return ChannelSendResult(
                provider="wxbot",
                metadata={
                    "suppressed": True,
                    "reason": exc.reason,
                    "output_kind": exc.output_kind,
                },
            )
        except WxbotGroupDeliverySuppressed as exc:
            return ChannelSendResult(
                provider="wxbot",
                metadata={"suppressed": True, "reason": exc.reason},
            )
        return ChannelSendResult(
            message_id=str(reply_id),
            provider="wxbot",
            metadata={"reply_queue_id": reply_id},
        )

    async def send_video(
        self,
        target: ChannelTarget,
        media: ChannelMedia,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        file_path = str(media.video_path or "").strip()
        if not file_path:
            raise ValueError("video_path is required for SDK file delivery")
        return await self.send_file(
            target,
            ChannelFile(
                file_path=file_path,
                file_name=Path(file_path).name,
            ),
            options,
        )

    async def send_file(
        self,
        target: ChannelTarget,
        file: ChannelFile,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        options = options or ChannelSendOptions()
        try:
            reply_id = await self._enqueue(
                target,
                reply_text="",
                msg_type="file",
                image_path="",
                image_url="",
                file_path=file.file_path,
                file_name=file.file_name,
                file_size=file.file_size,
                file_md5=file.file_md5,
                file_sha256=file.file_sha256,
                options=options,
            )
        except GroupSpeechBudgetExceeded as exc:
            return ChannelSendResult(
                provider="wxbot",
                metadata={
                    "suppressed": True,
                    "reason": exc.reason,
                    "output_kind": exc.output_kind,
                },
            )
        except WxbotGroupDeliverySuppressed as exc:
            return ChannelSendResult(
                provider="wxbot",
                metadata={"suppressed": True, "reason": exc.reason},
            )
        except GroupFileSendDenied as exc:
            return ChannelSendResult(
                provider="wxbot",
                metadata={"suppressed": True, "reason": exc.reason},
            )
        return ChannelSendResult(
            message_id=str(reply_id),
            provider="wxbot",
            metadata={"reply_queue_id": reply_id},
        )

    async def _enqueue(
        self,
        target: ChannelTarget,
        *,
        reply_text: str,
        msg_type: str,
        image_path: str,
        image_url: str,
        file_path: str,
        file_name: str,
        file_size: int | None,
        file_md5: str,
        file_sha256: str,
        options: ChannelSendOptions,
    ) -> int:
        await self._require_connection_enabled(target)
        delivery_metadata = dict(options.delivery_metadata or {})
        persona_language = str(
            delivery_metadata.get("persona_response_language")
            or target.metadata.get("persona_response_language")
            or ""
        ).strip().lower()
        if msg_type == "text" and persona_language in {"en", "en-us", "en-gb", "english"}:
            if _CJK_RE.search(str(reply_text or "")):
                reply_text = _ENGLISH_CHANNEL_FALLBACK
        if msg_type == "file" and (
            target.session_kind == "group"
            or target.session_id.endswith("@chatroom")
            or (target.external_conversation_id or "").endswith("@chatroom")
        ):
            await require_group_file_send_enabled(
                self._social_policy_store,
                tenant_id=target.tenant_id,
                session_id=target.external_conversation_id or target.session_id,
            )
        mention_sender = options.mention_sender
        if mention_sender is None:
            mention_sender = False

        reply_to_message_id = options.reply_to_message_id or target.reply_to_message_id
        delivery = {
            "channel": target.channel or "wechat",
            "adapter_id": target.adapter_id or "wechat-sdk",
            "connection_id": target.connection_id,
            "tenant_id": target.tenant_id,
            "session_id": target.session_id,
            "external_conversation_id": (target.external_conversation_id or target.session_id),
            "canonical_conversation_id": (target.canonical_conversation_id or target.session_id),
            "session_name": target.session_name,
            "session_kind": target.session_kind,
            "sender_name": target.sender_name,
            "sender_wxid": target.sender_id,
            "mention_sender": bool(mention_sender),
            "reply_to_msg_svr_id": reply_to_message_id,
            **delivery_metadata,
        }
        if persona_language in {"en", "en-us", "en-gb", "english"}:
            delivery["persona_response_language"] = "en"
        if (
            target.session_kind == "group" or target.session_id.endswith("@chatroom")
        ) and _is_source_bound_group_delivery(
            target,
            options=options,
            delivery=delivery,
        ):
            delivery = await self._prepare_group_delivery(
                target,
                options=options,
                delivery=delivery,
            )
        command_id = (
            options.idempotency_key
            or str(delivery.get("command_id") or "")
            or str(delivery.get("idempotency_key") or "")
        )
        enqueue_payload: dict[str, Any] = {
            "tenant_id": target.tenant_id,
            "session_id": target.session_id,
            "session_name": target.session_name,
            "sender_name": target.sender_name,
            "sender_wxid": target.sender_id,
            "reply_text": reply_text,
            "trace_id": options.trace_id,
            "msg_type": msg_type,
            "image_path": image_path,
            "image_url": image_url,
            "mention_sender": bool(mention_sender),
            "reply_to_msg_svr_id": reply_to_message_id,
            "session_kind": target.session_kind,
            "source_message": options.source_message,
            "delivery": delivery,
            "command_id": command_id,
        }
        if msg_type == "file":
            enqueue_payload.update(
                {
                    "file_path": file_path,
                    "file_name": file_name,
                    "file_size": file_size,
                    "file_md5": file_md5,
                    "file_sha256": file_sha256,
                }
            )
        return cast(int, await self._store.enqueue_reply(**enqueue_payload))

    async def _require_connection_enabled(self, target: ChannelTarget) -> None:
        """Fail closed before enqueueing for a managed connection.

        The adapter-level registry dispatcher is shared by all WeChat
        accounts.  This check is what prevents an unknown, disabled, deleted,
        or differently typed connection from using that shared dispatcher.
        """

        connection_id = str(target.connection_id or "").strip()
        if not connection_id or connection_id == LEGACY_WXBOT_CONNECTION_ID:
            return
        if self._connection_store is None:
            raise RuntimeError("wxbot_connection_state_reader_required")
        try:
            connection = await self._connection_store.get(
                target.tenant_id,
                connection_id,
            )
        except Exception as exc:
            raise RuntimeError("wxbot_connection_not_available") from exc
        if str(getattr(connection, "adapter_id", "") or "") != "wechat-sdk":
            raise RuntimeError("wxbot_connection_adapter_mismatch")
        if str(getattr(connection, "desired_state", "") or "") != "enabled":
            raise RuntimeError("wxbot_connection_not_enabled")

    async def _prepare_group_delivery(
        self,
        target: ChannelTarget,
        *,
        options: ChannelSendOptions,
        delivery: dict[str, Any],
    ) -> dict[str, Any]:
        prepared = dict(delivery)
        source_message_id = _group_source_message_id(
            target,
            options=options,
            delivery=prepared,
        )
        if not source_message_id:
            raise RuntimeError("wxbot_group_delivery_source_message_id_required")
        prepared["source_message_id"] = source_message_id

        response_kind = str(prepared.get("response_kind") or "").strip().lower()
        speech_class = str(prepared.get("speech_class") or "").strip().lower()
        status = str(prepared.get("participation_status") or "").strip().lower()
        if not status:
            status = (
                "must_reply"
                if response_kind in {"tool_progress", "tool_result"} or speech_class == "obligation"
                else "may_reply"
            )
        if status not in _GROUP_REVALIDATED_STATUSES:
            raise RuntimeError("wxbot_group_delivery_participation_status_invalid")
        prepared["participation_status"] = status
        if status == "must_reply":
            prepared["speech_class"] = "obligation"
        elif status == "defer":
            prepared.setdefault("speech_class", "scheduled")
        else:
            prepared.setdefault("speech_class", "soft")

        complete_contract = _has_complete_group_delivery_contract(prepared)
        if self._social_policy_store is None:
            raise RuntimeError("wxbot_group_delivery_policy_store_required")
        if not complete_contract:
            raise RuntimeError("wxbot_group_delivery_request_contract_required")

        document = await self._social_policy_store.get_group_policy(
            target.tenant_id,
            target.external_conversation_id or target.session_id,
        )
        current = group_policy_delivery_contract(
            document,
            tenant_id=target.tenant_id,
            session_id=target.external_conversation_id or target.session_id,
        )
        captured_version = prepared.get("participation_policy_version")
        if captured_version not in (None, ""):
            try:
                queued_version = int(captured_version)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("wxbot_group_delivery_policy_version_invalid") from exc
            if queued_version != int(current["participation_policy_version"]):
                raise WxbotGroupDeliverySuppressed("participation_policy_version_changed")
        captured_revalidation = prepared.get("send_revalidation_enabled")
        if not isinstance(captured_revalidation, bool):
            raise RuntimeError("wxbot_group_delivery_send_revalidation_enabled_invalid")
        if captured_revalidation != bool(current["send_revalidation_enabled"]):
            raise RuntimeError("wxbot_group_delivery_send_revalidation_policy_mismatch")
        for key, value in current.items():
            prepared.setdefault(key, value)
        if not _has_complete_group_delivery_contract(prepared):
            raise RuntimeError("wxbot_group_delivery_contract_incomplete")
        return prepared


def _group_source_message_id(
    target: ChannelTarget,
    *,
    options: ChannelSendOptions,
    delivery: dict[str, Any],
) -> str:
    source = options.source_message if isinstance(options.source_message, dict) else {}
    captured = source.get(_DELIVERY_CONTRACT_METADATA_KEY)
    captured_mapping = captured if isinstance(captured, dict) else {}
    source_metadata = source.get("metadata")
    metadata_mapping = source_metadata if isinstance(source_metadata, dict) else {}
    for value in (
        delivery.get("source_message_id"),
        captured_mapping.get("source_message_id"),
        source.get("message_id"),
        source.get("msg_svr_id"),
        metadata_mapping.get("message_id"),
        metadata_mapping.get("msg_svr_id"),
        options.reply_to_message_id,
        target.reply_to_message_id,
    ):
        source_id = str(value or "").strip()
        if source_id:
            return source_id[:128]
    return ""


def _is_source_bound_group_delivery(
    target: ChannelTarget,
    *,
    options: ChannelSendOptions,
    delivery: dict[str, Any],
) -> bool:
    if _group_source_message_id(target, options=options, delivery=delivery):
        return True
    return bool(
        str(delivery.get("response_kind") or "").strip() in {"tool_progress", "tool_result"}
        or any(key in delivery for key in _GROUP_DELIVERY_CONTRACT_FIELDS)
    )


def _has_complete_group_delivery_contract(delivery: dict[str, Any]) -> bool:
    if not all(key in delivery for key in _GROUP_DELIVERY_CONTRACT_FIELDS):
        return False
    if str(delivery.get("participation_status") or "").strip().lower() not in (
        _GROUP_REVALIDATED_STATUSES
    ):
        return False
    if not str(delivery.get("source_message_id") or "").strip():
        return False
    version = delivery.get("participation_policy_version")
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        return False
    try:
        int(version)
    except (TypeError, ValueError):
        return False
    return isinstance(delivery.get("send_revalidation_enabled"), bool)

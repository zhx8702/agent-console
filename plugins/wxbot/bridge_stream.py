"""SDK polling/SSE ingestion and inbound event publication."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.channel.adapters import WECHAT_SDK_ADAPTER_ID
from app.channel.identity import (
    LEGACY_WXBOT_CONNECTION_ID,
    canonical_conversation_id,
    canonical_message_id,
    canonical_participant_id,
)
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.types import Channel, InboundEvent, Message, MessageType
from plugins.wxbot.bridge_contract import (
    _SDK_JSON_CONTENT_TYPES,
    _SDK_MAX_JSON_BYTES,
    _SDK_MAX_SSE_BYTES,
    _SDK_SSE_CONNECTION_TIMEOUT_SECONDS,
    MEMBER_EVENT_TYPES,
    _partition_key,
)
from plugins.wxbot.bridge_state import WxbotBridgeState

log = get_logger(__name__)


class WxbotBridgeStreamMixin(WxbotBridgeState):
    async def _wait_for_bus(self) -> Any:
        while not self._stop.is_set():
            bus = getattr(self._container, "bus", None)
            if bus is not None:
                return bus
            await asyncio.sleep(0.5)
        return None

    async def _ingest_loop(self) -> None:
        bus = await self._wait_for_bus()
        if bus is None:
            return

        while not self._stop.is_set():
            try:
                ok = await self._try_unified_sse(bus)
                if ok:
                    continue
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("wxbot.bridge.unified_sse_error")

            try:
                ok = await self._try_sse(bus)
                if ok:
                    continue
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("wxbot.bridge.sse_error")

            try:
                await self._poll_once(bus)
            except asyncio.CancelledError:
                break
            except httpx.ConnectError:
                self._sdk_online = False
                self._ingest_mode = "offline"
                await asyncio.sleep(self._poll_interval * 3)
            except Exception:
                log.exception("wxbot.bridge.poll_error")
                await asyncio.sleep(self._poll_interval)

    async def _try_sse(self, bus: Any) -> bool:
        cursor = await self._get_legacy_cursor()
        cursor = await self._reconcile_legacy_ingest_cursor(cursor)
        stream = self._settings.bus_inbound_stream
        saw_message = False

        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as sse_client:
                async with self._stream_sdk(
                    sse_client,
                    self._sdk_url,
                    "/messages/stream",
                    params={"cursor": cursor},
                    headers=self._sdk_headers,
                    timeout_seconds=_SDK_SSE_CONNECTION_TIMEOUT_SECONDS,
                    max_response_bytes=_SDK_MAX_SSE_BYTES,
                ) as resp:
                    if resp.status_code != 200:
                        self._stream_mode = "legacy"
                        return False

                    self._sdk_online = True
                    self._stream_mode = "legacy"
                    self._ingest_mode = "legacy-sse"
                    log.info("wxbot.bridge.sse_connected", cursor=cursor)

                    buffer = ""
                    async for chunk in resp.aiter_text():
                        if self._stop.is_set():
                            return True

                        buffer += chunk
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            frame = frame.strip()
                            if not frame or frame.startswith(":"):
                                continue
                            if frame.startswith("data: "):
                                data_str = frame[6:]
                                try:
                                    msg = json.loads(data_str)
                                except json.JSONDecodeError:
                                    continue
                                await self._publish_legacy_message(msg, stream, bus)
                                saw_message = True
                                new_cursor = msg.get("id", cursor)
                                if new_cursor > cursor:
                                    cursor = new_cursor
                                    await self._set_legacy_cursor(cursor)
        except httpx.ConnectError:
            self._sdk_online = False
            self._stream_mode = "offline"
            self._ingest_mode = "offline"
            await asyncio.sleep(self._poll_interval * 3)
            return False
        except (httpx.ReadTimeout, httpx.RemoteProtocolError):
            log.info("wxbot.bridge.sse_disconnected")
            return saw_message

        return saw_message

    async def _event_loop(self) -> None:
        while not self._stop.is_set():
            if self._stream_mode == "unified":
                self._event_mode = "merged"
                await asyncio.sleep(self._poll_interval)
                continue
            try:
                ok = await self._try_event_sse()
                if ok:
                    continue
            except asyncio.CancelledError:
                break
            except httpx.ConnectError:
                self._event_mode = "offline"
                await asyncio.sleep(self._poll_interval * 3)
            except Exception:
                log.exception("wxbot.bridge.event_sse_error")
                self._event_mode = "error"
                await asyncio.sleep(self._poll_interval)

    async def _try_event_sse(self) -> bool:
        cursor = await self._get_event_cursor()
        cursor = await self._reconcile_event_cursor(cursor)

        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as sse_client:
                async with self._stream_sdk(
                    sse_client,
                    self._sdk_url,
                    "/events/stream",
                    params={"cursor": cursor},
                    headers=self._sdk_headers,
                    timeout_seconds=_SDK_SSE_CONNECTION_TIMEOUT_SECONDS,
                    max_response_bytes=_SDK_MAX_SSE_BYTES,
                ) as resp:
                    if resp.status_code != 200:
                        self._event_mode = f"http-{resp.status_code}"
                        return False

                    self._event_mode = "sse"
                    log.info("wxbot.bridge.event_sse_connected", cursor=cursor)

                    buffer = ""
                    async for chunk in resp.aiter_text():
                        if self._stop.is_set():
                            return True

                        buffer += chunk
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            parsed = self._parse_sse_frame(frame)
                            if not parsed:
                                continue
                            event_type, data_str = parsed
                            if event_type not in MEMBER_EVENT_TYPES:
                                continue
                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            await self._record_legacy_member_event(event)
                            new_cursor = int(event.get("id") or cursor)
                            if new_cursor > cursor:
                                cursor = new_cursor
                                await self._set_event_cursor(cursor)
        except httpx.ConnectError:
            self._event_mode = "offline"
            raise
        except (httpx.ReadTimeout, httpx.RemoteProtocolError):
            log.info("wxbot.bridge.event_sse_disconnected")
            self._event_mode = "reconnecting"
            return False

        return False

    async def _poll_once(self, bus: Any) -> None:
        self._stream_mode = "legacy"
        self._ingest_mode = "polling"
        cursor = await self._get_legacy_cursor()
        cursor = await self._reconcile_legacy_ingest_cursor(cursor)
        resp = await self._request_sdk(
            self._client,
            "GET",
            self._sdk_url,
            "/messages",
            params={"cursor": cursor, "limit": 100},
            headers={"Accept": "application/json", **self._sdk_headers},
            timeout_seconds=10.0,
            max_response_bytes=_SDK_MAX_JSON_BYTES,
            allowed_response_content_types=_SDK_JSON_CONTENT_TYPES,
        )
        if resp.status_code != 200:
            self._sdk_online = False
            await asyncio.sleep(self._poll_interval * 3)
            return

        self._sdk_online = True
        data = resp.json()
        messages = data.get("messages", [])
        if not messages:
            await asyncio.sleep(self._poll_interval)
            return

        stream = self._settings.bus_inbound_stream
        for msg in messages:
            await self._publish_legacy_message(msg, stream, bus)

        new_cursor = data.get("cursor", cursor)
        if new_cursor > cursor:
            await self._set_legacy_cursor(new_cursor)
        await asyncio.sleep(self._poll_interval)

    @staticmethod
    def _parse_sse_frame(frame: str) -> tuple[str, str] | None:
        event_type = "message"
        data_lines: list[str] = []
        for raw_line in frame.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line[6:].strip() or "message"
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return None
        return event_type, "\n".join(data_lines)

    async def _try_unified_sse(self, bus: Any) -> bool:
        cursor = await self._get_cursor()
        cursor = await self._reconcile_ingest_cursor(cursor)
        stream = self._settings.bus_inbound_stream
        saw_event = False

        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as sse_client:
                async with self._stream_sdk(
                    sse_client,
                    self._sdk_url,
                    "/stream",
                    params={"cursor": cursor},
                    headers=self._sdk_headers,
                    timeout_seconds=_SDK_SSE_CONNECTION_TIMEOUT_SECONDS,
                    max_response_bytes=_SDK_MAX_SSE_BYTES,
                ) as resp:
                    if resp.status_code == 404:
                        self._stream_mode = "legacy"
                        return False
                    if resp.status_code != 200:
                        self._stream_mode = f"http-{resp.status_code}"
                        self._ingest_mode = f"http-{resp.status_code}"
                        return False

                    self._sdk_online = True
                    self._stream_mode = "unified"
                    self._ingest_mode = "unified-sse"
                    self._event_mode = "merged"
                    log.info("wxbot.bridge.unified_sse_connected", cursor=cursor)

                    buffer = ""
                    async for chunk in resp.aiter_text():
                        if self._stop.is_set():
                            return True

                        buffer += chunk
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            parsed = self._parse_sse_frame(frame)
                            if not parsed:
                                continue
                            _, data_str = parsed
                            try:
                                envelope = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            await self._handle_stream_event(envelope, stream, bus)
                            saw_event = True
                            new_cursor = int(envelope.get("id") or cursor)
                            if new_cursor > cursor:
                                cursor = new_cursor
                                await self._set_cursor(cursor)
                                await self._set_event_cursor(cursor)
        except httpx.ConnectError:
            self._sdk_online = False
            self._stream_mode = "offline"
            self._ingest_mode = "offline"
            await asyncio.sleep(self._poll_interval * 3)
            return False
        except (httpx.ReadTimeout, httpx.RemoteProtocolError):
            log.info("wxbot.bridge.unified_sse_disconnected")
            self._stream_mode = "reconnecting"
            return saw_event

        return saw_event

    async def _record_legacy_member_event(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") or {}
        saved = await self._store.save_member_event(
            tenant_id=self._tenant_id,
            connection_id=self._connection_id or LEGACY_WXBOT_CONNECTION_ID,
            sdk_event_id=int(event.get("id") or 0),
            event_type=str(event.get("event_type") or ""),
            session_id=str(event.get("session_id") or ""),
            session_name=str(event.get("session_name") or ""),
            entity_wxid=str(event.get("entity_wxid") or ""),
            entity_name=str(event.get("entity_name") or ""),
            payload=payload if isinstance(payload, dict) else {},
            created_ts=int(event.get("created_ts") or 0),
        )
        if saved:
            log.info(
                "wxbot.bridge.member_event_saved",
                sdk_event_id=event.get("id"),
                event_type=event.get("event_type"),
                session_id=event.get("session_id"),
            )

    async def _record_stream_member_event(self, envelope: dict[str, Any]) -> None:
        member = envelope.get("member") or {}
        operator = envelope.get("operator") or {}
        session = envelope.get("session") or {}
        payload = {
            "operator": operator if isinstance(operator, dict) else {},
            "raw": envelope.get("raw") if isinstance(envelope.get("raw"), dict) else {},
            "meta": envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {},
        }
        saved = await self._store.save_member_event(
            tenant_id=self._tenant_id,
            connection_id=self._connection_id or LEGACY_WXBOT_CONNECTION_ID,
            sdk_event_id=int(envelope.get("id") or 0),
            event_type=str(envelope.get("event_type") or ""),
            session_id=str(session.get("id") or ""),
            session_name=str(session.get("name") or ""),
            entity_wxid=str(member.get("id") or ""),
            entity_name=str(member.get("name") or ""),
            payload=payload,
            created_ts=int(envelope.get("occurred_ts") or 0),
        )
        if saved:
            log.info(
                "wxbot.bridge.member_event_saved",
                sdk_event_id=envelope.get("id"),
                event_type=envelope.get("event_type"),
                session_id=session.get("id"),
            )

    async def _record_interactive_inbound(
        self,
        *,
        session_id: str,
        message_id: str,
        content: str,
        mentioned_me: bool,
        is_self_sent: bool,
    ) -> None:
        """Advance the send-time conversation cursor for every inbound message.

        The cursor is not an eligibility decision. A non-triggering group
        message can still answer the question or move the conversation on and
        must therefore cancel an older delayed ``may_reply`` response.
        """

        _ = content, mentioned_me
        if is_self_sent or not session_id or not message_id:
            return
        recorder = getattr(self._store, "record_interactive_inbound", None)
        if not callable(recorder):
            return
        try:
            await recorder(
                tenant_id=self._tenant_id,
                session_id=session_id,
                message_id=message_id,
            )
        except Exception as exc:
            log.warning(
                "wxbot.bridge.interaction_cursor_failed",
                session_id=session_id,
                message_id=message_id,
                error=str(exc),
            )

    async def _record_group_observation(self, event: InboundEvent) -> None:
        if not str(event.session_id or "").endswith("@chatroom"):
            return
        if event.metadata.get("capture_allowed") is False:
            return
        recorder = getattr(self._store, "record_group_observation", None)
        if not callable(recorder):
            return
        metadata = dict(event.metadata or {})
        durable_metadata_keys = {
            "source",
            "sdk_source",
            "sdk_stream_id",
            "sdk_event_id",
            "sdk_event_type",
            "msg_svr_id",
            "mentioned_me",
            "at_wxids",
            "mention_mode",
            "bot_mentioned",
            "bot_addressed",
            "bot_mention_position",
            "bot_mention_names",
            "bot_normalized_content",
            "wxbot_normalized_content",
            "bot_wxid",
            "quote",
            "quote_text",
            "quote_image_path",
            "quote_image_url",
            "image_observation",
            "media_status",
            "occurred_at",
            "occurred_ts",
        }
        durable_metadata = {key: metadata[key] for key in durable_metadata_keys if key in metadata}
        try:
            await recorder(
                tenant_id=event.tenant_id,
                session_id=event.session_id,
                message_id=event.message_id,
                session_name=str(metadata.get("session_name") or ""),
                sender_wxid=str(metadata.get("sender_wxid") or event.user_id or ""),
                sender_name=str(metadata.get("sender_name") or ""),
                msg_type=str(getattr(event.message.type, "value", event.message.type) or "text"),
                content=str(event.message.content or ""),
                mentioned_me=bool(metadata.get("bot_mentioned") or metadata.get("mentioned_me")),
                bot_addressed=bool(
                    metadata.get("bot_addressed")
                    if metadata.get("bot_addressed") is not None
                    else metadata.get("mentioned_me")
                ),
                is_self_sent=bool(metadata.get("is_self_sent")),
                occurred_ts=int(metadata.get("occurred_ts") or event.received_at.timestamp()),
                metadata=durable_metadata,
                summary_debounce_seconds=float(
                    getattr(
                        self._settings,
                        "wxbot_group_summary_debounce_seconds",
                        5.0,
                    )
                    or 5.0
                ),
            )
        except Exception as exc:
            # The inbound worker repeats this durable write before acquiring the
            # Session lock, so a transient bridge-side DB error does not drop it.
            log.warning(
                "wxbot.bridge.group_observation_failed",
                session_id=event.session_id,
                message_id=event.message_id,
                error_type=exc.__class__.__name__,
            )

    async def _publish_legacy_message(self, msg: dict[str, Any], stream: str, bus: Any) -> None:
        message_id = str(msg.get("msg_svr_id") or "").strip()
        if not await self._mark_inbound_seen(message_id):
            log.info(
                "wxbot.bridge.inbound_duplicate_suppressed",
                mode="legacy",
                message_id=message_id,
                session_id=msg.get("session_id", ""),
            )
            return

        occurred_at = self._parse_sdk_timestamp(
            msg.get("occurred_at"),
            msg.get("created_at"),
            msg.get("occurred_ts"),
            msg.get("recv_ts"),
            msg.get("created_ts"),
            msg.get("timestamp"),
        )
        if self._is_stale_inbound_message(occurred_at):
            log.warning(
                "wxbot.bridge.stale_legacy_message_dropped",
                message_id=message_id,
                session_id=msg.get("session_id", ""),
                occurred_at=occurred_at.isoformat() if occurred_at else "",
                max_age_seconds=self._max_inbound_message_age_seconds(),
            )
            return

        capture_allowed = self._capture_allowed(msg)
        if not capture_allowed:
            await self._record_interactive_inbound(
                session_id=str(msg.get("session_id") or ""),
                message_id=message_id,
                content="",
                mentioned_me=False,
                is_self_sent=bool(msg.get("is_self_sent")),
            )
            log.info(
                "wxbot.bridge.capture_disallowed",
                mode="legacy",
                message_id=message_id,
                session_id=str(msg.get("session_id") or ""),
                reason_code="capture_not_allowed",
            )
            return

        trace_id = new_trace_id()
        msg_type = msg.get("msg_type", "text")
        content = msg.get("msg_text", "")
        raw_occurred_at = str(msg.get("occurred_at") or msg.get("created_at") or "").strip()

        metadata: dict[str, Any] = {
            "source": "wxbot",
            "session_name": msg.get("session_name", ""),
            "sender_wxid": msg.get("sender_wxid", ""),
            "sender_name": msg.get("sender_name", ""),
            "msg_svr_id": msg.get("msg_svr_id", ""),
            "mentioned_me": bool(msg.get("mentioned_me")),
            "at_wxids": list(msg.get("at_wxids") or []),
            "mention_mode": str(msg.get("mention_mode") or ""),
            "is_self_sent": bool(msg.get("is_self_sent")),
            "bot_mentioned": bool(msg.get("bot_mentioned") or msg.get("mentioned_me")),
            "bot_addressed": bool(
                msg.get("bot_addressed")
                if msg.get("bot_addressed") is not None
                else msg.get("mentioned_me")
            ),
            "bot_mention_position": str(msg.get("bot_mention_position") or ""),
            "bot_mention_names": list(msg.get("bot_mention_names") or []),
            "bot_normalized_content": str(msg.get("bot_normalized_content") or ""),
            "bot_wxid": str(msg.get("bot_wxid") or ""),
            "capture_allowed": capture_allowed,
            "capture_reason": str(msg.get("capture_reason") or ""),
            "session_kind": "group"
            if str(msg.get("session_id", "")).endswith("@chatroom")
            else "private",
        }
        if occurred_at is not None:
            metadata["occurred_at"] = raw_occurred_at or occurred_at.isoformat()
            metadata["occurred_ts"] = int(occurred_at.timestamp())
        if msg_type == "image" and msg.get("image_path"):
            msg_record = msg if isinstance(msg, dict) else {}
            raw_record = self._record(msg_record.get("raw"))
            preview_url = (
                self._resolve_media_variant_url(
                    msg_record,
                    raw_record,
                    variant="preview",
                )
                or str(msg.get("image_preview_url") or "").strip()
            )
            thumbnail_url = (
                self._resolve_media_variant_url(
                    msg_record,
                    raw_record,
                    variant="thumbnail",
                )
                or str(msg.get("image_thumbnail_url") or "").strip()
            )
            if preview_url:
                preview_url = self._image_url(self._sdk_url, preview_url)
            if thumbnail_url:
                thumbnail_url = self._image_url(self._sdk_url, thumbnail_url)
            metadata["image_url"] = preview_url or self._image_url(
                self._sdk_url, str(msg["image_path"])
            )
            metadata["image_path"] = str(msg["image_path"])
            metadata["image_preview_url"] = preview_url or metadata["image_url"]
            metadata["image_preview_path"] = self._resolve_media_variant_path(
                msg_record, raw_record, variant="preview"
            ) or str(msg.get("image_preview_path") or msg["image_path"])
            if thumbnail_url:
                metadata["image_thumbnail_url"] = thumbnail_url
                metadata["image_thumbnail_path"] = self._resolve_media_variant_path(
                    msg_record, raw_record, variant="thumbnail"
                ) or str(msg.get("image_thumbnail_path") or "")
            image_variants = self._record(msg.get("image_variants")) or self._record(
                raw_record.get("image_variants")
            )
            if image_variants:
                metadata["image_variants"] = image_variants
            if not content:
                content = "[图片]"
        if msg_type == "image":
            media_status = str(
                msg.get("media_status") or ("ready" if msg.get("image_path") else "pending")
            )
            metadata["media_status"] = media_status
            metadata["image_failure_reason"] = str(msg.get("image_failure_reason") or "")
            metadata["media"] = {
                "type": "image",
                "status": media_status,
                "variant": str(msg.get("media_variant") or ""),
                "image_path": str(msg.get("image_path") or ""),
                "image_url": str(msg.get("image_url") or metadata.get("image_url") or ""),
                "image_preview_path": str(metadata.get("image_preview_path") or ""),
                "image_preview_url": str(metadata.get("image_preview_url") or ""),
                "image_thumbnail_path": str(metadata.get("image_thumbnail_path") or ""),
                "image_thumbnail_url": str(metadata.get("image_thumbnail_url") or ""),
                "image_variants": metadata.get("image_variants") or {},
                "failure_reason": str(msg.get("image_failure_reason") or ""),
            }
        self._apply_quote_metadata(metadata, msg.get("quote"))
        self._apply_image_observation_metadata(metadata, msg_type)

        external_message_id = message_id or f"wxbot-{trace_id}"
        external_conversation_id = str(msg.get("session_id") or "")
        external_participant_id = str(msg.get("sender_wxid") or "unknown")
        connection_id = self._connection_id or LEGACY_WXBOT_CONNECTION_ID
        canonical_session_id = canonical_conversation_id(
            connection_id,
            external_conversation_id,
        )
        canonical_user_id = canonical_participant_id(
            connection_id,
            external_participant_id,
        )
        canonical_msg_id = canonical_message_id(connection_id, external_message_id)
        metadata.update(
            adapter_id=WECHAT_SDK_ADAPTER_ID,
            connection_id=connection_id,
            external_message_id=external_message_id,
            canonical_message_id=canonical_msg_id,
            external_conversation_id=external_conversation_id,
            canonical_conversation_id=canonical_session_id,
            external_participant_id=external_participant_id,
            canonical_participant_id=canonical_user_id,
        )
        event = InboundEvent(
            message_id=canonical_msg_id,
            tenant_id=self._tenant_id,
            channel=Channel.WECHAT,
            adapter_id=WECHAT_SDK_ADAPTER_ID,
            connection_id=connection_id,
            user_id=canonical_user_id,
            session_id=canonical_session_id,
            external_message_id=external_message_id,
            canonical_message_id=canonical_msg_id,
            external_conversation_id=external_conversation_id,
            canonical_conversation_id=canonical_session_id,
            external_participant_id=external_participant_id,
            canonical_participant_id=canonical_user_id,
            message=Message(type=MessageType.TEXT, content=content),
            trace_id=trace_id,
            metadata=metadata,
        )

        await self._record_group_observation(event)
        await self._record_interactive_inbound(
            session_id=event.session_id,
            message_id=event.message_id,
            content=content,
            mentioned_me=bool(metadata.get("mentioned_me")),
            is_self_sent=bool(metadata.get("is_self_sent")),
        )

        try:
            await bus.publish(
                stream=stream,
                payload=event.model_dump(mode="json"),
                partition_key=_partition_key(
                    self._tenant_id,
                    str(msg.get("session_id") or ""),
                    self._connection_id,
                ),
            )
        except Exception:
            await self._release_inbound_seen(message_id)
            raise
        if not bool(metadata.get("is_self_sent")):
            await self._record_connection_activity("inbound")
        log.info(
            "wxbot.bridge.published",
            message_id=event.message_id,
            session_id=event.session_id,
        )

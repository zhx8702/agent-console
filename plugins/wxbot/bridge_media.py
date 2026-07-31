"""Media normalization, deferred resolution, and delivery-event projection."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from app.channel.adapters import WECHAT_SDK_ADAPTER_ID
from app.channel.identity import (
    LEGACY_WXBOT_CONNECTION_ID,
    canonical_conversation_id,
    canonical_message_id,
    canonical_participant_id,
)
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.types import Attachment, Channel, InboundEvent, Message, MessageType
from plugins.wxbot.bridge_contract import (
    _IMAGE_PREVIEW_VARIANTS,
    _IMAGE_THUMBNAIL_VARIANTS,
    _SDK_JSON_CONTENT_TYPES,
    _SDK_MAX_JSON_BYTES,
    MEDIA_READY_EVENT_TYPE,
    MEMBER_EVENT_TYPES,
    _partition_key,
)
from plugins.wxbot.bridge_state import WxbotBridgeState

log = get_logger(__name__)


class WxbotBridgeMediaMixin(WxbotBridgeState):
    @staticmethod
    def _rfc3339_or_now(value: str | None) -> str:
        if value:
            return value
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _capture_allowed(payload: dict[str, Any]) -> bool:
        """Default to capture only when the field is absent; malformed values fail closed."""

        if "capture_allowed" not in payload:
            return True
        raw = payload.get("capture_allowed")
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw == 1
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "allow", "allowed"}
        return False

    @staticmethod
    def _image_url(sdk_url: str, image_path: str) -> str:
        path = str(image_path or "").strip()
        if not path:
            return ""
        if path.startswith(("http://", "https://")):
            parsed = urlsplit(path)
            url_path = unquote(parsed.path).replace("\\", "/")
            marker = "/images/"
            marker_index = url_path.lower().rfind(marker)
            if marker_index < 0:
                return path
            relative = WxbotBridgeMediaMixin._image_relative_path(
                url_path[marker_index + len(marker) :]
            )
            return urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    f"/images/{quote(relative, safe='/')}",
                    parsed.query,
                    parsed.fragment,
                )
            )
        relative = WxbotBridgeMediaMixin._image_relative_path(path)
        return f"{sdk_url.rstrip('/')}/images/{quote(relative, safe='/')}"

    @staticmethod
    def _image_relative_path(image_path: str) -> str:
        normalized = str(image_path or "").strip().replace("\\", "/")
        lower = normalized.lower()
        marker = "/images/"
        marker_index = lower.rfind(marker)
        if marker_index >= 0:
            normalized = normalized[marker_index + len(marker) :]
        elif lower.startswith("images/"):
            normalized = normalized[len("images/") :]
        return normalized.lstrip("/")

    @staticmethod
    def _file_url(sdk_url: str, file_url: str) -> str:
        value = str(file_url or "").strip()
        if not value:
            return ""
        if value.startswith(("http://", "https://")):
            return value
        return f"{sdk_url.rstrip('/')}/{value.lstrip('/')}"

    @staticmethod
    def _first_nonempty_str(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _record(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _first_present_value(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return ""

    def _file_fields(
        self,
        message: dict[str, Any],
        media: dict[str, Any] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        media_record = self._record(media)
        raw_record = self._record(raw)
        file_size = self._first_present_value(
            message.get("file_size"),
            media_record.get("file_size"),
            media_record.get("size"),
            raw_record.get("file_size"),
            raw_record.get("size"),
        )
        try:
            normalized_file_size = int(file_size) if file_size != "" else 0
        except (TypeError, ValueError):
            normalized_file_size = 0

        file_url = self._first_nonempty_str(
            message.get("file_url"),
            media_record.get("file_url"),
            raw_record.get("file_url"),
        )
        file_download_status = self._first_nonempty_str(
            media_record.get("file_download_status"),
            message.get("file_download_status"),
            media_record.get("status"),
            raw_record.get("file_download_status"),
        ).lower()
        media_status = self._first_nonempty_str(
            media_record.get("media_status"),
            media_record.get("status"),
            message.get("media_status"),
            raw_record.get("media_status"),
            file_download_status,
        ).lower()
        return {
            "file_raw_name": self._first_nonempty_str(
                message.get("file_raw_name"),
                media_record.get("file_raw_name"),
                raw_record.get("file_raw_name"),
            ),
            "file_name": self._first_nonempty_str(
                message.get("file_name"),
                media_record.get("file_name"),
                raw_record.get("file_name"),
            ),
            "file_ext": self._first_nonempty_str(
                message.get("file_ext"),
                media_record.get("file_ext"),
                raw_record.get("file_ext"),
            ),
            "file_size": normalized_file_size,
            "file_md5": self._first_nonempty_str(
                message.get("file_md5"),
                media_record.get("file_md5"),
                media_record.get("md5"),
                raw_record.get("file_md5"),
                raw_record.get("md5"),
            ),
            "file_sha256": self._first_nonempty_str(
                message.get("file_sha256"),
                media_record.get("file_sha256"),
                media_record.get("sha256"),
                raw_record.get("file_sha256"),
                raw_record.get("sha256"),
            ),
            "file_url": self._file_url(self._sdk_url, file_url),
            "file_download_status": file_download_status,
            "file_failure_reason": self._first_nonempty_str(
                message.get("file_failure_reason"),
                media_record.get("file_failure_reason"),
                media_record.get("failure_reason"),
                raw_record.get("file_failure_reason"),
            ),
            "media_status": media_status,
        }

    @staticmethod
    def _file_placeholder(file_name: str) -> str:
        return f"[文件] {file_name}".rstrip()

    @staticmethod
    def _file_attachment(fields: dict[str, Any]) -> dict[str, Any]:
        """Project one SDK file into the shared inbound attachment contract."""

        extension = str(fields.get("file_ext") or "").strip().lower().lstrip(".")
        mime_by_ext = {
            "txt": "text/plain",
            "md": "text/markdown",
            "csv": "text/csv",
            "json": "application/json",
            "pdf": "application/pdf",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xls": "application/vnd.ms-excel",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "zip": "application/zip",
        }
        attachment = Attachment(
            type=MessageType.FILE,
            url=str(fields.get("file_url") or "").strip() or None,
            mime=mime_by_ext.get(extension),
            size=max(0, int(fields.get("file_size") or 0)),
            name=str(fields.get("file_name") or fields.get("file_raw_name") or "").strip()
            or None,
            ext=extension or None,
            md5=str(fields.get("file_md5") or "").strip() or None,
            sha256=str(fields.get("file_sha256") or "").strip() or None,
            status=str(fields.get("media_status") or "").strip() or None,
            download_status=str(fields.get("file_download_status") or "").strip() or None,
            failure_reason=str(fields.get("file_failure_reason") or "").strip() or None,
        )
        return attachment.model_dump(exclude_none=True)

    def _media_payload_type(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
    ) -> str:
        return self._first_nonempty_str(
            message.get("type"),
            media.get("message_type"),
            media.get("type"),
        ).lower()

    def _image_variant_records(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
        variants: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for container in (media, message):
            image_variants = self._record(container.get("image_variants"))
            variants_payload = self._record(container.get("variants"))
            for name in variants:
                for payload in (
                    image_variants.get(name),
                    variants_payload.get(name),
                    container.get(name),
                ):
                    record = self._record(payload)
                    if record:
                        records.append(record)
        return records

    def _image_variant_path(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
        variants: tuple[str, ...],
    ) -> str:
        for record in self._image_variant_records(message, media, variants):
            path = self._first_nonempty_str(
                record.get("image_path"),
                record.get("path"),
                record.get("local_path"),
                record.get("media_path"),
            )
            if path:
                return path
        return ""

    def _image_variant_url(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
        variants: tuple[str, ...],
    ) -> str:
        for record in self._image_variant_records(message, media, variants):
            explicit = self._first_nonempty_str(
                record.get("image_url"),
                record.get("url"),
                record.get("media_url"),
            )
            if explicit:
                return self._image_url(self._sdk_url, explicit)
            path = self._first_nonempty_str(
                record.get("image_path"),
                record.get("path"),
                record.get("local_path"),
                record.get("media_path"),
            )
            if path:
                return self._image_url(self._sdk_url, path)
        return ""

    def _resolve_media_ready_path(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
    ) -> str:
        if self._media_payload_type(message, media) == "file":
            return self._first_nonempty_str(
                media.get("file_path"),
                message.get("file_path"),
            )
        return self._first_nonempty_str(
            media.get("image_path"),
            media.get("path"),
            media.get("local_path"),
            message.get("image_path"),
            message.get("path"),
            message.get("local_path"),
        )

    def _resolve_media_ready_url(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
    ) -> str:
        if self._media_payload_type(message, media) == "file":
            return self._file_url(
                self._sdk_url,
                self._first_nonempty_str(
                    media.get("file_url"),
                    message.get("file_url"),
                ),
            )
        explicit = self._first_nonempty_str(
            media.get("image_url"),
            media.get("url"),
            message.get("image_url"),
            message.get("url"),
        )
        if explicit:
            return self._image_url(self._sdk_url, explicit)

        media_path = self._resolve_media_ready_path(message, media)
        if media_path:
            return self._image_url(self._sdk_url, media_path)
        return ""

    def _resolve_media_variant_path(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
        *,
        variant: str,
    ) -> str:
        if variant == "thumbnail":
            return self._image_variant_path(
                message,
                media,
                _IMAGE_THUMBNAIL_VARIANTS,
            ) or self._first_nonempty_str(
                media.get("image_thumbnail_path"),
                media.get("thumbnail_path"),
                media.get("thumb_path"),
                message.get("image_thumbnail_path"),
                message.get("thumbnail_path"),
                message.get("thumb_path"),
            )
        if variant == "preview":
            return self._image_variant_path(
                message,
                media,
                _IMAGE_PREVIEW_VARIANTS,
            ) or self._first_nonempty_str(
                media.get("image_preview_path"),
                media.get("preview_path"),
                message.get("image_preview_path"),
                message.get("preview_path"),
            )
        return ""

    def _resolve_media_variant_url(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
        *,
        variant: str,
    ) -> str:
        if variant == "thumbnail":
            variant_url = self._image_variant_url(message, media, _IMAGE_THUMBNAIL_VARIANTS)
            if variant_url:
                return variant_url
            explicit = self._first_nonempty_str(
                media.get("image_thumbnail_url"),
                media.get("thumbnail_url"),
                media.get("thumb_url"),
                message.get("image_thumbnail_url"),
                message.get("thumbnail_url"),
                message.get("thumb_url"),
            )
        elif variant == "preview":
            variant_url = self._image_variant_url(message, media, _IMAGE_PREVIEW_VARIANTS)
            if variant_url:
                return variant_url
            explicit = self._first_nonempty_str(
                media.get("image_preview_url"),
                media.get("preview_url"),
                message.get("image_preview_url"),
                message.get("preview_url"),
            )
        else:
            explicit = ""
        if explicit:
            return self._image_url(self._sdk_url, explicit)

        path = self._resolve_media_variant_path(message, media, variant=variant)
        if path:
            return self._image_url(self._sdk_url, path)
        return ""

    def _extract_quote_image(self, quote_payload: Any) -> tuple[str, str, str, str]:
        if not isinstance(quote_payload, dict):
            return "", "", "", ""
        candidates = [
            quote_payload,
            quote_payload.get("message"),
            quote_payload.get("media"),
            quote_payload.get("quoted_message"),
            quote_payload.get("quoted"),
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            raw_candidate = self._record(candidate.get("raw"))
            preview_url = self._image_variant_url(candidate, raw_candidate, _IMAGE_PREVIEW_VARIANTS)
            thumbnail_url = self._image_variant_url(
                candidate,
                raw_candidate,
                _IMAGE_THUMBNAIL_VARIANTS,
            )
            image_path = self._first_nonempty_str(
                candidate.get("image_path"),
                candidate.get("path"),
                candidate.get("local_path"),
            )
            image_url = self._first_nonempty_str(
                candidate.get("image_url"),
                candidate.get("url"),
            )
            preview_url = preview_url or self._first_nonempty_str(
                candidate.get("image_preview_url"),
                candidate.get("preview_url"),
            )
            thumbnail_url = thumbnail_url or self._first_nonempty_str(
                candidate.get("image_thumbnail_url"),
                candidate.get("thumbnail_url"),
                candidate.get("thumb_url"),
            )
            if preview_url and not preview_url.startswith(("http://", "https://")):
                preview_url = self._image_url(self._sdk_url, preview_url)
            if thumbnail_url and not thumbnail_url.startswith(("http://", "https://")):
                thumbnail_url = self._image_url(self._sdk_url, thumbnail_url)
            if image_path:
                resolved_url = preview_url or self._image_url(self._sdk_url, image_path)
                return image_path, resolved_url, preview_url or resolved_url, thumbnail_url
            if image_url:
                resolved_url = self._image_url(self._sdk_url, image_url)
                return "", resolved_url, preview_url or resolved_url, thumbnail_url
        return "", "", "", ""

    def _extract_quote_text(self, quote_payload: Any) -> str:
        if not isinstance(quote_payload, dict):
            return ""
        candidates = [
            quote_payload,
            quote_payload.get("message"),
            quote_payload.get("quoted_message"),
            quote_payload.get("quoted"),
            quote_payload.get("raw"),
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            text = self._first_nonempty_str(
                candidate.get("text"),
                candidate.get("content"),
                candidate.get("message_text"),
                candidate.get("msg"),
                candidate.get("body"),
                candidate.get("caption"),
            ).strip()
            if text and text not in {"[图片]", "[image]"}:
                return text
        return ""

    def _apply_quote_metadata(self, metadata: dict[str, Any], quote_payload: Any) -> None:
        if not isinstance(quote_payload, dict):
            return
        metadata["quote"] = quote_payload
        quote_text = self._extract_quote_text(quote_payload)
        if quote_text:
            metadata["quote_text"] = quote_text
        image_path, image_url, preview_url, thumbnail_url = self._extract_quote_image(quote_payload)
        if image_path:
            metadata["quote_image_path"] = image_path
        if image_url:
            metadata["quote_image_url"] = image_url
        if preview_url:
            metadata["quote_image_preview_url"] = preview_url
        if thumbnail_url:
            metadata["quote_image_thumbnail_url"] = thumbnail_url

    @staticmethod
    def _apply_image_observation_metadata(metadata: dict[str, Any], msg_type: str) -> None:
        current_image_found = bool(metadata.get("image_url") or metadata.get("image_path"))
        quote_image_found = bool(
            metadata.get("quote_image_url") or metadata.get("quote_image_path")
        )
        media_status = str(metadata.get("media_status") or "")
        failure_reason = str(metadata.get("image_failure_reason") or "")
        metadata["image_observation"] = {
            "current_image_found": current_image_found,
            "quote_image_found": quote_image_found,
            "attachment_count": 1 if current_image_found else 0,
            "quote_attachment_count": 1 if quote_image_found else 0,
            "media_status": media_status,
            "failure_reason": failure_reason,
            "skip_reason": (
                failure_reason
                or ("pending" if msg_type == "image" and not current_image_found else "")
            ),
        }

    def _media_ready_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        pending = self._pending_media_messages.get(message_id)
        if pending is None:
            return None
        return pending

    @staticmethod
    def _stream_message_id(envelope: dict[str, Any]) -> str:
        message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
        return str(
            message.get("id") or envelope.get("message_id") or envelope.get("event_id") or ""
        ).strip()

    @staticmethod
    def _stream_message_type(envelope: dict[str, Any]) -> str:
        message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
        media = envelope.get("media") if isinstance(envelope.get("media"), dict) else {}
        return (
            str(message.get("type") or media.get("message_type") or media.get("type") or "text")
            .strip()
            .lower()
        )

    def _stream_ready_media(self, envelope: dict[str, Any]) -> tuple[str, str]:
        message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
        media = envelope.get("media") if isinstance(envelope.get("media"), dict) else {}
        image_path = self._resolve_media_ready_path(message, media)
        image_url = self._resolve_media_ready_url(message, media)
        return image_path, image_url

    @staticmethod
    def _stream_media_status(envelope: dict[str, Any]) -> str:
        message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
        media = envelope.get("media") if isinstance(envelope.get("media"), dict) else {}
        meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}
        return (
            str(
                media.get("status")
                or message.get("media_status")
                or message.get("file_download_status")
                or media.get("file_download_status")
                or meta.get("media_status")
                or ""
            )
            .strip()
            .lower()
        )

    def _track_stream_media_until_ready(self, envelope: dict[str, Any]) -> None:
        msg_type = self._stream_message_type(envelope)
        if msg_type not in {"image", "file"}:
            return
        media_path, media_url = self._stream_ready_media(envelope)
        if media_path or media_url:
            return
        media_status = self._stream_media_status(envelope)
        if media_status == "failed":
            return
        message_id = self._stream_message_id(envelope)
        if not message_id:
            return
        self._prune_pending_media()
        tracked = dict(envelope)
        tracked["_tracked_at_monotonic"] = time.monotonic()
        self._pending_media_messages[message_id] = tracked
        log.info(
            "wxbot.bridge.media_message_tracked_until_ready",
            message_id=message_id,
            media_type=msg_type,
            session_id=str((envelope.get("session") or {}).get("id") or ""),
        )

    def _prune_pending_media(self) -> None:
        """Bound deferred-media memory and avoid polling abandoned messages forever."""

        now = time.monotonic()
        ttl = max(
            60.0,
            float(
                getattr(self._settings, "wxbot_pending_media_ttl_seconds", 15 * 60)
                or 15 * 60
            ),
        )
        stale_ids = [
            message_id
            for message_id, envelope in self._pending_media_messages.items()
            if now - float(envelope.get("_tracked_at_monotonic") or now) > ttl
        ]
        for message_id in stale_ids:
            self._pending_media_messages.pop(message_id, None)
            log.warning(
                "wxbot.bridge.pending_media_expired",
                message_id=message_id,
                ttl_seconds=int(ttl),
            )
        max_items = max(
            1,
            int(
                getattr(self._settings, "wxbot_pending_media_max_items", 1000)
                or 1000
            ),
        )
        overflow = len(self._pending_media_messages) - max_items
        if overflow <= 0:
            return
        oldest = sorted(
            self._pending_media_messages.items(),
            key=lambda item: float(item[1].get("_tracked_at_monotonic") or now),
        )[:overflow]
        for message_id, _envelope in oldest:
            self._pending_media_messages.pop(message_id, None)
            log.warning(
                "wxbot.bridge.pending_media_capacity_evicted",
                message_id=message_id,
                max_items=max_items,
            )

    def _merge_media_ready_into_pending_message(self, envelope: dict[str, Any]) -> dict[str, Any]:
        message_id = self._stream_message_id(envelope)
        pending = self._pending_media_messages.pop(message_id, None)
        merged = dict(pending or envelope)

        pending_message = (
            pending.get("message")
            if isinstance(pending, dict) and isinstance(pending.get("message"), dict)
            else {}
        )
        ready_message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
        ready_media = envelope.get("media") if isinstance(envelope.get("media"), dict) else {}
        message = {**pending_message, **ready_message}
        msg_type = self._first_nonempty_str(
            message.get("type"),
            ready_media.get("message_type"),
            ready_media.get("type"),
            self._stream_message_type(pending or {}),
            "image",
        ).lower()
        if msg_type == "file":
            file_fields = self._file_fields(
                message,
                ready_media,
                envelope.get("raw") if isinstance(envelope.get("raw"), dict) else {},
            )
            message.update(file_fields)
            if message_id and not message.get("id"):
                message["id"] = message_id
            message["type"] = "file"
            merged_media = {**ready_media, **file_fields, "type": "file"}
            if file_fields["media_status"]:
                merged_media["status"] = file_fields["media_status"]
            merged["event_type"] = "message.received"
            merged["message"] = message
            merged["media"] = merged_media
            for key in ("session", "sender", "raw", "meta"):
                if key not in merged or not merged.get(key):
                    merged[key] = envelope.get(key)
            if envelope.get("occurred_at") or envelope.get("occurred_ts"):
                merged["occurred_at"] = envelope.get("occurred_at") or merged.get("occurred_at")
                merged["occurred_ts"] = envelope.get("occurred_ts") or merged.get("occurred_ts")
            return merged

        image_path = self._resolve_media_ready_path(ready_message, ready_media)
        image_url = self._resolve_media_ready_url(ready_message, ready_media)
        if image_path:
            message["image_path"] = image_path
        if image_url:
            message["image_url"] = image_url
        preview_path = self._resolve_media_variant_path(
            ready_message, ready_media, variant="preview"
        )
        preview_url = self._resolve_media_variant_url(ready_message, ready_media, variant="preview")
        thumbnail_path = self._resolve_media_variant_path(
            ready_message, ready_media, variant="thumbnail"
        )
        thumbnail_url = self._resolve_media_variant_url(
            ready_message, ready_media, variant="thumbnail"
        )
        if preview_path:
            message["image_preview_path"] = preview_path
        if preview_url:
            message["image_preview_url"] = preview_url
        if thumbnail_path:
            message["image_thumbnail_path"] = thumbnail_path
        if thumbnail_url:
            message["image_thumbnail_url"] = thumbnail_url
        if message_id and not message.get("id"):
            message["id"] = message_id
        message["type"] = "image"

        merged["event_type"] = "message.received"
        merged["message"] = message
        merged["media"] = ready_media
        for key in ("session", "sender", "raw", "meta"):
            if key not in merged or not merged.get(key):
                merged[key] = envelope.get(key)
        if envelope.get("occurred_at") or envelope.get("occurred_ts"):
            merged["occurred_at"] = envelope.get("occurred_at") or merged.get("occurred_at")
            merged["occurred_ts"] = envelope.get("occurred_ts") or merged.get("occurred_ts")
        return merged

    async def _pending_media_resolver_loop(self) -> None:
        bus = await self._wait_for_bus()
        if bus is None:
            return
        stream = self._settings.bus_inbound_stream
        while not self._stop.is_set():
            try:
                if self._pending_media_messages:
                    await self._resolve_pending_media_from_sdk(stream, bus)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("wxbot.bridge.pending_media_resolve_failed")
            await asyncio.sleep(max(1.0, self._poll_interval))

    async def _resolve_pending_media_from_sdk(self, stream: str, bus: Any) -> int:
        self._prune_pending_media()
        if not self._pending_media_messages or self._client is None:
            return 0

        pending_ids = list(self._pending_media_messages)
        batch_size = min(50, len(pending_ids))
        start = self._pending_media_resolve_offset % len(pending_ids)
        selected_ids = pending_ids[start : start + batch_size]
        if len(selected_ids) < batch_size:
            selected_ids.extend(pending_ids[: batch_size - len(selected_ids)])
        self._pending_media_resolve_offset = (start + batch_size) % len(pending_ids)

        semaphore = asyncio.Semaphore(8)

        async def fetch_message(message_id: str) -> dict[str, Any] | None:
            try:
                async with semaphore:
                    resp = await self._request_sdk(
                        self._client,
                        "GET",
                        self._sdk_url,
                        f"/messages/{quote(message_id, safe='')}",
                        headers={"Accept": "application/json", **self._sdk_headers},
                        timeout_seconds=5.0,
                        max_response_bytes=_SDK_MAX_JSON_BYTES,
                        allowed_response_content_types=_SDK_JSON_CONTENT_TYPES,
                    )
            except Exception:
                log.info(
                    "wxbot.bridge.pending_media_lookup_failed",
                    message_id=message_id,
                )
                return None
            if resp.status_code != 200:
                return None
            payload = resp.json()
            message = payload.get("message") if isinstance(payload, dict) else None
            if isinstance(message, dict):
                return message
            # Compatibility with older SDK builds and test doubles.
            rows = payload.get("messages") if isinstance(payload, dict) else None
            if isinstance(rows, list):
                return next(
                    (
                        row
                        for row in rows
                        if isinstance(row, dict)
                        and str(row.get("msg_svr_id") or row.get("id") or "").strip() == message_id
                    ),
                    None,
                )
            return None

        fetched = await asyncio.gather(*(fetch_message(message_id) for message_id in selected_ids))
        messages = [message for message in fetched if isinstance(message, dict)]

        resolved = 0
        pending_id_set = set(self._pending_media_messages)
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            message_id = str(msg.get("msg_svr_id") or msg.get("id") or "").strip()
            if message_id not in pending_id_set:
                continue

            pending = self._pending_media_messages.get(message_id) or {}
            pending_type = self._stream_message_type(pending)
            msg_type = str(msg.get("msg_type") or pending_type or "image").strip().lower()
            ready_envelope = dict(pending)
            ready_message = dict(
                pending.get("message") if isinstance(pending.get("message"), dict) else {}
            )
            if msg_type == "file":
                file_fields = self._file_fields(msg, msg, self._record(msg.get("raw")))
                if not file_fields["file_url"]:
                    continue
                ready_message.update(
                    {
                        "id": message_id,
                        "type": "file",
                        "text": (
                            ready_message.get("text")
                            or msg.get("msg_text")
                            or self._file_placeholder(str(file_fields["file_name"]))
                        ),
                        **file_fields,
                    }
                )
                media = {
                    "type": "file",
                    "status": file_fields["media_status"] or "ready",
                    **file_fields,
                }
            else:
                image_path = str(msg.get("image_path") or "").strip()
                image_url = str(msg.get("image_url") or "").strip()
                if not image_path and not image_url:
                    continue
                ready_message.update(
                    {
                        "id": message_id,
                        "type": "image",
                        "text": ready_message.get("text") or msg.get("msg_text") or "[图片]",
                    }
                )
                if image_path:
                    ready_message["image_path"] = image_path
                if image_url:
                    ready_message["image_url"] = image_url
                for key in (
                    "image_preview_path",
                    "image_preview_url",
                    "image_thumbnail_path",
                    "image_thumbnail_url",
                ):
                    value = str(msg.get(key) or "").strip()
                    if value:
                        ready_message[key] = value
                if isinstance(msg.get("image_variants"), dict):
                    ready_message["image_variants"] = dict(msg["image_variants"])

                media = {"type": "image"}
                if image_path:
                    media["image_path"] = image_path
                if image_url:
                    media["image_url"] = image_url
                for key in (
                    "image_preview_path",
                    "image_preview_url",
                    "image_thumbnail_path",
                    "image_thumbnail_url",
                ):
                    value = str(msg.get(key) or "").strip()
                    if value:
                        media[key] = value
                if isinstance(msg.get("image_variants"), dict):
                    media["image_variants"] = dict(msg["image_variants"])

            ready_at = datetime.now(UTC)
            ready_envelope.update(
                {
                    "event_type": MEDIA_READY_EVENT_TYPE,
                    "message": ready_message,
                    "media": media,
                    # This is a newly observed media-ready transition, even when
                    # the original message is old.  Using recv_ts here pushes
                    # deferred images outside the recent-media query window and
                    # leaves the queue placeholder permanently unenriched.
                    "occurred_ts": int(ready_at.timestamp()),
                    "occurred_at": ready_at.isoformat(),
                }
            )
            await self._record_stream_media_ready(ready_envelope)
            merged = self._merge_media_ready_into_pending_message(ready_envelope)
            await self._publish_stream_message(merged, stream, bus)
            resolved += 1
            log.info(
                "wxbot.bridge.pending_media_resolved_from_sdk_messages",
                message_id=message_id,
                session_id=str(msg.get("session_id") or ""),
            )
        return resolved

    async def _record_stream_media_ready(self, envelope: dict[str, Any]) -> None:
        session = envelope.get("session") or {}
        sender = envelope.get("sender") or {}
        message = envelope.get("message") if isinstance(envelope.get("message"), dict) else {}
        media = envelope.get("media") if isinstance(envelope.get("media"), dict) else {}
        raw = envelope.get("raw") if isinstance(envelope.get("raw"), dict) else {}
        meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}

        media_path = self._resolve_media_ready_path(message, media)
        media_url = self._resolve_media_ready_url(message, media)
        msg_type = self._first_nonempty_str(message.get("type"), media.get("message_type"))
        media_type = self._first_nonempty_str(media.get("type"), msg_type, "image")
        payload = {
            "message": message,
            "media": media,
            "raw": raw,
            "meta": meta,
        }
        saved = await self._store.save_media_ready_event(
            tenant_id=self._tenant_id,
            connection_id=self._connection_id or LEGACY_WXBOT_CONNECTION_ID,
            sdk_event_id=int(envelope.get("id") or 0),
            event_type=str(envelope.get("event_type") or MEDIA_READY_EVENT_TYPE),
            stream_event_id=str(envelope.get("event_id") or ""),
            message_id=self._first_nonempty_str(
                message.get("id"),
                envelope.get("message_id"),
            ),
            session_id=str(session.get("id") or ""),
            session_name=str(session.get("name") or ""),
            sender_wxid=str(sender.get("id") or ""),
            sender_name=str(sender.get("name") or ""),
            msg_type=msg_type,
            media_type=media_type,
            media_path=media_path,
            media_url=media_url,
            payload=payload,
            created_ts=int(envelope.get("occurred_ts") or 0),
        )
        if saved:
            log.info(
                "wxbot.bridge.media_ready_saved",
                sdk_event_id=envelope.get("id"),
                event_type=envelope.get("event_type"),
                message_id=self._first_nonempty_str(message.get("id"), envelope.get("message_id")),
                session_id=session.get("id"),
            )

    async def _publish_stream_message(
        self, envelope: dict[str, Any], stream: str, bus: Any
    ) -> None:
        message = envelope.get("message") or {}
        if not self._capture_allowed(message):
            message_id = str(message.get("id") or envelope.get("event_id") or "").strip()
            if not await self._mark_inbound_seen(message_id):
                log.info(
                    "wxbot.bridge.inbound_duplicate_suppressed",
                    mode="unified",
                    message_id=message_id,
                    session_id=str((envelope.get("session") or {}).get("id") or ""),
                )
                return
            occurred_at = self._parse_sdk_timestamp(
                envelope.get("occurred_at"),
                envelope.get("occurred_ts"),
                message.get("recv_ts"),
            )
            if self._is_stale_inbound_message(occurred_at):
                log.warning(
                    "wxbot.bridge.stale_stream_message_dropped",
                    message_id=message_id,
                    session_id=str((envelope.get("session") or {}).get("id") or ""),
                    occurred_at=occurred_at.isoformat() if occurred_at else "",
                    max_age_seconds=self._max_inbound_message_age_seconds(),
                )
                return
            await self._record_interactive_inbound(
                session_id=str((envelope.get("session") or {}).get("id") or ""),
                message_id=message_id,
                content="",
                mentioned_me=False,
                is_self_sent=bool(message.get("is_self_sent")),
            )
            log.info(
                "wxbot.bridge.capture_disallowed",
                mode="unified",
                message_id=message_id,
                session_id=str((envelope.get("session") or {}).get("id") or ""),
                reason_code="capture_not_allowed",
            )
            return
        # Publish the textual placeholder immediately, as the legacy SDK path does.
        # Media decryption may take minutes or fail permanently; withholding the
        # inbound event until then makes a real WeChat message disappear from the
        # queue and from downstream observability.  Keep a copy only so a later
        # media-ready event can still be recorded and correlated.
        self._track_stream_media_until_ready(envelope)

        message_id = str(message.get("id") or envelope.get("event_id") or "").strip()
        if not await self._mark_inbound_seen(message_id):
            log.info(
                "wxbot.bridge.inbound_duplicate_suppressed",
                mode="unified",
                message_id=message_id,
                session_id=str((envelope.get("session") or {}).get("id") or ""),
            )
            return

        occurred_at = self._parse_sdk_timestamp(
            envelope.get("occurred_at"),
            envelope.get("occurred_ts"),
            message.get("recv_ts"),
        )
        if self._is_stale_inbound_message(occurred_at):
            log.warning(
                "wxbot.bridge.stale_stream_message_dropped",
                message_id=message_id,
                session_id=str((envelope.get("session") or {}).get("id") or ""),
                occurred_at=occurred_at.isoformat() if occurred_at else "",
                max_age_seconds=self._max_inbound_message_age_seconds(),
            )
            return

        trace_id = new_trace_id()
        session = envelope.get("session") or {}
        sender = envelope.get("sender") or {}
        raw = envelope.get("raw") if isinstance(envelope.get("raw"), dict) else {}
        meta = envelope.get("meta") if isinstance(envelope.get("meta"), dict) else {}

        msg_type = str(message.get("type") or "text").strip().lower()
        content = str(message.get("text") or "")
        raw_occurred_at = str(envelope.get("occurred_at") or "").strip()
        session_id = str(session.get("id") or "")
        session_kind = str(
            session.get("kind") or ("group" if session_id.endswith("@chatroom") else "private")
        )
        metadata: dict[str, Any] = {
            "source": "wxbot",
            "sdk_source": envelope.get("source") or "wxbot-sdk",
            "sdk_stream_id": envelope.get("id"),
            "sdk_event_id": envelope.get("event_id") or f"stream:{envelope.get('id')}",
            "sdk_event_type": envelope.get("event_type") or "message.received",
            "session_name": str(session.get("name") or ""),
            "sender_wxid": str(sender.get("id") or ""),
            "sender_name": str(sender.get("name") or ""),
            "sender_roles": (
                list(sender.get("roles") or [])
                if isinstance(sender.get("roles"), list)
                else [
                    str(sender.get("role") or sender.get("member_role") or "").strip()
                ]
                if str(sender.get("role") or sender.get("member_role") or "").strip()
                else []
            ),
            "sender_is_group_admin": bool(
                sender.get("is_group_admin")
                or sender.get("group_admin")
                or sender.get("is_admin")
                or str(sender.get("role") or sender.get("member_role") or "")
                .strip()
                .lower()
                in {"admin", "group_admin", "owner"}
            ),
            "sender_is_group_owner": bool(
                sender.get("is_group_owner")
                or sender.get("group_owner")
                or sender.get("is_owner")
                or str(sender.get("role") or sender.get("member_role") or "")
                .strip()
                .lower()
                == "owner"
            ),
            "msg_svr_id": str(message.get("id") or ""),
            "mentioned_me": bool(message.get("mentioned_me")),
            "at_wxids": list(message.get("at_wxids") or []),
            "mention_mode": str(message.get("mention_mode") or ""),
            "is_self_sent": bool(message.get("is_self_sent")),
            "bot_mentioned": bool(message.get("bot_mentioned") or message.get("mentioned_me")),
            "bot_addressed": bool(
                message.get("bot_addressed")
                if message.get("bot_addressed") is not None
                else message.get("mentioned_me")
            ),
            "bot_mention_position": str(message.get("bot_mention_position") or ""),
            "bot_mention_names": list(message.get("bot_mention_names") or []),
            "bot_normalized_content": str(message.get("bot_normalized_content") or ""),
            "bot_wxid": str(message.get("bot_wxid") or ""),
            "capture_allowed": self._capture_allowed(message),
            "capture_reason": str(message.get("capture_reason") or ""),
            "session_kind": session_kind,
            "occurred_ts": int(occurred_at.timestamp()) if occurred_at else 0,
            "occurred_at": raw_occurred_at
            or (occurred_at.isoformat() if occurred_at else self._rfc3339_or_now("")),
            "raw": raw,
            "meta": meta,
        }
        if msg_type == "image":
            image_path = str(message.get("image_path") or "")
            media = envelope.get("media") if isinstance(envelope.get("media"), dict) else {}
            media_with_raw = {**raw, **media}
            if not self._record(media_with_raw.get("image_variants")) and self._record(
                raw.get("image_variants")
            ):
                media_with_raw["image_variants"] = raw["image_variants"]
            preview_path = (
                self._resolve_media_variant_path(message, media_with_raw, variant="preview")
                or image_path
            )
            preview_url = (
                self._resolve_media_variant_url(message, media_with_raw, variant="preview")
                or (self._image_url(self._sdk_url, preview_path) if preview_path else "")
                or str(message.get("image_url") or "")
            )
            thumbnail_path = self._resolve_media_variant_path(
                message, media_with_raw, variant="thumbnail"
            )
            thumbnail_url = self._resolve_media_variant_url(
                message, media_with_raw, variant="thumbnail"
            )
            image_variants = self._record(media_with_raw.get("image_variants")) or self._record(
                message.get("image_variants")
            )
            if image_path:
                metadata["image_url"] = preview_url or self._image_url(self._sdk_url, image_path)
                metadata["image_path"] = image_path
                metadata["image_preview_path"] = preview_path
                metadata["image_preview_url"] = preview_url or metadata["image_url"]
                if thumbnail_path:
                    metadata["image_thumbnail_path"] = thumbnail_path
                if thumbnail_url:
                    metadata["image_thumbnail_url"] = thumbnail_url
                if image_variants:
                    metadata["image_variants"] = image_variants
            if not content:
                content = "[图片]"
            media_status = self._stream_media_status(envelope) or (
                "ready" if image_path else "pending"
            )
            metadata["media_status"] = media_status
            metadata["image_failure_reason"] = str(
                media.get("failure_reason") or message.get("image_failure_reason") or ""
            )
            metadata["media"] = {
                "type": "image",
                "status": media_status,
                "variant": str(media.get("variant") or message.get("media_variant") or ""),
                "image_path": image_path,
                "image_url": str(message.get("image_url") or metadata.get("image_url") or ""),
                "image_preview_path": str(metadata.get("image_preview_path") or ""),
                "image_preview_url": str(metadata.get("image_preview_url") or ""),
                "image_thumbnail_path": str(metadata.get("image_thumbnail_path") or ""),
                "image_thumbnail_url": str(metadata.get("image_thumbnail_url") or ""),
                "image_variants": metadata.get("image_variants") or {},
                "failure_reason": metadata["image_failure_reason"],
            }
        elif msg_type == "file":
            media = envelope.get("media") if isinstance(envelope.get("media"), dict) else {}
            file_fields = self._file_fields(message, media, raw)
            if not file_fields["media_status"]:
                file_fields["media_status"] = "ready" if file_fields["file_url"] else "pending"
            if not file_fields["file_download_status"]:
                file_fields["file_download_status"] = file_fields["media_status"]
            for key, value in file_fields.items():
                metadata[key] = value
            metadata["media"] = {
                "type": "file",
                "status": file_fields["media_status"],
                **file_fields,
            }
            metadata["msg_type"] = "file"
            metadata["file_attachment"] = self._file_attachment(metadata)
            if not content:
                content = self._file_placeholder(str(file_fields["file_name"]))
        self._apply_quote_metadata(metadata, message.get("quote") or envelope.get("quote"))
        self._apply_image_observation_metadata(metadata, msg_type)

        external_message_id = message_id or f"wxbot-{trace_id}"
        external_conversation_id = session_id
        external_participant_id = str(sender.get("id") or "unknown")
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
            # Preserve the existing placeholder message contract; file
            # metadata below is the canonical attachment projection.
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
                    session_id,
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
            event_type=envelope.get("event_type"),
        )

    async def _handle_stream_event(self, envelope: dict[str, Any], stream: str, bus: Any) -> None:
        event_type = str(envelope.get("event_type") or "").strip()
        if event_type == "message.received":
            await self._publish_stream_message(envelope, stream, bus)
            return
        if event_type == MEDIA_READY_EVENT_TYPE:
            await self._record_stream_media_ready(envelope)
            message_id = self._stream_message_id(envelope)
            if self._media_ready_by_message_id(message_id) is not None:
                await self._publish_stream_message(
                    self._merge_media_ready_into_pending_message(envelope),
                    stream,
                    bus,
                )
            return
        if event_type in MEMBER_EVENT_TYPES:
            await self._record_stream_member_event(envelope)
            return
        if event_type in {"message.delivery.succeeded", "message.delivery.failed"}:
            await self._record_delivery_event(envelope, event_type)
            return
        if event_type == "auth.revoked":
            auth_payload = envelope.get("auth")
            auth_detail = auth_payload if isinstance(auth_payload, dict) else {}
            self._sdk_auth_state = "revoked"
            self._sdk_auth_reason = str(
                auth_detail.get("reason") or auth_detail.get("detail") or "auth revoked"
            ).strip()
            log.warning(
                "wxbot.bridge.auth_revoked",
                reason_code="remote_authorization_revoked",
            )
            return
        if event_type == "runtime.warning":
            log.warning("wxbot.bridge.runtime_warning")
            return
        log.info("wxbot.bridge.stream_event_ignored", event_type=event_type)

    async def _record_delivery_event(self, envelope: dict[str, Any], event_type: str) -> None:
        delivery = envelope.get("delivery") if isinstance(envelope.get("delivery"), dict) else {}
        raw = envelope.get("raw") if isinstance(envelope.get("raw"), dict) else {}
        raw_delivery = raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {}
        command_id = str(
            delivery.get("command_id")
            or raw_delivery.get("command_id")
            or raw_delivery.get("idempotency_key")
            or ""
        ).strip()
        sdk_outbound_id = self._to_int(delivery.get("outbound_id"))
        status = str(delivery.get("status") or "").strip().lower()
        error = str(delivery.get("error") or "").strip()

        if not command_id:
            log.info(
                "wxbot.bridge.delivery_event_unmatched",
                event_type=event_type,
                has_delivery=bool(delivery),
                has_session=isinstance(envelope.get("session"), dict),
            )
            return

        if event_type == "message.delivery.succeeded":
            rows = await self._store.mark_reply_delivery_succeeded(
                command_id,
                tenant_id=self._tenant_id,
                connection_id=self._connection_id,
                sdk_outbound_id=sdk_outbound_id,
            )
            if rows:
                await self._record_connection_activity("outbound_delivered")
        else:
            rows = await self._store.mark_reply_delivery_failed(
                command_id,
                tenant_id=self._tenant_id,
                connection_id=self._connection_id,
                error=error or status or event_type,
                terminal=status == "failed",
                sdk_outbound_id=sdk_outbound_id,
            )

        log.info(
            "wxbot.bridge.delivery_event_recorded",
            event_type=event_type,
            tenant_id=self._tenant_id,
            command_id=command_id,
            sdk_outbound_id=sdk_outbound_id,
            status=status,
            matched=len(rows),
        )

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

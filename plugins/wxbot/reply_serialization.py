from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from app.common.types import OutboundReply, ReplySegment
from plugins.wxbot.hook_context import _GROUP_SEGMENT_STAGGER_SECONDS


def _is_absolute_file_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _optional_file_size(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("file_size must be a non-negative integer")
    size = int(value)
    if size < 0:
        raise ValueError("file_size must be a non-negative integer")
    return size


def _valid_digest(value: str, length: int) -> bool:
    return not value or (
        len(value) == length and all(character in "0123456789abcdef" for character in value)
    )


def _segment_to_queue_payload(segment: ReplySegment) -> dict[str, Any] | None:
    metadata = segment.metadata or {}
    msg_type = (
        str(metadata.get("wxbot_msg_type") or metadata.get("msg_type") or "text").strip().lower()
    )
    image_path = str(metadata.get("image_path") or "").strip()
    image_url = str(metadata.get("image_url") or "").strip()
    reply_text = str(metadata.get("text") or segment.content or "")

    if msg_type == "image":
        if not image_path and not image_url:
            return None
        return {
            "msg_type": "image",
            "reply_text": reply_text,
            "image_path": image_path,
            "image_url": image_url,
        }

    if msg_type == "file":
        file_path = str(metadata.get("file_path") or "").strip()
        file_url = str(metadata.get("file_url") or "").strip()
        file_md5 = str(metadata.get("file_md5") or "").strip().lower()
        file_sha256 = str(metadata.get("file_sha256") or "").strip().lower()
        try:
            file_size = _optional_file_size(metadata.get("file_size"))
        except (TypeError, ValueError):
            return None
        if (
            not file_path
            or file_url
            or not _is_absolute_file_path(file_path)
            or not _valid_digest(file_md5, 32)
            or not _valid_digest(file_sha256, 64)
        ):
            return None
        return {
            "msg_type": "file",
            "reply_text": reply_text,
            "image_path": "",
            "image_url": "",
            "file_path": file_path,
            "file_name": str(metadata.get("file_name") or "").strip(),
            "file_size": file_size,
            "file_md5": file_md5,
            "file_sha256": file_sha256,
        }

    if not reply_text.strip():
        return None
    return {
        "msg_type": "text",
        "reply_text": reply_text,
        "image_path": "",
        "image_url": "",
    }


def _collect_wxbot_messages(reply: OutboundReply) -> list[dict[str, Any]]:
    if reply.segments:
        items = [
            payload
            for payload in (_segment_to_queue_payload(segment) for segment in reply.segments)
            if payload is not None
        ]
        if items:
            return items
    if reply.primary_text.strip():
        return [
            {
                "msg_type": "text",
                "reply_text": reply.primary_text,
                "image_path": "",
                "image_url": "",
            }
        ]
    return []


def _group_text_stats(messages: list[dict[str, Any]]) -> tuple[int, int]:
    text = "\n".join(
        str(item.get("reply_text") or "").strip()
        for item in messages
        if item.get("msg_type") == "text" and str(item.get("reply_text") or "").strip()
    )
    return len(text), len([line for line in text.splitlines() if line.strip()])


def _staggered_not_before(value: object, *, index: int) -> str:
    now = datetime.now(UTC)
    base = now
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            base = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        except ValueError:
            base = now
    return (max(base, now) + timedelta(seconds=_GROUP_SEGMENT_STAGGER_SECONDS * index)).isoformat()

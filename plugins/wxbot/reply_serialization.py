from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.common.types import OutboundReply, ReplySegment
from plugins.wxbot.hook_context import _GROUP_SEGMENT_STAGGER_SECONDS


def _segment_to_queue_payload(segment: ReplySegment) -> dict[str, str] | None:
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

    if not reply_text.strip():
        return None
    return {
        "msg_type": "text",
        "reply_text": reply_text,
        "image_path": "",
        "image_url": "",
    }


def _collect_wxbot_messages(reply: OutboundReply) -> list[dict[str, str]]:
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


def _group_text_stats(messages: list[dict[str, str]]) -> tuple[int, int]:
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

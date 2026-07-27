from __future__ import annotations

import re
from typing import Any

REPLY_POLICY_OVERRIDE_KEY = "reply_policy_override"


def match_reply_policy(
    mode: str,
    content: str,
    keywords: list[str],
    *,
    mentioned_me: bool = False,
    is_group: bool = False,
) -> tuple[bool, str]:
    """Apply one reply-policy implementation at ingress and egress."""
    if mode == "all":
        return True, "reply_mode_all"
    if mode == "off":
        return False, "reply_mode_off"
    if mode == "contains":
        if is_group and mentioned_me:
            return True, "reply_mode_contains_mention"
        normalized_content = str(content or "").casefold()
        for keyword in keywords:
            value = str(keyword or "").strip().casefold()
            if not value:
                continue
            if value.isascii():
                if re.search(
                    rf"(?<![a-z0-9_]){re.escape(value)}(?![a-z0-9_])",
                    normalized_content,
                ):
                    return True, "reply_mode_contains_match"
            elif len(value) >= 2 and value in normalized_content:
                return True, "reply_mode_contains_match"
            elif len(value) == 1 and normalized_content.strip() == value:
                return True, "reply_mode_contains_match"
        return False, "reply_mode_contains_no_match"
    return False, "reply_mode_unknown"


def set_reply_policy_override(
    extras: dict[str, Any],
    *,
    force_send: bool | None = None,
    mention_sender: bool | None = None,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = extras.get(REPLY_POLICY_OVERRIDE_KEY)
    payload = dict(current) if isinstance(current, dict) else {}
    if force_send is not None:
        payload["force_send"] = bool(force_send)
    if mention_sender is not None:
        payload["mention_sender"] = bool(mention_sender)
    if reason:
        payload["reason"] = reason
    if metadata:
        payload["metadata"] = {
            **(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}),
            **metadata,
        }
    extras[REPLY_POLICY_OVERRIDE_KEY] = payload
    return payload


def get_reply_policy_override(extras: dict[str, Any]) -> dict[str, Any]:
    value = extras.get(REPLY_POLICY_OVERRIDE_KEY)
    return dict(value) if isinstance(value, dict) else {}

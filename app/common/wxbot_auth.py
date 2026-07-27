"""Authentication helpers for calls from Agent Console to the local wxbot SDK."""

from __future__ import annotations

from typing import Any


def wxbot_sdk_headers(settings: Any) -> dict[str, str]:
    token = str(getattr(settings, "wxbot_api_token", "") or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}

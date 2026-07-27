"""
HMAC signature + timestamp-window verification for inbound webhooks.
"""
from __future__ import annotations

import time

from app.common.config import Settings
from app.common.exceptions import SignatureError
from app.common.hashing import verify_hmac


def verify_signature(
    *,
    settings: Settings,
    tenant_id: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    now: float | None = None,
) -> None:
    """Raise SignatureError if signature or timestamp is invalid.

    - Looks up the tenant secret from settings.
    - Verifies |now - ts| <= settings.inbound_signature_window_seconds.
    - Verifies HMAC-SHA256(secret, body) == signature (constant-time).
    """
    if not signature or not timestamp:
        raise SignatureError("missing_signature_or_timestamp")

    try:
        ts = int(timestamp)
    except (TypeError, ValueError) as e:
        raise SignatureError("invalid_timestamp") from e

    current = now if now is not None else time.time()
    if abs(current - ts) > settings.inbound_signature_window_seconds:
        raise SignatureError("timestamp_out_of_window")

    secret = settings.get_tenant_secret(tenant_id)
    if not secret:
        raise SignatureError("unknown_tenant")

    if not verify_hmac(secret, body, signature):
        raise SignatureError("bad_signature")

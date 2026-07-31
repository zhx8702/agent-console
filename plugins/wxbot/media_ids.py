"""Signed, tenant-scoped media identifiers for wxbot admin APIs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlsplit


class InvalidMediaID(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MediaLocator:
    tenant_id: str
    kind: Literal["sdk_path", "remote_url"]
    value: str
    expires_at: int
    resource_type: Literal["image", "file"] = "image"


def issue_media_id(
    locator: str,
    settings: Any,
    *,
    tenant_id: str,
    resource_type: Literal["image", "file"] = "image",
    now: int | None = None,
    ttl_seconds: int = 7 * 24 * 60 * 60,
) -> str:
    tenant = _required(tenant_id, "tenant_id", max_length=64)
    if resource_type not in {"image", "file"}:
        raise InvalidMediaID("invalid media resource type")
    raw = str(locator or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        kind: Literal["sdk_path", "remote_url"] = "remote_url"
        value = _normalize_remote_url(raw)
    else:
        kind = "sdk_path"
        value = normalize_sdk_media_path(raw)
    issued_at = int(time.time() if now is None else now)
    payload = {
        "v": 1,
        "t": tenant,
        "k": kind,
        "r": resource_type,
        "l": value,
        "e": issued_at + max(60, int(ttl_seconds)),
    }
    encoded = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(_signing_key(settings), encoded.encode(), hashlib.sha256).digest()
    return f"mid1.{encoded}.{_b64url(signature)}"


def resolve_media_id(
    media_id: str,
    settings: Any,
    *,
    expected_tenant_id: str | None = None,
    now: int | None = None,
) -> MediaLocator:
    try:
        prefix, encoded, supplied = str(media_id or "").strip().split(".", 2)
    except ValueError as exc:
        raise InvalidMediaID("invalid media id") from exc
    if prefix != "mid1" or not encoded or not supplied:
        raise InvalidMediaID("invalid media id")
    expected = hmac.new(_signing_key(settings), encoded.encode(), hashlib.sha256).digest()
    try:
        supplied_signature = _unb64url(supplied)
    except ValueError as exc:
        raise InvalidMediaID("invalid media id signature") from exc
    if not hmac.compare_digest(supplied_signature, expected):
        raise InvalidMediaID("invalid media id signature")
    try:
        payload = json.loads(_unb64url(encoded))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidMediaID("invalid media id payload") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise InvalidMediaID("unsupported media id")
    tenant = _required(payload.get("t"), "tenant_id", max_length=64)
    expected_tenant = str(expected_tenant_id or "").strip()
    if expected_tenant and not hmac.compare_digest(tenant, expected_tenant):
        raise InvalidMediaID("media id tenant mismatch")
    try:
        expires_at = int(payload.get("e") or 0)
    except (TypeError, ValueError) as exc:
        raise InvalidMediaID("invalid media id expiry") from exc
    current = int(time.time() if now is None else now)
    if expires_at <= current:
        raise InvalidMediaID("media id expired")
    kind = str(payload.get("k") or "")
    value = str(payload.get("l") or "")
    resource_type = str(payload.get("r") or "image")
    if resource_type not in {"image", "file"}:
        raise InvalidMediaID("invalid media resource type")
    if kind == "sdk_path":
        normalized = normalize_sdk_media_path(value)
    elif kind == "remote_url":
        normalized = _normalize_remote_url(value)
    else:
        raise InvalidMediaID("invalid media locator kind")
    return MediaLocator(
        tenant_id=tenant,
        kind=kind,  # type: ignore[arg-type]
        value=normalized,
        expires_at=expires_at,
        resource_type=resource_type,  # type: ignore[arg-type]
    )


def normalize_sdk_media_path(value: str) -> str:
    decoded = str(value or "")
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if (
        not decoded
        or len(decoded) > 1024
        or "\x00" in decoded
        or "\\" in decoded
        or decoded.startswith("/")
    ):
        raise InvalidMediaID("invalid sdk media path")
    parts = decoded.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise InvalidMediaID("invalid sdk media path")
    if "://" in decoded or ":" in parts[0]:
        raise InvalidMediaID("invalid sdk media path")
    return "/".join(parts)


def _normalize_remote_url(value: str) -> str:
    if len(value) > 2048:
        raise InvalidMediaID("media URL is too long")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise InvalidMediaID("invalid media URL")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidMediaID("media URL credentials are not allowed")
    return parsed.geturl()


def _signing_key(settings: Any) -> bytes:
    app_env = str(getattr(settings, "app_env", "dev") or "dev").strip().lower()
    dedicated = str(getattr(settings, "media_id_signing_secret", "") or "").strip()
    if dedicated:
        return dedicated.encode()
    if app_env not in {"dev", "test"}:
        raise InvalidMediaID("dedicated media id signing key is not configured")
    for field in (
        "wxbot_api_token",
        "admin_bearer_token",
        "outbound_hmac_secret",
    ):
        value = str(getattr(settings, field, "") or "").strip()
        if value:
            return value.encode()
    raise InvalidMediaID("media id signing key is not configured")


def _required(value: object, field: str, *, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise InvalidMediaID(f"invalid {field}")
    return normalized


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError("invalid base64url") from exc

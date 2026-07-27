from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.wxbot.media_ids import (
    InvalidMediaID,
    issue_media_id,
    normalize_sdk_media_path,
    resolve_media_id,
)


def _settings():
    return SimpleNamespace(
        media_id_signing_secret="media-secret-for-tests",
        wxbot_api_token="",
        admin_bearer_token="",
        outbound_hmac_secret="",
    )


def test_media_id_round_trip_is_signed_tenant_scoped_and_expiring() -> None:
    token = issue_media_id(
        "images/hash-1/photo.png",
        _settings(),
        tenant_id="tenant-1",
        now=100,
        ttl_seconds=60,
    )
    locator = resolve_media_id(
        token,
        _settings(),
        expected_tenant_id="tenant-1",
        now=120,
    )
    assert locator.kind == "sdk_path"
    assert locator.value == "images/hash-1/photo.png"

    with pytest.raises(InvalidMediaID, match="tenant mismatch"):
        resolve_media_id(token, _settings(), expected_tenant_id="tenant-2", now=120)
    with pytest.raises(InvalidMediaID, match="expired"):
        resolve_media_id(token, _settings(), expected_tenant_id="tenant-1", now=160)


def test_media_id_rejects_tampering_credentials_and_path_traversal() -> None:
    token = issue_media_id(
        "https://cdn.example/image.png",
        _settings(),
        tenant_id="tenant-1",
        now=100,
    )
    with pytest.raises(InvalidMediaID, match="signature"):
        resolve_media_id(token[:-1] + ("A" if token[-1] != "A" else "B"), _settings(), now=120)
    with pytest.raises(InvalidMediaID, match="credentials"):
        issue_media_id(
            "https://user:pass@example.com/image.png",
            _settings(),
            tenant_id="tenant-1",
            now=100,
        )
    for value in ("../secret.png", "/etc/passwd", "images/%2e%2e/secret.png", "C:/x.png"):
        with pytest.raises(InvalidMediaID):
            normalize_sdk_media_path(value)


@pytest.mark.parametrize("app_env", ["prod", "production", "staging", "qa"])
def test_media_id_requires_a_dedicated_signing_secret_in_production_like_environments(
    app_env: str,
) -> None:
    settings = SimpleNamespace(
        app_env=app_env,
        media_id_signing_secret="",
        wxbot_api_token="production-wxbot-token",
        admin_bearer_token="production-admin-token",
        outbound_hmac_secret="production-outbound-secret",
    )

    with pytest.raises(InvalidMediaID, match="dedicated media id signing key"):
        issue_media_id("images/hash-1/photo.png", settings, tenant_id="tenant-1")


def test_media_id_keeps_the_development_only_legacy_key_fallback() -> None:
    settings = SimpleNamespace(
        app_env="dev",
        media_id_signing_secret="",
        wxbot_api_token="development-wxbot-token",
        admin_bearer_token="",
        outbound_hmac_secret="",
    )

    token = issue_media_id("images/hash-1/photo.png", settings, tenant_id="tenant-1")
    assert resolve_media_id(token, settings, expected_tenant_id="tenant-1").kind == "sdk_path"

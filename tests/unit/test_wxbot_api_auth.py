from __future__ import annotations

from wxbot_client.api.auth import (
    api_token_is_high_entropy,
    request_token_authorized,
    token_required_for_host,
)

STRONG_TOKEN = "sdk-test-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def test_wxbot_api_requires_high_entropy_token_on_every_interface() -> None:
    assert token_required_for_host("0.0.0.0", "") is True
    assert token_required_for_host("127.0.0.1", "") is True
    assert token_required_for_host("127.0.0.1", "a" * 64) is True
    assert token_required_for_host("0.0.0.0", STRONG_TOKEN) is False
    assert api_token_is_high_entropy(STRONG_TOKEN) is True


def test_wxbot_api_accepts_bearer_or_compatibility_header() -> None:
    assert request_token_authorized(
        STRONG_TOKEN,
        authorization=f"Bearer {STRONG_TOKEN}",
    )
    assert request_token_authorized(
        STRONG_TOKEN,
        header_token=STRONG_TOKEN,
    )
    assert not request_token_authorized(
        STRONG_TOKEN,
        authorization="Bearer wrong",
    )
    assert not request_token_authorized(STRONG_TOKEN)


def test_wxbot_api_never_authorizes_when_configured_token_is_unsafe() -> None:
    assert request_token_authorized("", authorization="") is False
    assert request_token_authorized("a" * 64, authorization=f"Bearer {'a' * 64}") is False

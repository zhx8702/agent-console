from __future__ import annotations

import json

import httpx
import pytest

from plugins.tibo_reset.client import (
    TiboResetClient,
    TiboResetClientError,
    notification_validation,
    parse_reset_payload,
)


def _entry(**overrides):
    item = {
        "id": "2077607697487188198",
        "text": (
            "Another reset for our Codex and ChatGPT Work users.\n\n"
            "Should have that sweet 100% weekly usage limit back in a few minutes."
        ),
        "createdAt": "2026-07-16T04:14:09Z",
        "sourceUrl": "https://x.com/thsottiaux/status/2077607697487188198",
        "confidence": 1,
        "evidence": "Should have that sweet 100% weekly usage limit back in a few minutes",
        "statedReason": "hit 9M active users",
        "resetType": "weekly_usage",
        "beneficiaries": "everyone",
    }
    item.update(overrides)
    return item


def test_parse_reset_payload_preserves_full_original_text_and_string_id() -> None:
    raw = _entry()
    entries = parse_reset_payload({"resets": [raw]})

    assert entries[0].tweet_id == "2077607697487188198"
    assert isinstance(entries[0].tweet_id, str)
    assert entries[0].text == raw["text"]
    assert entries[0].source_url == raw["sourceUrl"]
    assert notification_validation(entries[0]) == (True, "verified")


def test_notification_validation_rejects_current_reply_false_positive() -> None:
    entry = parse_reset_payload(
        {
            "resets": [
                _entry(
                    id="2075287108680601929",
                    text="@ClaudeDevs I smell fear",
                    sourceUrl="https://x.com/thsottiaux/status/2075287108680601929",
                    evidence="We've reset 5-hour and weekly rate limits for all users.",
                    resetType="rate_limit",
                )
            ]
        }
    )[0]

    assert notification_validation(entry) == (False, "product_not_mentioned")


def test_parse_reset_payload_fails_closed_on_schema_change() -> None:
    with pytest.raises(TiboResetClientError, match="createdAt"):
        parse_reset_payload({"resets": [_entry(createdAt="not-a-date")]})


@pytest.mark.asyncio
async def test_client_uses_etag_and_cached_entries_for_304() -> None:
    seen_headers: list[str] = []
    payload = {"resets": [_entry()]}

    def responder(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("if-none-match", ""))
        if len(seen_headers) == 1:
            return httpx.Response(
                200,
                content=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "ETag": 'W/"feed-v1"'},
            )
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        client = TiboResetClient("https://tibo-reset.test/api/resets", http_client=http)
        first = await client.fetch_resets()
        second = await client.fetch_resets()

    assert first == second
    assert seen_headers == ["", 'W/"feed-v1"']


@pytest.mark.asyncio
async def test_client_rejects_insecure_feed_without_leaking_url_or_body() -> None:
    secret_url = "http://127.0.0.1/feed?token=do-not-log"

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "text/plain"},
            content=b"private upstream response body",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
        client = TiboResetClient(secret_url, http_client=http)
        with pytest.raises(TiboResetClientError) as captured:
            await client.fetch_resets()

    message = str(captured.value)
    assert message == "failed to fetch tibo reset feed"
    assert "do-not-log" not in message
    assert "private upstream response body" not in message

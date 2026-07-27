from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from plugins.draw import avatar as avatar_module


@pytest.mark.asyncio
async def test_group_avatar_roster_uses_exact_private_sdk_origin_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_safe_trusted_service_request(
        client,
        method,
        base_url,
        path,
        *,
        headers,
        timeout_seconds,
        max_response_bytes,
        allowed_response_content_types,
        **kwargs,
    ) -> httpx.Response:
        _ = client, kwargs
        captured.update(
            method=method,
            base_url=base_url,
            path=path,
            headers=dict(headers),
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            content_types=allowed_response_content_types,
        )
        url = f"{base_url}{path}"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "members": [
                    {
                        "wxid": "wxid_alice",
                        "display_name": "Alice",
                        "avatar": {"cached": True},
                    }
                ]
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(
        avatar_module,
        "safe_trusted_service_request",
        fake_safe_trusted_service_request,
    )
    settings = SimpleNamespace(
        wxbot_sdk_url="http://127.0.0.1:5080",
        wxbot_api_token="sdk-secret",
    )

    result = await avatar_module.resolve_group_avatar_reference(
        settings,
        session_id="room@chatroom",
        query="Alice",
    )

    assert result is not None
    assert result.wxid == "wxid_alice"
    assert result.avatar_url == (
        "http://127.0.0.1:5080/ext/roster/avatars/wxid_alice"
    )
    assert captured["method"] == "GET"
    assert captured["base_url"] == "http://127.0.0.1:5080"
    assert captured["path"] == "/ext/roster/groups/room%40chatroom/members"
    assert captured["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer sdk-secret",
    }
    assert captured["timeout_seconds"] == 10.0
    assert captured["max_response_bytes"] == 2 * 1024 * 1024

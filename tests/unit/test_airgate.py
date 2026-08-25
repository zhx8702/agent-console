from __future__ import annotations

import pytest

from app.common.airgate import AirgateClient, AirgateError


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_airgate_video_submit_and_poll_resolves_nested_video_url() -> None:
    client = AirgateClient(
        base_url="https://airgate.example/v1",
        model="grok-imagine-video-1.5-preview",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=1,
    )
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request(method: str, url: str, *, json=None, response_kind="json"):
        _ = response_kind
        calls.append((method, url, json))
        if method == "POST":
            return _Response({"request_id": "request-1", "status": "pending"})
        return _Response(
            {
                "status": "completed",
                "video": {"url": "https://cdn.example/request-1.mp4"},
            }
        )

    client._request = fake_request  # type: ignore[method-assign]

    result = await client.generate_video(
        prompt="一只猫在海边奔跑",
        duration=6,
        resolution="720p",
    )

    assert result.request_id == "request-1"
    assert result.status == "completed"
    assert result.video_url == "https://cdn.example/request-1.mp4"
    assert calls[0] == (
        "POST",
        "https://airgate.example/v1/videos/generations",
        {
            "model": "grok-imagine-video-1.5-preview",
            "prompt": "一只猫在海边奔跑",
            "duration": 6,
            "resolution": "720p",
        },
    )
    assert calls[1][0:2] == (
        "GET",
        "https://airgate.example/v1/videos/request-1",
    )


@pytest.mark.asyncio
async def test_airgate_video_rejects_failed_poll() -> None:
    client = AirgateClient(
        base_url="https://airgate.example",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=1,
    )

    async def fake_request(method: str, url: str, *, json=None, response_kind="json"):
        _ = method, url, json, response_kind
        return _Response(
            {"request_id": "request-2", "status": "failed", "message": "quota exceeded"}
        )

    client._request = fake_request  # type: ignore[method-assign]

    with pytest.raises(AirgateError, match="quota exceeded"):
        await client.generate_video(prompt="test", duration=1, resolution="480p")

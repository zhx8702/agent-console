from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.video.store import VideoStore


class _FakeVideoClient:
    async def generate_video(self, **kwargs):
        _ = kwargs
        return SimpleNamespace(request_id="request-1", video_url="https://cdn.example/video.mp4")

    async def download_video(self, video_url, *, destination_dir, file_stem):
        _ = video_url
        destination = Path(destination_dir) / f"{file_stem}.mp4"
        destination.write_bytes(b"fake-video")
        return destination, "video/mp4"


@pytest.mark.asyncio
async def test_video_store_stages_result_in_shared_file_outbox(tmp_path: Path) -> None:
    storage_dir = tmp_path / "video-cache"
    outbound_dir = tmp_path / "wxbot-outbound"
    settings = SimpleNamespace(
        video_storage_dir=str(storage_dir),
        wxbot_outbound_file_dir=str(outbound_dir),
        wxbot_outbound_file_max_bytes=1024,
        video_api_url="https://airgate.example/v1",
        video_api_key="test-key",
        video_api_model="grok-imagine-video-1.5-preview",
        video_api_timeout_seconds=30,
        video_api_poll_interval_seconds=1,
        video_api_poll_timeout_seconds=30,
        video_api_key_header="Authorization",
        video_api_key_prefix="Bearer",
        video_api_extra_body="",
    )
    store = VideoStore(settings)
    store._get_client = lambda: _FakeVideoClient()  # type: ignore[method-assign]

    result = await store.generate_video("a cat walking", duration=5, resolution="480p")

    assert Path(result.local_path).parent == outbound_dir.resolve()
    assert Path(result.local_path).read_bytes() == b"fake-video"
    assert list(storage_dir.glob("*.mp4"))

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.wxbot.bridge import SdkBridge
from plugins.wxbot.plugin import WxbotAdminMediaEventProvider


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        _ = ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _Store:
    def __init__(self) -> None:
        self.saved_media_ready_events: list[dict[str, Any]] = []

    async def save_media_ready_event(self, **kwargs: Any) -> bool:
        self.saved_media_ready_events.append(kwargs)
        return True


class _Bus:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    async def publish(self, *, stream: str, payload: dict, partition_key: str) -> None:
        self.items.append(
            {
                "stream": stream,
                "payload": payload,
                "partition_key": partition_key,
            }
        )


def _bridge() -> tuple[SdkBridge, _Store]:
    store = _Store()
    return (
        SdkBridge(
            sdk_url="http://127.0.0.1:5080",
            tenant_id="demo",
            container=object(),
            settings=SimpleNamespace(
                bus_inbound_stream="inbound:events",
                wxbot_bridge_max_message_age_seconds=0,
            ),
            store=store,  # type: ignore[arg-type]
            redis=_Redis(),
        ),
        store,
    )


@pytest.mark.asyncio
async def test_unified_file_message_is_text_placeholder_and_ready_is_an_update() -> None:
    bridge, store = _bridge()
    bus = _Bus()
    md5 = "9e107d9d372bb6826bd81d3542a419d6"
    sha256 = "1d3c43633f2b30c61186f81bb9d635327d0485094d65619745c0bf44f42996ae"

    await bridge._handle_stream_event(
        {
            "id": 701,
            "event_id": "stream:701",
            "event_type": "message.received",
            "occurred_ts": 1777000701,
            "source": "wxbot-sdk",
            "session": {"id": "wx-private", "name": "私聊", "kind": "private"},
            "sender": {"id": "wxid_sender", "name": "用户A"},
            "message": {
                "id": "msg-file-701",
                "type": "file",
                "text": "",
                "file_name": "report.pdf",
                "file_ext": "pdf",
                "file_size": 123456,
                "file_md5": md5,
                "file_sha256": "",
                "file_url": "",
                "file_download_status": "pending",
                "file_failure_reason": "",
                "media_status": "pending",
            },
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    payload = bus.items[0]["payload"]
    assert payload["message"] == {
        "type": "text",
        "content": "[文件] report.pdf",
        "attachments": [],
    }
    assert payload["metadata"]["file_name"] == "report.pdf"
    assert payload["metadata"]["file_ext"] == "pdf"
    assert payload["metadata"]["file_size"] == 123456
    assert payload["metadata"]["file_md5"] == md5
    assert payload["metadata"]["file_sha256"] == ""
    assert payload["metadata"]["file_url"] == ""
    assert payload["metadata"]["file_download_status"] == "pending"
    assert payload["metadata"]["file_failure_reason"] == ""
    assert payload["metadata"]["media_status"] == "pending"
    assert payload["metadata"]["media"]["type"] == "file"
    assert "msg-file-701" in bridge._pending_media_messages

    await bridge._handle_stream_event(
        {
            "id": 702,
            "event_id": "stream:702",
            "event_type": "message.media.ready",
            "occurred_ts": 1777000702,
            "session": {"id": "wx-private", "name": "私聊", "kind": "private"},
            "sender": {"id": "wxid_sender", "name": "用户A"},
            "message": {
                "id": "msg-file-701",
                "type": "file",
                "text": "[文件] report.pdf",
                "file_name": "report.pdf",
                "file_ext": "pdf",
                "file_size": 123456,
                "file_md5": md5,
                "file_sha256": sha256,
                "file_download_status": "ready",
                "file_url": "/files/hash-701/report.pdf",
                "media_status": "ready",
            },
            "media": {
                "type": "file",
                "status": "ready",
                "file_name": "report.pdf",
                "file_size": 123456,
                "md5": md5,
                "sha256": sha256,
                "file_url": "/files/hash-701/report.pdf",
            },
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    assert "msg-file-701" not in bridge._pending_media_messages
    assert len(store.saved_media_ready_events) == 1
    saved = store.saved_media_ready_events[0]
    assert saved["message_id"] == "msg-file-701"
    assert saved["msg_type"] == "file"
    assert saved["media_type"] == "file"
    assert saved["media_url"] == "http://127.0.0.1:5080/files/hash-701/report.pdf"
    assert saved["payload"]["message"]["file_sha256"] == sha256
    assert saved["payload"]["media"]["sha256"] == sha256


@pytest.mark.asyncio
async def test_legacy_file_message_preserves_file_metadata_without_file_content_parsing() -> None:
    bridge, _ = _bridge()
    bus = _Bus()

    await bridge._publish_legacy_message(
        {
            "msg_svr_id": "legacy-file-1",
            "session_id": "wx-private",
            "session_name": "私聊",
            "sender_wxid": "wxid_sender",
            "sender_name": "用户A",
            "msg_type": "file",
            "msg_text": "",
            "file_name": "archive.zip",
            "file_ext": "zip",
            "file_size": 42,
            "file_md5": "legacy-md5",
            "file_sha256": "legacy-sha256",
            "file_url": "/files/hash-legacy/archive.zip",
            "file_download_status": "ready",
            "file_failure_reason": "",
            "media_status": "ready",
            "recv_ts": 1777000801,
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    payload = bus.items[0]["payload"]
    assert payload["message"]["type"] == "text"
    assert payload["message"]["content"] == "[文件] archive.zip"
    assert payload["metadata"]["file_name"] == "archive.zip"
    assert payload["metadata"]["file_ext"] == "zip"
    assert payload["metadata"]["file_size"] == 42
    assert payload["metadata"]["file_md5"] == "legacy-md5"
    assert payload["metadata"]["file_sha256"] == "legacy-sha256"
    assert payload["metadata"]["file_url"] == "http://127.0.0.1:5080/files/hash-legacy/archive.zip"
    assert payload["metadata"]["file_download_status"] == "ready"
    assert payload["metadata"]["file_failure_reason"] == ""
    assert payload["metadata"]["media_status"] == "ready"


def test_admin_file_media_event_projects_file_attachment_without_image_fields() -> None:
    store = SimpleNamespace(
        settings=SimpleNamespace(
            channel_connection_id="wechat-main",
            app_env="test",
            media_id_signing_secret="test-media-secret",
        )
    )
    provider = WxbotAdminMediaEventProvider(store)  # type: ignore[arg-type]

    item = provider._to_admin_media_event(
        {
            "connection_id": "wechat-main",
            "sdk_event_id": 801,
            "event_type": "message.media.ready",
            "stream_event_id": "stream:801",
            "message_id": "file-801",
            "session_id": "wx-private",
            "sender_wxid": "wxid_sender",
            "msg_type": "file",
            "media_type": "file",
            "media_url": "/files/hash-801/report.pdf",
            "payload": {
                "message": {
                    "type": "file",
                    "text": "[文件] report.pdf",
                    "file_name": "report.pdf",
                    "file_ext": "pdf",
                    "file_size": 123456,
                    "file_md5": "file-md5",
                    "file_sha256": "file-sha256",
                    "file_download_status": "ready",
                    "media_status": "ready",
                },
                "media": {
                    "type": "file",
                    "status": "ready",
                    "file_url": "/files/hash-801/report.pdf",
                },
            },
            "created_ts": 1777000801,
        },
        "demo",
    )

    payload = item["payload"]
    attachment = payload["message"]["attachments"][0]
    assert attachment == {
        "type": "file",
        "file_name": "report.pdf",
        "file_ext": "pdf",
        "file_size": 123456,
        "file_md5": "file-md5",
        "file_sha256": "file-sha256",
        "file_url": "/files/hash-801/report.pdf",
        "file_download_status": "ready",
        "file_failure_reason": "",
        "media_status": "ready",
    }
    assert "image_url" not in attachment
    assert "image_path" not in attachment
    assert "image_variants" not in attachment
    assert "image_observation" not in payload["metadata"]

    projected = provider.project_recent_message(item, "demo")
    projected_attachment = projected["payload"]["message"]["attachments"][0]
    assert str(projected_attachment["file_media_id"]).startswith("mid1.")
    assert "file_url" not in projected_attachment
    assert "image_url" not in projected_attachment

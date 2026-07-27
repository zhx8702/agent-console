from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID, canonical_conversation_id
from plugins.wxbot.bridge_runtime import read_bridge_runtime_status
from plugins.wxbot.media_ids import resolve_media_id
from plugins.wxbot.plugin import WxbotAdminMediaEventProvider
from plugins.wxbot.store import WxbotStore, normalize_wxbot_event_connection_id


def _member_event(connection_id: str) -> dict[str, Any]:
    return {
        "tenant_id": "tenant-a",
        "connection_id": connection_id,
        "sdk_event_id": 7,
        "event_type": "group.member.joined",
        "session_id": "room@chatroom",
        "session_name": "群",
        "entity_wxid": "wxid-member",
        "entity_name": "成员",
        "payload": {},
        "created_ts": 1,
    }


def _media_event(connection_id: str) -> dict[str, Any]:
    return {
        "tenant_id": "tenant-a",
        "connection_id": connection_id,
        "sdk_event_id": 7,
        "event_type": "message.media.ready",
        "stream_event_id": "stream-7",
        "message_id": "message-7",
        "session_id": "room@chatroom",
        "session_name": "群",
        "sender_wxid": "wxid-member",
        "sender_name": "成员",
        "msg_type": "image",
        "media_type": "image",
        "media_path": "image.png",
        "media_url": "",
        "payload": {},
        "created_ts": 1,
    }


@pytest.mark.asyncio
async def test_wxbot_event_saves_dedupe_per_connection_and_canonicalize_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member_keys: set[tuple[str, str, int]] = set()
    media_keys: set[tuple[str, str, int]] = set()

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values = params or {}
        if sql.startswith("INSERT INTO plugin_wxbot_member_events"):
            assert "ON CONFLICT (tenant_id, connection_id, sdk_event_id)" in sql
            keys = member_keys
        elif sql.startswith("INSERT INTO plugin_wxbot_media_ready_events"):
            assert "ON CONFLICT (tenant_id, connection_id, sdk_event_id)" in sql
            keys = media_keys
        else:
            return []
        key = (
            str(values["tid"]),
            str(values["connection_id"]),
            int(values["eid"]),
        )
        if key in keys:
            return []
        keys.add(key)
        return [{"id": len(keys)}]

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    assert await store.save_member_event(**_member_event("wechat-a")) is True
    assert await store.save_member_event(**_member_event("wechat-a")) is False
    assert await store.save_member_event(**_member_event("wechat-b")) is True
    assert await store.save_member_event(**_member_event("")) is True
    assert await store.save_member_event(**_member_event(LEGACY_WXBOT_CONNECTION_ID)) is False

    assert await store.save_media_ready_event(**_media_event("wechat-a")) is True
    assert await store.save_media_ready_event(**_media_event("wechat-a")) is False
    assert await store.save_media_ready_event(**_media_event("wechat-b")) is True
    assert await store.save_media_ready_event(**_media_event("")) is True
    assert await store.save_media_ready_event(**_media_event(LEGACY_WXBOT_CONNECTION_ID)) is False

    expected = {
        ("tenant-a", "wechat-a", 7),
        ("tenant-a", "wechat-b", 7),
        ("tenant-a", LEGACY_WXBOT_CONNECTION_ID, 7),
    }
    assert member_keys == expected
    assert media_keys == expected


@pytest.mark.asyncio
async def test_wxbot_event_lists_and_stats_are_strictly_connection_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values = dict(params or {})
        calls.append((sql, values))
        assert "WHERE tenant_id = :tid AND connection_id = :connection_id" in sql
        if "COUNT(*) AS n" in sql:
            return [{"event_type": "event.type", "n": 2}]
        return [
            {
                "connection_id": values["connection_id"],
                "payload_json": '{"safe": true}',
            }
        ]

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    member_rows = await store.list_member_events(
        "tenant-a",
        connection_id="wechat-a",
    )
    media_rows = await store.list_media_ready_events(
        "tenant-a",
        connection_id="",
    )
    assert await store.member_event_stats(
        "tenant-a",
        connection_id="wechat-b",
    ) == {"event.type": 2}
    assert await store.media_ready_stats("tenant-a") == {"event.type": 2}

    assert member_rows[0]["connection_id"] == "wechat-a"
    assert member_rows[0]["payload"] == {"safe": True}
    assert media_rows[0]["connection_id"] == LEGACY_WXBOT_CONNECTION_ID
    assert [params["connection_id"] for _, params in calls] == [
        "wechat-a",
        LEGACY_WXBOT_CONNECTION_ID,
        "wechat-b",
        LEGACY_WXBOT_CONNECTION_ID,
    ]


def test_wxbot_event_connection_scope_rejects_unstable_identifiers() -> None:
    assert normalize_wxbot_event_connection_id("") == LEGACY_WXBOT_CONNECTION_ID
    assert (
        normalize_wxbot_event_connection_id(LEGACY_WXBOT_CONNECTION_ID)
        == LEGACY_WXBOT_CONNECTION_ID
    )
    assert normalize_wxbot_event_connection_id(" wechat-a ") == "wechat-a"
    with pytest.raises(ValueError, match="stable identifier"):
        normalize_wxbot_event_connection_id("wechat a")
    with pytest.raises(ValueError, match="stable identifier"):
        normalize_wxbot_event_connection_id("a" * 65)


@pytest.mark.asyncio
async def test_admin_media_provider_uses_managed_connection_and_signs_locators() -> None:
    settings = SimpleNamespace(
        channel_connection_id="wechat-main",
        app_env="test",
        media_id_signing_secret="unit-media-secret",
    )

    class Store:
        def __init__(self) -> None:
            self.settings = settings
            self.calls: list[dict[str, Any]] = []

        async def list_media_ready_events(
            self,
            tenant_id: str,
            limit: int,
            *,
            connection_id: str,
        ) -> list[dict[str, Any]]:
            self.calls.append(
                {
                    "tenant_id": tenant_id,
                    "limit": limit,
                    "connection_id": connection_id,
                }
            )
            return [
                {
                    "connection_id": "wechat-main",
                    "sdk_event_id": "ready-7",
                    "event_type": "message.media.ready",
                    "stream_event_id": "stream-7",
                    "message_id": "message-7",
                    "session_id": "room@chatroom",
                    "session_name": "测试群",
                    "sender_wxid": "wxid-member",
                    "sender_name": "成员",
                    "msg_type": "image",
                    "media_type": "image",
                    "media_path": "images/full.png",
                    "media_url": "",
                    "payload": {
                        "message": {"text": "[图片]"},
                        "media": {
                            "status": "ready",
                            "image_preview_path": "/images/preview.png",
                        },
                    },
                    "created_ts": 1,
                }
            ]

    store = Store()
    provider = WxbotAdminMediaEventProvider(store)  # type: ignore[arg-type]
    session_id = canonical_conversation_id("wechat-main", "room@chatroom")
    rows = await provider.list_recent_media_events(
        tenant_id="tenant-a",
        session_id=session_id,
        limit=10,
    )

    assert store.calls == [
        {
            "tenant_id": "tenant-a",
            "limit": 10,
            "connection_id": "wechat-main",
        }
    ]
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["connection_id"] == "wechat-main"
    assert payload["external_message_id"] == "message-7"
    assert payload["session_id"] == session_id
    attachment = payload["message"]["attachments"][0]
    assert str(attachment["media_id"]).startswith("mid1.")
    assert resolve_media_id(
        str(attachment["media_id"]),
        settings,
        expected_tenant_id="tenant-a",
    ).value == "full.png"
    assert str(payload["metadata"]["media"]["preview_media_id"]).startswith("mid1.")
    serialized = str(rows[0])
    assert "images/full.png" not in serialized
    assert "/images/preview.png" not in serialized


class _Redis:
    def __init__(self) -> None:
        self.read_keys: list[str] = []

    async def get(self, key: str) -> None:
        self.read_keys.append(key)
        return None

    async def ttl(self, key: str) -> int:
        self.read_keys.append(key)
        return -2


class _RuntimeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def member_event_stats(
        self,
        tenant_id: str,
        *,
        connection_id: str = "",
    ) -> dict[str, int]:
        self.calls.append(("member", tenant_id, connection_id))
        return {"group.member.joined": 1}

    async def media_ready_stats(
        self,
        tenant_id: str,
        *,
        connection_id: str = "",
    ) -> dict[str, int]:
        self.calls.append(("media", tenant_id, connection_id))
        return {"message.media.ready": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection_id", "expected"),
    [
        ("", LEGACY_WXBOT_CONNECTION_ID),
        (LEGACY_WXBOT_CONNECTION_ID, LEGACY_WXBOT_CONNECTION_ID),
        ("wechat-a", "wechat-a"),
    ],
)
async def test_bridge_runtime_status_reads_only_the_selected_connection(
    connection_id: str,
    expected: str,
) -> None:
    store = _RuntimeStore()
    status = await read_bridge_runtime_status(
        _Redis(),
        store,  # type: ignore[arg-type]
        SimpleNamespace(wxbot_sdk_url="http://127.0.0.1:5080"),
        "tenant-a",
        connection_id=connection_id,
    )

    assert status["connection_id"] == expected
    assert store.calls == [
        ("member", "tenant-a", expected),
        ("media", "tenant-a", expected),
    ]

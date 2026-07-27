from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

pytest.importorskip("flask")

from wxbot_client import queue_store
from wxbot_client.api.server import create_app
from wxbot_client.queue_migrations import migrate

SDK_API_TOKEN = "sdk-test-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@pytest.fixture
def sdk_api(tmp_path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))

    runtime = SimpleNamespace(is_active=lambda: False, _lock=None, _guard=None)
    sealed_core = ModuleType("sealed_core")
    sealed_core.runtime = runtime
    identity = {
        "ready": True,
        "self_wxid": "wxid_bot",
        "self_rowid": 7,
        "reason": "",
        "checked_at": 1,
    }
    ingest_loader = ModuleType("sealed_core.ingest_loader")
    ingest_loader.resolve_self_identity = lambda: dict(identity)
    ingest_loader.identity_status = lambda refresh=False: dict(identity)
    ingest_loader.build_session_mapping = lambda: {}
    sealed_core.ingest_loader = ingest_loader
    monkeypatch.setitem(sys.modules, "sealed_core", sealed_core)
    monkeypatch.setitem(sys.modules, "sealed_core.ingest_loader", ingest_loader)
    monkeypatch.setitem(sys.modules, "queue_store", queue_store)

    state = {
        "group_require_at_me": False,
        "writes": 0,
        "identity": identity,
    }
    config = ModuleType("config")
    config.DECRYPTED_DIR = str(tmp_path / "images" / "decrypted")
    config.API_TOKEN = SDK_API_TOKEN
    config.API_HOST = "127.0.0.1"

    def trigger_debug_summary():
        return {
            "group_require_at_me": state["group_require_at_me"],
            "group_capture_mode": (
                "mention_or_command" if state["group_require_at_me"] else "all_group_messages"
            ),
        }

    def set_group_require_at_me(enabled: bool):
        state["group_require_at_me"] = bool(enabled)
        state["writes"] += 1
        return trigger_debug_summary()

    config.summary = lambda: {"group_require_at_me": state["group_require_at_me"]}
    config.trigger_debug_summary = trigger_debug_summary
    config.set_group_require_at_me = set_group_require_at_me
    monkeypatch.setitem(sys.modules, "config", config)

    client = create_app().test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {SDK_API_TOKEN}"
    return client, state


def test_api_requires_authentication_even_on_loopback(sdk_api) -> None:
    client, _state = sdk_api

    authenticated = client.get("/status")
    authorization = client.environ_base.pop("HTTP_AUTHORIZATION")
    try:
        unauthenticated = client.get("/status")
    finally:
        client.environ_base["HTTP_AUTHORIZATION"] = authorization

    assert authenticated.status_code == 200
    assert unauthenticated.status_code == 401
    assert unauthenticated.get_json() == {"error": "wxbot_api_unauthorized"}


def test_send_requires_key_and_rejects_nested_unknown_fields_without_side_effects(
    sdk_api,
) -> None:
    client, _state = sdk_api
    payload = {
        "target": {"session_id": "room@chatroom", "typo": "bad"},
        "content": {"text": "你好"},
    }

    missing_key = client.post(
        "/send",
        json={"session_id": "room@chatroom", "text": "你好"},
    )
    unknown = client.post(
        "/send/envelope",
        headers={"Idempotency-Key": "send-command-1"},
        json=payload,
    )

    assert missing_key.status_code == 428
    assert unknown.status_code == 400
    assert unknown.get_json()["error"] == "unknown_target_fields:typo"
    assert queue_store.list_outbound() == []


def test_queue_message_get_resolves_exact_row(sdk_api) -> None:
    client, _state = sdk_api
    row_id = queue_store.push_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="待核对",
        command_id="sdk-exact-row",
    )

    found = client.get(f"/queue/messages/{row_id}")
    missing = client.get(f"/queue/messages/{row_id + 1}")

    assert found.status_code == 200
    assert found.get_json()["session_id"] == "room@chatroom"
    assert missing.status_code == 404


def test_send_batch_is_atomic_and_uses_distinct_derived_command_ids(sdk_api) -> None:
    client, _state = sdk_api
    invalid = client.post(
        "/send/batch",
        headers={"Idempotency-Key": "batch-command-1"},
        json={
            "messages": [
                {"session_id": "room@chatroom", "text": "第一条"},
                {"session_id": "room@chatroom", "unexpected": True},
            ]
        },
    )
    assert invalid.status_code == 400
    assert queue_store.list_outbound() == []

    payload = {
        "messages": [
            {"session_id": "room@chatroom", "text": "第一条"},
            {"session_id": "room@chatroom", "text": "第二条"},
        ]
    }
    first = client.post(
        "/send/batch",
        headers={"Idempotency-Key": "batch-command-1"},
        json=payload,
    )
    replay = client.post(
        "/send/batch",
        headers={"Idempotency-Key": "batch-command-1"},
        json=payload,
    )

    assert first.status_code == replay.status_code == 200
    first_ids = [item["id"] for item in first.get_json()["results"]]
    replay_ids = [item["id"] for item in replay.get_json()["results"]]
    assert first_ids == replay_ids
    assert len(set(first_ids)) == 2
    assert len(queue_store.list_outbound()) == 2


def test_queue_clear_and_debug_config_use_durable_idempotency(sdk_api) -> None:
    client, state = sdk_api
    queue_store.push_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="待清理",
        command_id="clear-target",
    )

    clear_payload = {"status": "pending", "session_id": "room@chatroom"}
    first = client.post(
        "/queue/clear",
        headers={"Idempotency-Key": "clear-command-1"},
        json=clear_payload,
    )
    replay = client.post(
        "/queue/clear",
        headers={"Idempotency-Key": "clear-command-1"},
        json=clear_payload,
    )
    conflict = client.post(
        "/queue/clear",
        headers={"Idempotency-Key": "clear-command-1"},
        json={"status": "failed", "session_id": "room@chatroom"},
    )

    assert first.status_code == replay.status_code == 200
    assert first.get_json() == replay.get_json()
    assert first.headers["Idempotency-Replayed"] == "false"
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert conflict.status_code == 409

    debug_payload = {"group_require_at_me": True}
    updated = client.post(
        "/debug/trigger-config",
        headers={"Idempotency-Key": "trigger-command-1"},
        json=debug_payload,
    )
    debug_replay = client.post(
        "/debug/trigger-config",
        headers={"Idempotency-Key": "trigger-command-1"},
        json=debug_payload,
    )
    debug_conflict = client.post(
        "/debug/trigger-config",
        headers={"Idempotency-Key": "trigger-command-1"},
        json={"group_require_at_me": False},
    )

    assert updated.status_code == debug_replay.status_code == 200
    assert updated.get_json() == debug_replay.get_json()
    assert state["writes"] == 1
    assert debug_conflict.status_code == 409


def test_status_fails_when_self_identity_cannot_be_resolved(sdk_api) -> None:
    client, state = sdk_api

    healthy = client.get("/status")
    state["identity"].update(
        {
            "ready": False,
            "self_wxid": "",
            "self_rowid": None,
            "reason": "self_rowid_missing",
        }
    )
    unhealthy = client.get("/status")

    assert healthy.status_code == 200
    assert healthy.get_json()["identity"]["self_rowid"] == 7
    assert unhealthy.status_code == 503
    assert unhealthy.get_json()["status"] == "unhealthy"
    assert unhealthy.get_json()["identity"]["reason"] == "self_rowid_missing"


def test_send_api_fails_closed_when_self_identity_is_unavailable(sdk_api) -> None:
    client, state = sdk_api
    state["identity"].update(
        {
            "ready": False,
            "self_wxid": "",
            "self_rowid": None,
            "reason": "self_rowid_missing",
        }
    )

    response = client.post(
        "/send",
        json={
            "session_id": "room@chatroom",
            "text": "不应进入本地发送队列",
            "command_id": "identity-closed-1",
        },
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "self_identity_unavailable"
    assert queue_store.list_outbound() == []


def test_stream_envelope_carries_identity_and_reclassifies_self_sender(sdk_api) -> None:
    client, _state = sdk_api
    queue_store.push_inbound(
        msg_svr_id="self-message-1",
        session_id="room@chatroom",
        session_name="测试群",
        sender_wxid="wxid_bot",
        sender_name="机器人",
        msg_text="自产消息",
        recv_ts=1,
        metadata={"is_self_sent": False, "capture_allowed": True},
    )

    response = client.get("/stream?cursor=0", buffered=False)
    iterator = iter(response.response)
    _id_line = next(iterator)
    _event_line = next(iterator)
    data_line = next(iterator).decode("utf-8")
    response.close()
    envelope = json.loads(data_line.removeprefix("data: ").strip())

    assert envelope["identity"] == {
        "ready": True,
        "self_wxid": "wxid_bot",
        "self_rowid": 7,
    }
    assert envelope["message"]["identity_resolved"] is True
    assert envelope["message"]["is_self_sent"] is True

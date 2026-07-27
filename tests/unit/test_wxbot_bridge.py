from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugins.wxbot.bridge as bridge_module
from app.channel.reply_policy import match_reply_policy
from app.common.types import Channel, InboundEvent, Message, MessageType
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
)
from app.social.speech_ledger import GroupSpeechBudgetExceeded
from plugins.wxbot.bridge import (
    SELF_HEAL_COOLDOWN_SECONDS,
    SELF_HEAL_RECURRENCE_THRESHOLD,
    SdkBridge,
    _partition_key,
    read_bridge_runtime_status,
)
from plugins.wxbot.store import WxbotStore


class _FakeClient:
    def __init__(self, *, statuses: list[int] | None = None) -> None:
        self.calls: list[dict] = []
        self._statuses = list(statuses or [200])

    async def post(self, url: str, json: dict):
        self.calls.append({"url": url, "json": json})
        status_code = self._statuses.pop(0) if self._statuses else 200

        class _Resp:
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code

        return _Resp(status_code)


def test_reply_policy_uses_word_boundaries_and_safe_cjk_keywords() -> None:
    assert match_reply_policy("contains", "ask AI please", ["ai"], is_group=True)[0]
    assert not match_reply_policy("contains", "said hello", ["ai"], is_group=True)[0]
    assert not match_reply_policy("contains", "今天吃饭", ["吃"], is_group=True)[0]
    assert match_reply_policy("contains", "吃", ["吃"], is_group=True)[0]
    assert match_reply_policy("contains", "普通消息", ["机器人"], mentioned_me=True, is_group=True)[
        0
    ]


class _FakeGetClient:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.calls: list[dict] = []
        self._payload = payload
        self._status_code = status_code

    async def get(self, url: str, params: dict | None = None):
        self.calls.append({"url": url, "params": params or {}})

        class _Resp:
            def __init__(self, payload: dict, status_code: int) -> None:
                self._payload = payload
                self.status_code = status_code

            def json(self) -> dict:
                return self._payload

        return _Resp(self._payload, self._status_code)


@pytest.fixture(autouse=True)
def _adapt_bridge_http_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_safe_trusted_service_request(
        client,
        method: str,
        base_url: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        **kwargs,
    ):
        _ = kwargs
        url = f"{base_url.rstrip('/')}{path}"
        if method == "POST":
            return await client.post(url, json=json or {})
        return await client.get(url, params=params)

    monkeypatch.setattr(
        bridge_module,
        "safe_trusted_service_request",
        fake_safe_trusted_service_request,
    )


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def ttl(self, key: str) -> int:
        return 25 if key in self.values else -2


class _FakeStore:
    def __init__(self) -> None:
        self.sent_ids: list[int] = []
        self.failed_ids: list[tuple[int, str]] = []
        self.updated_commands: list[dict[str, object]] = []
        self.saved_member_events: list[dict[str, object]] = []
        self.seen_group_members: list[dict[str, object]] = []
        self.saved_media_ready_events: list[dict[str, object]] = []
        self.saved_group_observations: list[dict[str, object]] = []

    async def mark_reply_sent(self, reply_id: int, **kwargs) -> bool:
        _ = kwargs
        self.sent_ids.append(reply_id)
        return True

    async def mark_reply_queued(self, reply_id: int, **kwargs) -> bool:
        _ = kwargs
        self.sent_ids.append(reply_id)
        return True

    async def update_reply_command(self, reply_id: int, **kwargs) -> bool:
        self.updated_commands.append({"reply_id": reply_id, **kwargs})
        return True

    async def mark_reply_failed(self, reply_id: int, error: str, **kwargs) -> str:
        _ = kwargs
        self.failed_ids.append((reply_id, error))
        return "failed"

    async def save_member_event(self, **kwargs) -> bool:
        self.saved_member_events.append(kwargs)
        return True

    async def save_media_ready_event(self, **kwargs) -> bool:
        self.saved_media_ready_events.append(kwargs)
        return True

    async def record_group_observation(self, **kwargs) -> bool:
        self.saved_group_observations.append(kwargs)
        return True

    async def record_group_member_seen(self, **kwargs) -> None:
        self.seen_group_members.append(kwargs)

    async def media_ready_stats(self, tenant_id: str, *, connection_id: str = "") -> dict[str, int]:
        _ = tenant_id, connection_id
        return {"message.media.ready": len(self.saved_media_ready_events)}

    async def member_event_stats(
        self, tenant_id: str, *, connection_id: str = ""
    ) -> dict[str, int]:
        _ = tenant_id, connection_id
        return {"group.member.joined": len(self.saved_member_events)}


class _FakeBus:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    async def publish(self, *, stream: str, payload: dict, partition_key: str) -> None:
        self.items.append(
            {
                "stream": stream,
                "payload": payload,
                "partition_key": partition_key,
            }
        )


@pytest.mark.asyncio
async def test_wxbot_store_claim_interactive_reply_is_atomic_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": params or {}})
        if sql.startswith("UPDATE plugin_wxbot_interaction_cursor"):
            return [{"latest_message_id": "msg-2"}]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    claimed = await store.claim_interactive_reply(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="msg-2",
        cooldown_seconds=1.5,
    )

    assert claimed is True
    assert len(calls) == 2
    update_sql = str(calls[1]["sql"])
    assert "latest_message_id = :message_id" in update_sql
    assert "last_replied_message_id <> :message_id" in update_sql
    assert "RETURNING latest_message_id" in update_sql
    assert calls[1]["params"] == {
        "tenant_id": "demo",
        "session_id": "room@chatroom",
        "message_id": "msg-2",
        "cooldown": 1.5,
    }


@pytest.mark.asyncio
async def test_wxbot_store_claim_interactive_reply_rejects_stale_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = params
        if sql.startswith("UPDATE plugin_wxbot_interaction_cursor"):
            return []
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    claimed = await store.claim_interactive_reply(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="msg-old",
    )

    assert claimed is False


@pytest.mark.asyncio
async def test_wxbot_store_adaptive_cooldown_uses_burst_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": params or {}})
        if sql.startswith("UPDATE plugin_wxbot_interaction_cursor"):
            return [{"latest_message_id": "msg-3"}]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())
    assert await store.claim_interactive_reply(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="msg-3",
        cooldown_seconds=1.0,
        adaptive_cooldown=True,
        adaptive_max_seconds=6.0,
    )
    assert "burst_count" in str(calls[1]["sql"])
    assert calls[1]["params"] == {
        "tenant_id": "demo",
        "session_id": "room@chatroom",
        "message_id": "msg-3",
        "cooldown": 1.0,
        "adaptive_max": 6.0,
    }


@pytest.mark.asyncio
async def test_wxbot_member_events_update_membership_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": params or {}})
        if sql.startswith("INSERT INTO plugin_wxbot_member_events"):
            return [{"id": 1}]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())
    saved = await store.save_member_event(
        tenant_id="demo",
        sdk_event_id=42,
        event_type="group.member.left",
        session_id="room@chatroom",
        session_name="Room",
        entity_wxid="wxid_a",
        entity_name="Alice",
        payload={},
        created_ts=1,
    )
    assert saved is True
    membership_call = next(
        call
        for call in calls
        if str(call["sql"]).startswith("INSERT INTO plugin_wxbot_group_membership")
    )
    assert membership_call["params"]["active"] is False  # type: ignore[index]


@pytest.mark.asyncio
async def test_bridge_group_observation_updates_membership_projection() -> None:
    bridge, store = _build_bridge()
    event = InboundEvent(
        message_id="msg-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid-a",
        session_id="room@chatroom",
        message=Message(
            type=MessageType.TEXT,
            content="hello",
        ),
        trace_id="trace-1",
        metadata={"sender_name": "Alice"},
    )

    await bridge._record_group_observation(event)

    assert store.seen_group_members == [
        {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "user_wxid": "wxid-a",
            "user_name": "Alice",
        }
    ]


@pytest.mark.asyncio
async def test_wxbot_new_tenant_does_not_mention_group_sender_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = sql, params
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    policy = await WxbotStore(SimpleNamespace()).get_global_policy("new-tenant")

    assert policy["group_reply_mention_sender"] is False


def _build_bridge(
    store: _FakeStore | None = None,
    *,
    max_message_age_seconds: int = 0,
    tenant_id: str = "demo",
    connection_id: str = "",
    redis: _FakeRedis | None = None,
    social_policy_store: object | None = None,
    owners_scope_execution_allowed: object | None = None,
    connection_activity_recorder: object | None = None,
) -> tuple[SdkBridge, _FakeStore]:
    fake_store = store or _FakeStore()
    bridge = SdkBridge(
        sdk_url="http://127.0.0.1:5080",
        tenant_id=tenant_id,
        container=object(),
        settings=SimpleNamespace(
            bus_inbound_stream="inbound:events",
            wxbot_bridge_max_message_age_seconds=max_message_age_seconds,
        ),
        store=fake_store,
        redis=redis or _FakeRedis(),
        social_policy_store=social_policy_store,  # type: ignore[arg-type]
        connection_id=connection_id,
        owners_scope_execution_allowed=owners_scope_execution_allowed,  # type: ignore[arg-type]
        connection_activity_recorder=connection_activity_recorder,  # type: ignore[arg-type]
    )
    return bridge, fake_store


@pytest.mark.asyncio
async def test_bridge_cursor_and_partition_keys_are_tenant_scoped() -> None:
    redis = _FakeRedis()
    tenant_a, _ = _build_bridge(tenant_id="tenant-a", redis=redis)
    tenant_b, _ = _build_bridge(tenant_id="tenant-b", redis=redis)

    await tenant_a._set_cursor(11)
    await tenant_a._set_legacy_cursor(12)
    await tenant_a._set_event_cursor(13)
    await tenant_b._set_cursor(21)
    await tenant_b._set_legacy_cursor(22)
    await tenant_b._set_event_cursor(23)

    assert await tenant_a._get_cursor() == 11
    assert await tenant_b._get_cursor() == 21
    assert redis.values[tenant_a.legacy_cursor_key] == "12"
    assert redis.values[tenant_b.legacy_cursor_key] == "22"
    assert redis.values[tenant_a.event_cursor_key] == "13"
    assert redis.values[tenant_b.event_cursor_key] == "23"
    assert _partition_key("tenant-a", "same-session") != _partition_key(
        "tenant-b",
        "same-session",
    )


@pytest.mark.asyncio
async def test_bridge_runtime_keys_are_scoped_by_connection() -> None:
    redis = _FakeRedis()
    primary, _ = _build_bridge(
        tenant_id="tenant-a",
        connection_id="wechat-primary",
        redis=redis,
    )
    backup, _ = _build_bridge(
        tenant_id="tenant-a",
        connection_id="wechat-backup",
        redis=redis,
    )

    await primary._set_cursor(11)
    await backup._set_cursor(21)

    assert primary.cursor_key != backup.cursor_key
    assert primary.leader_key != backup.leader_key
    assert primary._inbound_dedupe_key("same-message") != backup._inbound_dedupe_key("same-message")
    assert _partition_key("tenant-a", "same-session", "wechat-primary") != _partition_key(
        "tenant-a", "same-session", "wechat-backup"
    )
    assert await primary._get_cursor() == 11
    assert await backup._get_cursor() == 21


def test_legacy_and_managed_bridge_namespaces_cannot_collide() -> None:
    legacy, _ = _build_bridge(tenant_id="tenant:connection")
    managed, _ = _build_bridge(
        tenant_id="tenant",
        connection_id="connection",
    )

    assert legacy.cursor_key != managed.cursor_key
    assert legacy.status_key != managed.status_key
    assert _partition_key("tenant:connection", "same:session") != _partition_key(
        "tenant",
        "same:session",
        "connection",
    )


@pytest.mark.asyncio
async def test_bridge_advances_cursor_for_every_group_message() -> None:
    class InteractionStore(_FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.interactions: list[str] = []

        async def get_session_policy(self, tenant_id: str, session_id: str) -> dict:
            _ = tenant_id, session_id
            return {"effective_mode": "contains", "trigger_keywords": ["ai"]}

        async def record_interactive_inbound(self, **kwargs) -> None:
            self.interactions.append(str(kwargs["message_id"]))

    store = InteractionStore()
    bridge, _ = _build_bridge(store)
    await bridge._record_interactive_inbound(
        session_id="room@chatroom",
        message_id="not-a-match",
        content="said hello",
        mentioned_me=False,
        is_self_sent=False,
    )
    await bridge._record_interactive_inbound(
        session_id="room@chatroom",
        message_id="match",
        content="ask AI please",
        mentioned_me=False,
        is_self_sent=False,
    )
    await bridge._record_interactive_inbound(
        session_id="room@chatroom",
        message_id="self-message",
        content="bot output",
        mentioned_me=False,
        is_self_sent=True,
    )

    # Even a non-triggering message can make an older delayed soft reply
    # stale. Self-sent messages must not supersede member conversation.
    assert store.interactions == ["not-a-match", "match"]


@pytest.mark.asyncio
async def test_wxbot_store_claim_pending_reply_leases_one_recoverable_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": params or {}})
        if "WITH picked AS" not in sql:
            return []
        return [
            {
                "id": 12,
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "sender_name": "群友A",
                "sender_wxid": "wxid_user_a",
                "mention_sender": False,
                "reply_to_msg_svr_id": "svr-12",
                "session_kind": "group",
                "reply_text": "尚未得知 海总的烤鱼有多好吃",
                "msg_type": "text",
                "image_path": "",
                "image_url": "",
                "source_message_json": "{}",
                "delivery_json": "{}",
                "command_id": "wxbot-reply:demo:m-12:0",
                "trace_id": "trace-12",
                "participation_status": "may_reply",
                "source_message_id": "m-12",
                "not_before": None,
                "expires_at": None,
                "attempt_count": 1,
                "claim_owner": "bridge-a",
                "claim_token": str((params or {})["claim_token"]),
                "claim_until": None,
                "created_at": None,
            }
        ]

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    row = await WxbotStore(SimpleNamespace()).claim_pending_reply(
        "demo",
        connection_id="wechat-primary",
        claim_owner="bridge-a",
        lease_seconds=45,
        max_attempts=3,
    )

    assert row is not None
    assert row["id"] == 12
    assert row["reply_text"] == "尚未得知 海总的烤鱼有多好吃"
    assert row["attempt_count"] == 1
    assert len(row["claim_token"]) == 32
    assert len(calls) == 3
    cancel_sql = str(calls[0]["sql"])
    assert "SET status = 'cancelled'" in cancel_sql
    assert "reply_expired" in cancel_sql
    # Semantic stale/answer checks happen after a fenced lease; the claim
    # query only performs deterministic expiry cleanup.
    assert "participation_cursor_unavailable" not in cancel_sql
    assert "superseded_by_newer_message" not in cancel_sql
    assert "COALESCE(attempt_count, 0) >= :max_attempts" in str(calls[1]["sql"])
    sql = str(calls[2]["sql"])
    assert "SET status = 'sending'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "pending.status = 'sending'" in sql
    assert "pending.claim_until <= NOW()" in sql
    assert "attempt_count = COALESCE(q.attempt_count, 0) + 1" in sql
    assert "LIMIT 1" in sql
    assert "pending.not_before <= NOW()" in sql
    assert "pending.expires_at > NOW()" in sql
    assert "pending.participation_status <> 'may_reply'" not in sql
    assert "cursor_row.latest_message_id = pending.source_message_id" not in sql
    assert "q.connection_id = :connection_id" in cancel_sql
    assert "connection_id = :connection_id" in str(calls[1]["sql"])
    assert "pending.connection_id = :connection_id" in sql
    assert calls[0]["params"] == {
        "tid": "demo",
        "connection_id": "wechat-primary",
    }
    assert calls[1]["params"] == {
        "tid": "demo",
        "max_attempts": 3,
        "connection_id": "wechat-primary",
    }
    assert calls[2]["params"]["tid"] == "demo"
    assert calls[2]["params"]["claim_owner"] == "bridge-a"
    assert calls[2]["params"]["lease_seconds"] == 45.0
    assert calls[2]["params"]["max_attempts"] == 3
    assert calls[2]["params"]["connection_id"] == "wechat-primary"


@pytest.mark.asyncio
async def test_wxbot_reply_completion_fences_expired_worker_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "tenant_id": "demo",
        "id": 12,
        "status": "sending",
        "claim_token": "new-token",
    }
    statements: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        statements.append(sql)
        values = params or {}
        if (
            values.get("tenant_id") != state["tenant_id"]
            or values.get("id") != state["id"]
            or values.get("claim_token") != state["claim_token"]
            or state["status"] != "sending"
        ):
            return []
        if "SET status = 'queued'" in sql:
            state["status"] = "queued"
            state["claim_token"] = ""
            return [{"id": state["id"]}]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    assert not await store.mark_reply_queued(
        12,
        tenant_id="demo",
        claim_token="old-token",
    )
    assert (
        await store.mark_reply_failed(
            12,
            "late timeout",
            tenant_id="demo",
            claim_token="old-token",
        )
        == "stale_claim"
    )
    assert await store.mark_reply_queued(
        12,
        tenant_id="demo",
        claim_token="new-token",
    )
    assert state["status"] == "queued"
    assert all("claim_token = :claim_token" in sql for sql in statements)
    assert all("claim_until > NOW()" in sql for sql in statements)


@pytest.mark.asyncio
async def test_wxbot_reply_last_attempt_becomes_terminal_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": params or {}})
        return [{"status": "failed"}]

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    result = await WxbotStore(SimpleNamespace()).mark_reply_failed(
        12,
        "third failure",
        tenant_id="demo",
        claim_token="claim-3",
        max_attempts=3,
    )

    assert result == "failed"
    assert "THEN 'failed' ELSE 'pending'" in str(calls[0]["sql"])
    assert calls[0]["params"]["max_attempts"] == 3


@pytest.mark.asyncio
async def test_bridge_claims_each_reply_only_when_ready_to_send() -> None:
    order: list[str] = []

    class ClaimStore(_FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.pending = [
                {"id": 1, "claim_token": "token-1"},
                {"id": 2, "claim_token": "token-2"},
            ]

        async def claim_pending_reply(self, tenant_id: str, **kwargs):
            assert tenant_id == "demo"
            assert kwargs["claim_owner"]
            assert kwargs["connection_id"] == "wechat-primary"
            order.append("claim")
            return self.pending.pop(0) if self.pending else None

    store = ClaimStore()
    bridge, _ = _build_bridge(store, connection_id="wechat-primary")

    async def ready_bus():
        return object()

    async def send(reply: dict) -> None:
        order.append(f"send-{reply['id']}")
        if reply["id"] == 2:
            bridge._stop.set()

    bridge._wait_for_bus = ready_bus  # type: ignore[method-assign]
    bridge._send_one_reply = send  # type: ignore[method-assign]

    await bridge._send_loop()

    assert order == ["claim", "send-1", "claim", "send-2"]


@pytest.mark.asyncio
async def test_wxbot_store_enqueue_persists_participation_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": params or {}})
        if sql.startswith("INSERT INTO plugin_wxbot_reply_queue"):
            return [{"id": 21}]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    not_before = datetime(2026, 7, 17, 1, 2, 3, tzinfo=UTC)
    expires_at = not_before + timedelta(seconds=45)

    reply_id = await WxbotStore(SimpleNamespace()).enqueue_reply(
        "demo",
        "room@chatroom",
        "测试群",
        "群友A",
        "我补充一句。",
        delivery={
            "connection_id": "wechat-primary",
            "participation_status": "may_reply",
            "source_message_id": "msg-21",
            "not_before": not_before.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    )

    assert reply_id == 21
    insert = calls[0]
    assert "participation_status" in str(insert["sql"])
    assert "connection_id" in str(insert["sql"])
    assert "source_message_id" in str(insert["sql"])
    assert "not_before" in str(insert["sql"])
    assert "expires_at" in str(insert["sql"])
    assert insert["params"]["participation_status"] == "may_reply"
    assert insert["params"]["connection_id"] == "wechat-primary"
    assert insert["params"]["source_message_id"] == "msg-21"
    assert insert["params"]["not_before"] == not_before
    assert insert["params"]["expires_at"] == expires_at


@pytest.mark.asyncio
async def test_wxbot_reply_idempotency_is_scoped_per_tenant_and_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: dict[tuple[str, str, str], int] = {}
    insert_sql: list[str] = []
    replay_sql: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values = params or {}
        if sql.startswith("INSERT INTO plugin_wxbot_reply_queue"):
            insert_sql.append(sql)
            key = (
                str(values["tid"]),
                str(values["connection_id"]),
                str(values["command_id"]),
            )
            if key in replies:
                return []
            reply_id = len(replies) + 1
            replies[key] = reply_id
            return [{"id": reply_id}]
        if sql.startswith("SELECT id FROM plugin_wxbot_reply_queue"):
            replay_sql.append(sql)
            key = (
                str(values["tid"]),
                str(values["connection_id"]),
                str(values["command_id"]),
            )
            reply_id = replies.get(key)
            return [{"id": reply_id}] if reply_id is not None else []
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    first = await store.enqueue_reply(
        "tenant-a",
        "room",
        "群",
        "成员",
        "A",
        delivery={"connection_id": "wechat-primary"},
        command_id="shared-command",
    )
    duplicate = await store.enqueue_reply(
        "tenant-a",
        "room",
        "群",
        "成员",
        "A2",
        delivery={"connection_id": "wechat-primary"},
        command_id="shared-command",
    )
    second_connection = await store.enqueue_reply(
        "tenant-a",
        "room",
        "群",
        "成员",
        "B",
        delivery={"connection_id": "wechat-backup"},
        command_id="shared-command",
    )
    other_tenant = await store.enqueue_reply(
        "tenant-b",
        "room",
        "群",
        "成员",
        "C",
        delivery={"connection_id": "wechat-primary"},
        command_id="shared-command",
    )

    assert (first, duplicate, second_connection, other_tenant) == (1, 1, 2, 3)
    assert len(replies) == 3
    assert all(
        "ON CONFLICT (tenant_id, connection_id, command_id)" in statement
        for statement in insert_sql
    )
    assert replay_sql
    assert all(
        "tenant_id = :tid AND connection_id = :connection_id" in statement
        for statement in replay_sql
    )


@pytest.mark.asyncio
async def test_wxbot_delivery_updates_cannot_cross_tenant_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": 1,
            "tenant_id": "tenant-a",
            "session_id": "room-a",
            "command_id": "shared-command",
            "status": "sending",
        },
        {
            "id": 2,
            "tenant_id": "tenant-b",
            "session_id": "room-b",
            "command_id": "shared-command",
            "status": "sending",
        },
    ]
    statements: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        statements.append(sql)
        values = params or {}
        matched = [
            row
            for row in rows
            if row["tenant_id"] == values["tenant_id"] and row["command_id"] == values["command_id"]
        ]
        for row in matched:
            row["status"] = "failed" if "SET status = 'failed'" in sql else "sent"
        return [dict(row) for row in matched]

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    succeeded = await store.mark_reply_delivery_succeeded(
        "shared-command",
        tenant_id="tenant-a",
    )
    failed = await store.mark_reply_delivery_failed(
        "shared-command",
        tenant_id="tenant-b",
        terminal=True,
        error="rejected",
    )

    assert [row["id"] for row in succeeded] == [1]
    assert [row["id"] for row in failed] == [2]
    assert [row["status"] for row in rows] == ["sent", "failed"]
    assert all(
        "WHERE tenant_id = :tenant_id AND command_id = :command_id" in statement
        for statement in statements
    )


@pytest.mark.asyncio
async def test_wxbot_sdk_event_deduplication_is_scoped_per_connection(
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

    async def save_member(tenant_id: str, connection_id: str = "") -> bool:
        return await store.save_member_event(
            tenant_id=tenant_id,
            connection_id=connection_id,
            sdk_event_id=7,
            event_type="group.member.joined",
            session_id="room",
            session_name="群",
            entity_wxid="wxid-member",
            entity_name="成员",
            payload={},
            created_ts=1,
        )

    async def save_media(tenant_id: str, connection_id: str = "") -> bool:
        return await store.save_media_ready_event(
            tenant_id=tenant_id,
            connection_id=connection_id,
            sdk_event_id=7,
            event_type="message.media.ready",
            stream_event_id="stream-7",
            message_id="message-7",
            session_id="room",
            session_name="群",
            sender_wxid="wxid-member",
            sender_name="成员",
            msg_type="image",
            media_type="image",
            media_path="image.png",
            media_url="",
            payload={},
            created_ts=1,
        )

    assert [
        await save_member("tenant-a", "wechat-a"),
        await save_member("tenant-a", "wechat-a"),
    ] == [
        True,
        False,
    ]
    assert await save_member("tenant-a", "wechat-b") is True
    assert await save_member("tenant-b", "wechat-a") is True
    assert [
        await save_media("tenant-a", "wechat-a"),
        await save_media("tenant-a", "wechat-a"),
    ] == [
        True,
        False,
    ]
    assert await save_media("tenant-a", "wechat-b") is True
    assert await save_media("tenant-b", "wechat-a") is True
    expected = {
        ("tenant-a", "wechat-a", 7),
        ("tenant-a", "wechat-b", 7),
        ("tenant-b", "wechat-a", 7),
    }
    assert member_keys == expected
    assert media_keys == expected


def test_wxbot_schema_uses_tenant_scoped_external_identifiers() -> None:
    source = Path("migrations/versions/20260718_0016_wxbot_schema.py").read_text(encoding="utf-8")

    assert "(tenant_id, command_id)" in source
    assert "(tenant_id, sdk_event_id)" in source
    assert "sdk_event_id BIGINT NOT NULL UNIQUE" not in source
    assert "DROP INDEX IF EXISTS idx_wxbot_reply_queue_command_id_unique" in source
    assert "plugin_wxbot_member_events_sdk_event_id_key" in source
    assert "plugin_wxbot_media_ready_events_sdk_event_id_key" in source
    assert "claim_owner VARCHAR(128) NOT NULL DEFAULT ''" in source
    assert "claim_token VARCHAR(64) NOT NULL DEFAULT ''" in source
    assert "claim_until TIMESTAMPTZ" in source
    assert "idx_wxbot_reply_queue_claim" in source

    adoption = Path("migrations/versions/20260718_0017_message_reliability.py").read_text(
        encoding="utf-8"
    )
    assert "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS" in adoption
    assert "idx_wxbot_reply_queue_claim" in adoption


@pytest.mark.asyncio
async def test_bridge_ignores_untrusted_delivery_tenant_id() -> None:
    class DeliveryStore(_FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.delivery_calls: list[dict[str, object]] = []

        async def mark_reply_delivery_succeeded(
            self,
            command_id: str,
            **kwargs,
        ) -> list[dict]:
            self.delivery_calls.append({"kind": "succeeded", "command_id": command_id, **kwargs})
            return [{"id": 1}]

        async def mark_reply_delivery_failed(
            self,
            command_id: str,
            **kwargs,
        ) -> list[dict]:
            self.delivery_calls.append({"kind": "failed", "command_id": command_id, **kwargs})
            return [{"id": 1}]

    store = DeliveryStore()
    bridge, _ = _build_bridge(store, connection_id="wechat-primary")
    poisoned_delivery = {
        "command_id": "shared-command",
        "tenant_id": "tenant-b",
        "outbound_id": 9,
    }

    await bridge._record_delivery_event(
        {"delivery": {**poisoned_delivery, "status": "succeeded"}},
        "message.delivery.succeeded",
    )
    await bridge._record_delivery_event(
        {
            "delivery": {
                **poisoned_delivery,
                "status": "failed",
                "error": "rejected",
            }
        },
        "message.delivery.failed",
    )

    assert [call["kind"] for call in store.delivery_calls] == [
        "succeeded",
        "failed",
    ]
    assert all(call["tenant_id"] == "demo" for call in store.delivery_calls)
    assert all(call["connection_id"] == "wechat-primary" for call in store.delivery_calls)
    assert all(call["command_id"] == "shared-command" for call in store.delivery_calls)


@pytest.mark.asyncio
async def test_bridge_records_only_accepted_inbound_and_confirmed_delivery_activity() -> None:
    class DeliveryStore(_FakeStore):
        async def mark_reply_delivery_succeeded(self, *_args, **_kwargs) -> list[dict]:
            return [{"id": 1}]

        async def mark_reply_delivery_failed(self, *_args, **_kwargs) -> list[dict]:
            return [{"id": 1}]

    activity: list[str] = []

    async def record(direction: str) -> None:
        activity.append(direction)

    bridge, _ = _build_bridge(
        DeliveryStore(),
        connection_id="wechat-primary",
        connection_activity_recorder=record,
    )
    bus = _FakeBus()
    await bridge._publish_legacy_message(
        {
            "id": 101,
            "session_id": "room@chatroom",
            "sender_wxid": "wxid-a",
            "content": "hello",
            "msg_type": "text",
        },
        "inbound:events",
        bus,
    )
    await bridge._record_delivery_event(
        {"delivery": {"command_id": "cmd-1", "status": "succeeded"}},
        "message.delivery.succeeded",
    )
    await bridge._record_delivery_event(
        {"delivery": {"command_id": "cmd-2", "status": "failed"}},
        "message.delivery.failed",
    )

    assert activity == ["inbound", "outbound_delivered"]


@pytest.mark.asyncio
async def test_bridge_overwrites_untrusted_outbound_delivery_tenant_id() -> None:
    store = _FakeStore()
    bridge, _ = _build_bridge(store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 23,
            "claim_token": "claim-23",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "sender_name": "成员",
            "reply_text": "隔离检查",
            "msg_type": "text",
            "delivery": {
                "tenant_id": "tenant-b",
                "command_id": "shared-command",
            },
        }
    )

    assert client.calls[0]["json"]["delivery"]["tenant_id"] == "demo"
    assert store.updated_commands[0]["delivery"]["tenant_id"] == "demo"


@pytest.mark.asyncio
async def test_wxbot_store_participation_snapshot_counts_durable_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = WxbotStore(SimpleNamespace())

    async def recent(*args, **kwargs):
        _ = args, kwargs
        return [
            {
                "sender_wxid": "wxid_user",
                "is_self_sent": False,
                "occurred_ts": 100,
            },
            {
                "sender_wxid": "wxid_bot",
                "is_self_sent": True,
                "occurred_ts": 99,
            },
        ]

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = sql, params
        return [{"soft_replies_last_10m": 2, "soft_replies_last_hour": 5}]

    store.list_recent_group_observations = recent  # type: ignore[method-assign]
    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    snapshot = await store.get_participation_snapshot(
        "demo",
        "room@chatroom",
        now=datetime.fromtimestamp(100, tz=UTC),
    )

    assert snapshot["bot_messages_last_40"] == 1
    assert snapshot["total_messages_last_40"] == 2
    assert snapshot["soft_replies_last_10m"] == 2
    assert snapshot["soft_replies_last_hour"] == 5
    assert snapshot["bot_replied_within_60s"] is True


@pytest.mark.asyncio
async def test_wxbot_store_resolves_direct_quote_to_bot_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": params or {}})
        return [{"id": 8}]

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    targets_bot = await WxbotStore(SimpleNamespace()).quote_targets_bot(
        "demo",
        "room@chatroom",
        {"refer_msg_svr_id": "bot-message-1"},
    )

    assert targets_bot is True
    assert "is_self_sent = TRUE" in str(calls[0]["sql"])
    assert "metadata_json::jsonb ->> 'msg_svr_id'" in str(calls[0]["sql"])
    assert calls[0]["params"]["reference_0"] == "bot-message-1"


@pytest.mark.asyncio
async def test_wxbot_store_retry_failed_reply_is_single_fenced_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append({"sql": sql, "params": params or {}})
        return [{"status": "pending"}]

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    status = await WxbotStore(SimpleNamespace()).mark_reply_failed(
        12,
        "temporary sdk failure",
        tenant_id="demo",
        claim_token="current-token",
    )

    assert status == "retry"
    assert len(calls) == 1
    retry_sql = str(calls[0]["sql"])
    assert "CASE WHEN COALESCE(attempt_count, 0) >= :max_attempts" in retry_sql
    assert "tenant_id = :tenant_id" in retry_sql
    assert "claim_token = :claim_token" in retry_sql
    assert "claim_until > NOW()" in retry_sql
    assert calls[0]["params"] == {
        "id": 12,
        "tenant_id": "demo",
        "claim_token": "current-token",
        "err": "temporary sdk failure",
        "max_attempts": 3,
        "legacy_connection_id": "legacy-wechat-default",
    }


@pytest.mark.asyncio
async def test_sdk_bridge_auto_takes_over_after_leader_lock_released() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.leader_key] = "other-instance"
    bridge._leader_retry_interval = 0.1

    activated = asyncio.Event()

    async def _fake_activate_leader() -> None:
        activated.set()

    bridge._activate_leader = _fake_activate_leader  # type: ignore[method-assign]

    await bridge.start()

    assert bridge._is_leader is False
    assert bridge._ingest_mode == "standby"

    await asyncio.sleep(0.15)
    assert activated.is_set() is False

    await bridge._redis.delete(bridge.leader_key)
    await asyncio.wait_for(activated.wait(), timeout=1.0)

    assert bridge._is_leader is True

    await bridge.stop()


def test_sdk_bridge_parse_sse_frame_supports_event_blocks() -> None:
    parsed = SdkBridge._parse_sse_frame(
        "id: 11\n"
        "event: group.member.joined\n"
        'data: {"id":11,\n'
        'data: "event_type":"group.member.joined"}\n'
    )

    assert parsed == (
        "group.member.joined",
        '{"id":11,\n"event_type":"group.member.joined"}',
    )


def test_sdk_bridge_image_url_normalizes_windows_absolute_paths() -> None:
    image_url = SdkBridge._image_url(
        "http://127.0.0.1:5080/",
        r"C:\Users\Example\AppData\Local\Programs\wx-bot-client\data\images\d61cfa7a98e798a6ec6ef90782da12ee\284.png",
    )

    assert image_url == "http://127.0.0.1:5080/images/d61cfa7a98e798a6ec6ef90782da12ee/284.png"


def test_sdk_bridge_image_url_normalizes_legacy_absolute_image_urls() -> None:
    image_url = SdkBridge._image_url(
        "http://127.0.0.1:5080/",
        r"http://192.0.2.94:5080/images/C:\Users\Example\AppData\Local\Programs\wx-bot-client\data\images\d61cfa7a98e798a6ec6ef90782da12ee\284.png",
    )

    assert image_url == "http://192.0.2.94:5080/images/d61cfa7a98e798a6ec6ef90782da12ee/284.png"


@pytest.mark.asyncio
async def test_sdk_bridge_records_legacy_member_event_payload() -> None:
    bridge, store = _build_bridge()

    await bridge._record_legacy_member_event(
        {
            "id": 21,
            "event_type": "group.member.joined",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "entity_wxid": "wxid_new",
            "entity_name": "新群友",
            "payload": {"inviter_wxid": "wxid_admin"},
            "created_ts": 1710000001,
        }
    )

    assert store.saved_member_events == [
        {
            "tenant_id": "demo",
            "connection_id": "legacy-wechat-default",
            "sdk_event_id": 21,
            "event_type": "group.member.joined",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "entity_wxid": "wxid_new",
            "entity_name": "新群友",
            "payload": {"inviter_wxid": "wxid_admin"},
            "created_ts": 1710000001,
        }
    ]


@pytest.mark.asyncio
async def test_sdk_bridge_records_stream_member_event_payload() -> None:
    bridge, store = _build_bridge(connection_id="wechat-main")

    await bridge._record_stream_member_event(
        {
            "id": 31,
            "event_id": "stream:31",
            "event_type": "group.member.joined",
            "occurred_ts": 1710000002,
            "session": {
                "id": "room@chatroom",
                "name": "测试群",
                "kind": "group",
            },
            "member": {
                "id": "wxid_new",
                "name": "新群友",
            },
            "operator": {
                "id": "wxid_admin",
                "name": "管理员",
            },
            "raw": {"kind": "joined"},
            "meta": {"source": "stream"},
        }
    )

    assert store.saved_member_events == [
        {
            "tenant_id": "demo",
            "connection_id": "wechat-main",
            "sdk_event_id": 31,
            "event_type": "group.member.joined",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "entity_wxid": "wxid_new",
            "entity_name": "新群友",
            "payload": {
                "operator": {
                    "id": "wxid_admin",
                    "name": "管理员",
                },
                "raw": {"kind": "joined"},
                "meta": {"source": "stream"},
            },
            "created_ts": 1710000002,
        }
    ]


@pytest.mark.asyncio
async def test_sdk_bridge_publish_legacy_message_preserves_metadata_for_all_messages() -> None:
    bridge, store = _build_bridge()
    bus = _FakeBus()

    await bridge._publish_legacy_message(
        {
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "sender_wxid": "wxid_group_user",
            "sender_name": "群友A",
            "msg_svr_id": "msg-1",
            "msg_type": "text",
            "msg_text": "你好，想问一下报价",
            "mentioned_me": 1,
            "at_wxids": ["wxid_bot"],
            "mention_mode": "metadata",
            "is_self_sent": False,
            "bot_mentioned": True,
            "bot_addressed": True,
            "bot_mention_position": "leading",
            "bot_mention_names": ["机器人"],
            "bot_normalized_content": "你好，想问一下报价",
            "bot_wxid": "wxid_bot",
            "capture_allowed": True,
            "capture_reason": "bot_mention",
        },
        "inbound:events",
        bus,
    )
    await bridge._publish_legacy_message(
        {
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "sender_wxid": "wxid_group_user",
            "sender_name": "群友B",
            "msg_svr_id": "msg-2",
            "msg_type": "text",
            "msg_text": "今天天气不错",
        },
        "inbound:events",
        bus,
    )
    await bridge._publish_legacy_message(
        {
            "session_id": "private-user",
            "session_name": "私聊",
            "sender_wxid": "wxid_private_user",
            "sender_name": "用户B",
            "msg_svr_id": "msg-3",
            "msg_type": "image",
            "msg_text": "",
            "image_path": "images/demo.png",
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 3

    first = bus.items[0]["payload"]
    assert first["session_id"] == "room@chatroom"
    assert first["message"]["content"] == "你好，想问一下报价"
    assert first["metadata"]["source"] == "wxbot"
    assert first["metadata"]["sender_name"] == "群友A"
    assert first["metadata"]["mentioned_me"] is True
    assert first["metadata"]["at_wxids"] == ["wxid_bot"]
    assert first["metadata"]["mention_mode"] == "metadata"
    assert first["metadata"]["is_self_sent"] is False
    assert first["metadata"]["bot_addressed"] is True
    assert first["metadata"]["bot_mention_names"] == ["机器人"]
    assert first["metadata"]["bot_normalized_content"] == "你好，想问一下报价"
    assert first["metadata"]["capture_reason"] == "bot_mention"
    assert first["metadata"]["session_kind"] == "group"

    second = bus.items[1]["payload"]
    assert second["session_id"] == "room@chatroom"
    assert second["message"]["content"] == "今天天气不错"
    assert [item["message_id"] for item in store.saved_group_observations] == [
        "msg-1",
        "msg-2",
    ]
    assert store.saved_group_observations[0]["bot_addressed"] is True
    assert store.saved_group_observations[1]["bot_addressed"] is False

    third = bus.items[2]["payload"]
    assert third["session_id"] == "private-user"
    assert third["message"]["content"] == "[图片]"
    assert third["metadata"]["image_url"] == "http://127.0.0.1:5080/images/demo.png"
    assert third["metadata"]["mentioned_me"] is False
    assert third["metadata"]["session_kind"] == "private"
    assert bus.items[2]["partition_key"] == "demo:private-user"


@pytest.mark.asyncio
async def test_sdk_bridge_publish_legacy_message_dedupes_same_msg_svr_id() -> None:
    bridge, _ = _build_bridge()
    bus = _FakeBus()

    msg = {
        "session_id": "room@chatroom",
        "session_name": "测试群",
        "sender_wxid": "wxid_group_user",
        "sender_name": "群友A",
        "msg_svr_id": "dup-1",
        "msg_type": "text",
        "msg_text": "@bot 你好",
        "mentioned_me": 1,
    }
    await bridge._publish_legacy_message(msg, "inbound:events", bus)
    await bridge._publish_legacy_message(msg, "inbound:events", bus)

    assert len(bus.items) == 1
    assert bus.items[0]["payload"]["message_id"] == "dup-1"


@pytest.mark.asyncio
async def test_sdk_bridge_capture_disallowed_advances_cursor_without_storing_or_publishing() -> (
    None
):
    class _CaptureStore(_FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.interactions: list[dict[str, object]] = []

        async def record_interactive_inbound(self, **kwargs) -> None:
            self.interactions.append(kwargs)

    store = _CaptureStore()
    bridge, _ = _build_bridge(store)
    bus = _FakeBus()

    await bridge._publish_legacy_message(
        {
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "sender_wxid": "wxid_private",
            "sender_name": "群友A",
            "msg_svr_id": "private-legacy",
            "msg_type": "text",
            "msg_text": "不得持久化的内容",
            "capture_allowed": False,
            "capture_reason": "privacy_policy",
        },
        "inbound:events",
        bus,
    )
    await bridge._publish_stream_message(
        {
            "id": 202,
            "event_id": "stream:202",
            "event_type": "message.received",
            "session": {
                "id": "room@chatroom",
                "name": "测试群",
                "kind": "group",
            },
            "sender": {"id": "wxid_private", "name": "群友A"},
            "message": {
                "id": "private-unified",
                "type": "text",
                "text": "同样不得持久化的内容",
                "capture_allowed": "false",
            },
        },
        "inbound:events",
        bus,
    )

    assert bus.items == []
    assert store.saved_group_observations == []
    assert [(item["session_id"], item["message_id"]) for item in store.interactions] == [
        ("room@chatroom", "private-legacy"),
        ("room@chatroom", "private-unified"),
    ]
    assert all("content" not in item for item in store.interactions)
    assert bridge._inbound_dedupe_key("private-legacy") in bridge._redis.values
    assert bridge._inbound_dedupe_key("private-unified") in bridge._redis.values


@pytest.mark.asyncio
async def test_sdk_bridge_releases_inbound_dedupe_marker_when_bus_publish_fails() -> None:
    class _FailingBus:
        async def publish(self, **kwargs) -> None:
            _ = kwargs
            raise RuntimeError("redis stream unavailable")

    bridge, _ = _build_bridge()
    message_id = "retry-after-publish-failure"

    with pytest.raises(RuntimeError, match="redis stream unavailable"):
        await bridge._publish_legacy_message(
            {
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "sender_wxid": "wxid_group_user",
                "sender_name": "群友A",
                "msg_svr_id": message_id,
                "msg_type": "text",
                "msg_text": "这条需要重试",
            },
            "inbound:events",
            _FailingBus(),
        )

    assert bridge._inbound_dedupe_key(message_id) not in bridge._redis.values


@pytest.mark.asyncio
async def test_sdk_bridge_publish_legacy_message_drops_stale_messages() -> None:
    bridge, _ = _build_bridge(max_message_age_seconds=60)
    bus = _FakeBus()
    stale_ts = int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())

    await bridge._publish_legacy_message(
        {
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "sender_wxid": "wxid_group_user",
            "sender_name": "群友A",
            "msg_svr_id": "stale-legacy",
            "msg_type": "text",
            "msg_text": "@bot 签到",
            "mentioned_me": 1,
            "recv_ts": stale_ts,
        },
        "inbound:events",
        bus,
    )

    assert bus.items == []


@pytest.mark.asyncio
async def test_sdk_bridge_publish_stream_message_maps_unified_envelope() -> None:
    bridge, _ = _build_bridge()
    bus = _FakeBus()

    await bridge._publish_stream_message(
        {
            "id": 101,
            "event_id": "stream:101",
            "event_type": "message.received",
            "occurred_ts": 1777000000,
            "occurred_at": "2026-04-21T21:30:00+08:00",
            "source": "wxbot-sdk",
            "session": {
                "id": "room@chatroom",
                "name": "测试群",
                "kind": "group",
            },
            "sender": {
                "id": "wxid_group_user",
                "name": "群友A",
            },
            "message": {
                "id": "msg-101",
                "type": "image",
                "text": "",
                "image_path": "images/demo.png",
                "mentioned_me": True,
                "at_wxids": ["wxid_bot"],
                "mention_mode": "metadata",
                "is_self_sent": True,
                "bot_mentioned": True,
                "bot_addressed": True,
                "bot_mention_position": "leading",
                "bot_mention_names": ["机器人"],
                "bot_normalized_content": "[图片]",
                "bot_wxid": "wxid_bot",
                "capture_allowed": True,
                "capture_reason": "bot_mention",
                "recv_ts": 1777000000,
            },
            "raw": {"legacy": {"msg_svr_id": "msg-101"}},
            "meta": {"compat": "v2"},
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    payload = bus.items[0]["payload"]
    assert payload["message_id"] == "msg-101"
    assert payload["session_id"] == "room@chatroom"
    assert payload["user_id"] == "wxid_group_user"
    assert payload["message"]["content"] == "[图片]"
    assert payload["metadata"]["sdk_stream_id"] == 101
    assert payload["metadata"]["sdk_event_id"] == "stream:101"
    assert payload["metadata"]["sdk_event_type"] == "message.received"
    assert payload["metadata"]["session_kind"] == "group"
    assert payload["metadata"]["mentioned_me"] is True
    assert payload["metadata"]["at_wxids"] == ["wxid_bot"]
    assert payload["metadata"]["mention_mode"] == "metadata"
    assert payload["metadata"]["is_self_sent"] is True
    assert payload["metadata"]["bot_addressed"] is True
    assert payload["metadata"]["bot_mention_names"] == ["机器人"]
    assert payload["metadata"]["capture_reason"] == "bot_mention"
    assert payload["metadata"]["image_path"] == "images/demo.png"
    assert payload["metadata"]["image_url"] == "http://127.0.0.1:5080/images/demo.png"
    assert payload["metadata"]["occurred_at"] == "2026-04-21T21:30:00+08:00"
    assert payload["metadata"]["raw"] == {"legacy": {"msg_svr_id": "msg-101"}}
    assert payload["metadata"]["meta"] == {"compat": "v2"}
    assert bus.items[0]["partition_key"] == "demo:room@chatroom"


@pytest.mark.asyncio
async def test_sdk_bridge_publish_stream_message_uses_raw_preview_variant() -> None:
    bridge, _ = _build_bridge()
    bus = _FakeBus()

    await bridge._publish_stream_message(
        {
            "id": 102,
            "event_id": "stream:102",
            "event_type": "message.received",
            "occurred_ts": 1777000000,
            "source": "wxbot-sdk",
            "session": {
                "id": "wx-private",
                "name": "私聊",
                "kind": "private",
            },
            "sender": {
                "id": "wxid_sender",
                "name": "用户A",
            },
            "message": {
                "id": "msg-102",
                "type": "image",
                "text": "[图片]",
                "image_path": "images/hash-102/102_thumbnail.jpg",
            },
            "raw": {
                "image_variants": {
                    "preview": {
                        "image_path": "images/hash-102/102_preview.jpg",
                        "image_url": "/images/hash-102/102_preview.jpg",
                    },
                    "thumbnail": {
                        "image_path": "images/hash-102/102_thumbnail.jpg",
                        "image_url": "/images/hash-102/102_thumbnail.jpg",
                    },
                }
            },
            "meta": {},
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    payload = bus.items[0]["payload"]
    assert (
        payload["metadata"]["image_url"] == "http://127.0.0.1:5080/images/hash-102/102_preview.jpg"
    )
    assert (
        payload["metadata"]["image_preview_url"]
        == "http://127.0.0.1:5080/images/hash-102/102_preview.jpg"
    )
    assert payload["metadata"]["image_thumbnail_url"] == (
        "http://127.0.0.1:5080/images/hash-102/102_thumbnail.jpg"
    )
    assert payload["metadata"]["image_variants"]["preview"]["image_url"] == (
        "/images/hash-102/102_preview.jpg"
    )


@pytest.mark.asyncio
async def test_sdk_bridge_publish_stream_message_dedupes_same_message_id() -> None:
    bridge, _ = _build_bridge()
    bus = _FakeBus()

    envelope = {
        "id": 201,
        "event_id": "stream:201",
        "event_type": "message.received",
        "occurred_ts": 1777000001,
        "occurred_at": "2026-04-21T21:30:01+08:00",
        "source": "wxbot-sdk",
        "session": {
            "id": "room@chatroom",
            "name": "测试群",
            "kind": "group",
        },
        "sender": {
            "id": "wxid_group_user",
            "name": "群友A",
        },
        "message": {
            "id": "msg-201",
            "type": "text",
            "text": "同一条只处理一次",
            "image_path": "",
            "mentioned_me": True,
            "recv_ts": 1777000001,
        },
        "raw": {},
        "meta": {},
    }
    await bridge._publish_stream_message(envelope, "inbound:events", bus)
    await bridge._publish_stream_message(envelope, "inbound:events", bus)

    assert len(bus.items) == 1
    assert bus.items[0]["payload"]["message_id"] == "msg-201"


@pytest.mark.asyncio
async def test_sdk_bridge_publish_stream_message_drops_stale_messages() -> None:
    bridge, _ = _build_bridge(max_message_age_seconds=60)
    bus = _FakeBus()
    stale_ts = int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())

    await bridge._publish_stream_message(
        {
            "id": 202,
            "event_id": "stream:202",
            "event_type": "message.received",
            "occurred_ts": stale_ts,
            "source": "wxbot-sdk",
            "session": {
                "id": "room@chatroom",
                "name": "测试群",
                "kind": "group",
            },
            "sender": {
                "id": "wxid_group_user",
                "name": "群友A",
            },
            "message": {
                "id": "msg-202",
                "type": "text",
                "text": "@bot 签到",
                "mentioned_me": True,
            },
        },
        "inbound:events",
        bus,
    )

    assert bus.items == []


@pytest.mark.asyncio
async def test_sdk_bridge_records_stream_media_ready_without_publishing_inbound() -> None:
    bridge, store = _build_bridge()
    bus = _FakeBus()

    await bridge._handle_stream_event(
        {
            "id": 301,
            "event_id": "stream:301",
            "event_type": "message.media.ready",
            "occurred_ts": 1777000301,
            "session": {
                "id": "room@chatroom",
                "name": "测试群",
            },
            "sender": {
                "id": "wxid_group_user",
                "name": "群友A",
            },
            "message": {
                "id": "msg-301",
                "type": "image",
            },
            "media": {
                "type": "image",
                "image_path": "images/ready.png",
            },
            "raw": {"legacy": {"msg_svr_id": "msg-301"}},
            "meta": {"stage": "decrypt-ready"},
        },
        "inbound:events",
        bus,
    )

    assert bus.items == []
    assert store.saved_media_ready_events == [
        {
            "tenant_id": "demo",
            "connection_id": "legacy-wechat-default",
            "sdk_event_id": 301,
            "event_type": "message.media.ready",
            "stream_event_id": "stream:301",
            "message_id": "msg-301",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "sender_wxid": "wxid_group_user",
            "sender_name": "群友A",
            "msg_type": "image",
            "media_type": "image",
            "media_path": "images/ready.png",
            "media_url": "http://127.0.0.1:5080/images/ready.png",
            "payload": {
                "message": {
                    "id": "msg-301",
                    "type": "image",
                },
                "media": {
                    "type": "image",
                    "image_path": "images/ready.png",
                },
                "raw": {"legacy": {"msg_svr_id": "msg-301"}},
                "meta": {"stage": "decrypt-ready"},
            },
            "created_ts": 1777000301,
        }
    ]


@pytest.mark.asyncio
async def test_sdk_bridge_publishes_image_placeholder_while_waiting_for_media() -> None:
    bridge, store = _build_bridge()
    bus = _FakeBus()

    await bridge._handle_stream_event(
        {
            "id": 401,
            "event_id": "stream:401",
            "event_type": "message.received",
            "occurred_ts": 1777000401,
            "source": "wxbot-sdk",
            "session": {
                "id": "wx-private",
                "name": "私聊",
                "kind": "private",
            },
            "sender": {
                "id": "wxid_sender",
                "name": "用户A",
            },
            "message": {
                "id": "msg-401",
                "type": "image",
                "text": "[图片]",
            },
            "raw": {"legacy": {"msg_svr_id": "msg-401"}},
            "meta": {},
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    placeholder = bus.items[0]["payload"]
    assert placeholder["message_id"] == "msg-401"
    assert placeholder["message"]["content"] == "[图片]"
    assert placeholder["metadata"]["media_status"] == "pending"
    assert placeholder["metadata"]["sender_name"] == "用户A"
    assert "msg-401" in bridge._pending_media_messages

    await bridge._handle_stream_event(
        {
            "id": 402,
            "event_id": "stream:402",
            "event_type": "message.media.ready",
            "occurred_ts": 1777000402,
            "session": {
                "id": "wx-private",
                "name": "私聊",
                "kind": "private",
            },
            "sender": {
                "id": "wxid_sender",
                "name": "用户A",
            },
            "message": {
                "id": "msg-401",
                "type": "image",
            },
            "media": {
                "type": "image",
                "image_path": "images/hash-401/401_preview.jpg",
                "image_variants": {
                    "preview": {
                        "image_path": "images/hash-401/401_preview.jpg",
                        "image_url": "http://127.0.0.1:5080/images/hash-401/401_preview.jpg",
                    },
                    "thumbnail": {
                        "image_path": "images/hash-401/401_thumbnail.jpg",
                        "image_url": "http://127.0.0.1:5080/images/hash-401/401_thumbnail.jpg",
                    },
                },
            },
            "raw": {"legacy": {"msg_svr_id": "msg-401"}},
            "meta": {"stage": "decrypt-ready"},
        },
        "inbound:events",
        bus,
    )

    assert "msg-401" not in bridge._pending_media_messages
    assert len(bus.items) == 1
    payload = bus.items[0]["payload"]
    assert payload["message_id"] == "msg-401"
    assert payload["message"]["content"] == "[图片]"
    assert payload["metadata"]["sender_name"] == "用户A"
    assert store.saved_media_ready_events[0]["message_id"] == "msg-401"


@pytest.mark.asyncio
async def test_sdk_bridge_resolves_tracked_image_from_sdk_messages() -> None:
    bridge, store = _build_bridge()
    bus = _FakeBus()
    bridge._client = _FakeGetClient(
        {
            "message": {
                "id": 6,
                "msg_svr_id": "msg-501",
                "session_id": "wx-private",
                "msg_text": "[图片]",
                "msg_type": "image",
                "image_path": "images/hash-501/501.png",
                "recv_ts": 1777000501,
            }
        }
    )

    await bridge._handle_stream_event(
        {
            "id": 501,
            "event_id": "stream:501",
            "event_type": "message.received",
            "occurred_ts": 1777000501,
            "source": "wxbot-sdk",
            "session": {
                "id": "wx-private",
                "name": "私聊",
                "kind": "private",
            },
            "sender": {
                "id": "wxid_sender",
                "name": "用户A",
            },
            "message": {
                "id": "msg-501",
                "type": "image",
                "text": "[图片]",
            },
            "raw": {"legacy": {"msg_svr_id": "msg-501"}},
            "meta": {},
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    assert bus.items[0]["payload"]["message"]["content"] == "[图片]"
    assert bus.items[0]["payload"]["metadata"]["media_status"] == "pending"
    resolved = await bridge._resolve_pending_media_from_sdk("inbound:events", bus)

    assert resolved == 1
    assert bridge._client.calls == [
        {
            "url": "http://127.0.0.1:5080/messages/msg-501",
            "params": {},
        }
    ]
    assert "msg-501" not in bridge._pending_media_messages
    assert len(bus.items) == 1
    payload = bus.items[0]["payload"]
    assert payload["message_id"] == "msg-501"
    assert store.saved_media_ready_events[0]["message_id"] == "msg-501"
    assert store.saved_media_ready_events[0]["media_path"] == "images/hash-501/501.png"
    assert store.saved_media_ready_events[0]["created_ts"] > 1777000501


@pytest.mark.asyncio
async def test_sdk_bridge_extracts_quoted_image_metadata_from_stream_message() -> None:
    bridge, _ = _build_bridge()
    bus = _FakeBus()

    await bridge._handle_stream_event(
        {
            "id": 601,
            "event_id": "stream:601",
            "event_type": "message.received",
            "occurred_ts": 1777000601,
            "source": "wxbot-sdk",
            "session": {
                "id": "wx-private",
                "name": "私聊",
                "kind": "private",
            },
            "sender": {
                "id": "wxid_sender",
                "name": "用户A",
            },
            "message": {
                "id": "msg-601",
                "type": "text",
                "text": "这张图改成梵高风格",
                "quote": {
                    "msg_svr_id": "quoted-image",
                    "image_path": r"C:\Users\Example\AppData\Local\Programs\wx-bot-client\data\images\hash-601\601.png",
                    "image_preview_url": "http://127.0.0.1:5080/images/hash-601/601.png",
                    "image_thumbnail_url": "http://127.0.0.1:5080/images/hash-601/601_thumb.png",
                },
            },
            "raw": {"legacy": {"msg_svr_id": "msg-601"}},
            "meta": {},
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    payload = bus.items[0]["payload"]
    assert payload["message_id"] == "msg-601"
    assert payload["metadata"]["quote_image_path"] == (
        r"C:\Users\Example\AppData\Local\Programs\wx-bot-client\data\images\hash-601\601.png"
    )
    assert payload["metadata"]["quote_image_url"] == "http://127.0.0.1:5080/images/hash-601/601.png"
    assert (
        payload["metadata"]["quote_image_preview_url"]
        == "http://127.0.0.1:5080/images/hash-601/601.png"
    )
    assert (
        payload["metadata"]["quote_image_thumbnail_url"]
        == "http://127.0.0.1:5080/images/hash-601/601_thumb.png"
    )
    assert payload["metadata"]["image_observation"] == {
        "current_image_found": False,
        "quote_image_found": True,
        "attachment_count": 0,
        "quote_attachment_count": 1,
        "media_status": "",
        "failure_reason": "",
        "skip_reason": "",
    }


@pytest.mark.asyncio
async def test_sdk_bridge_extracts_quoted_text_metadata_from_stream_message() -> None:
    bridge, _ = _build_bridge()
    bus = _FakeBus()

    await bridge._handle_stream_event(
        {
            "id": 602,
            "event_id": "stream:602",
            "event_type": "message.received",
            "occurred_ts": 1777000602,
            "source": "wxbot-sdk",
            "session": {
                "id": "wx-private",
                "name": "私聊",
                "kind": "private",
            },
            "sender": {
                "id": "wxid_sender",
                "name": "用户A",
            },
            "message": {
                "id": "msg-602",
                "type": "text",
                "text": "/draw 画成电影海报",
                "quote": {
                    "msg_svr_id": "quoted-text",
                    "message": {"text": "一辆红色跑车停在雨夜霓虹街头"},
                },
            },
            "raw": {"legacy": {"msg_svr_id": "msg-602"}},
            "meta": {},
        },
        "inbound:events",
        bus,
    )

    assert len(bus.items) == 1
    payload = bus.items[0]["payload"]
    assert payload["message_id"] == "msg-602"
    assert payload["metadata"]["quote_text"] == "一辆红色跑车停在雨夜霓虹街头"
    assert payload["metadata"]["quote"]["msg_svr_id"] == "quoted-text"


@pytest.mark.asyncio
async def test_sdk_bridge_send_one_reply_prefers_envelope_protocol() -> None:
    store = _FakeStore()
    bridge, _ = _build_bridge(store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 9,
            "claim_token": "claim-9",
            "session_id": "wx-1",
            "session_name": "测试会话",
            "sender_name": "客服",
            "mention_sender": False,
            "reply_text": "",
            "msg_type": "image",
            "image_path": "images/demo.png",
        }
    )

    assert client.calls == [
        {
            "url": "http://127.0.0.1:5080/send/envelope",
            "json": {
                "target": {
                    "session_id": "wx-1",
                    "session_name": "测试会话",
                    "session_kind": "",
                },
                "sender": {
                    "wxid": "",
                    "name": "客服",
                },
                "content": {
                    "msg_type": "image",
                    "text": "",
                    "image_path": "images/demo.png",
                    "image_url": "",
                },
                "reply": {
                    "mention_sender": False,
                    "reply_to_msg_svr_id": "",
                },
                "source_message": {},
                "delivery": {
                    "command_id": "wxbot-reply:9",
                    "idempotency_key": "wxbot-reply:9",
                    "reply_queue_id": 9,
                    "tenant_id": "demo",
                    "trace_id": "",
                },
                "command_id": "wxbot-reply:9",
                "metadata": {
                    "tenant_id": "demo",
                    "trace_id": "",
                    "command_id": "wxbot-reply:9",
                    "protocol": "envelope",
                },
            },
        }
    ]
    assert store.sent_ids == [9]
    assert store.failed_ids == []


@pytest.mark.asyncio
async def test_sdk_bridge_send_one_reply_falls_back_to_legacy_send() -> None:
    store = _FakeStore()
    bridge, _ = _build_bridge(store)
    client = _FakeClient(statuses=[404, 200])
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 10,
            "claim_token": "claim-10",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "sender_name": "客服",
            "sender_wxid": "wxid_customer",
            "mention_sender": True,
            "reply_to_msg_svr_id": "123456",
            "reply_text": "结构化回退",
            "msg_type": "text",
            "image_path": "",
            "session_kind": "group",
            "source_message": {"message_id": "msg-101"},
            "delivery": {"channel": "wechat"},
            "trace_id": "trace-10",
        }
    )

    assert client.calls == [
        {
            "url": "http://127.0.0.1:5080/send/envelope",
            "json": {
                "target": {
                    "session_id": "room@chatroom",
                    "session_name": "测试群",
                    "session_kind": "group",
                },
                "sender": {
                    "wxid": "wxid_customer",
                    "name": "客服",
                },
                "content": {
                    "msg_type": "text",
                    "text": "结构化回退",
                    "image_path": "",
                    "image_url": "",
                },
                "reply": {
                    "mention_sender": True,
                    "reply_to_msg_svr_id": "123456",
                },
                "source_message": {"message_id": "msg-101"},
                "delivery": {
                    "channel": "wechat",
                    "command_id": "wxbot-reply:10",
                    "idempotency_key": "wxbot-reply:10",
                    "reply_queue_id": 10,
                    "tenant_id": "demo",
                    "trace_id": "trace-10",
                },
                "command_id": "wxbot-reply:10",
                "metadata": {
                    "tenant_id": "demo",
                    "trace_id": "trace-10",
                    "command_id": "wxbot-reply:10",
                    "protocol": "envelope",
                },
            },
        },
        {
            "url": "http://127.0.0.1:5080/send",
            "json": {
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "sender_name": "客服",
                "sender_wxid": "wxid_customer",
                "mention_sender": True,
                "reply_to_msg_svr_id": "123456",
                "session_kind": "group",
                "text": "结构化回退",
                "msg_type": "text",
                "image_path": "",
                "image_url": "",
                "source_message": {"message_id": "msg-101"},
                "delivery": {
                    "channel": "wechat",
                    "command_id": "wxbot-reply:10",
                    "idempotency_key": "wxbot-reply:10",
                    "reply_queue_id": 10,
                    "tenant_id": "demo",
                    "trace_id": "trace-10",
                },
                "command_id": "wxbot-reply:10",
            },
        },
    ]
    assert store.sent_ids == [10]
    assert store.failed_ids == []


@pytest.mark.asyncio
async def test_sdk_bridge_send_one_reply_passes_image_url_to_sdk() -> None:
    store = _FakeStore()
    bridge, _ = _build_bridge(store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 11,
            "claim_token": "claim-11",
            "session_id": "wx-2",
            "session_name": "异机图片会话",
            "sender_name": "客服",
            "reply_text": "",
            "msg_type": "image",
            "image_path": "",
            "image_url": "http://198.51.100.94:8000/plugins/draw/files/demo.png",
        }
    )

    assert client.calls == [
        {
            "url": "http://127.0.0.1:5080/send/envelope",
            "json": {
                "target": {
                    "session_id": "wx-2",
                    "session_name": "异机图片会话",
                    "session_kind": "",
                },
                "sender": {
                    "wxid": "",
                    "name": "客服",
                },
                "content": {
                    "msg_type": "image",
                    "text": "",
                    "image_path": "",
                    "image_url": "http://198.51.100.94:8000/plugins/draw/files/demo.png",
                },
                "reply": {
                    "mention_sender": False,
                    "reply_to_msg_svr_id": "",
                },
                "source_message": {},
                "delivery": {
                    "command_id": "wxbot-reply:11",
                    "idempotency_key": "wxbot-reply:11",
                    "reply_queue_id": 11,
                    "tenant_id": "demo",
                    "trace_id": "",
                },
                "command_id": "wxbot-reply:11",
                "metadata": {
                    "tenant_id": "demo",
                    "trace_id": "",
                    "command_id": "wxbot-reply:11",
                    "protocol": "envelope",
                },
            },
        }
    ]
    assert store.sent_ids == [11]
    assert store.failed_ids == []


def _group_activity_execution_delivery() -> dict[str, object]:
    return {
        "source": "group_activity",
        "execution_owners": ["group_activity", "wxbot"],
        "execution_owner_versions": {
            "group_activity": "0.1.0",
            "wxbot": "0.2.0",
        },
        "execution_tenant_id": "demo",
        "execution_session_id": "room@chatroom",
    }


@pytest.mark.asyncio
async def test_sdk_bridge_cancels_claimed_group_activity_reply_when_owner_gate_denies() -> None:
    store = _FakeStore()
    cancelled: list[dict[str, object]] = []
    gate_calls: list[tuple[dict[str, str], str, str]] = []

    async def gate(
        owner_versions: dict[str, str],
        tenant_id: str,
        session_id: str,
    ) -> bool:
        gate_calls.append((owner_versions, tenant_id, session_id))
        return False

    async def cancel(reply_id: int, **kwargs: object) -> bool:
        cancelled.append({"reply_id": reply_id, **kwargs})
        return True

    store.cancel_claimed_reply = cancel  # type: ignore[attr-defined]
    bridge, _ = _build_bridge(
        store,
        owners_scope_execution_allowed=gate,
    )
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 12,
            "claim_token": "claim-12",
            "session_id": "room@chatroom",
            "session_kind": "group",
            "reply_text": "这条不应发送",
            "msg_type": "text",
            "delivery": _group_activity_execution_delivery(),
        }
    )

    assert gate_calls == [
        (
            {"group_activity": "0.1.0", "wxbot": "0.2.0"},
            "demo",
            "room@chatroom",
        )
    ]
    assert client.calls == []
    assert cancelled == [
        {
            "reply_id": 12,
            "tenant_id": "demo",
            "connection_id": "",
            "claim_token": "claim-12",
            "reason": "execution_owner_disabled",
        }
    ]
    persisted = store.updated_commands[-1]["delivery"]
    assert isinstance(persisted, dict)
    assert persisted["execution_owner_gate"]["before_sdk"] == {
        "allowed": False,
        "reason": "execution_owner_disabled",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate_delivery", "with_gate", "expected_reason"),
    [
        (lambda delivery: delivery, False, "execution_owner_gate_missing"),
        (
            lambda delivery: {**delivery, "execution_session_id": "other@chatroom"},
            True,
            "execution_owner_scope_mismatch",
        ),
        (
            lambda delivery: {
                **delivery,
                "execution_owner_versions": {"group_activity": "0.1.0"},
            },
            True,
            "execution_owner_versions_invalid",
        ),
    ],
)
async def test_sdk_bridge_fails_closed_for_invalid_group_activity_owner_contract(
    mutate_delivery,
    with_gate: bool,
    expected_reason: str,
) -> None:
    store = _FakeStore()
    cancelled: list[dict[str, object]] = []
    gate_calls = 0

    async def gate(
        owner_versions: dict[str, str],
        tenant_id: str,
        session_id: str,
    ) -> bool:
        nonlocal gate_calls
        _ = owner_versions, tenant_id, session_id
        gate_calls += 1
        return True

    async def cancel(reply_id: int, **kwargs: object) -> bool:
        cancelled.append({"reply_id": reply_id, **kwargs})
        return True

    store.cancel_claimed_reply = cancel  # type: ignore[attr-defined]
    bridge, _ = _build_bridge(
        store,
        owners_scope_execution_allowed=gate if with_gate else None,
    )
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 14,
            "claim_token": "claim-14",
            "session_id": "room@chatroom",
            "session_kind": "group",
            "reply_text": "契约不完整时不得发送",
            "msg_type": "text",
            "delivery": mutate_delivery(_group_activity_execution_delivery()),
        }
    )

    assert client.calls == []
    assert gate_calls == 0
    assert cancelled[0]["reason"] == expected_reason


@pytest.mark.asyncio
async def test_sdk_bridge_records_post_sdk_owner_gate_drift_without_retrying_send() -> None:
    store = _FakeStore()
    decisions = iter((True, False))

    async def gate(
        owner_versions: dict[str, str],
        tenant_id: str,
        session_id: str,
    ) -> bool:
        assert owner_versions == {"group_activity": "0.1.0", "wxbot": "0.2.0"}
        assert (tenant_id, session_id) == ("demo", "room@chatroom")
        return next(decisions)

    bridge, _ = _build_bridge(
        store,
        owners_scope_execution_allowed=gate,
    )
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 13,
            "claim_token": "claim-13",
            "session_id": "room@chatroom",
            "session_kind": "group",
            "reply_text": "远端已经确认的消息不能本地改成可重试",
            "msg_type": "text",
            "delivery": _group_activity_execution_delivery(),
        }
    )

    assert len(client.calls) == 1
    assert store.sent_ids == [13]
    assert store.failed_ids == []
    persisted = store.updated_commands[-1]["delivery"]
    assert isinstance(persisted, dict)
    assert persisted["execution_owner_gate"] == {
        "before_sdk": {"allowed": True, "reason": "execution_owners_allowed"},
        "after_sdk": {"allowed": False, "reason": "execution_owner_disabled"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("participation_status", "volatile_state", "expected_reason"),
    [
        ("may_reply", {"valid_member_answer_exists": True}, "answered_before_send"),
        ("may_reply", {"topic_changed": True}, "topic_changed_before_send"),
        (
            "may_reply",
            {"superseded_by_newer_message": True},
            "superseded_before_send",
        ),
        (
            "must_reply",
            {"valid_member_answer_exists": True},
            "obligation_answered_before_send",
        ),
        (
            "must_reply",
            {"superseded_by_newer_message": True},
            "obligation_superseded_before_send",
        ),
    ],
)
async def test_sdk_bridge_cancels_group_reply_after_semantic_revalidation(
    participation_status: str,
    volatile_state: dict[str, bool],
    expected_reason: str,
) -> None:
    store = _FakeStore()
    cancelled: list[dict[str, object]] = []

    async def analyze(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "context_available": True,
            "source_is_self_sent": False,
            "reason_codes": ["semantic_test"],
            **volatile_state,
        }

    async def snapshot(*args: object, **kwargs: object) -> dict[str, int]:
        _ = args, kwargs
        return {
            "bot_messages_last_40": 0,
            "total_messages_last_40": 5,
            "soft_replies_last_10m": 1,
            "soft_replies_last_hour": 1,
            "consecutive_bot_messages": 0,
        }

    async def policy(*args: object) -> dict[str, object]:
        _ = args
        current_hour = datetime.now(UTC).hour
        return {
            "effective_mode": "all",
            "participation_policy": {
                "timezone": "UTC",
                "quiet_start_hour": (current_hour + 1) % 24,
                "quiet_end_hour": (current_hour + 2) % 24,
            },
        }

    async def cancel(reply_id: int, **kwargs: object) -> bool:
        cancelled.append({"reply_id": reply_id, **kwargs})
        return True

    store.get_group_reply_revalidation = analyze  # type: ignore[attr-defined]
    store.get_participation_snapshot = snapshot  # type: ignore[attr-defined]
    store.get_session_policy = policy  # type: ignore[attr-defined]
    store.cancel_claimed_reply = cancel  # type: ignore[attr-defined]
    bridge, _ = _build_bridge(store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 21,
            "claim_token": "claim-21",
            "session_id": "room@chatroom",
            "reply_text": "准备发送的回复",
            "msg_type": "text",
            "participation_status": participation_status,
            "source_message_id": "question-21",
            "delivery": {
                "participation_score": 70,
                "participation_reason_codes": ["score_threshold_met"],
            },
        }
    )

    assert client.calls == []
    assert cancelled[0]["reply_id"] == 21
    assert cancelled[0]["tenant_id"] == "demo"
    assert cancelled[0]["claim_token"] == "claim-21"
    assert cancelled[0]["reason"] == expected_reason


@pytest.mark.asyncio
async def test_proactive_send_revalidation_persists_queue_outcome_and_audit() -> None:
    store = _FakeStore()
    cancelled: list[dict[str, object]] = []

    async def analyze(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "context_available": True,
            "source_is_self_sent": False,
            "valid_member_answer_exists": True,
            "reason_codes": ["newer_member_answer"],
        }

    async def snapshot(*args: object, **kwargs: object) -> dict[str, int]:
        _ = args, kwargs
        return {
            "bot_messages_last_40": 0,
            "total_messages_last_40": 5,
            "soft_replies_last_10m": 1,
            "soft_replies_last_hour": 1,
            "consecutive_bot_messages": 0,
        }

    async def cancel(reply_id: int, **kwargs: object) -> bool:
        cancelled.append({"reply_id": reply_id, **kwargs})
        return True

    class _AuditStore:
        def __init__(self) -> None:
            current_hour = datetime.now(UTC).hour
            self.document = GroupParticipationPolicyDocument(
                tenant_id="demo",
                session_id="room@chatroom",
                version=7,
                kill_switches=KillSwitches(),
                effective_enabled=True,
                policy=ParticipationPolicyValues(
                    timezone="UTC",
                    quiet_start_hour=(current_hour + 1) % 24,
                    quiet_end_hour=(current_hour + 2) % 24,
                    proactive_enabled=True,
                ),
            )
            self.events: list[dict[str, object]] = []

        async def get_group_policy(self, tenant_id: str, session_id: str):
            _ = tenant_id, session_id
            return self.document

        async def record_participation_event(self, **kwargs: object):
            self.events.append(kwargs)
            return SimpleNamespace(**kwargs)

    store.get_group_reply_revalidation = analyze  # type: ignore[attr-defined]
    store.get_participation_snapshot = snapshot  # type: ignore[attr-defined]
    store.cancel_claimed_reply = cancel  # type: ignore[attr-defined]
    social_store = _AuditStore()
    bridge, _ = _build_bridge(store, social_policy_store=social_store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 27,
            "claim_token": "claim-27",
            "session_id": "room@chatroom",
            "reply_text": "这条暖场已经不合时宜",
            "msg_type": "text",
            "trace_id": "trace-proactive-27",
            "created_at": datetime.now(UTC) - timedelta(seconds=8),
            "participation_status": "may_reply",
            "source_message_id": "source-27",
            "delivery": {
                "requested_proactive": True,
                "participation_policy_version": 7,
                "participation_score": 60,
                "participation_reason_codes": ["proactive_silence_met"],
                "send_revalidation_enabled": True,
                "humanization_stage": "proactive",
                "humanization_cohort": "proactive_canary",
                "speech_class": "scheduled",
                "speech_budget_enabled": True,
                "duplicate_guard_enabled": True,
                "duplicate_guard_outcome": "topic_guard_passed",
            },
        }
    )

    assert client.calls == []
    assert cancelled[0]["reason"] == "answered_before_send"
    persisted_delivery = store.updated_commands[0]["delivery"]
    assert isinstance(persisted_delivery, dict)
    assert persisted_delivery["send_revalidation"]["outcome"] == "cancelled_before_sdk"
    assert persisted_delivery["send_revalidation"]["final_status"] == "cancel"
    assert len(social_store.events) == 1
    event = social_store.events[0]
    assert event["runtime_stage"] == "revalidation"
    assert event["delivery_stage"] == "cancelled_before_sdk"
    assert event["trace_id"] == "trace-proactive-27"
    assert event["decision"].status.value == "cancel"
    summary = event["signal_summary"]
    assert summary["humanization_cohort"] == "proactive_canary"
    assert summary["actual_delay_seconds"] >= 8
    assert summary["duplicate_guard_enabled"] is True
    assert summary["duplicate_guard_outcome"] == "topic_guard_passed"
    assert summary["valid_member_answer_exists"] is True


@pytest.mark.asyncio
async def test_sdk_bridge_reschedules_deferred_reply_without_marking_failure() -> None:
    store = _FakeStore()
    rescheduled: list[dict[str, object]] = []

    async def analyze(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "context_available": True,
            "source_is_self_sent": False,
            "reason_codes": ["semantic_test"],
        }

    async def snapshot(*args: object, **kwargs: object) -> dict[str, int]:
        _ = args, kwargs
        return {
            "bot_messages_last_40": 0,
            "total_messages_last_40": 10,
            "soft_replies_last_10m": 2,
            "soft_replies_last_hour": 2,
            "consecutive_bot_messages": 0,
        }

    async def policy(*args: object) -> dict[str, object]:
        _ = args
        current_hour = datetime.now(UTC).hour
        return {
            "effective_mode": "all",
            "participation_policy": {
                "timezone": "UTC",
                "quiet_start_hour": (current_hour + 1) % 24,
                "quiet_end_hour": (current_hour + 2) % 24,
            },
        }

    async def reschedule(reply_id: int, **kwargs: object) -> bool:
        rescheduled.append({"reply_id": reply_id, **kwargs})
        return True

    store.get_group_reply_revalidation = analyze  # type: ignore[attr-defined]
    store.get_participation_snapshot = snapshot  # type: ignore[attr-defined]
    store.get_session_policy = policy  # type: ignore[attr-defined]
    store.reschedule_claimed_reply = reschedule  # type: ignore[attr-defined]
    bridge, _ = _build_bridge(store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 24,
            "claim_token": "claim-24",
            "session_id": "room@chatroom",
            "reply_text": "预算恢复后再发",
            "msg_type": "text",
            "participation_status": "defer",
            "source_message_id": "question-24",
            "delivery": {
                "participation_score": 70,
                "participation_reason_codes": ["soft_budget_10m_exhausted"],
            },
        }
    )

    assert client.calls == []
    assert store.failed_ids == []
    assert rescheduled[0]["reply_id"] == 24
    assert rescheduled[0]["claim_token"] == "claim-24"
    assert rescheduled[0]["reason"] == "soft_budget_10m_exhausted_at_send"
    assert rescheduled[0]["not_before"] > datetime.now(UTC)


@pytest.mark.asyncio
async def test_sdk_bridge_prepares_shared_speech_slot_immediately_before_send() -> None:
    store = _FakeStore()
    phases: list[str] = []

    async def prepare(reply: dict, **kwargs: object) -> bool:
        phases.append("speech_prepared")
        assert kwargs == {
            "tenant_id": "demo",
            "connection_id": "",
            "claim_token": "claim-25",
        }
        reply["delivery"]["speech_ledger"] = {"reservation_id": "reserved-25"}
        return True

    class _OrderedClient(_FakeClient):
        async def post(self, url: str, json: dict):
            assert phases == ["speech_prepared"]
            phases.append("sdk_send")
            return await super().post(url, json)

    store.prepare_claimed_reply_speech = prepare  # type: ignore[attr-defined]
    bridge, _ = _build_bridge(store)
    bridge._client = _OrderedClient()

    await bridge._send_one_reply(
        {
            "id": 25,
            "claim_token": "claim-25",
            "session_id": "room@chatroom",
            "session_kind": "group",
            "reply_text": "现在可以发送",
            "msg_type": "text",
            "delivery": {"speech_budget_enabled": True},
        }
    )

    assert phases == ["speech_prepared", "sdk_send"]
    assert store.sent_ids == [25]
    assert store.failed_ids == []


@pytest.mark.asyncio
async def test_sdk_bridge_cancels_when_send_time_speech_budget_denies() -> None:
    store = _FakeStore()
    cancelled: list[dict[str, object]] = []

    async def prepare(*args: object, **kwargs: object) -> bool:
        _ = args, kwargs
        raise GroupSpeechBudgetExceeded(
            "budget_10m",
            output_kind="ordinary",
            idempotency_key="reply-26",
        )

    async def cancel(reply_id: int, **kwargs: object) -> bool:
        cancelled.append({"reply_id": reply_id, **kwargs})
        return True

    store.prepare_claimed_reply_speech = prepare  # type: ignore[attr-defined]
    store.cancel_claimed_reply = cancel  # type: ignore[attr-defined]
    bridge, _ = _build_bridge(store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 26,
            "claim_token": "claim-26",
            "session_id": "room@chatroom",
            "session_kind": "group",
            "reply_text": "这条被预算拦截",
            "msg_type": "text",
            "delivery": {"speech_budget_enabled": True},
        }
    )

    assert client.calls == []
    assert store.failed_ids == []
    assert cancelled == [
        {
            "reply_id": 26,
            "tenant_id": "demo",
            "connection_id": "",
            "claim_token": "claim-26",
            "reason": "budget_10m",
        }
    ]


@pytest.mark.asyncio
async def test_sdk_bridge_revalidation_dependency_failure_retries_without_sending() -> None:
    store = _FakeStore()

    async def unavailable(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        raise RuntimeError("database unavailable")

    async def unused(*args: object, **kwargs: object) -> dict[str, object]:
        _ = args, kwargs
        return {}

    store.get_group_reply_revalidation = unavailable  # type: ignore[attr-defined]
    store.get_participation_snapshot = unused  # type: ignore[attr-defined]
    store.get_session_policy = unused  # type: ignore[attr-defined]
    bridge, _ = _build_bridge(store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 22,
            "claim_token": "claim-22",
            "session_id": "room@chatroom",
            "reply_text": "不能在未校验时发送",
            "msg_type": "text",
            "participation_status": "must_reply",
            "source_message_id": "question-22",
            "delivery": {"force_send": True},
        }
    )

    assert client.calls == []
    assert store.failed_ids == [(22, "sdk request failed: RuntimeError")]


class _PublicSocialPolicyStore:
    def __init__(self, document: GroupParticipationPolicyDocument) -> None:
        self.document = document
        self.calls: list[tuple[str, str]] = []

    async def get_group_policy(
        self,
        tenant_id: str,
        session_id: str,
    ) -> GroupParticipationPolicyDocument:
        self.calls.append((tenant_id, session_id))
        return self.document


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "queued_version", "expected_reason"),
    [
        (
            GroupParticipationPolicyDocument(
                tenant_id="demo",
                session_id="room@chatroom",
                version=8,
                kill_switches=KillSwitches(),
                effective_enabled=True,
                policy=ParticipationPolicyValues(),
            ),
            7,
            "participation_policy_version_changed",
        ),
        (
            GroupParticipationPolicyDocument(
                tenant_id="demo",
                session_id="room@chatroom",
                version=7,
                kill_switches=KillSwitches(group_enabled=False),
                effective_enabled=False,
                policy=ParticipationPolicyValues(),
            ),
            7,
            "participation_disabled_at_send",
        ),
    ],
)
async def test_sdk_bridge_public_policy_fences_stale_or_disabled_reply(
    document: GroupParticipationPolicyDocument,
    queued_version: int,
    expected_reason: str,
) -> None:
    store = _FakeStore()
    cancelled: list[dict[str, object]] = []

    async def cancel(reply_id: int, **kwargs: object) -> bool:
        cancelled.append({"reply_id": reply_id, **kwargs})
        return True

    store.cancel_claimed_reply = cancel  # type: ignore[attr-defined]
    social_store = _PublicSocialPolicyStore(document)
    bridge, _ = _build_bridge(store, social_policy_store=social_store)
    client = _FakeClient()
    bridge._client = client

    await bridge._send_one_reply(
        {
            "id": 23,
            "claim_token": "claim-23",
            "session_id": "cx1:c:managed-room@chatroom",
            "reply_text": "旧策略下生成的回复不能发",
            "msg_type": "text",
            "participation_status": "must_reply",
            "source_message_id": "question-23",
            "delivery": {
                "external_conversation_id": "room@chatroom",
                "participation_policy_version": queued_version,
                "send_revalidation_enabled": True,
            },
        }
    )

    assert social_store.calls == [("demo", "room@chatroom")]
    assert client.calls == []
    assert cancelled[0]["reason"] == expected_reason


@pytest.mark.asyncio
async def test_sdk_bridge_keeps_cursor_when_sdk_bounds_are_empty() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.cursor_key] = "77"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 0, "max_event_id": 0, "max_stream_id": 0}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    cursor = await bridge._reconcile_ingest_cursor(77)

    assert cursor == 77
    assert bridge._redis.values[bridge.cursor_key] == "77"


@pytest.mark.asyncio
async def test_sdk_bridge_resets_stale_ingest_cursor_when_sdk_rebuilt() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.cursor_key] = "77"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 3, "max_event_id": 0, "max_stream_id": 5}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    cursor = await bridge._reconcile_ingest_cursor(77)

    assert cursor == 0
    assert bridge._redis.values[bridge.cursor_key] == "0"


@pytest.mark.asyncio
async def test_sdk_bridge_reconcile_all_cursors_resets_stale_cursors_and_requests_reconnect() -> (
    None
):
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.cursor_key] = "77"
    bridge._redis.values[bridge.legacy_cursor_key] = "15"
    bridge._redis.values[bridge.event_cursor_key] = "21"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 3, "max_event_id": 9, "max_stream_id": 5}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    reset = await bridge._reconcile_all_cursors()

    assert reset is True
    assert bridge._redis.values[bridge.cursor_key] == "0"
    assert bridge._redis.values[bridge.legacy_cursor_key] == "0"
    assert bridge._redis.values[bridge.event_cursor_key] == "0"
    assert bridge._cursor_reset_generation == 1


@pytest.mark.asyncio
async def test_sdk_bridge_reconcile_all_cursors_keeps_cursors_when_sdk_bounds_are_empty() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.cursor_key] = "77"
    bridge._redis.values[bridge.legacy_cursor_key] = "15"
    bridge._redis.values[bridge.event_cursor_key] = "21"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 0, "max_event_id": 0, "max_stream_id": 0}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    reset = await bridge._reconcile_all_cursors()

    assert reset is False
    assert bridge._redis.values[bridge.cursor_key] == "77"
    assert bridge._redis.values[bridge.legacy_cursor_key] == "15"
    assert bridge._redis.values[bridge.event_cursor_key] == "21"
    assert bridge._cursor_reset_generation == 0


@pytest.mark.asyncio
async def test_sdk_bridge_reconnect_request_cancels_ingest_and_event_tasks_only() -> None:
    bridge, _ = _build_bridge()

    async def _sleep_forever() -> None:
        await asyncio.sleep(60)

    ingest = asyncio.create_task(_sleep_forever(), name="wxbot-bridge-ingest")
    event = asyncio.create_task(_sleep_forever(), name="wxbot-bridge-events")
    send = asyncio.create_task(_sleep_forever(), name="wxbot-bridge-send")
    bridge._tasks = [ingest, event, send]

    bridge._request_stream_reconnect(reason="test")
    await asyncio.sleep(0)

    assert ingest.cancelled()
    assert event.cancelled()
    assert not send.cancelled()
    send.cancel()
    await asyncio.gather(send, return_exceptions=True)


@pytest.mark.asyncio
async def test_sdk_bridge_self_heals_missing_leader_key_by_restoring_lease_and_tasks() -> None:
    bridge, _ = _build_bridge()
    bridge._is_leader = True
    bridge._sdk_online = True
    bridge._sdk_auth_state = "ok"
    bridge._diagnostics["leader_present"] = True

    for _ in range(SELF_HEAL_RECURRENCE_THRESHOLD - 1):
        assert await bridge._check_leader_health() is False

    healed = await bridge._check_leader_health()

    assert healed is True
    assert bridge._is_leader is True
    assert bridge._diagnostics["last_self_heal_reason"] == "leader_missing"
    assert bridge._diagnostics["leader_present"] is True
    assert bridge.leader_key in bridge._redis.values
    assert not bridge._task_snapshot()["missing"]
    for task in bridge._tasks:
        task.cancel()
    await asyncio.gather(*bridge._tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_sdk_bridge_self_heals_missing_task_idempotently() -> None:
    bridge, _ = _build_bridge()
    bridge._is_leader = True
    bridge._sdk_online = True
    bridge._sdk_auth_state = "ok"
    bridge._diagnostics["leader_present"] = True
    bridge._redis.values[bridge.leader_key] = bridge._leader_token

    async def _sleep_forever() -> None:
        await asyncio.sleep(60)

    bridge._tasks = [
        asyncio.create_task(_sleep_forever(), name="wxbot-bridge-ingest"),
        asyncio.create_task(_sleep_forever(), name="wxbot-bridge-events"),
    ]
    try:
        for _ in range(SELF_HEAL_RECURRENCE_THRESHOLD - 1):
            assert await bridge._check_task_health() is False

        healed = await bridge._check_task_health()
        task_names = [task.get_name() for task in bridge._tasks if not task.done()]

        assert healed is True
        assert task_names.count("wxbot-bridge-ingest") == 1
        assert task_names.count("wxbot-bridge-events") == 1
        assert "wxbot-bridge-send" in task_names
        assert "wxbot-bridge-pending-media" in task_names
        assert "wxbot-bridge-cursor-reconcile" in task_names
        assert bridge._diagnostics["last_self_heal_reason"] == "task_missing"
    finally:
        for task in bridge._tasks:
            task.cancel()
        await asyncio.gather(*bridge._tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_sdk_bridge_repeated_self_heal_does_not_duplicate_tasks() -> None:
    bridge, _ = _build_bridge()
    bridge._is_leader = True
    bridge._sdk_online = True
    bridge._sdk_auth_state = "ok"
    bridge._diagnostics["leader_present"] = True

    async def _sleep_forever() -> None:
        await asyncio.sleep(60)

    bridge._tasks = [
        asyncio.create_task(_sleep_forever(), name="wxbot-bridge-ingest"),
        asyncio.create_task(_sleep_forever(), name="wxbot-bridge-events"),
    ]
    try:
        assert await bridge._self_heal_bridge(reason="task_missing") is True
        bridge._last_self_heal_by_reason["task_missing"] -= SELF_HEAL_COOLDOWN_SECONDS
        assert await bridge._self_heal_bridge(reason="task_missing") is True
        task_names = [task.get_name() for task in bridge._tasks if not task.done()]

        assert sorted(task_names) == sorted(set(task_names))
        assert not bridge._task_snapshot()["missing"]
    finally:
        for task in bridge._tasks:
            task.cancel()
        await asyncio.gather(*bridge._tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_sdk_bridge_cursor_lag_stall_triggers_stream_reconnect_once_per_cooldown() -> None:
    bridge, _ = _build_bridge()
    bridge._sdk_online = True
    bridge._sdk_auth_state = "ok"
    bridge._redis.values[bridge.cursor_key] = "10"
    bridge._redis.values[bridge.legacy_cursor_key] = "10"
    bridge._redis.values[bridge.event_cursor_key] = "0"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 30, "max_event_id": 0, "max_stream_id": 30}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    await bridge._reconcile_all_cursors()
    assert bridge._cursor_reset_generation == 0
    assert bridge._cursor_stall_count == 1

    await bridge._reconcile_all_cursors()
    assert bridge._cursor_reset_generation == 1
    assert bridge._diagnostics["last_self_heal_reason"] == "bridge_ingest_stalled"

    await bridge._reconcile_all_cursors()
    assert bridge._cursor_reset_generation == 1


@pytest.mark.asyncio
async def test_sdk_bridge_cursor_progress_resets_lag_stall_decision() -> None:
    bridge, _ = _build_bridge()
    bridge._sdk_online = True
    bridge._sdk_auth_state = "ok"
    bridge._redis.values[bridge.cursor_key] = "10"
    bridge._redis.values[bridge.legacy_cursor_key] = "10"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 30, "max_event_id": 0, "max_stream_id": 30}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    await bridge._reconcile_all_cursors()
    bridge._redis.values[bridge.cursor_key] = "15"
    bridge._redis.values[bridge.legacy_cursor_key] = "15"
    await bridge._reconcile_all_cursors()

    assert bridge._cursor_stall_count == 1
    assert bridge._cursor_reset_generation == 0


@pytest.mark.asyncio
async def test_sdk_bridge_self_heal_cooldown_is_idempotent() -> None:
    bridge, _ = _build_bridge()
    bridge._sdk_online = True
    bridge._sdk_auth_state = "ok"

    first = await bridge._self_heal_bridge(reason="bridge_ingest_stalled")
    second = await bridge._self_heal_bridge(reason="bridge_ingest_stalled")

    assert first is True
    assert second is False
    assert bridge._cursor_reset_generation == 1
    bridge._last_self_heal_by_reason["bridge_ingest_stalled"] -= SELF_HEAL_COOLDOWN_SECONDS
    third = await bridge._self_heal_bridge(reason="bridge_ingest_stalled")
    assert third is True
    assert bridge._cursor_reset_generation == 2


@pytest.mark.asyncio
async def test_sdk_bridge_status_exposes_task_leader_cursor_diagnostics() -> None:
    bridge, store = _build_bridge()
    bridge._is_leader = True
    bridge._sdk_online = True
    bridge._sdk_auth_state = "ok"
    bridge._diagnostics["leader_present"] = False
    bridge._diagnostics["cursor"] = {"max_lag": 20}
    bridge._redis.values[bridge.cursor_key] = "10"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 0, "max_event_id": 0, "max_stream_id": 0}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]
    await bridge._publish_runtime_status()

    status = await read_bridge_runtime_status(
        bridge._redis,
        store,  # type: ignore[arg-type]
        bridge._settings,
        "demo",
    )

    assert status["diagnostics"]["leader_missing"] is True
    assert "wxbot-bridge-ingest" in status["diagnostics"]["tasks_missing"]
    assert status["diagnostics"]["cursor"]["cursor"] == 10
    assert status["tasks"]["missing"]
    assert status["bridge_leader"] is False
    assert status["leader"]["ttl"] == -2


@pytest.mark.asyncio
async def test_sdk_bridge_status_watchdog_detects_cursor_lag_when_reconcile_task_missing() -> None:
    bridge, _ = _build_bridge()
    bridge._is_leader = True
    bridge._sdk_online = True
    bridge._sdk_auth_state = "ok"
    bridge._diagnostics["leader_present"] = True
    bridge._redis.values[bridge.leader_key] = bridge._leader_token
    bridge._redis.values[bridge.cursor_key] = "10"
    bridge._redis.values[bridge.legacy_cursor_key] = "10"
    bridge._redis.values[bridge.event_cursor_key] = "0"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 30, "max_event_id": 0, "max_stream_id": 30}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    await bridge._status_watchdog()
    assert bridge._cursor_stall_count == 1
    assert bridge._cursor_reset_generation == 0

    await bridge._status_watchdog()
    assert bridge._cursor_reset_generation == 1
    assert bridge._diagnostics["last_self_heal_reason"] == "bridge_ingest_stalled"


@pytest.mark.asyncio
async def test_sdk_bridge_resets_stale_legacy_ingest_cursor_against_inbound_bounds() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.legacy_cursor_key] = "15"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 3, "max_event_id": 0, "max_stream_id": 30}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    cursor = await bridge._reconcile_legacy_ingest_cursor(15)

    assert cursor == 0
    assert bridge._redis.values[bridge.legacy_cursor_key] == "0"


@pytest.mark.asyncio
async def test_sdk_bridge_keeps_empty_ingest_cursor_for_rebuilt_sdk_queue() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.cursor_key] = "0"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 3, "max_event_id": 0, "max_stream_id": 5}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    cursor = await bridge._reconcile_ingest_cursor(0)

    assert cursor == 0
    assert bridge._redis.values[bridge.cursor_key] == "0"


@pytest.mark.asyncio
async def test_sdk_bridge_resets_stale_event_cursor_when_sdk_rebuilt() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.event_cursor_key] = "77"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 0, "max_event_id": 9, "max_stream_id": 0}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    cursor = await bridge._reconcile_event_cursor(77)

    assert cursor == 0
    assert bridge._redis.values[bridge.event_cursor_key] == "0"


@pytest.mark.asyncio
async def test_sdk_bridge_keeps_unified_event_cursor_within_stream_bounds() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.event_cursor_key] = "493"

    bounds = {"max_inbound_id": 346, "max_event_id": 2, "max_stream_id": 493}
    cursor = await bridge._reconcile_event_cursor(493, bounds)

    assert cursor == 493
    assert bridge._redis.values[bridge.event_cursor_key] == "493"


@pytest.mark.asyncio
async def test_sdk_bridge_keeps_empty_event_cursor_for_rebuilt_sdk_queue() -> None:
    bridge, _ = _build_bridge()
    bridge._redis.values[bridge.event_cursor_key] = "0"

    async def _fake_sdk_bounds() -> dict[str, int]:
        return {"max_inbound_id": 0, "max_event_id": 9, "max_stream_id": 0}

    bridge._sdk_queue_bounds = _fake_sdk_bounds  # type: ignore[method-assign]

    cursor = await bridge._reconcile_event_cursor(0)

    assert cursor == 0
    assert bridge._redis.values[bridge.event_cursor_key] == "0"

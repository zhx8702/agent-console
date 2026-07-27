from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.channel import canonical_conversation_id
from plugins.tibo_reset.client import TiboResetEntry
from plugins.tibo_reset.service import TiboResetService, delivery_command_id
from plugins.tibo_reset.store import TiboResetStore


def _tweet(tweet_id: str = "2077607697487188198") -> TiboResetEntry:
    return TiboResetEntry(
        tweet_id=tweet_id,
        text="Another reset for our Codex and ChatGPT Work users.\n\nFull original text.",
        created_at="2026-07-16T04:14:09Z",
        source_url=f"https://x.com/thsottiaux/status/{tweet_id}",
        confidence=1,
        evidence="Another reset for our Codex and ChatGPT Work users.",
        reset_type="weekly_usage",
        beneficiaries="everyone",
    )


class _FakeClient:
    api_url = "https://tibo-reset.test/api/resets"

    def __init__(self, entries: list[TiboResetEntry]) -> None:
        self.entries = entries

    async def fetch_resets(self) -> list[TiboResetEntry]:
        return list(self.entries)


class _FakeStore:
    def __init__(
        self,
        candidates: list[dict] | None = None,
        *,
        on_claim=None,
    ) -> None:
        self.candidates = candidates or []
        self.on_claim = on_claim
        self.queued: list[dict] = []
        self.failed: list[dict] = []
        self.claimed: list[dict] = []
        self.poll_errors: list[str] = []
        self.deliverable_calls = 0
        self.expire_calls: list[int] = []

    async def expire_stale_queued(self, *, max_age_seconds: int = 300):
        self.expire_calls.append(max_age_seconds)
        return {
            "delivery_count": 3,
            "dlq_count": 2,
            "sent_count": 1,
            "reply_count": 2,
        }

    async def ingest_feed(self, entries):
        return {
            "baseline": not self.candidates,
            "fetched": len(entries),
            "inserted": len(entries),
            "eligible_inserted": len(self.candidates),
        }

    async def mark_poll_failed(self, error: str) -> None:
        self.poll_errors.append(error)

    async def list_enabled_scopes(self, *, limit: int = 500):
        return [
            {
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "enabled_at": "2026-07-16T00:00:00+00:00",
            }
        ]

    async def list_deliverable(self, **kwargs):
        self.deliverable_calls += 1
        return list(self.candidates)

    async def claim_delivery(self, **kwargs):
        self.claimed.append(kwargs)
        if self.on_claim is not None:
            self.on_claim()
        return {"id": len(self.claimed), **kwargs}

    async def mark_delivery_queued(self, delivery_id: int, *, reply_queue_id: int | None):
        self.queued.append({"delivery_id": delivery_id, "reply_queue_id": reply_queue_id})

    async def mark_delivery_failed(self, delivery_id: int, *, error: str):
        self.failed.append({"delivery_id": delivery_id, "error": error})

    async def runtime_status(self):
        return {"initialized": True}


class _FakeOutbound:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[dict] = []

    async def send_text(self, target, text, options=None):
        self.sent.append({"target": target, "text": text, "options": options})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(metadata={"reply_queue_id": 42})


class _ScopeGate:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(
        self,
        owner: str,
        *,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        self.calls.append((owner, tenant_id, session_id))
        return self.enabled


def _candidate(entry: TiboResetEntry) -> dict:
    return {
        "tweet_id": entry.tweet_id,
        "text": entry.text,
        "source_url": entry.source_url,
        "reset_type": entry.reset_type,
        "beneficiaries": entry.beneficiaries,
    }


@pytest.mark.asyncio
async def test_first_sync_only_builds_baseline_without_sending_history() -> None:
    tweet = _tweet()
    store = _FakeStore(candidates=[])
    outbound = _FakeOutbound()
    service = TiboResetService(
        store=store,
        client=_FakeClient([tweet]),
        outbound=outbound,
        scope_execution_allowed=_ScopeGate(),
    )

    result = await service.poll_once()

    assert result["ingest"]["baseline"] is True
    assert result["queued"] == 0
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_new_verified_tweet_sends_full_original_and_source_link() -> None:
    tweet = _tweet()
    store = _FakeStore(candidates=[_candidate(tweet)])
    outbound = _FakeOutbound()
    service = TiboResetService(
        store=store,
        client=_FakeClient([tweet]),
        outbound=outbound,
        scope_execution_allowed=_ScopeGate(),
    )

    result = await service.poll_once()

    assert result["queued"] == 1
    assert outbound.sent[0]["text"] == f"{tweet.text}\n\n原文: {tweet.source_url}"
    assert outbound.sent[0]["target"].session_id == "room@chatroom"
    assert outbound.sent[0]["target"].session_kind == "group"
    expected_command_id = delivery_command_id("demo", "room@chatroom", tweet.tweet_id)
    assert outbound.sent[0]["options"].idempotency_key == expected_command_id
    expires_at = datetime.fromisoformat(outbound.sent[0]["options"].delivery_metadata["expires_at"])
    assert 295 <= (expires_at - datetime.now(UTC)).total_seconds() <= 300
    assert store.claimed[0]["command_id"] == expected_command_id
    assert store.queued == [{"delivery_id": 1, "reply_queue_id": 42}]
    assert store.expire_calls == [300]
    assert result["dlq"] == 2
    assert result["settled_sent"] == 1
    assert result["expired_replies"] == 2


@pytest.mark.asyncio
async def test_managed_connection_uses_canonical_budget_scope_and_scheduled_delivery() -> None:
    tweet = _tweet()
    store = _FakeStore(candidates=[_candidate(tweet)])
    outbound = _FakeOutbound()
    connection_id = "wechat-primary"
    service = TiboResetService(
        store=store,
        client=_FakeClient([tweet]),
        outbound=outbound,
        scope_execution_allowed=_ScopeGate(),
        connection_id=connection_id,
    )

    result = await service.poll_once()

    assert result["queued"] == 1
    target = outbound.sent[0]["target"]
    assert target.connection_id == connection_id
    assert target.external_conversation_id == "room@chatroom"
    assert target.session_id == canonical_conversation_id(
        connection_id,
        "room@chatroom",
    )
    assert target.canonical_conversation_id == target.session_id
    delivery = outbound.sent[0]["options"].delivery_metadata
    assert delivery["source"] == "tibo_reset"
    assert delivery["speech_output_kind"] == "report"
    assert delivery["speech_class"] == "scheduled"
    assert delivery["speech_budget_enabled"] is False
    assert delivery["deferred_candidate"] is True


@pytest.mark.asyncio
async def test_stale_failed_and_running_notifications_expire_from_creation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.tibo_reset.store as store_module

    class _Result:
        returns_rows = True

        @staticmethod
        def fetchall():
            return [
                SimpleNamespace(
                    _mapping={
                        "delivery_count": 2,
                        "dlq_count": 2,
                        "sent_count": 0,
                        "reply_count": 0,
                    }
                )
            ]

    class _Connection:
        def __init__(self) -> None:
            self.sql = ""
            self.params: dict = {}

        async def execute(self, statement, params):
            self.sql = str(statement)
            self.params = dict(params)
            return _Result()

    connection = _Connection()

    @asynccontextmanager
    async def _connection_context():
        yield connection

    monkeypatch.setattr(store_module, "_write_connection", _connection_context)
    store = TiboResetStore(SimpleNamespace())

    result = await store.expire_stale_queued(max_age_seconds=300)

    assert result == {
        "delivery_count": 2,
        "dlq_count": 2,
        "sent_count": 0,
        "reply_count": 0,
    }
    assert "status IN ('queued', 'failed', 'running')" in connection.sql
    assert "COALESCE(queued_at, created_at)" in connection.sql
    assert "updated_at, created_at" not in connection.sql
    assert connection.params == {"max_age_seconds": 300}


@pytest.mark.asyncio
async def test_delivery_failure_is_persisted_for_retry() -> None:
    tweet = _tweet()
    store = _FakeStore(candidates=[_candidate(tweet)])
    outbound = _FakeOutbound(RuntimeError("wxbot unavailable"))
    service = TiboResetService(
        store=store,
        client=_FakeClient([tweet]),
        outbound=outbound,
        scope_execution_allowed=_ScopeGate(),
    )

    result = await service.poll_once()

    assert result["status"] == "partial"
    assert result["failed"] == 1
    assert store.failed == [{"delivery_id": 1, "error": "wxbot unavailable"}]


@pytest.mark.asyncio
async def test_disabled_session_is_gated_before_delivery_claim() -> None:
    tweet = _tweet()
    gate = _ScopeGate(enabled=False)
    store = _FakeStore(candidates=[_candidate(tweet)])
    outbound = _FakeOutbound()
    service = TiboResetService(
        store=store,
        client=_FakeClient([tweet]),
        outbound=outbound,
        scope_execution_allowed=gate,
    )

    result = await service.poll_once()

    assert result["scope_denied"] == 1
    assert store.deliverable_calls == 0
    assert store.claimed == []
    assert outbound.sent == []
    assert gate.calls == [("tibo_reset", "demo", "room@chatroom")]


@pytest.mark.asyncio
async def test_missing_tibo_scope_gate_fails_closed_before_claim() -> None:
    tweet = _tweet()
    store = _FakeStore(candidates=[_candidate(tweet)])
    outbound = _FakeOutbound()
    service = TiboResetService(
        store=store,
        client=_FakeClient([tweet]),
        outbound=outbound,
    )

    result = await service.poll_once()

    assert result["scope_denied"] == 1
    assert store.claimed == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_midflight_scope_disable_defers_claim_before_send() -> None:
    tweet = _tweet()
    gate = _ScopeGate()
    store = _FakeStore(
        candidates=[_candidate(tweet)],
        on_claim=lambda: setattr(gate, "enabled", False),
    )
    outbound = _FakeOutbound()
    service = TiboResetService(
        store=store,
        client=_FakeClient([tweet]),
        outbound=outbound,
        scope_execution_allowed=gate,
    )

    result = await service.poll_once()

    assert result["claimed"] == 1
    assert result["queued"] == 0
    assert result["deferred"] == 1
    assert store.failed == [{"delivery_id": 1, "error": "scope_execution_denied"}]
    assert outbound.sent == []
    assert gate.calls == [
        ("tibo_reset", "demo", "room@chatroom"),
        ("tibo_reset", "demo", "room@chatroom"),
        ("tibo_reset", "demo", "room@chatroom"),
    ]

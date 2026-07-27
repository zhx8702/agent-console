from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.memory.store as memory_store_module
from app.common.types import ChatResponse
from plugins.memory.graph_extractor import MemoryGraphLLMExtractor
from plugins.memory.store import (
    MemoryStore,
    _job_idempotency_key,
    _llm_job_scope_filter_sql,
    _semantic_key,
)


class _LLM:
    def __init__(self, content: str | None = None, *, exc: Exception | None = None) -> None:
        self.content = content or "{}"
        self.exc = exc
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return ChatResponse(content=self.content)


def _settings(**kwargs):
    data = {
        "memory_llm_extraction_enabled": True,
        "memory_llm_extraction_timeout_seconds": 0.2,
        "memory_llm_extraction_max_actions": 4,
        "memory_llm_extraction_min_confidence": 0.75,
        "memory_llm_extraction_job_enabled": True,
        "memory_llm_extraction_job_drain_enabled": False,
        "memory_llm_extraction_job_scope_allowlist": "",
        "memory_llm_extraction_job_drain_batch_size": 5,
        "memory_llm_extraction_job_drain_max_claims": 0,
        "memory_llm_extraction_job_max_attempts": 3,
        "memory_llm_extraction_job_backoff_seconds": 1.0,
        "memory_llm_extraction_job_timeout_seconds": 0.2,
        "memory_llm_extraction_job_lock_ttl_seconds": 30.0,
        "memory_graph_llm_extraction_enabled": False,
        "memory_graph_llm_extraction_timeout_seconds": 0.2,
        "memory_graph_llm_extraction_max_actions": 16,
        "memory_graph_llm_extraction_max_entities": 8,
        "memory_graph_llm_extraction_max_facts": 4,
        "memory_graph_llm_extraction_max_episodes": 2,
        "memory_graph_llm_extraction_min_confidence": 0.8,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def _job(**kwargs):
    data = {
        "id": 9,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "s1",
        "source_event_id": 7,
        "source_trace_id": "trace-1",
        "status": "running",
        "attempts": 0,
        "max_attempts": 3,
    }
    data.update(kwargs)
    return data


async def _allow_memory_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


class _GraphWriteFake:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
        self.memory_items: dict[int, dict[str, Any]] = {}
        self.facts: dict[int, dict[str, Any]] = {}
        self.episodes: dict[str, dict[str, Any]] = {}
        self.next_entity_id = 1
        self.next_memory_item_id = 20
        self.execution_connections: list[tuple[str, object | None]] = []

    async def exec(self, sql: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        self.execution_connections.append(
            (sql, memory_store_module._ACTIVE_MUTATION_CONNECTION.get())
        )
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            item = self.memory_items.get(int(params["id"]))
            return [self._row(item)] if item else []

        if "FROM plugin_memory_item" in sql and "normalized_key = :normalized_key" in sql:
            rows = [
                self._row(item)
                for item in self.memory_items.values()
                if item.get("tenant_id") == params["tid"]
                and item.get("channel") == params["channel"]
                and item.get("source_key") == params["source_key"]
                and item.get("user_id") == params["uid"]
                and item.get("scope_type") == params["scope_type"]
                and item.get("session_id", "") == params["sid"]
                and item.get("normalized_key") == params["normalized_key"]
                and item.get("deleted_at") is None
            ]
            return rows[: int(params.get("lim") or 20)]

        if "SELECT id FROM plugin_memory_item" in sql:
            for item in self.memory_items.values():
                if (
                    item.get("tenant_id") == params["tid"]
                    and item.get("channel") == params["channel"]
                    and item.get("source_key") == params["source_key"]
                    and item.get("user_id") == params["uid"]
                    and item.get("scope_type") == params["scope_type"]
                    and item.get("session_id", "") == params["sid"]
                    and item.get("source_type") == params["source_type"]
                    and item.get("normalized_key") == params["normalized_key"]
                    and item.get("deleted_at") is None
                ):
                    return [{"id": item["id"]}]
            return []

        if "INSERT INTO plugin_memory_item" in sql:
            item_id = self.next_memory_item_id
            self.next_memory_item_id += 1
            row = self._row(
                {
                    "id": item_id,
                    "tenant_id": params["tid"],
                    "channel": params["channel"],
                    "source_key": params["source_key"],
                    "user_id": params["uid"],
                    "session_id": params["sid"],
                    "scope_type": params["scope_type"],
                    "source_type": params["source_type"],
                    "memory_type": params["memory_type"],
                    "content": params["content"],
                    "value_json": params["value_json"],
                    "normalized_key": params["normalized_key"],
                    "confidence": params["confidence"],
                    "status": params["status"],
                    "pinned": params["pinned"],
                    "priority": params["priority"],
                    "sensitivity": params["sensitivity"],
                    "source_event_id": params.get("source_event_id"),
                    "source_trace_id": params.get("source_trace_id") or "",
                    "original_text": params.get("original_text") or "",
                    "deleted_at": None,
                }
            )
            self.memory_items[item_id] = row
            return [row]

        if "UPDATE plugin_memory_item SET content = :content" in sql:
            item = self.memory_items[int(params["id"])]
            item.update(
                {
                    "content": params["content"],
                    "value_json": params["value_json"],
                    "memory_type": params["memory_type"],
                    "confidence": max(
                        float(item.get("confidence") or 0.0), float(params["confidence"])
                    ),
                    "status": params["status"],
                    "pinned": bool(item.get("pinned")) or bool(params["pinned"]),
                    "priority": max(int(item.get("priority") or 0), int(params["priority"])),
                    "sensitivity": params["sensitivity"],
                }
            )
            return []

        if "UPDATE plugin_memory_item SET status = 'invalidated'" in sql:
            item = self.memory_items.get(int(params["id"]))
            if (
                item is None
                or item.get("tenant_id") != params.get("tenant_id")
                or item.get("channel") != params.get("channel")
                or item.get("user_id") != params.get("user_id")
                or item.get("status") != params.get("status")
                or item.get("source_type") == "manual"
                or bool(item.get("pinned"))
                or item.get("deleted_at") is not None
            ):
                return []
            item["status"] = "invalidated"
            item["value_json"] = params["value_json"]
            return [{"id": item["id"]}]

        if "INSERT INTO plugin_memory_entity" in sql:
            key = (
                params["tid"],
                params["channel"],
                params["source_key"],
                params["uid"],
                params["entity_type"],
                params["normalized_name"],
            )
            if key not in self.entities:
                self.entities[key] = {"id": self.next_entity_id, **params}
                self.next_entity_id += 1
            return [{"id": self.entities[key]["id"]}]

        if "INSERT INTO plugin_memory_fact" in sql:
            existing = self.facts.get(int(params["memory_item_id"]), {})
            self.facts[int(params["memory_item_id"])] = {
                **existing,
                **params,
                "confidence": max(
                    float(existing.get("confidence") or 0.0),
                    float(params.get("confidence") or 0.0),
                ),
            }
            return []

        if "UPDATE plugin_memory_fact SET status" in sql:
            fact = self.facts.get(int(params["memory_item_id"]))
            if fact:
                fact["status"] = params["status"]
            return []

        if "INSERT INTO plugin_memory_episode" in sql:
            self.episodes[params["memory_item_ids"]] = dict(params)
            return []

        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                self._row(self.memory_items[item_id])
                for item_id in params["memory_item_ids"]
                if item_id in self.memory_items
            ]

        return []

    @staticmethod
    def _row(item: dict[str, Any] | None) -> dict[str, Any]:
        if item is None:
            return {}
        row = {
            "id": item.get("id"),
            "tenant_id": item.get("tenant_id", "demo"),
            "channel": item.get("channel", "wechat"),
            "source_key": item.get("source_key", "wxbot"),
            "user_id": item.get("user_id", "wxid_a"),
            "session_id": item.get("session_id", ""),
            "scope_type": item.get("scope_type", "identity"),
            "source_type": item.get("source_type", "auto"),
            "memory_type": item.get("memory_type", "profile_fact"),
            "content": item.get("content", ""),
            "value_json": item.get("value_json", "{}"),
            "normalized_key": item.get("normalized_key", ""),
            "confidence": item.get("confidence", 0.0),
            "status": item.get("status", "active"),
            "pinned": item.get("pinned", False),
            "priority": item.get("priority", 0),
            "sensitivity": item.get("sensitivity", "normal"),
            "source_event_id": item.get("source_event_id"),
            "source_trace_id": item.get("source_trace_id", ""),
            "original_text": item.get("original_text", ""),
            "occurrence_count": item.get("occurrence_count", 1),
            "first_seen_at": item.get("first_seen_at"),
            "last_seen_at": item.get("last_seen_at"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "deleted_at": item.get("deleted_at"),
        }
        row["value"] = json.loads(row["value_json"]) if isinstance(row["value_json"], str) else {}
        return row


def _graph_store_with_fakes(monkeypatch: pytest.MonkeyPatch, llm: _LLM):
    store = MemoryStore(
        _settings(memory_llm_extraction_enabled=False, memory_graph_llm_extraction_enabled=True),
        llm_service=llm,
    )
    graph = _GraphWriteFake()
    updates: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        updates.append((sql, params))
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if "UPDATE plugin_memory_extraction_job SET" in sql:
            return []
        return await graph.exec(sql, params)

    async def fake_list_memory_items(**kwargs):
        return []

    async def fake_get_session_profile(**kwargs):
        return {"session_summary": ""}

    async def fake_refresh(item):
        return None

    mutation_connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    @asynccontextmanager
    async def fake_mutation_transaction():
        token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(mutation_connection)
        try:
            yield mutation_connection
        finally:
            memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store, "get_session_profile", fake_get_session_profile)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)
    monkeypatch.setattr(store, "_mutation_transaction", fake_mutation_transaction)
    return store, graph, updates


@pytest.mark.asyncio
async def test_llm_job_enqueue_uses_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        assert "ON CONFLICT (idempotency_key)" in sql
        return [{"id": 1, "status": "pending", "attempts": 0, **(params or {})}]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(_settings(), llm_service=_LLM())

    first = await store.enqueue_llm_extraction_job(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        trace_id="trace-1",
        source_event_id=7,
    )
    second = await store.enqueue_llm_extraction_job(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        trace_id="trace-1",
        source_event_id=7,
    )

    assert first is not None and second is not None
    assert calls[0][1]["idempotency_key"] == calls[1][1]["idempotency_key"]
    assert calls[0][1]["idempotency_key"].startswith("memory:llm:event:7:")
    assert len(calls[0][1]["idempotency_key"]) <= 256


def test_llm_job_idempotency_key_is_bounded_and_stable() -> None:
    base = {
        "tenant_id": "tenant-" + ("x" * 160),
        "channel": "wechat",
        "source_key": "source-" + ("y" * 160),
        "user_id": "user-" + ("z" * 160),
        "session_id": "session-" + ("s" * 160),
        "trace_id": "trace-1",
        "source_event_id": 123456789,
    }

    event_key = _job_idempotency_key(**base)
    same_event_key = _job_idempotency_key(**{**base, "trace_id": "trace-2"})
    trace_key = _job_idempotency_key(**{**base, "source_event_id": None})
    same_trace_key = _job_idempotency_key(**{**base, "source_event_id": None})
    other_user_trace_key = _job_idempotency_key(
        **{**base, "source_event_id": None, "user_id": "other-user"}
    )

    assert event_key == same_event_key
    assert len(event_key) <= 256
    assert event_key.startswith("memory:llm:event:123456789:")
    assert trace_key == same_trace_key
    assert trace_key != other_user_trace_key
    assert len(trace_key) <= 256


@pytest.mark.asyncio
async def test_llm_job_disabled_does_not_enqueue_or_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    disabled_queue = MemoryStore(
        _settings(memory_llm_extraction_job_enabled=False),
        llm_service=_LLM(),
    )
    disabled_llm = MemoryStore(
        _settings(memory_llm_extraction_enabled=False),
        llm_service=_LLM(),
    )

    enqueued = await disabled_queue.enqueue_llm_extraction_job(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        trace_id="trace-1",
        source_event_id=7,
    )
    claimed = await disabled_llm.claim_llm_extraction_jobs()

    assert enqueued is None
    assert claimed == []
    assert called is False


def test_llm_job_scope_allowlist_parser_supports_compact_and_csv_tokens() -> None:
    sql, params = _llm_job_scope_filter_sql(
        "demo:wechat:wxbot:wxid_a:s1; tenant_id=demo,channel=web,source_key=site,user_id=u2"
    )

    assert "tenant_id = :scope_0_tenant_id" in sql
    assert "session_id = :scope_0_session_id" in sql
    assert "tenant_id = :scope_1_tenant_id" in sql
    assert params["scope_0_user_id"] == "wxid_a"
    assert params["scope_0_session_id"] == "s1"
    assert params["scope_1_channel"] == "web"


def test_llm_job_scope_allowlist_parser_fails_closed_for_invalid_non_empty_value() -> None:
    sql, params = _llm_job_scope_filter_sql("not-a-valid-token")

    assert "AND FALSE" in sql
    assert params == {}


@pytest.mark.asyncio
async def test_llm_job_claim_applies_scope_allowlist_and_batch_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(
        _settings(
            memory_llm_extraction_job_drain_batch_size=50,
            memory_llm_extraction_job_scope_allowlist="demo:wechat:wxbot:wxid_a",
        ),
        llm_service=_LLM(),
    )

    claimed = await store.claim_llm_extraction_jobs(limit=2, worker_id="worker-a")

    assert claimed == []
    sql, params = calls[0]
    assert "LIMIT :limit" in sql
    assert "tenant_id = :scope_0_tenant_id" in sql
    assert "user_id = :scope_0_user_id" in sql
    assert params is not None
    assert params["limit"] == 2
    assert params["scope_0_tenant_id"] == "demo"
    assert params["scope_0_user_id"] == "wxid_a"


@pytest.mark.asyncio
async def test_llm_job_scope_allowlist_mismatch_claims_no_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "tenant_id = :scope_0_tenant_id" in sql
        assert params is not None
        assert params["scope_0_tenant_id"] == "other"
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(
        _settings(memory_llm_extraction_job_scope_allowlist="other:wechat:wxbot:wxid_a"),
        llm_service=_LLM(),
    )

    assert await store.claim_llm_extraction_jobs(worker_id="worker-a") == []


@pytest.mark.asyncio
async def test_llm_job_allowlist_matching_smoke_job_can_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed: list[int] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "tenant_id = :scope_0_tenant_id" in sql
        assert params is not None
        assert params["limit"] == 1
        assert params["scope_0_tenant_id"] == "demo"
        return [_job(id=31, tenant_id="demo")]

    async def fake_process(job: dict[str, Any], **_kwargs: Any) -> str:
        processed.append(int(job["id"]))
        return "succeeded"

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(
        _settings(memory_llm_extraction_job_scope_allowlist="demo:wechat:wxbot:wxid_a"),
        llm_service=_LLM(),
    )
    monkeypatch.setattr(store, "process_llm_extraction_job", fake_process)

    result = await store.drain_llm_extraction_jobs(
        limit=1,
        worker_id="worker-a",
        scope_execution_allowed=_allow_memory_scope,
    )

    assert result == {"claimed": 1, "succeeded": 1, "failed": 0, "dead": 0}
    assert processed == [31]


@pytest.mark.asyncio
async def test_llm_job_drain_defers_disabled_scope_without_spending_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed: list[int] = []
    deferred: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("WITH candidate AS"):
            return [
                _job(
                    id=41,
                    tenant_id="tenant-disabled",
                    session_id="session-a",
                    locked_by="worker-a",
                ),
                _job(
                    id=42,
                    tenant_id="tenant-enabled",
                    session_id="session-b",
                    locked_by="worker-a",
                ),
            ]
        if sql.startswith("UPDATE plugin_memory_extraction_job SET status = 'pending'"):
            deferred.append(dict(params or {}))
            return [{"id": int((params or {})["id"])}]
        return []

    async def fake_process(job: dict[str, Any], **_kwargs: Any) -> str:
        processed.append(int(job["id"]))
        return "succeeded"

    async def scope_allowed(tenant_id: str, _session_id: str) -> bool:
        return tenant_id == "tenant-enabled"

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(_settings(), llm_service=_LLM())
    monkeypatch.setattr(store, "process_llm_extraction_job", fake_process)

    result = await store.drain_llm_extraction_jobs(
        limit=2,
        worker_id="worker-a",
        scope_execution_allowed=scope_allowed,
    )

    assert result == {"claimed": 1, "succeeded": 1, "failed": 0, "dead": 0}
    assert processed == [42]
    assert deferred == [
        {
            "id": 41,
            "locked_by": "worker-a",
            "defer_seconds": 30.0,
        }
    ]


@pytest.mark.asyncio
async def test_llm_job_drain_fails_closed_when_scope_gate_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deferred: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("WITH candidate AS"):
            return [_job(id=43, locked_by="worker-a")]
        if sql.startswith("UPDATE plugin_memory_extraction_job SET status = 'pending'"):
            deferred.append(dict(params or {}))
            return [{"id": 43}]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(_settings(), llm_service=_LLM())

    result = await store.drain_llm_extraction_jobs(limit=1, worker_id="worker-a")

    assert result == {"claimed": 0, "succeeded": 0, "failed": 0, "dead": 0}
    assert deferred == [
        {
            "id": 43,
            "locked_by": "worker-a",
            "defer_seconds": 30.0,
        }
    ]


@pytest.mark.asyncio
async def test_llm_job_cancel_releases_unvisited_claims_before_escaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [
        _job(id=51, locked_by="worker-a"),
        _job(id=52, locked_by="worker-a"),
        _job(id=53, locked_by="worker-a"),
    ]
    processing = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    deferred: list[int] = []

    store = MemoryStore(_settings(), llm_service=_LLM())

    async def claim(**_kwargs: Any) -> list[dict[str, Any]]:
        return jobs

    async def process(job: dict[str, Any], **_kwargs: Any) -> str:
        assert int(job["id"]) == 51
        processing.set()
        await asyncio.Event().wait()
        return "succeeded"

    async def defer(job: dict[str, Any], **_kwargs: Any) -> bool:
        cleanup_started.set()
        await release_cleanup.wait()
        deferred.append(int(job["id"]))
        return True

    monkeypatch.setattr(store, "claim_llm_extraction_jobs", claim)
    monkeypatch.setattr(store, "process_llm_extraction_job", process)
    monkeypatch.setattr(store, "defer_llm_extraction_job", defer)

    drain = asyncio.create_task(
        store.drain_llm_extraction_jobs(
            limit=3,
            worker_id="worker-a",
            scope_execution_allowed=_allow_memory_scope,
        )
    )
    await asyncio.wait_for(processing.wait(), timeout=1)
    drain.cancel()
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    drain.cancel()
    await asyncio.sleep(0)

    assert drain.done() is False
    release_cleanup.set()
    outcome = await asyncio.gather(drain, return_exceptions=True)

    assert isinstance(outcome[0], asyncio.CancelledError)
    assert sorted(deferred) == [52, 53]


@pytest.mark.asyncio
async def test_process_success_marks_succeeded_and_updates_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _semantic_key("preference", "brand", "Adidas")
    llm = _LLM(
        json.dumps(
            {
                "actions": [
                    {
                        "op": "add",
                        "memory_type": "preference",
                        "content": "用户喜欢 Adidas",
                        "normalized_key": key,
                        "confidence": 0.95,
                        "sensitivity": "normal",
                        "reason": "explicit preference",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    store = MemoryStore(_settings(), llm_service=llm)
    applied = []
    updates: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        updates.append((sql, params))
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "记住我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        return []

    async def fake_list_memory_items(**kwargs):
        return []

    async def fake_apply(**kwargs):
        applied.append(kwargs)
        return {"id": 22, **kwargs}

    async def fake_refresh(item):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store, "_apply_structured_memory_action", fake_apply)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    status = await store.process_llm_extraction_job(
        _job(), scope_execution_allowed=_allow_memory_scope
    )

    assert status == "succeeded"
    assert applied[0]["action"]["content"] == "用户喜欢 Adidas"
    assert any("status = 'succeeded'" in sql for sql, _ in updates)


@pytest.mark.asyncio
async def test_process_rechecks_scope_after_llm_before_memory_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    enabled = True
    applied: list[dict[str, Any]] = []
    job_updates: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "记住我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if sql.startswith("UPDATE plugin_memory_extraction_job SET status = 'pending'"):
            job_updates.append("deferred")
            return [{"id": 9}]
        if "UPDATE plugin_memory_extraction_job SET" in sql:
            job_updates.append("attempt-spent")
        return []

    async def fake_list_memory_items(**_kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def fake_extract_actions(**_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal enabled
        enabled = False
        return [
            {
                "op": "add",
                "memory_type": "preference",
                "content": "用户喜欢 Adidas",
                "normalized_key": _semantic_key("preference", "brand", "Adidas"),
                "confidence": 0.95,
            }
        ]

    async def fake_apply(**kwargs: Any) -> dict[str, Any]:
        applied.append(kwargs)
        return {"id": 22}

    async def scope_allowed(_tenant_id: str, _session_id: str) -> bool:
        return enabled

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store.structured_extractor, "extract_actions", fake_extract_actions)
    monkeypatch.setattr(store, "_apply_structured_memory_action", fake_apply)

    status = await store.process_llm_extraction_job(
        _job(locked_by="worker-a"),
        worker_id="worker-a",
        scope_execution_allowed=scope_allowed,
    )

    assert status == "deferred"
    assert applied == []
    assert job_updates == ["deferred"]


@pytest.mark.asyncio
async def test_process_error_retries_then_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM(exc=RuntimeError("bad json")))
    statuses: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "记住我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if "UPDATE plugin_memory_extraction_job SET" in sql and params:
            statuses.append(str(params["status"]))
        return []

    async def fake_list_memory_items(**kwargs):
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)

    failed = await store.process_llm_extraction_job(
        _job(attempts=0, max_attempts=2),
        scope_execution_allowed=_allow_memory_scope,
    )
    dead = await store.process_llm_extraction_job(
        _job(attempts=1, max_attempts=2),
        scope_execution_allowed=_allow_memory_scope,
    )

    assert failed == "failed"
    assert dead == "dead"
    assert statuses == ["failed", "dead"]


@pytest.mark.asyncio
async def test_process_timeout_records_error_type_and_timeout_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        _settings(memory_llm_extraction_job_timeout_seconds=0.01), llm_service=_LLM("{}")
    )
    updates: list[dict] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "记住我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if "UPDATE plugin_memory_extraction_job SET" in sql and params:
            updates.append(params)
        return []

    async def slow_enhance(**kwargs):
        await memory_store_module.asyncio.sleep(0.2)
        return 1

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_enhance_memory_with_llm", slow_enhance)

    status = await store.process_llm_extraction_job(
        _job(attempts=0, max_attempts=2),
        scope_execution_allowed=_allow_memory_scope,
    )

    assert status == "failed"
    result = json.loads(updates[-1]["result_json"])
    assert result["error_type"] == "TimeoutError"
    assert result["timeout"] is True
    assert result["graph"]["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_llm_extraction_job_stats_filters_and_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "GROUP BY status" in sql:
            return [{"status": "failed", "count": 2}]
        if "AS error_type" in sql and "GROUP BY tenant_id" not in sql:
            return [{"error_type": "TimeoutError", "count": 2}]
        if "GROUP BY tenant_id" in sql:
            return [
                {
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "s1",
                    "status": "failed",
                    "error_type": "TimeoutError",
                    "count": 2,
                }
            ]
        if "retryable_failed" in sql:
            return [
                {
                    "retryable_failed": 1,
                    "exhausted_failed": 1,
                    "ready": 2,
                    "delayed": 3,
                }
            ]
        if "avg_seconds" in sql:
            return [
                {
                    "avg_seconds": 1.25,
                    "max_seconds": 3.5,
                    "pending_avg_age_seconds": 12.0,
                }
            ]
        if "graph_error" in sql:
            return [
                {
                    "graph_error": 1,
                    "graph_facts": 2,
                    "graph_episodes": 1,
                    "graph_entities": 2,
                    "graph_skipped": 1,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(_settings())

    stats = await store.get_llm_extraction_job_status_counts(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        status="failed",
        error_type="TimeoutError",
        limit=500,
    )

    assert stats["status_counts"]["failed"] == 2
    assert stats["error_type_counts"] == {"TimeoutError": 2}
    assert stats["scope_counts"][0]["tenant_id"] == "demo"
    assert stats["retry_counts"] == {
        "retryable_failed": 1,
        "exhausted_failed": 1,
        "ready": 2,
        "delayed": 3,
    }
    assert stats["dead_scope_counts"][0]["status"] == "dead"
    assert stats["retry_scope_counts"][0]["retryable"] is True
    assert stats["latency_seconds"] == {"avg": 1.25, "max": 3.5, "pending_avg_age": 12.0}
    assert stats["graph_result_counts"] == {
        "error": 1,
        "facts": 2,
        "episodes": 1,
        "entities": 2,
        "skipped": 1,
    }
    assert all(call[1]["limit"] == 100 for call in calls)
    assert all("result_json::jsonb" in call[0] for call in calls[1:])
    assert calls[0][1]["tenant_id"] == "demo"
    assert calls[0][1]["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_llm_extraction_job_maintenance_dry_run_selects_without_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return [{"id": 9}, {"id": 10}]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(_settings())

    result = await store.maintain_llm_extraction_jobs(
        actions=["retry"],
        dry_run=True,
        tenant_id="demo",
        status="failed",
        limit=500,
    )

    assert result["dry_run"] is True
    assert result["limit"] == 100
    assert result["would_affect"] == 2
    assert result["affected"] == 0
    assert result["ids"] == [9, 10]
    assert len(calls) == 1
    assert calls[0][0].startswith("SELECT id FROM plugin_memory_extraction_job")
    assert "UPDATE plugin_memory_extraction_job" not in calls[0][0]
    assert calls[0][1]["limit"] == 100


@pytest.mark.asyncio
async def test_process_manual_pinned_protection_still_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _semantic_key("preference", "brand", "Adidas")
    llm = _LLM(
        json.dumps(
            {
                "actions": [
                    {
                        "op": "update",
                        "memory_type": "preference",
                        "content": "用户喜欢 Adidas",
                        "normalized_key": key,
                        "confidence": 0.95,
                        "sensitivity": "normal",
                        "reason": "llm update",
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    store = MemoryStore(_settings(), llm_service=llm)
    calls: list[tuple[str, dict | None]] = []
    inserted: list[dict] = []
    manual = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "preference",
        "content": "人工：用户喜欢 Adidas",
        "value_json": "{}",
        "normalized_key": key,
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "记住我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if sql.startswith("SELECT id, audience_scope"):
            return []
        if "FROM plugin_memory_item" in sql and "normalized_key = :normalized_key" in sql:
            return [manual]
        if "INSERT INTO plugin_memory_item" in sql and params:
            row = {
                **manual,
                "id": 23,
                "source_type": params["source_type"],
                "content": params["content"],
                "status": params["status"],
                "pinned": params["pinned"],
                "value_json": params["value_json"],
            }
            inserted.append(row)
            return [row]
        return []

    async def fake_list_memory_items(**kwargs):
        return [
            {
                "id": 1,
                "content": "人工：用户喜欢 Adidas",
                "normalized_key": key,
                "memory_type": "preference",
                "source_type": "manual",
                "status": "active",
                "pinned": True,
                "sensitivity": "normal",
            }
        ]

    async def fake_refresh(item):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    status = await store.process_llm_extraction_job(
        _job(), scope_execution_allowed=_allow_memory_scope
    )

    assert status == "succeeded"
    assert inserted[0]["status"] == "pending"
    assert "manual_or_pinned_conflict:" in inserted[0]["value_json"]
    assert not any("UPDATE plugin_memory_item SET content = :content" in sql for sql, _ in calls)


def test_pending_sensitive_not_injected_after_job_processing() -> None:
    identity_profile = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "long_term_memory": "",
        "manual_notes": "",
        "long_term_items_json": "[]",
        "message_count": 0,
        "imported_message_count": 0,
    }
    session_profile = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "s1",
        "user_id": "wxid_a",
        "short_term_memory": "",
        "manual_notes": "",
        "message_count": 0,
        "imported_message_count": 0,
    }

    runtime = memory_store_module._build_runtime_profile_from_items(
        identity_profile,
        session_profile,
        [
            {
                "id": 1,
                "source_type": "auto",
                "memory_type": "profile_fact",
                "content": "手机号 13800138000",
                "normalized_key": "profile_fact:phone:main",
                "confidence": 0.95,
                "status": "pending",
                "pinned": False,
                "priority": 0,
                "sensitivity": "pii",
            }
        ],
        [],
    )

    assert "13800138000" not in runtime["long_term_memory"]


@pytest.mark.asyncio
async def test_graph_llm_success_writes_entities_fact_and_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _LLM(
        json.dumps(
            {
                "entities": [
                    {"key": "user", "type": "user", "name": "user:wxid_a", "confidence": 1.0},
                    {"key": "brand:adidas", "type": "brand", "name": "Adidas", "confidence": 0.94},
                ],
                "facts": [
                    {
                        "subject_key": "user",
                        "predicate": "likes",
                        "object_key": "brand:adidas",
                        "content": "用户喜欢 Adidas",
                        "confidence": 0.94,
                        "sensitivity": "normal",
                        "memory_key": _semantic_key(
                            "graph_fact", "likes", "user|likes|brand:adidas"
                        ),
                    }
                ],
                "episodes": [
                    {
                        "title": "用户表达品牌偏好",
                        "summary": "用户说自己喜欢 Adidas。",
                        "importance": 4,
                        "confidence": 0.9,
                        "sensitivity": "normal",
                    }
                ],
                "invalidations": [],
                "conflicts": [],
            },
            ensure_ascii=False,
        )
    )
    store = MemoryStore(
        _settings(memory_llm_extraction_enabled=False, memory_graph_llm_extraction_enabled=True),
        llm_service=llm,
    )
    graph = _GraphWriteFake()
    updates: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        updates.append((sql, params))
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if "UPDATE plugin_memory_extraction_job SET" in sql:
            return []
        return await graph.exec(sql, params)

    async def fake_list_memory_items(**kwargs):
        return []

    async def fake_get_session_profile(**kwargs):
        return {"session_summary": "用户讨论购物偏好"}

    async def fake_refresh(item):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store, "get_session_profile", fake_get_session_profile)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    status = await store.process_llm_extraction_job(
        _job(), scope_execution_allowed=_allow_memory_scope
    )

    assert status == "succeeded"
    assert len(graph.entities) == 2
    assert len(graph.facts) == 1
    assert len(graph.episodes) == 1
    assert next(iter(graph.facts.values()))["source_event_id"] == 7
    fact_item = next(
        item for item in graph.memory_items.values() if item["memory_type"] == "profile_fact"
    )
    fact_value = json.loads(fact_item["value_json"])
    assert "original_text" not in fact_value["evidence"][0]
    assert fact_value["evidence"][0] == {
        "source_event_id": 7,
        "source_trace_id": "trace-1",
        "reason": "llm_graph_extraction",
    }
    assert fact_item["original_text"] == "我喜欢 Adidas"
    success_update = [params for sql, params in updates if "status = 'succeeded'" in sql][-1]
    assert json.loads(success_update["result_json"])["graph"]["facts"] == 1
    fact_sql = next(sql for sql, _ in updates if "INSERT INTO plugin_memory_fact" in sql)
    assert "CASE WHEN CAST(:status AS VARCHAR) IN" in fact_sql


def test_graph_llm_parser_caps_actions_after_entities() -> None:
    extractor = MemoryGraphLLMExtractor(
        settings=_settings(
            memory_graph_llm_extraction_max_actions=4,
            memory_graph_llm_extraction_max_entities=2,
            memory_graph_llm_extraction_max_facts=2,
            memory_graph_llm_extraction_max_episodes=2,
        )
    )
    result = extractor._parse_graph(
        json.dumps(
            {
                "entities": [
                    {"key": "user", "type": "user", "name": "user", "confidence": 1.0},
                    {"key": "brand:adidas", "type": "brand", "name": "Adidas", "confidence": 0.9},
                    {"key": "brand:nike", "type": "brand", "name": "Nike", "confidence": 0.9},
                ],
                "facts": [
                    {"predicate": "", "content": "invalid", "confidence": 0.9},
                    {
                        "subject_key": "user",
                        "predicate": "likes",
                        "object_value": "Adidas",
                        "confidence": 0.9,
                    },
                    {
                        "subject_key": "user",
                        "predicate": "owns",
                        "object_value": "shoes",
                        "confidence": 0.9,
                    },
                    {
                        "subject_key": "user",
                        "predicate": "extra",
                        "object_value": "ignored",
                        "confidence": 0.9,
                    },
                ],
                "episodes": [
                    {"title": "Shop", "summary": "User discussed shopping.", "confidence": 0.9},
                    {"title": "Run", "summary": "User discussed running.", "confidence": 0.9},
                ],
                "invalidations": [{"normalized_key": "old:key", "reason": "new preference"}],
                "conflicts": [{"normalized_key": "conflict:key", "reason": "conflict"}],
            }
        )
    )

    assert len(result["entities"]) == 2
    assert len(result["facts"]) == 2
    assert len(result["episodes"]) == 2
    assert result["invalidations"] == []
    assert result["conflicts"] == []


def test_graph_llm_parser_preserves_zero_limits_and_bounds_memory_keys() -> None:
    extractor = MemoryGraphLLMExtractor(
        settings=_settings(
            memory_graph_llm_extraction_max_actions=8,
            memory_graph_llm_extraction_max_episodes=0,
            memory_graph_llm_extraction_min_confidence=0.0,
        )
    )
    long_key = "graph_fact:" + ("x" * 120)

    result = extractor._parse_graph(
        json.dumps(
            {
                "entities": [],
                "facts": [
                    {
                        "subject_key": "user",
                        "predicate": "likes",
                        "object_value": "Adidas",
                        "content": "用户喜欢 Adidas",
                        "confidence": 0.0,
                        "memory_key": long_key,
                        "invalidates_normalized_key": long_key,
                    }
                ],
                "episodes": [
                    {"title": "Ignored", "summary": "Episode cap is zero.", "confidence": 0.9}
                ],
                "invalidations": [{"normalized_key": long_key, "reason": "old key"}],
                "conflicts": [],
            }
        )
    )

    assert result["facts"][0]["status"] == "active"
    assert len(result["facts"][0]["memory_key"]) <= 64
    assert len(result["facts"][0]["invalidates_normalized_key"]) <= 64
    assert result["episodes"] == []
    assert len(result["invalidations"][0]["normalized_key"]) <= 64


@pytest.mark.asyncio
async def test_graph_llm_malformed_json_and_timeout_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if "UPDATE plugin_memory_extraction_job SET" in sql and params:
            updates.append(params)
        return []

    async def fake_list_memory_items(**kwargs):
        return []

    async def fake_get_session_profile(**kwargs):
        return {"session_summary": ""}

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    malformed = MemoryStore(
        _settings(memory_llm_extraction_enabled=False, memory_graph_llm_extraction_enabled=True),
        llm_service=_LLM("not json"),
    )
    monkeypatch.setattr(malformed, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(malformed, "get_session_profile", fake_get_session_profile)
    assert (
        await malformed.process_llm_extraction_job(
            _job(), scope_execution_allowed=_allow_memory_scope
        )
        == "succeeded"
    )
    malformed_result = json.loads(updates[-1]["result_json"])
    assert malformed_result["graph"]["error"] == 1
    assert malformed_result["graph"]["error_type"] == "ValueError"
    assert "status" not in updates[-1]

    class _SlowLLM(_LLM):
        async def chat(self, request):
            await memory_store_module.asyncio.sleep(0.2)
            return await super().chat(request)

    timeout_store = MemoryStore(
        _settings(
            memory_llm_extraction_enabled=False,
            memory_graph_llm_extraction_enabled=True,
            memory_graph_llm_extraction_timeout_seconds=0.01,
            memory_llm_extraction_job_timeout_seconds=0.02,
        ),
        llm_service=_SlowLLM("{}"),
    )
    monkeypatch.setattr(timeout_store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(timeout_store, "get_session_profile", fake_get_session_profile)
    assert (
        await timeout_store.process_llm_extraction_job(
            _job(), scope_execution_allowed=_allow_memory_scope
        )
        == "succeeded"
    )
    timeout_result = json.loads(updates[-1]["result_json"])
    assert timeout_result["graph"]["error"] == 1
    assert timeout_result["graph"]["error_type"] == "TimeoutError"
    assert "status" not in updates[-1]


@pytest.mark.asyncio
async def test_graph_llm_branch_exception_result_includes_type_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if "UPDATE plugin_memory_extraction_job SET" in sql and params:
            updates.append(params)
        return []

    async def fail_graph(**kwargs):
        raise RuntimeError("secret raw content")

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(
        _settings(memory_llm_extraction_enabled=False, memory_graph_llm_extraction_enabled=True),
        llm_service=_LLM("{}"),
    )
    monkeypatch.setattr(store, "_enhance_memory_graph_with_llm", fail_graph)

    assert (
        await store.process_llm_extraction_job(
            _job(), scope_execution_allowed=_allow_memory_scope
        )
        == "succeeded"
    )

    graph_result = json.loads(updates[-1]["result_json"])["graph"]
    assert graph_result["error"] == 1
    assert graph_result["error_type"] == "RuntimeError"
    assert "secret raw content" not in updates[-1]["result_json"]


@pytest.mark.asyncio
async def test_graph_llm_low_confidence_and_sensitive_pending_or_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _LLM(
        json.dumps(
            {
                "entities": [],
                "facts": [
                    {
                        "subject_key": "user",
                        "predicate": "phone",
                        "object_value": "13800138000",
                        "content": "手机号 13800138000",
                        "confidence": 0.95,
                        "sensitivity": "pii",
                    },
                    {
                        "subject_key": "user",
                        "predicate": "maybe_likes",
                        "object_value": "跑步",
                        "content": "用户可能喜欢跑步",
                        "confidence": 0.4,
                        "sensitivity": "normal",
                    },
                    {
                        "subject_key": "user",
                        "predicate": "secret",
                        "object_value": "token",
                        "content": "api token 是 abc",
                        "confidence": 0.99,
                        "sensitivity": "sensitive",
                        "status": "skipped",
                    },
                ],
                "episodes": [],
                "invalidations": [],
                "conflicts": [],
            },
            ensure_ascii=False,
        )
    )
    store, graph, _updates = _graph_store_with_fakes(monkeypatch, llm)

    status = await store.process_llm_extraction_job(
        _job(), scope_execution_allowed=_allow_memory_scope
    )

    assert status == "succeeded"
    assert [item["status"] for item in graph.memory_items.values()] == ["pending", "pending"]
    assert len(graph.facts) == 2


@pytest.mark.asyncio
async def test_graph_llm_manual_pinned_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    memory_key = _semantic_key("graph_fact", "likes", "user|likes|brand:adidas")
    llm = _LLM(
        json.dumps(
            {
                "entities": [
                    {"key": "user", "type": "user", "name": "user:wxid_a", "confidence": 1.0}
                ],
                "facts": [
                    {
                        "subject_key": "user",
                        "predicate": "likes",
                        "object_value": "Adidas",
                        "content": "用户喜欢 Adidas",
                        "confidence": 0.95,
                        "sensitivity": "normal",
                        "memory_key": memory_key,
                    }
                ],
                "episodes": [],
                "invalidations": [],
                "conflicts": [],
            },
            ensure_ascii=False,
        )
    )
    store, graph, _updates = _graph_store_with_fakes(monkeypatch, llm)
    graph.memory_items[1] = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "profile_fact",
        "content": "人工：用户喜欢 Adidas",
        "value_json": "{}",
        "value": {},
        "normalized_key": memory_key,
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "deleted_at": None,
    }

    status = await store.process_llm_extraction_job(
        _job(), scope_execution_allowed=_allow_memory_scope
    )

    assert status == "succeeded"
    assert len(graph.memory_items) == 1
    assert graph.facts == {}


@pytest.mark.asyncio
async def test_graph_llm_invalidation_marks_old_fact_invalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = _semantic_key("graph_fact", "likes", "user|likes|brand:nike")
    llm = _LLM(
        json.dumps(
            {
                "entities": [],
                "facts": [],
                "episodes": [],
                "invalidations": [{"normalized_key": old_key, "reason": "new preference"}],
                "conflicts": [],
            },
            ensure_ascii=False,
        )
    )
    store, graph, _updates = _graph_store_with_fakes(monkeypatch, llm)
    graph.memory_items[1] = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "profile_fact",
        "content": "用户喜欢 Nike",
        "value_json": "{}",
        "value": {},
        "normalized_key": old_key,
        "confidence": 0.9,
        "status": "active",
        "pinned": False,
        "priority": 0,
        "sensitivity": "normal",
        "deleted_at": None,
    }
    graph.facts[1] = {"memory_item_id": 1, "status": "active"}

    status = await store.process_llm_extraction_job(
        _job(), scope_execution_allowed=_allow_memory_scope
    )

    assert status == "succeeded"
    assert graph.memory_items[1]["status"] == "invalidated"
    invalidated_value = json.loads(graph.memory_items[1]["value_json"])
    assert invalidated_value["invalidations"] == [
        {
            "reason": "new preference",
            "source_event_id": 7,
            "source_trace_id": "trace-1",
        }
    ]
    assert graph.facts[1]["status"] == "invalidated"
    item_update_connection = next(
        connection
        for sql, connection in graph.execution_connections
        if "UPDATE plugin_memory_item SET status = 'invalidated'" in sql
    )
    graph_update_connection = next(
        connection
        for sql, connection in graph.execution_connections
        if "INSERT INTO plugin_memory_fact" in sql
    )
    assert item_update_connection is graph_update_connection
    assert item_update_connection is not None
    assert any(
        "FOR UPDATE" in sql and connection is item_update_connection
        for sql, connection in graph.execution_connections
    )


@pytest.mark.asyncio
async def test_graph_llm_duplicate_run_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _LLM(
        json.dumps(
            {
                "entities": [
                    {"key": "user", "type": "user", "name": "user:wxid_a", "confidence": 1.0}
                ],
                "facts": [
                    {
                        "subject_key": "user",
                        "predicate": "likes",
                        "object_value": "Adidas",
                        "content": "用户喜欢 Adidas",
                        "confidence": 0.95,
                        "sensitivity": "normal",
                    }
                ],
                "episodes": [],
                "invalidations": [],
                "conflicts": [],
            },
            ensure_ascii=False,
        )
    )
    store, graph, _updates = _graph_store_with_fakes(monkeypatch, llm)

    assert (
        await store.process_llm_extraction_job(
            _job(), scope_execution_allowed=_allow_memory_scope
        )
        == "succeeded"
    )
    assert (
        await store.process_llm_extraction_job(
            _job(), scope_execution_allowed=_allow_memory_scope
        )
        == "succeeded"
    )

    assert len(graph.memory_items) == 1
    assert len(graph.facts) == 1

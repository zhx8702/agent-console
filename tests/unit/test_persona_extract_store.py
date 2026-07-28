from __future__ import annotations

import json
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app.common.types import ChatResponse
from plugins.persona_extract import store as store_module
from plugins.persona_extract.pipeline import PersonaMessageChunk
from plugins.persona_extract.store import PersonaExtractStore, PersonaJobRequestConflict


class _FlakyLlm:
    def __init__(self, failures: list[BaseException], content: str = "ok") -> None:
        self.failures = list(failures)
        self.content = content
        self.calls = 0

    async def chat(self, request):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return ChatResponse(content=self.content)


def _settings(**overrides):
    values = {
        "persona_extract_stage_timeout_seconds": 10.0,
        "persona_extract_stage_max_retries": 2,
        "persona_extract_stage_retry_backoff_seconds": 0.01,
        "wxbot_default_tenant_id": "demo",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_create_job_persists_frozen_input_and_hides_it_from_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if sql.startswith("INSERT INTO plugin_persona_jobs"):
            return [{"id": 41}]
        if sql.startswith("SELECT * FROM plugin_persona_jobs"):
            return [
                {
                    "id": 41,
                    "status": "pending",
                    "request_id": "request-41",
                    "request_hash": "private-hash",
                    "input_messages_json": '[{"text":"secret"}]',
                    "checkpoint_json": "{}",
                    "artifact_json": "",
                    "run_attempt": 0,
                    "total_chunks": 0,
                    "completed_chunks": 0,
                }
            ]
        return []

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    store = PersonaExtractStore(_settings(persona_extract_job_max_attempts=3))

    job, replayed = await store.create_job_idempotent(
        tenant_id="demo",
        session_id="room",
        target_user_id="alice",
        messages=[{"sender_name": "Alice", "text": "secret"}],
        request_id="request-41",
    )

    assert replayed is False
    assert job["client_request_id"] == "request-41"
    assert job["attempt_count"] == 0
    assert job["max_attempts"] == 3
    assert "input_messages_json" not in job
    assert "request_hash" not in job
    insert_params = calls[0][1] or {}
    assert json.loads(str(insert_params["input_messages_json"])) == [
        {"timestamp": "", "sender_name": "Alice", "text": "secret"}
    ]


@pytest.mark.asyncio
async def test_create_job_rejects_idempotency_key_payload_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = params
        if sql.startswith("INSERT INTO plugin_persona_jobs"):
            return []
        if sql.startswith("SELECT id, request_hash"):
            return [{"id": 41, "request_hash": "another-payload"}]
        return []

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    store = PersonaExtractStore(_settings())

    with pytest.raises(PersonaJobRequestConflict):
        await store.create_job_idempotent(
            tenant_id="demo",
            session_id="room",
            target_user_id="alice",
            request_id="reused-key",
        )


@pytest.mark.asyncio
async def test_chunk_creation_and_lease_maintenance_are_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return [{"id": 41}] if "RETURNING job.id" in sql else []

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    store = PersonaExtractStore(_settings())
    await store.ensure_job_chunks(
        41,
        run_attempt=2,
        claim_owner="worker-a",
        chunks=[
            PersonaMessageChunk(
                index=0,
                text="message",
                message_count=1,
                estimated_tokens=2,
                input_hash="hash",
            )
        ],
    )
    await store.renew_job_lease(
        41,
        run_attempt=2,
        claim_owner="worker-a",
        lease_seconds=180,
    )

    insert_sql = calls[0][0]
    assert "job.run_attempt = :attempt" in insert_sql
    assert "job.claim_owner = :owner" in insert_sql
    assert "job.cancel_requested_at IS NULL" in insert_sql
    renew_sql = calls[-1][0]
    assert "run_attempt = :attempt" in renew_sql
    assert "claim_owner = :owner" in renew_sql
    assert "cancel_requested_at IS NULL" not in renew_sql


@pytest.mark.asyncio
async def test_collect_messages_uses_exact_private_sdk_origin_auth_and_bounded_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    gate_calls: list[tuple[str, str]] = []

    async def history_scope_gate(tenant_id: str, session_id: str) -> bool:
        gate_calls.append((tenant_id, session_id))
        return True

    store = PersonaExtractStore(
        _settings(
            wxbot_sdk_url="http://127.0.0.1:5080",
            wxbot_api_token="sdk-secret",
        ),
        history_scope_gate=history_scope_gate,
    )

    async def fake_get_job(job_id: int) -> dict:
        assert job_id == 7
        return {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "target_user_id": "wxid_alice",
            "target_name": "Alice",
            "days_limit": 30,
            "max_messages": 100,
            "checkpoint": {
                "source_identity": {
                    "connection_id": "legacy-wechat-default",
                    "adapter_id": "wxbot",
                    "external_session_id": "external-room@chatroom",
                }
            },
        }

    async def fake_safe_trusted_service_request(
        client,
        method,
        base_url,
        path,
        *,
        json,
        headers,
        timeout_seconds,
        max_response_bytes,
        allowed_response_content_types,
    ) -> httpx.Response:
        _ = client
        captured.update(
            method=method,
            base_url=base_url,
            path=path,
            json=json,
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
                "messages": [
                    {
                        "sender_name": "Alice",
                        "text": "hello",
                        "timestamp": "2026-07-18 10:00:00",
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(store, "get_job", fake_get_job)
    monkeypatch.setattr(
        store_module,
        "safe_trusted_service_request",
        fake_safe_trusted_service_request,
    )

    messages = await store.collect_messages_for_job(7)

    assert messages == [
        {
            "sender_name": "Alice",
            "text": "hello",
            "timestamp": "2026-07-18 10:00:00",
        }
    ]
    assert captured["method"] == "POST"
    assert captured["base_url"] == "http://127.0.0.1:5080"
    assert captured["path"] == "/ext/persona/messages"
    assert captured["json"] == {
        "session_id": "external-room@chatroom",
        "target_wxid": "wxid_alice",
        "target_name": "Alice",
        "days_limit": 30,
        "max_messages": 100,
    }
    assert captured["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer sdk-secret",
    }
    assert captured["max_response_bytes"] == 10 * 1024 * 1024
    assert captured["timeout_seconds"] == 45.0
    assert gate_calls == [
        ("demo", "room@chatroom"),
        ("demo", "room@chatroom"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "connection_id", "expected_error"),
    [
        ("other", "legacy-wechat-default", "legacy_wxbot_history_tenant_unavailable"),
        ("demo", "wechat-managed", "connection_scoped_history_unavailable"),
        ("demo", "", "connection_id cannot be empty"),
    ],
)
async def test_collect_messages_fails_closed_outside_explicit_legacy_scope(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: str,
    connection_id: str,
    expected_error: str,
) -> None:
    network_called = False

    async def history_scope_gate(_tenant_id: str, _session_id: str) -> bool:
        return True

    store = PersonaExtractStore(
        _settings(wxbot_sdk_url="http://127.0.0.1:5080"),
        history_scope_gate=history_scope_gate,
    )

    async def fake_get_job(_job_id: int) -> dict:
        return {
            "tenant_id": tenant_id,
            "session_id": "canonical-room",
            "target_user_id": "wxid_alice",
            "checkpoint": {
                "source_identity": {
                    "connection_id": connection_id,
                    "external_session_id": "external-room@chatroom",
                }
            },
        }

    async def fail_if_called(*args, **kwargs):
        nonlocal network_called
        _ = args, kwargs
        network_called = True
        raise AssertionError("network must not be called")

    monkeypatch.setattr(store, "get_job", fake_get_job)
    monkeypatch.setattr(
        store_module,
        "safe_trusted_service_request",
        fail_if_called,
    )

    with pytest.raises(RuntimeError, match=expected_error):
        await store.collect_messages_for_job(7)

    assert network_called is False


@pytest.mark.asyncio
async def test_collect_messages_revalidates_cross_owner_scope_after_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = iter((True, False))

    async def history_scope_gate(_tenant_id: str, _session_id: str) -> bool:
        return next(decisions)

    store = PersonaExtractStore(
        _settings(
            wxbot_sdk_url="http://127.0.0.1:5080",
            wxbot_api_token="sdk-secret",
        ),
        history_scope_gate=history_scope_gate,
    )

    async def fake_get_job(_job_id: int) -> dict:
        return {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "target_user_id": "wxid_alice",
            "checkpoint": {
                "source_identity": {
                    "connection_id": "legacy-wechat-default",
                    "external_session_id": "room@chatroom",
                }
            },
        }

    async def fake_request(*args, **kwargs) -> httpx.Response:
        _ = args, kwargs
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"messages": []},
            request=httpx.Request("POST", "http://127.0.0.1:5080/ext/persona/messages"),
        )

    monkeypatch.setattr(store, "get_job", fake_get_job)
    monkeypatch.setattr(store_module, "safe_trusted_service_request", fake_request)

    with pytest.raises(RuntimeError, match="persona_or_wxbot_scope_disabled"):
        await store.collect_messages_for_job(7)


@pytest.mark.asyncio
async def test_offline_export_streams_to_private_bundle_without_persisting_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    job = {
        "id": 7,
        "tenant_id": "demo",
        "session_id": "room@chatroom",
        "session_name": "产品群",
        "target_user_id": "wxid_alice",
        "target_name": "Alice",
        "days_limit": 0,
        "max_messages": 0,
        "status": "running",
        "checkpoint": {
            "workflow": "offline_export",
            "export_mode": "full",
            "source_identity": {
                "connection_id": "legacy-wechat-default",
                "external_session_id": "room@chatroom",
            },
        },
    }
    sql_calls: list[tuple[str, dict | None]] = []
    settings = _settings(
        wxbot_sdk_url="http://127.0.0.1:5080",
        persona_extract_offline_export_dir=str(tmp_path),
        persona_extract_offline_export_timeout_seconds=60.0,
        persona_extract_offline_export_max_bytes=1024 * 1024,
    )
    store = PersonaExtractStore(
        settings,
        history_scope_gate=lambda _tenant, _session: _async_true(),
    )

    async def fake_get_job(_job_id: int) -> dict:
        return dict(job)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        sql_calls.append((sql, params))
        if "status = 'awaiting_import'" in sql:
            job["status"] = "awaiting_import"
            return [{"id": 7}]
        return []

    async def fake_update_claimed_job(*args, **kwargs) -> bool:
        _ = args, kwargs
        return True

    async def fake_cancel_requested(*args, **kwargs) -> bool:
        _ = args, kwargs
        return False

    async def no_previous(**kwargs):
        _ = kwargs
        return None

    async def no_slugs(_tenant_id: str) -> set[str]:
        return set()

    payload = json.dumps(
        {
            "messages": [
                {
                    "sender_name": "Alice",
                    "text": "hello",
                    "timestamp": "2026-07-18 10:00:00",
                }
            ]
        }
    ).encode()

    @asynccontextmanager
    async def fake_stream(*args, **kwargs):
        _ = args, kwargs
        yield httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=payload,
            request=httpx.Request("POST", "http://127.0.0.1:5080/ext/persona/messages"),
        )

    monkeypatch.setattr(store, "get_job", fake_get_job)
    monkeypatch.setattr(store, "update_claimed_job", fake_update_claimed_job)
    monkeypatch.setattr(store, "job_cancel_requested", fake_cancel_requested)
    monkeypatch.setattr(store, "_load_previous_artifact", no_previous)
    monkeypatch.setattr(store, "_list_existing_slugs", no_slugs)
    monkeypatch.setattr(store_module, "_exec", fake_exec)
    monkeypatch.setattr(store_module, "safe_trusted_service_stream", fake_stream)

    result = await store.prepare_offline_export(
        7,
        run_attempt=1,
        claim_owner="worker-a",
    )

    assert result["status"] == "awaiting_import"
    archive = tmp_path / "persona-offline-7.zip"
    assert archive.is_file()
    with zipfile.ZipFile(archive) as bundle:
        assert json.loads(bundle.read("input/messages.jsonl"))["text"] == "hello"
    update_sql, update_params = next(
        (sql, params)
        for sql, params in sql_calls
        if "status = 'awaiting_import'" in sql
    )
    assert "input_messages_json = '[]'" in update_sql
    assert (update_params or {})["msg_count"] == 1
    assert not (tmp_path / ".persona-offline-7.source.json.part").exists()


async def _async_true() -> bool:
    return True


@pytest.mark.asyncio
async def test_call_llm_markdown_retries_transient_gateway_error(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(store_module.asyncio, "sleep", fake_sleep)
    llm = _FlakyLlm([RuntimeError("openai responses unavailable: 502 Bad Gateway")], "# done")
    store = PersonaExtractStore(_settings())

    result = await store._call_llm_markdown(
        llm_service=llm,
        tenant_id="demo",
        trace_id="trace",
        stage="persona",
        system="system",
        user="user",
    )

    assert result == "# done"
    assert llm.calls == 2
    assert sleeps == [0.01]


@pytest.mark.asyncio
async def test_call_llm_markdown_stops_after_retry_budget(monkeypatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        _ = seconds

    monkeypatch.setattr(store_module.asyncio, "sleep", fake_sleep)
    llm = _FlakyLlm(
        [
            RuntimeError("openai responses unavailable: 504 Gateway Time-out"),
            RuntimeError("openai responses unavailable: 504 Gateway Time-out"),
        ]
    )
    store = PersonaExtractStore(_settings(persona_extract_stage_max_retries=1))

    with pytest.raises(RuntimeError) as exc:
        await store._call_llm_markdown(
            llm_service=llm,
            tenant_id="demo",
            trace_id="trace",
            stage="work",
            system="system",
            user="private message body should not leak",
        )

    assert llm.calls == 2
    message = str(exc.value)
    assert "persona_extract work stage failed after 2 attempts" in message
    assert "private message body" not in message


class _InMemoryPipelineStore(PersonaExtractStore):
    def __init__(self, *, completed_chunk: bool = False) -> None:
        super().__init__(
            _settings(
                persona_extract_chunk_max_tokens=8_000,
                persona_extract_chunk_max_messages=400,
                persona_extract_chunk_concurrency=2,
                persona_extract_aggregate_max_items=80,
                persona_extract_knowledge_sample_max_chars=50_000,
            )
        )
        self.job = {
            "id": 41,
            "tenant_id": "demo",
            "session_id": "group-1@chatroom",
            "session_name": "测试群",
            "target_user_id": "wxid_member_a",
            "target_name": "成员A",
            "status": "running",
            "run_attempt": 1,
            "claim_owner": "worker-a",
            "current_stage": "prepare",
            "checkpoint": {},
            "msg_count": 0,
            "days_limit": 90,
            "max_messages": 2_000,
            "result_text": "",
            "output_slug": "",
            "mode": "",
            "artifact": None,
        }
        self.chunks: list[dict] = []
        if completed_chunk:
            self.chunks.append(
                {
                    "chunk_index": 0,
                    "status": "completed",
                    "input_text": "2026-06-10 成员A: 你好",
                    "result_json": json.dumps(
                        {
                            "tone_signals": ["简洁直接"],
                            "confidence": 0.9,
                        },
                        ensure_ascii=False,
                    ),
                }
            )

    async def get_job(self, job_id: int) -> dict | None:
        return dict(self.job) if job_id == 41 else None

    async def job_cancel_requested(self, *args, **kwargs) -> bool:
        _ = args, kwargs
        return False

    async def acknowledge_claimed_cancel(self, *args, **kwargs) -> bool:
        _ = args, kwargs
        return True

    async def update_claimed_job(self, job_id: int, **kwargs) -> bool:
        _ = job_id, kwargs.pop("run_attempt"), kwargs.pop("claim_owner")
        self.job.update(kwargs)
        return True

    async def ensure_job_chunks(self, job_id: int, *, chunks, **kwargs) -> bool:
        _ = job_id, kwargs
        if not self.chunks:
            self.chunks = [
                {
                    "chunk_index": chunk.index,
                    "status": "pending",
                    "input_text": chunk.text,
                    "result_json": "",
                }
                for chunk in chunks
            ]
        return True

    async def list_job_chunks(self, job_id: int) -> list[dict]:
        _ = job_id
        return [dict(chunk) for chunk in self.chunks]

    async def mark_chunk_running(self, job_id: int, chunk_index: int, **kwargs) -> bool:
        _ = job_id, kwargs
        self.chunks[chunk_index]["status"] = "running"
        return True

    async def complete_chunk(self, job_id: int, chunk_index: int, *, result, **kwargs) -> bool:
        _ = job_id, kwargs
        self.chunks[chunk_index].update(
            status="completed",
            result_json=json.dumps(result, ensure_ascii=False),
        )
        return True

    async def fail_chunk(self, *args, **kwargs) -> None:
        _ = args, kwargs

    async def complete_claimed_job(self, job_id: int, **kwargs) -> bool:
        _ = job_id, kwargs.pop("run_attempt"), kwargs.pop("claim_owner")
        self.job.update(kwargs, status="completed", current_stage="completed")
        return True

    async def _load_previous_artifact(self, **kwargs):
        _ = kwargs
        return None

    async def _list_existing_slugs(self, tenant_id: str) -> set[str]:
        _ = tenant_id
        return set()


@pytest.mark.asyncio
async def test_run_extraction_resumes_completed_chunks_and_synthesizes_once(
    monkeypatch,
) -> None:
    store = _InMemoryPipelineStore(completed_chunk=True)
    calls: list[str] = []

    async def fake_call_llm_markdown(self, **kwargs):
        _ = self
        calls.append(kwargs["stage"])
        return json.dumps(
            {
                "work_md": "# 成员A 的工作能力画像\n\n- 务实",
                "persona_md": "# 成员A 的表达风格参考\n\n- 简洁",
                "skill_prompt": "# 成员A\n\n## PART A\n简洁\n\n## PART B\n直接",
                "impression": "表达简洁直接。",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        _InMemoryPipelineStore,
        "_call_llm_markdown",
        fake_call_llm_markdown,
    )
    result = await store.run_extraction(
        41,
        [{"sender_name": "成员A", "text": "你好"}],
        object(),
        run_attempt=1,
        claim_owner="worker-a",
    )

    assert calls == ["synthesis"]
    assert store.job["status"] == "completed"
    assert result["artifact"]["files"]["work.md"].startswith("# 成员A")


@pytest.mark.asyncio
async def test_run_extraction_revalidates_scope_after_chunk_llm_before_persist(
    monkeypatch,
) -> None:
    store = _InMemoryPipelineStore()
    enabled = True
    llm_calls: list[str] = []

    async def execution_allowed() -> bool:
        return enabled

    async def fake_call_llm_markdown(self, **kwargs):
        nonlocal enabled
        _ = self
        llm_calls.append(kwargs["stage"])
        enabled = False
        return json.dumps({"tone_signals": ["简洁"], "confidence": 0.8})

    monkeypatch.setattr(
        _InMemoryPipelineStore,
        "_call_llm_markdown",
        fake_call_llm_markdown,
    )

    with pytest.raises(RuntimeError, match="persona_extract_scope_disabled"):
        await store.run_extraction(
            41,
            [{"sender_name": "成员A", "text": "你好"}],
            object(),
            run_attempt=1,
            claim_owner="worker-a",
            execution_allowed=execution_allowed,
        )

    assert llm_calls == ["map"]
    assert store.job["current_stage"] == "disabled"
    assert store.job["artifact"] is None

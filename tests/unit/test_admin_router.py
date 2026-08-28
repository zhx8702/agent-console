from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin.audit import AdminAuditEvent, install_admin_audit_middleware
from app.admin.dlq_service import DLQDeleteResult, DLQMessage, DLQReplayResult
from app.admin.kb_router import build_admin_router
from app.admin.mutation_ledger import (
    MutationIdempotencyConflictError,
    MutationOutcome,
)
from app.channel.adapters import WECHAT_SDK_DESCRIPTOR, ChannelAdapterRegistration
from app.common.config import Settings
from app.faq.engine import FAQEngine
from app.faq.store import FAQStore, InMemoryFAQRepository
from app.kb.ingest import IngestionService
from app.kb.service import InMemoryKBStore, KnowledgeBaseService
from app.kb.vector.memory_store import InMemoryVectorStore
from app.llm.providers.fake_provider import FakeProvider
from app.orchestrator.effect_handlers import EffectHandlerRegistry
from app.orchestrator.effect_log import PostgresEffectLog
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.manager import PluginLifecycleExecutionResult
from app.plugin.state import PluginScopeState, PluginScopeVersionConflictError
from tests.unit._schema_fixtures import bootstrap_effect_log_schema


class _ProbeRedis:
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

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def xadd(self, stream: str, payload: dict[str, str]) -> str:
        _ = stream, payload
        return "1-0"


class _FakeDLQService:
    def __init__(self) -> None:
        self.items = [
            DLQMessage(
                id="1-0",
                stream="cs:dlq",
                tenant_id="demo",
                origin_stream="cs:outbound",
                origin_id="orig-1",
                reason="client_error",
                attempts=5,
                payload={"tenant_id": "demo", "message": "hello"},
                headers={"tenant_id": "demo"},
            )
        ]
        self.replay_calls: list[tuple[str, bool, str]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.delete_records: dict[str, str] = {}

    async def list_messages(
        self,
        *,
        limit: int = 100,
        before_id: str | None = None,
        tenant_id: str | None = None,
    ) -> tuple[list[DLQMessage], str | None]:
        _ = before_id
        rows = list(self.items)
        if tenant_id:
            rows = [item for item in rows if item.tenant_id == tenant_id]
        return rows[:limit], None

    async def get_message(self, entry_id: str) -> DLQMessage | None:
        for item in self.items:
            if item.id == entry_id:
                return item
        return None

    async def replay_message(
        self,
        entry_id: str,
        *,
        idempotency_key: str,
        delete_after_replay: bool = True,
    ) -> DLQReplayResult:
        item = await self.get_message(entry_id)
        if item is None:
            raise KeyError(entry_id)
        self.replay_calls.append((entry_id, delete_after_replay, idempotency_key))
        return DLQReplayResult(
            entry_id=item.id,
            origin_stream=item.origin_stream,
            replayed_message_id="999-0",
            deleted=delete_after_replay,
            tenant_id=str(item.tenant_id or ""),
        )

    async def delete_message(
        self,
        entry_id: str,
        *,
        idempotency_key: str,
    ) -> DLQDeleteResult:
        existing = self.delete_records.get(idempotency_key)
        if existing is not None:
            if existing != entry_id:
                raise ValueError("dlq_delete_idempotency_conflict")
            return DLQDeleteResult(
                entry_id=entry_id,
                deleted=True,
                tenant_id="demo",
                idempotent_replayed=True,
            )
        for idx, item in enumerate(self.items):
            if item.id == entry_id:
                del self.items[idx]
                self.delete_records[idempotency_key] = entry_id
                self.delete_calls.append((entry_id, idempotency_key))
                return DLQDeleteResult(
                    entry_id=entry_id,
                    deleted=True,
                    tenant_id=str(item.tenant_id or ""),
                )
        raise KeyError(entry_id)


class _CaptureAuditSink:
    def __init__(self) -> None:
        self.events: list[AdminAuditEvent] = []

    async def write(self, event: AdminAuditEvent) -> None:
        self.events.append(event)


class _FakeHookRunner:
    @property
    def summary(self) -> dict[str, list[str]]:
        return {"before_capability": ["persona_extract.skill_injector"]}

    def owner_summary(self) -> dict[str, list[str]]:
        return {"persona_extract": ["persona_extract.skill_injector"]}


class _FakePluginRegistry:
    def __init__(self) -> None:
        self.hook_runner = _FakeHookRunner()

    @property
    def summary(self) -> list[dict[str, str]]:
        return [
            {
                "name": "wxbot",
                "version": "0.2.0",
                "description": "WeChat bot SDK bridge",
            },
            {
                "name": "persona_extract",
                "version": "0.1.0",
                "description": "Persona extraction",
            },
        ]

    def all_api_routers(self) -> list[tuple[str, object]]:
        return [("wxbot", object()), ("persona_extract", object())]

    def all_channel_adapters(self) -> list[ChannelAdapterRegistration]:
        return [ChannelAdapterRegistration(descriptor=WECHAT_SDK_DESCRIPTOR)]

    def all_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.persona_extract.skill_enrich",
                owner="persona_extract",
                name="Persona skill enrich",
                inputs={"event", "session", "pre", "route"},
                outputs={"signals.persona"},
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="plugin.wxbot.reply_policy",
                owner="wxbot",
                name="WeChat reply policy",
                inputs={"event", "session", "pre"},
                outputs={"signals.reply_policy"},
                error_policy="fail_open",
            ),
        ]

    def all_permissions(self) -> dict[str, set[str]]:
        return {
            "persona_extract": {"storage:shared"},
            "wxbot": {"hooks:pipeline"},
        }


class _FakePluginScopeStore:
    def __init__(self) -> None:
        self.version = 1
        self.enabled = True
        self.records: dict[str, tuple[str, MutationOutcome]] = {}

    async def list_scope_states(
        self,
        *,
        tenant_id: str,
        session_id: str | None = None,
        plugin_name: str = "",
    ) -> list[PluginScopeState]:
        return [
            PluginScopeState(
                tenant_id=tenant_id,
                session_id=session_id or "",
                plugin_name=plugin_name or "draw",
                enabled=self.enabled,
                config={"session_name": "测试群"},
                version=self.version,
                updated_at="2026-07-18T00:00:00Z",
            )
        ]

    async def run_admin_mutation(self, *, identity, audit, mutate) -> MutationOutcome:
        _ = audit
        fingerprint = repr(identity.request_payload)
        existing = self.records.get(identity.idempotency_key)
        if existing is not None:
            if existing[0] != fingerprint:
                raise MutationIdempotencyConflictError("test conflict")
            return MutationOutcome(
                response=existing[1].response,
                status_code=existing[1].status_code,
                replayed=True,
                mutation_id=existing[1].mutation_id,
            )
        change = await mutate()
        outcome = MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id="plugin-scope-test",
        )
        self.records[identity.idempotency_key] = (fingerprint, outcome)
        return outcome


class _FakePluginManager:
    def __init__(self) -> None:
        self.lifecycle_records: dict[
            str,
            tuple[tuple[str, str, str], PluginLifecycleExecutionResult],
        ] = {}
        self.lifecycle_side_effects = 0
        self.state_store = _FakePluginScopeStore()
        self.scope_mutations = 0

    async def execute_lifecycle(
        self,
        operation: str,
        plugin_name: str,
        body: dict[str, object],
        request,
        *,
        idempotency_key: str,
    ) -> PluginLifecycleExecutionResult:
        fingerprint = (operation, plugin_name, repr(sorted(body.items())))
        existing = self.lifecycle_records.get(idempotency_key)
        if existing is not None:
            if existing[0] != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="plugin_lifecycle_idempotency_conflict",
                )
            stored = existing[1]
            return PluginLifecycleExecutionResult(
                response=stored.response,
                before_state=stored.before_state,
                after_state=stored.after_state,
                policy_version=stored.policy_version,
                idempotent_replayed=True,
            )
        self.lifecycle_side_effects += 1
        if operation == "install":
            response = await self.install(body, request)
        elif operation == "upgrade":
            response = await self.upgrade(plugin_name, body, request)
        elif operation == "uninstall":
            response = await self.uninstall(plugin_name, request)
        elif operation == "enable":
            response = await self.enable(plugin_name, request)
        else:
            response = await self.disable(plugin_name, request)
        result = PluginLifecycleExecutionResult(
            response=response,
            before_state={"plugin_name": plugin_name, "enabled": True},
            after_state={"plugin_name": plugin_name, "enabled": operation != "disable"},
            policy_version=1,
        )
        self.lifecycle_records[idempotency_key] = (fingerprint, result)
        return result

    async def installed(self) -> dict[str, object]:
        return {"plugins": [{"name": "draw", "status": "active", "enabled": True}]}

    async def marketplace(self) -> dict[str, object]:
        return {
            "items": [
                {
                    "name": "draw",
                    "version": "0.1.0",
                    "installed": True,
                    "compatible": True,
                    "permissions": [{"id": "admin_api"}],
                    "warnings": [],
                }
            ],
            "restart_required": False,
        }

    async def install_preview(self, body: dict[str, object]) -> dict[str, object]:
        return {
            "name": body["name"],
            "version": "0.1.0",
            "compatible": True,
            "permission_changes": {"added": ["admin_api"], "removed": []},
            "restart_required": True,
            "permissions": [{"id": "admin_api"}],
            "warnings": [],
        }

    async def install(self, body: dict[str, object], request) -> dict[str, object]:
        _ = request
        return {"plugin": {"name": body["name"], "enabled": False, "restart_required": True}}

    async def upgrade_preview(self, name: str, body: dict[str, object]) -> dict[str, object]:
        _ = body
        return {
            "name": name,
            "version": "0.2.0",
            "compatible": True,
            "permissions": [{"id": "admin_api"}],
            "warnings": [],
        }

    async def upgrade(self, name: str, body: dict[str, object], request) -> dict[str, object]:
        _ = body
        _ = request
        return {"plugin": {"name": name, "version": "0.2.0", "restart_required": True}}

    async def uninstall(self, name: str, request) -> dict[str, object]:
        _ = request
        return {"plugin": {"name": name, "installed": False, "restart_required": True}}

    async def events(
        self,
        *,
        plugin_name: str = "",
        event_type: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, object]:
        return {
            "events": [
                {
                    "plugin_name": plugin_name or "draw",
                    "event_type": event_type or "install_succeeded",
                    "status": "ok",
                    "message": "",
                    "metadata": {"limit": limit, "offset": offset},
                }
            ]
        }

    async def scope_states(
        self,
        *,
        tenant_id: str,
        session_id: str | None = None,
        plugin_name: str = "",
    ) -> dict[str, object]:
        return {
            "items": [
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id or "",
                    "plugin_name": plugin_name or "draw",
                    "enabled": True,
                    "config": {"session_name": "测试群"},
                    "version": self.state_store.version,
                }
            ]
        }

    async def set_scope_state(
        self,
        name: str,
        body: dict[str, object],
        request,
        *,
        expected_version: int,
    ) -> dict[str, object]:
        _ = request
        if expected_version != self.state_store.version:
            raise PluginScopeVersionConflictError(
                expected=expected_version,
                current=self.state_store.version,
            )
        self.state_store.version += 1
        self.state_store.enabled = bool(body["enabled"])
        self.scope_mutations += 1
        return {
            "scope_state": {
                "tenant_id": body["tenant_id"],
                "session_id": body.get("session_id") or "",
                "plugin_name": name,
                "enabled": body["enabled"],
                "config": body.get("config") or {},
                "version": self.state_store.version,
            }
        }

    async def restart_instructions(self) -> dict[str, object]:
        return {
            "actionable": False,
            "restart_required": True,
            "message": "Restart the FastAPI process or container through the deployment system.",
        }

    async def config_schema(self, name: str) -> dict[str, object]:
        return {"plugin_name": name, "schema": {}, "admin_ui": {}}

    async def runtime(self, name: str) -> dict[str, object]:
        runtime_status = {"configured": True}
        return {"plugin_name": name, "runtime_status": runtime_status, "runtime": runtime_status}

    async def enable(self, name: str, request) -> dict[str, object]:
        return {"plugin": {"name": name, "enabled": True}, "restart_required": False}

    async def disable(self, name: str, request) -> dict[str, object]:
        return {
            "plugin": {"name": name, "enabled": False},
            "disable_mode": "runtime_filtered",
            "restart_required": False,
        }


class _FakeStreamService:
    async def summary(self) -> list[dict[str, object]]:
        return [
            {
                "stream_key": "inbound",
                "stream": "cs:inbound",
                "length": 2,
                "first_entry": "1000-0",
                "last_entry": "1001-0",
                "pending_total": 0,
                "groups": [
                    {
                        "name": "cs-system",
                        "consumers": 1,
                        "pending": 0,
                        "last_delivered_id": "1001-0",
                        "lag": 0,
                        "entries_read": 2,
                    }
                ],
            }
        ]

    async def list_messages(
        self,
        *,
        stream_key: str,
        limit: int = 100,
        before_id: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[list[object], str | None]:
        _ = (limit, before_id, tenant_id, session_id, trace_id)
        if stream_key != "inbound":
            raise KeyError(stream_key)
        item = type(
            "StreamMessage",
            (),
            {
                "id": "1001-0",
                "stream_key": "inbound",
                "stream": "cs:inbound",
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_a",
                "trace_id": "trace-1",
                "channel": "wechat",
                "attempts": 0,
                "reason": None,
                "origin_stream": None,
                "origin_id": None,
                "created_ts_ms": 1001,
                "payload": {"tenant_id": "demo", "trace_id": "trace-1"},
                "headers": {"tenant_id": "demo"},
            },
        )()
        return [item], None

    async def get_message(self, *, stream_key: str, entry_id: str):
        if stream_key != "inbound":
            raise KeyError(stream_key)
        if entry_id != "1001-0":
            return None
        items, _ = await self.list_messages(stream_key=stream_key)
        return items[0]


class _FakeMediaEventProvider:
    name = "fake_media"

    async def list_recent_media_events(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        _ = limit
        if tenant_id != "demo":
            return []
        if session_id and session_id != "group-1@chatroom":
            return []
        return [
            {
                "id": "media:fake:1002",
                "source": "media_event",
                "owner": "fake_media",
                "stream_key": "media_events",
                "stream": "admin:media_events",
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_a",
                "trace_id": None,
                "channel": "wechat",
                "attempts": 0,
                "reason": "message.media.ready",
                "origin_stream": None,
                "origin_id": "1002",
                "created_ts_ms": 1002,
                "headers": {"source": "media_event", "owner": "fake_media"},
                "payload": {
                    "message_id": "image-1002",
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "session_id": "group-1@chatroom",
                    "message": {
                        "type": "image",
                        "content": "[图片]",
                        "attachments": [
                            {
                                "type": "image",
                                "image_url": "http://127.0.0.1/images/image-1002.png",
                            }
                        ],
                    },
                },
            }
        ]


class _MatchingStreamService(_FakeStreamService):
    async def list_messages(self, **kwargs):
        items, cursor = await super().list_messages(**kwargs)
        items[0].payload = {
            "tenant_id": "demo",
            "message_id": "cx1:m:stable-message",
            "external_message_id": "image-1002",
            "connection_id": "wechat-main",
            "message": {"type": "text", "content": "[图片]", "attachments": []},
            "metadata": {
                "connection_id": "wechat-main",
                "external_message_id": "image-1002",
                "quote_text": "保留的引用原文",
                "media_status": "pending",
            },
        }
        return items, cursor


class _MatchingMediaEventProvider(_FakeMediaEventProvider):
    async def list_recent_media_events(self, **kwargs):
        rows = await super().list_recent_media_events(**kwargs)
        rows[0]["payload"].update(
            {
                "message_id": "cx1:m:stable-message",
                "external_message_id": "image-1002",
                "connection_id": "wechat-main",
                "metadata": {
                    "connection_id": "wechat-main",
                    "external_message_id": "image-1002",
                    "media_status": "ready",
                    "media": {"status": "ready"},
                },
            }
        )
        return rows


def _build_test_app(
    settings: Settings,
    dlq_service: _FakeDLQService | None = None,
    stream_service: _FakeStreamService | None = None,
    plugin_registry: _FakePluginRegistry | None = None,
    plugin_manager: object | None = None,
    orchestrator: object | None = None,
    effect_handler_registry: EffectHandlerRegistry | None = None,
    effect_log_store: PostgresEffectLog | None = None,
    media_event_providers: list[object] | None = None,
) -> FastAPI:
    vector = InMemoryVectorStore()
    llm = FakeProvider()
    faq_store = FAQStore(InMemoryFAQRepository(), vector, llm, embed_model=settings.llm_embed_model)
    faq_engine = FAQEngine(
        vector, llm, settings, threshold=0.88, embed_model=settings.llm_embed_model
    )
    kb_store = InMemoryKBStore()
    ingest = IngestionService(kb_store, vector, llm, settings)
    kb_service = KnowledgeBaseService(kb_store, vector, ingest)
    app = FastAPI()
    app.include_router(
        build_admin_router(
            faq_store,
            kb_service,
            settings,
            dlq_service,
            stream_service=stream_service,
            plugin_registry=plugin_registry,
            faq_engine=faq_engine,
            plugin_manager=plugin_manager,
            orchestrator=orchestrator,
            effect_handler_registry=effect_handler_registry,
            effect_log_store=effect_log_store,
            media_event_providers=media_event_providers,
        )
    )
    return app


@pytest.mark.asyncio
async def test_admin_router_requires_bearer() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(app=_build_test_app(settings))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/v1/admin/faqs?tenant_id=demo")

    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing_admin_bearer"


@pytest.mark.asyncio
async def test_admin_router_rejects_invalid_bearer() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(app=_build_test_app(settings))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/admin/faqs?tenant_id=demo",
            headers={"Authorization": "Bearer wrong_token"},
        )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "invalid_admin_bearer"


@pytest.mark.asyncio
async def test_admin_router_accepts_valid_bearer() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(app=_build_test_app(settings))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        create = await client.post(
            "/v1/admin/faqs",
            headers={"Authorization": "Bearer unit_admin_token"},
            json={
                "tenant_id": "demo",
                "question": "退款怎么处理",
                "answer": "进入订单页发起退款。",
            },
        )
        listing = await client.get(
            "/v1/admin/faqs?tenant_id=demo",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert create.status_code == 200
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1


@pytest.mark.asyncio
async def test_admin_router_supports_session_scoped_faq_and_kb() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(app=_build_test_app(settings))
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        faq_create = await client.post(
            "/v1/admin/faqs",
            headers=headers,
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "question": "群 FAQ",
                "answer": "群答案",
            },
        )
        faq_list = await client.get(
            "/v1/admin/faqs?tenant_id=demo&session_id=group-1@chatroom",
            headers=headers,
        )
        doc_create = await client.post(
            "/v1/admin/kb/documents",
            headers=headers,
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "title": "群文档",
                "content": "这是群专属知识。",
            },
        )
        doc_list = await client.get(
            "/v1/admin/kb/documents?tenant_id=demo&session_id=group-1@chatroom",
            headers=headers,
        )

    assert faq_create.status_code == 200
    assert faq_create.json()["scope"] == "session"
    assert faq_create.json()["session_id"] == "group-1@chatroom"
    assert faq_list.status_code == 200
    assert faq_list.json()["scope"] == "session"
    assert len(faq_list.json()["items"]) == 1
    assert doc_create.status_code == 200
    assert doc_create.json()["scope"] == "session"
    assert doc_list.status_code == 200
    assert doc_list.json()["scope"] == "session"
    assert doc_list.json()["items"][0]["session_id"] == "group-1@chatroom"


@pytest.mark.asyncio
async def test_admin_router_can_get_update_and_search_kb_documents() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(app=_build_test_app(settings))
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/admin/kb/documents",
            headers=headers,
            json={
                "tenant_id": "demo",
                "title": "退款政策",
                "content": "旧退款政策内容",
                "metadata": {"category": "policy"},
            },
        )
        doc_id = created.json()["doc_id"]
        fetched = await client.get(
            f"/v1/admin/kb/documents/{doc_id}?tenant_id=demo",
            headers=headers,
        )
        updated = await client.put(
            f"/v1/admin/kb/documents/{doc_id}?tenant_id=demo",
            headers=headers,
            json={
                "title": "退款政策",
                "content": "新退款政策支持七天内处理。",
                "source": "manual",
                "metadata": {"category": "policy"},
            },
        )
        search = await client.post(
            "/v1/admin/kb/documents/search",
            headers=headers,
            json={"tenant_id": "demo", "query": "七天退款", "top_k": 3},
        )

    assert created.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["content"] == "旧退款政策内容"
    assert updated.status_code == 200
    assert updated.json()["updated"] == doc_id
    assert search.status_code == 200
    assert search.json()["items"]
    assert search.json()["items"][0]["doc_id"] == doc_id


@pytest.mark.asyncio
async def test_admin_router_can_preview_faq_hit() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(app=_build_test_app(settings))
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post(
            "/v1/admin/faqs",
            headers=headers,
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "question": "退款怎么处理",
                "answer": "进入订单页发起退款。",
            },
        )
        preview = await client.post(
            "/v1/admin/faqs/test",
            headers=headers,
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "query": "退款怎么处理",
            },
        )
        miss = await client.post(
            "/v1/admin/faqs/test",
            headers=headers,
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "query": "这个群里谁最帅",
            },
        )

    assert created.status_code == 200
    assert preview.status_code == 200
    assert preview.json()["matched"] is True
    assert preview.json()["reply_text"] == "进入订单页发起退款。"
    assert preview.json()["resolved_scope"] == "session"
    assert preview.json()["resolved_session_id"] == "group-1@chatroom"
    assert preview.json()["citation"]["snippet"] == "退款怎么处理"
    assert miss.status_code == 200
    assert miss.json()["matched"] is False


@pytest.mark.asyncio
async def test_admin_router_faq_preview_returns_miss_when_collection_not_initialized() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(app=_build_test_app(settings))
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        preview = await client.post(
            "/v1/admin/faqs/test",
            headers=headers,
            json={
                "tenant_id": "demo",
                "query": "没有任何 FAQ 时做一次测试",
            },
        )

    assert preview.status_code == 200
    assert preview.json()["matched"] is False


@pytest.mark.asyncio
async def test_admin_router_exposes_dlq_endpoints() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    dlq_service = _FakeDLQService()
    transport = httpx.ASGITransport(app=_build_test_app(settings, dlq_service))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get(
            "/v1/admin/dlq/messages?tenant_id=demo",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        missing_idempotency_key = await client.post(
            "/v1/admin/dlq/messages/1-0/replay",
            headers={"Authorization": "Bearer unit_admin_token"},
            json={"delete_after_replay": False},
        )
        replay = await client.post(
            "/v1/admin/dlq/messages/1-0/replay",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "Idempotency-Key": "dlq-replay-unit-1",
            },
            json={"delete_after_replay": False},
        )
        missing_delete_key = await client.delete(
            "/v1/admin/dlq/messages/1-0",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        deleted = await client.delete(
            "/v1/admin/dlq/messages/1-0",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "Idempotency-Key": "dlq-delete-unit-1",
            },
        )
        deleted_again = await client.delete(
            "/v1/admin/dlq/messages/1-0",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "Idempotency-Key": "dlq-delete-unit-1",
            },
        )

    assert listing.status_code == 200
    assert listing.json()["items"][0]["origin_stream"] == "cs:outbound"
    assert missing_idempotency_key.status_code == 400
    assert missing_idempotency_key.json()["detail"] == ("valid_idempotency_key_required")
    assert replay.status_code == 200
    assert replay.json()["replayed_message_id"] == "999-0"
    assert dlq_service.replay_calls == [("1-0", False, "dlq-replay-unit-1")]
    assert missing_delete_key.status_code == 400
    assert deleted.status_code == 200
    assert deleted_again.json() == deleted.json()
    assert deleted_again.headers["Idempotent-Replayed"] == "true"
    assert dlq_service.delete_calls == [("1-0", "dlq-delete-unit-1")]


@pytest.mark.asyncio
async def test_admin_router_keeps_dlq_when_knowledge_routes_disabled() -> None:
    settings = Settings(
        app_env="test",
        knowledge_features_enabled=False,
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    app = FastAPI()
    app.include_router(build_admin_router(None, None, settings, _FakeDLQService()))
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        dlq = await client.get("/v1/admin/dlq/messages?tenant_id=demo", headers=headers)
        faqs = await client.get("/v1/admin/faqs?tenant_id=demo", headers=headers)
        kb = await client.get("/v1/admin/kb/documents?tenant_id=demo", headers=headers)

    assert dlq.status_code == 200
    assert faqs.status_code == 404
    assert kb.status_code == 404


@pytest.mark.asyncio
async def test_admin_router_exposes_plugin_summary() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(settings, plugin_registry=_FakePluginRegistry())
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/admin/plugins/summary",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["plugins"][0]["name"] == "wxbot"
    assert "/plugins/wxbot" in payload["plugin_routes"]
    assert "wechat" in payload["channels"]
    assert payload["channel_adapters"][0]["adapter_id"] == "wechat-sdk"
    assert payload["hooks"]["before_capability"] == ["persona_extract.skill_injector"]
    assert payload["hook_owners"]["persona_extract"] == ["persona_extract.skill_injector"]


@pytest.mark.asyncio
async def test_admin_router_exposes_message_flows() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(settings, plugin_registry=_FakePluginRegistry())
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/admin/message-flows",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    flows = {item["name"]: item for item in payload["items"]}
    flow = flows["default_compatible_flow"]
    assert flow["name"] == "default_compatible_flow"
    assert flow["status"] == "degraded"
    assert flow["bindings"][0]["channel"] == "*"
    assert [step["id"] for step in flow["steps"]][:3] == [
        "load_session",
        "before_preprocess_hooks",
        "preprocess",
    ]
    assert flow["steps"][-1]["id"] == "commit"
    assert flow["steps"][-1]["effectful"] is True
    assert flow["steps"][-1]["effects"] == ["effects.commit"]
    assert flows["default_private_channel_flow"]["status"] == "degraded"
    assert flows["default_private_channel_flow"]["bindings"][0]["session_kind"] == "private"
    assert flows["default_group_channel_flow"]["status"] == "invalid"
    assert flows["default_wechat_group_flow"]["bindings"][0]["channel"] == "wechat"
    assert payload["plugin_step_count"] == 2
    assert any(
        step["kind"] == "plugin.persona_extract.skill_enrich" for step in payload["step_registry"]
    )
    assert any(
        step["kind"] == "plugin.wxbot.reply_policy" and step["owner"] == "wxbot"
        for step in payload["step_registry"]
    )


@pytest.mark.asyncio
async def test_admin_router_exposes_message_flow_shadow_run() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(settings, plugin_registry=_FakePluginRegistry())
    )
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/admin/message-flows/default_compatible_flow/shadow-run",
            headers=headers,
        )
        missing = await client.get(
            "/v1/admin/message-flows/not_found/shadow-run",
            headers=headers,
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["profile"]["name"] == "default_compatible_flow"
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["ok"] is True
    assert payload["run"]["steps"][0]["status"] == "shadow"
    assert payload["run"]["steps"][0]["reason"] == "shadow_noop"
    assert missing.status_code == 404
    assert missing.json()["detail"] == "message_flow_not_found"


@pytest.mark.asyncio
async def test_admin_router_resolves_message_flow() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(settings, plugin_registry=_FakePluginRegistry())
    )
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        wechat = await client.get(
            "/v1/admin/message-flows/resolve"
            "?channel=wechat&session_id=room@chatroom&message_type=text",
            headers=headers,
        )
        discord = await client.get(
            "/v1/admin/message-flows/resolve"
            "?channel=discord&session_kind=group&session_id=channel-1&message_type=text",
            headers=headers,
        )
        web = await client.get(
            "/v1/admin/message-flows/resolve?channel=web&session_id=s1&message_type=text",
            headers=headers,
        )

    assert wechat.status_code == 200
    assert wechat.json()["request"]["session_kind"] == "group"
    assert wechat.json()["profile"]["name"] == "default_wechat_group_flow"
    assert wechat.json()["binding"]["channel"] == "wechat"
    assert discord.status_code == 200
    assert discord.json()["profile"]["name"] == "default_group_channel_flow"
    assert web.status_code == 200
    assert web.json()["request"]["session_kind"] == "private"
    assert web.json()["profile"]["name"] == "default_private_channel_flow"
    assert any(candidate["matched"] for candidate in wechat.json()["candidates"])


@pytest.mark.asyncio
async def test_admin_router_exposes_message_flow_runtime_config() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="auto",
        orchestrator_flow_runtime_allowed_names="default_compatible_flow",
        orchestrator_flow_shadow_enabled=True,
        orchestrator_flow_shadow_name="auto",
        orchestrator_flow_shadow_mode="core_dry_run",
        orchestrator_flow_shadow_plugin_dry_run_enabled=True,
        orchestrator_flow_shadow_effect_dry_run_enabled=True,
        orchestrator_flow_effect_commit_backend="none",
        orchestrator_flow_effect_handlers_enabled=False,
        orchestrator_flow_effect_handler_allowlist="",
        orchestrator_flow_effect_log_backend="none",
        orchestrator_flow_effect_log_failure_policy="fail_closed",
    )
    effect_handler_registry = EffectHandlerRegistry()
    effect_handler_registry.register(
        "enqueue_channel_reply",
        "channel",
        lambda effect, ctx, record: None,
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            plugin_registry=_FakePluginRegistry(),
            effect_handler_registry=effect_handler_registry,
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/admin/message-flows/runtime",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["runtime"] == {
        "enabled": True,
        "name": "auto",
        "allowed_names": ["default_compatible_flow"],
        "allow_target_flows": False,
        "allow_compatible_fallback": False,
        "allowed": False,
        "reason": "requested_flow_not_allowed",
    }
    assert payload["shadow"]["enabled"] is True
    assert payload["shadow"]["name"] == "auto"
    assert payload["shadow"]["mode"] == "core_dry_run"
    assert payload["shadow"]["plugin_dry_run_enabled"] is True
    assert payload["shadow"]["effect_dry_run_enabled"] is True
    assert payload["effect_commit"] == {
        "backend": "none",
        "allowed": True,
        "reason": "allowed",
        "ttl_seconds": 604800,
        "key_prefix": "cs:flow:effect",
        "stream": "cs:flow:effects",
        "handlers_enabled": False,
        "handler_allowlist": [],
        "handler_mode": "off",
        "handlers_commit_backend_safe": False,
        "log_backend": "none",
        "log_failure_policy": "fail_closed",
    }
    assert payload["effect_handlers"] == {
        "count": 1,
        "owners": ["channel"],
        "types": ["enqueue_channel_reply"],
        "fallbacks": [
            {
                "type": "enqueue_channel_reply",
                "owner": "channel",
                "fallback_for": "missing exact channel owner",
            }
        ],
        "items": [
            {
                "type": "enqueue_channel_reply",
                "owner": "channel",
                "handler": "function",
            }
        ],
    }
    assert payload["last_runtime_result"] is None
    assert payload["last_shadow_result"] is None


@pytest.mark.asyncio
async def test_admin_router_lists_message_flow_effect_log() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_effect_log_backend="postgres",
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await bootstrap_effect_log_schema(factory)
        effect_log = PostgresEffectLog(factory)
        await effect_log.ensure_schema()
        await effect_log.record(
            idempotency_key="effect:tenant-1:trace-1:memory:save_memory:0",
            tenant_id="tenant-1",
            session_id="session-1",
            trace_id="trace-1",
            owner="memory",
            type="save_memory",
            payload={"user_text": "secret", "assistant_text": "hidden"},
        )
        await effect_log.record(
            idempotency_key="effect:tenant-1:trace-2:wxbot:enqueue_channel_reply:0",
            tenant_id="tenant-1",
            session_id="session-2",
            trace_id="trace-2",
            owner="wxbot",
            type="enqueue_channel_reply",
            payload={"body": "hello"},
        )
        transport = httpx.ASGITransport(app=_build_test_app(settings, effect_log_store=effect_log))

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.get(
                "/v1/admin/message-flows/effects?owner=memory",
                headers={"Authorization": "Bearer unit_admin_token"},
            )
    finally:
        await engine.dispose()

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["enabled"] is True
    assert payload["backend"] == "postgres"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["owner"] == "memory"
    assert payload["items"][0]["type"] == "save_memory"
    assert payload["items"][0]["payload_keys"] == ["assistant_text", "user_text"]
    assert "payload" not in payload["items"][0]


@pytest.mark.asyncio
async def test_admin_router_summarizes_message_flow_effect_log() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_effect_log_backend="postgres",
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await bootstrap_effect_log_schema(factory)
        effect_log = PostgresEffectLog(factory)
        await effect_log.ensure_schema()
        await effect_log.record(
            idempotency_key="effect:summary:memory",
            tenant_id="tenant-1",
            session_id="session-1",
            trace_id="trace-1",
            owner="memory",
            type="save_memory",
            payload={"user_text": "secret"},
        )
        await effect_log.record(
            idempotency_key="effect:summary:wxbot",
            tenant_id="tenant-1",
            session_id="session-2",
            trace_id="trace-2",
            owner="wxbot",
            type="enqueue_channel_reply",
            payload={"body": "hello"},
        )
        transport = httpx.ASGITransport(app=_build_test_app(settings, effect_log_store=effect_log))

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.get(
                "/v1/admin/message-flows/effects/summary?tenant_id=tenant-1",
                headers={"Authorization": "Bearer unit_admin_token"},
            )
    finally:
        await engine.dispose()

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["enabled"] is True
    assert payload["backend"] == "postgres"
    assert payload["summary"]["total"] == 2
    assert {"owner": "memory", "count": 1} in payload["summary"]["by_owner"]
    assert {"type": "enqueue_channel_reply", "count": 1} in payload["summary"]["by_type"]


@pytest.mark.asyncio
async def test_admin_router_probes_message_flow_effect_dry_run_with_audit() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_effect_commit_backend="memory",
        orchestrator_flow_effect_log_backend="postgres",
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await bootstrap_effect_log_schema(factory)
        effect_log = PostgresEffectLog(factory)
        await effect_log.ensure_schema()
        registry = EffectHandlerRegistry()
        transport = httpx.ASGITransport(
            app=_build_test_app(
                settings,
                effect_handler_registry=registry,
                effect_log_store=effect_log,
            )
        )

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/v1/admin/message-flows/effects/probe",
                headers={"Authorization": "Bearer unit_admin_token"},
                json={
                    "dry_run": True,
                    "repeat": 2,
                    "tenant_id": "tenant-probe",
                    "session_id": "session-probe",
                    "user_id": "user-probe",
                    "user_text": "probe memory",
                },
            )
    finally:
        await engine.dispose()

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dry_run"] is True
    assert payload["repeat"] == 2
    assert payload["effect"]["owner"] == "memory"
    assert payload["effect"]["type"] == "save_memory"
    assert payload["config"]["commit_backend"] == "memory"
    assert [item["status"] for item in payload["dispatches"]] == ["dry_run", "duplicate"]
    assert payload["dispatch"]["status"] == "duplicate"
    assert payload["dispatch"]["dry_run"] is True
    assert payload["dispatch"]["payload_keys"] == [
        "assistant_text",
        "channel",
        "probe",
        "session_id",
        "source_key",
        "tenant_id",
        "trace_id",
        "user_id",
        "user_text",
    ]


@pytest.mark.asyncio
async def test_admin_router_probes_wxbot_channel_reply_dry_run() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_effect_commit_backend="memory",
        orchestrator_flow_effect_log_backend="none",
        orchestrator_flow_effect_handler_allowlist="wxbot:enqueue_channel_reply",
        orchestrator_flow_effect_handlers_enabled=True,
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            effect_handler_registry=EffectHandlerRegistry(),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/admin/message-flows/effects/probe",
            headers={"Authorization": "Bearer unit_admin_token"},
            json={
                "owner": "wxbot",
                "type": "enqueue_channel_reply",
                "dry_run": True,
                "repeat": 2,
                "session_id": "admin-probe-wxbot-session",
                "assistant_text": "wxbot probe reply",
            },
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["effect"]["owner"] == "wxbot"
    assert payload["effect"]["type"] == "enqueue_channel_reply"
    assert [item["status"] for item in payload["dispatches"]] == ["dry_run", "duplicate"]
    assert payload["dispatches"][0]["payload_keys"] == [
        "body",
        "channel",
        "command_id",
        "delivery",
        "mention_sender",
        "probe",
        "reply_to_msg_svr_id",
        "sender_name",
        "sender_wxid",
        "session_id",
        "session_kind",
        "session_name",
        "source_message",
        "tenant_id",
        "trace_id",
        "user_id",
    ]


@pytest.mark.asyncio
async def test_admin_router_probe_rejects_real_run_without_redis_commit() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_effect_commit_backend="memory",
        orchestrator_flow_effect_handlers_enabled=True,
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            effect_handler_registry=EffectHandlerRegistry(),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/admin/message-flows/effects/probe",
            headers={
                "Authorization": "Bearer unit_admin_token",
                "Idempotency-Key": "probe-real-memory-backend",
            },
            json={"dry_run": False, "user_text": "probe memory"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "real_probe_requires_redis_commit_backend"


@pytest.mark.asyncio
async def test_admin_router_probe_requires_header_idempotency_key_for_real_run() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_effect_commit_backend="memory",
        orchestrator_flow_effect_handlers_enabled=True,
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            effect_handler_registry=EffectHandlerRegistry(),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/admin/message-flows/effects/probe",
            headers={"Authorization": "Bearer unit_admin_token"},
            json={
                "dry_run": False,
                "user_text": "probe memory",
                "idempotency_key": "legacy-body-key-is-not-enough",
            },
        )

    assert resp.status_code == 428
    assert resp.json()["detail"] == {"code": "idempotency_key_required"}


@pytest.mark.asyncio
async def test_admin_router_real_probe_replays_and_rejects_key_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.infra import redis_client as redis_client_module

    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_effect_commit_backend="redis",
        orchestrator_flow_effect_commit_stream="",
        orchestrator_flow_effect_handlers_enabled=True,
        orchestrator_flow_effect_handler_allowlist="memory:save_memory",
    )
    redis = _ProbeRedis()
    monkeypatch.setattr(redis_client_module, "get_redis", lambda: redis)
    calls: list[str] = []

    async def handler(effect, ctx, record) -> None:
        _ = ctx, record
        calls.append(str(effect.payload.get("user_text") or ""))

    registry = EffectHandlerRegistry()
    registry.register("save_memory", "memory", handler)
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            effect_handler_registry=registry,
        )
    )
    headers = {
        "Authorization": "Bearer unit_admin_token",
        "Idempotency-Key": "real-probe-user-intent-1",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/v1/admin/message-flows/effects/probe",
            headers=headers,
            json={"dry_run": False, "user_text": "first payload"},
        )
        replay = await client.post(
            "/v1/admin/message-flows/effects/probe",
            headers=headers,
            json={"dry_run": False, "user_text": "first payload"},
        )
        conflict = await client.post(
            "/v1/admin/message-flows/effects/probe",
            headers=headers,
            json={"dry_run": False, "user_text": "different payload"},
        )

    assert first.status_code == 200
    assert first.json()["dispatch"]["status"] == "recorded"
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["dispatch"]["status"] == "duplicate"
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {"code": "idempotency_key_conflict"}
    assert calls == ["first payload"]


@pytest.mark.asyncio
async def test_admin_router_probe_rejects_unsupported_effect() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            effect_handler_registry=EffectHandlerRegistry(),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/admin/message-flows/effects/probe",
            headers={"Authorization": "Bearer unit_admin_token"},
            json={"owner": "wxbot", "type": "enqueue_wxbot_reply"},
        )

    assert resp.status_code == 400
    assert (
        resp.json()["detail"] == "unsupported_probe_effect:memory_save_or_wxbot_channel_reply_only"
    )


@pytest.mark.asyncio
async def test_admin_router_effect_log_endpoint_reports_disabled() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_effect_log_backend="none",
    )
    transport = httpx.ASGITransport(app=_build_test_app(settings))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/admin/message-flows/effects",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": False,
        "backend": "none",
        "items": [],
    }


@pytest.mark.asyncio
async def test_admin_router_exposes_plugin_marketplace_management_endpoints() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(settings, plugin_manager=_FakePluginManager())
    )
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        installed = await client.get("/v1/admin/plugins/installed", headers=headers)
        marketplace = await client.get("/v1/admin/plugins/marketplace", headers=headers)
        install_preview = await client.post(
            "/v1/admin/plugins/install/preview",
            headers=headers,
            json={"name": "draw"},
        )
        install = await client.post(
            "/v1/admin/plugins/install",
            headers={**headers, "Idempotency-Key": "plugin-install-draw-1"},
            json={
                "name": "draw",
                "confirm_permissions": ["admin_api"],
                "confirm_restart_required": True,
            },
        )
        upgrade_preview = await client.post(
            "/v1/admin/plugins/draw/upgrade/preview",
            headers=headers,
            json={},
        )
        upgrade = await client.post(
            "/v1/admin/plugins/draw/upgrade",
            headers={**headers, "Idempotency-Key": "plugin-upgrade-draw-1"},
            json={"confirm_permissions": ["admin_api"], "confirm_restart_required": True},
        )
        uninstall = await client.post(
            "/v1/admin/plugins/draw/uninstall",
            headers={**headers, "Idempotency-Key": "plugin-uninstall-draw-1"},
        )
        events = await client.get(
            "/v1/admin/plugins/events?plugin_name=draw&event_type=install_succeeded&limit=25&offset=2",
            headers=headers,
        )
        scopes = await client.get(
            "/v1/admin/plugins/scopes?tenant_id=demo&session_id=room-1&plugin_name=draw",
            headers=headers,
        )
        scope_update = await client.post(
            "/v1/admin/plugins/draw/scopes",
            headers={
                **headers,
                "If-Match": '"plugin-scope-1"',
                "Idempotency-Key": "plugin-scope-draw-room-1",
            },
            json={
                "tenant_id": "demo",
                "session_id": "room-1",
                "enabled": False,
                "config": {"session_name": "测试群"},
            },
        )
        restart = await client.post("/v1/admin/runtime/restart-instructions", headers=headers)
        schema = await client.get("/v1/admin/plugins/draw/config-schema", headers=headers)
        runtime = await client.get("/v1/admin/plugins/draw/runtime", headers=headers)
        disabled = await client.post(
            "/v1/admin/plugins/draw/disable",
            headers={**headers, "Idempotency-Key": "plugin-disable-draw-1"},
        )
        enabled = await client.post(
            "/v1/admin/plugins/draw/enable",
            headers={**headers, "Idempotency-Key": "plugin-enable-draw-1"},
        )

    assert installed.status_code == 200
    assert installed.json()["plugins"][0]["name"] == "draw"
    assert marketplace.json()["items"][0]["name"] == "draw"
    assert install_preview.json()["permission_changes"]["added"] == ["admin_api"]
    assert install.json()["plugin"]["restart_required"] is True
    assert upgrade_preview.json()["version"] == "0.2.0"
    assert upgrade.json()["plugin"]["version"] == "0.2.0"
    assert uninstall.json()["plugin"]["installed"] is False
    assert events.json()["events"][0]["metadata"] == {"limit": 25, "offset": 2}
    assert scopes.json()["items"][0]["session_id"] == "room-1"
    assert scope_update.json()["scope_state"]["enabled"] is False
    assert restart.json()["actionable"] is False
    assert schema.status_code == 200
    assert runtime.json()["runtime_status"]["configured"] is True
    assert disabled.json()["plugin"]["enabled"] is False
    assert enabled.json()["plugin"]["enabled"] is True


@pytest.mark.asyncio
async def test_plugin_scope_route_requires_cas_and_exactly_replays_one_mutation() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    manager = _FakePluginManager()
    transport = httpx.ASGITransport(app=_build_test_app(settings, plugin_manager=manager))
    auth = {"Authorization": "Bearer unit_admin_token"}
    body = {
        "tenant_id": "demo",
        "session_id": "room-1",
        "enabled": False,
        "config": {"session_name": "测试群"},
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        initial = await client.get(
            "/v1/admin/plugins/scopes?tenant_id=demo&session_id=room-1&plugin_name=draw",
            headers=auth,
        )
        missing_if_match = await client.post(
            "/v1/admin/plugins/draw/scopes",
            headers=auth,
            json=body,
        )
        missing_key = await client.post(
            "/v1/admin/plugins/draw/scopes",
            headers={**auth, "If-Match": '"plugin-scope-1"'},
            json=body,
        )
        mutation_headers = {
            **auth,
            "If-Match": '"plugin-scope-1"',
            "Idempotency-Key": "plugin-scope-exact-replay",
        }
        first = await client.post(
            "/v1/admin/plugins/draw/scopes",
            headers=mutation_headers,
            json=body,
        )
        replay = await client.post(
            "/v1/admin/plugins/draw/scopes",
            headers=mutation_headers,
            json=body,
        )
        conflict = await client.post(
            "/v1/admin/plugins/draw/scopes",
            headers=mutation_headers,
            json={**body, "enabled": True},
        )
        stale = await client.post(
            "/v1/admin/plugins/draw/scopes",
            headers={
                **auth,
                "If-Match": '"plugin-scope-1"',
                "Idempotency-Key": "plugin-scope-stale-version",
            },
            json={**body, "enabled": True},
        )

    assert initial.status_code == 200
    assert initial.headers["etag"] == '"plugin-scope-1"'
    assert missing_if_match.status_code == 428
    assert missing_key.status_code == 428
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"plugin-scope-2"'
    assert replay.headers["idempotent-replayed"] == "true"
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"
    assert stale.status_code == 409
    assert stale.headers["etag"] == '"plugin-scope-2"'
    assert stale.json()["detail"]["code"] == "version_conflict"
    assert manager.scope_mutations == 1


@pytest.mark.asyncio
async def test_plugin_lifecycle_routes_require_key_replay_and_reject_key_rebinding() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    manager = _FakePluginManager()
    transport = httpx.ASGITransport(app=_build_test_app(settings, plugin_manager=manager))
    auth = {"Authorization": "Bearer unit_admin_token"}
    install_body = {
        "name": "draw",
        "confirm_permissions": ["admin_api"],
        "confirm_restart_required": True,
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing = [
            await client.post("/v1/admin/plugins/install", headers=auth, json=install_body),
            await client.post("/v1/admin/plugins/draw/enable", headers=auth),
            await client.post("/v1/admin/plugins/draw/disable", headers=auth),
            await client.post("/v1/admin/plugins/draw/upgrade", headers=auth, json={}),
            await client.post("/v1/admin/plugins/draw/uninstall", headers=auth),
        ]
        mutation_headers = {
            **auth,
            "Idempotency-Key": "plugin-install-draw-exact-replay-1",
        }
        first = await client.post(
            "/v1/admin/plugins/install",
            headers=mutation_headers,
            json=install_body,
        )
        replay = await client.post(
            "/v1/admin/plugins/install",
            headers=mutation_headers,
            json=install_body,
        )
        conflict = await client.post(
            "/v1/admin/plugins/install",
            headers=mutation_headers,
            json={**install_body, "confirm_restart_required": False},
        )

    assert {response.status_code for response in missing} == {400}
    assert all(
        response.json()["detail"] == "valid_idempotency_key_required" for response in missing
    )
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "plugin_lifecycle_idempotency_conflict"
    assert manager.lifecycle_side_effects == 1


@pytest.mark.asyncio
async def test_plugin_lifecycle_and_dlq_audits_are_semantic_scoped_and_redacted() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    sink = _CaptureAuditSink()
    app = _build_test_app(
        settings,
        _FakeDLQService(),
        plugin_manager=_FakePluginManager(),
    )
    install_admin_audit_middleware(app, settings, sink=sink)
    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": "Bearer unit_admin_token", "X-Trace-ID": "trace-safe-1"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        plugin = await client.post(
            "/v1/admin/plugins/install",
            headers={**auth, "Idempotency-Key": "plugin-secret-key-not-audited"},
            json={
                "name": "draw",
                "confirm_permissions": ["admin_api"],
                "confirm_restart_required": True,
            },
        )
        replay = await client.post(
            "/v1/admin/dlq/messages/1-0/replay",
            headers={**auth, "Idempotency-Key": "replay-secret-key-not-audited"},
            json={"delete_after_replay": False},
        )
        deleted = await client.delete(
            "/v1/admin/dlq/messages/1-0",
            headers={**auth, "Idempotency-Key": "delete-secret-key-not-audited"},
        )

    assert plugin.status_code == replay.status_code == deleted.status_code == 200
    assert len(sink.events) == 3
    by_reason = {event.reason: event for event in sink.events}
    assert set(by_reason) == {"plugin_install", "dlq_replay", "dlq_delete"}
    for event in sink.events:
        assert event.actor
        assert event.permission == "admin:danger"
        assert event.trace_id == "trace-safe-1"
        assert event.policy_version == 1
        assert event.before_state
        assert event.after_state
        assert "platform_admin" in event.after_state["actor_roles"]
        assert event.after_state["scope_type"] in {"platform", "tenant"}
        assert event.idempotency_key

    assert by_reason["plugin_install"].tenant_id == "*"
    assert by_reason["dlq_replay"].tenant_id not in {"", "demo"}
    rendered = json.dumps(
        [event.as_dict() for event in sink.events],
        ensure_ascii=False,
        sort_keys=True,
    )
    for forbidden in (
        "hello",
        "client_error",
        "confirm_permissions",
        "plugin-secret-key-not-audited",
        "replay-secret-key-not-audited",
        "delete-secret-key-not-audited",
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_admin_router_exposes_stream_admin_endpoints() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(settings, stream_service=_FakeStreamService())
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        summary = await client.get(
            "/v1/admin/streams/summary",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        listing = await client.get(
            "/v1/admin/streams/messages?stream=inbound&tenant_id=demo",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        detail = await client.get(
            "/v1/admin/streams/messages/inbound/1001-0",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert summary.status_code == 200
    assert summary.json()["streams"][0]["stream_key"] == "inbound"
    assert listing.status_code == 200
    assert listing.json()["items"][0]["trace_id"] == "trace-1"
    assert detail.status_code == 200
    assert detail.json()["session_id"] == "group-1@chatroom"


@pytest.mark.asyncio
async def test_admin_router_exposes_recent_messages_with_media_events() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            stream_service=_FakeStreamService(),
            media_event_providers=[_FakeMediaEventProvider()],
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get(
            "/v1/admin/streams/recent-messages?stream=inbound&tenant_id=demo",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert listing.status_code == 200
    payload = listing.json()
    assert [item["source"] for item in payload["items"]] == ["media_event", "stream"]
    assert payload["items"][0]["payload"]["message"]["attachments"][0]["image_url"].endswith(
        "/image-1002.png"
    )
    assert payload["sources"]["stream"]["count"] == 1
    assert payload["sources"]["media_events"]["count"] == 1
    assert payload["sources"]["media_events"]["providers"] == ["fake_media"]


@pytest.mark.asyncio
async def test_admin_router_enriches_pending_stream_message_with_ready_media() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            stream_service=_MatchingStreamService(),
            media_event_providers=[_MatchingMediaEventProvider()],
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get(
            "/v1/admin/streams/recent-messages?stream=inbound&tenant_id=demo",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == "1001-0"
    assert items[0]["source"] == "stream"
    assert items[0]["created_ts_ms"] == 1002
    merged_payload = items[0]["payload"]
    assert merged_payload["message"]["content"] == "[图片]"
    assert merged_payload["message"]["attachments"][0]["image_url"].endswith(
        "/image-1002.png"
    )
    assert merged_payload["metadata"]["media_status"] == "ready"
    assert merged_payload["metadata"]["quote_text"] == "保留的引用原文"


@pytest.mark.asyncio
async def test_admin_router_recent_messages_respects_merged_limit() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            stream_service=_FakeStreamService(),
            media_event_providers=[_FakeMediaEventProvider()],
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get(
            "/v1/admin/streams/recent-messages?stream=inbound&tenant_id=demo&limit=1",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1
    assert listing.json()["items"][0]["source"] == "media_event"


@pytest.mark.asyncio
async def test_admin_router_recent_messages_excludes_media_newer_than_cursor() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    transport = httpx.ASGITransport(
        app=_build_test_app(
            settings,
            stream_service=_FakeStreamService(),
            media_event_providers=[_FakeMediaEventProvider()],
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get(
            "/v1/admin/streams/recent-messages"
            "?stream=inbound&tenant_id=demo&before_id=1001-0",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert listing.status_code == 200
    payload = listing.json()
    assert [item["source"] for item in payload["items"]] == ["stream"]
    assert payload["sources"]["media_events"]["count"] == 0

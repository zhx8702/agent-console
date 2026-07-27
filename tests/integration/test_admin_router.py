"""Integration: admin router against real Postgres + in-process container."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from tests.integration.conftest import requires_postgres, requires_redis

pytestmark = [pytest.mark.integration, requires_redis, requires_postgres]


def _admin_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_faq_crud_roundtrip(redis_client):
    from app.admin.kb_router import build_admin_router
    from app.common.config import get_settings
    from app.main import build_container

    get_settings.cache_clear()
    settings = get_settings()
    container = await build_container(settings)
    app = FastAPI()
    app.include_router(
        build_admin_router(
            container.faq_store,
            container.kb_service,
            settings,
        )
    )
    transport = httpx.ASGITransport(app=app)
    headers = _admin_headers(settings.admin_bearer_token)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create
        resp = await client.post(
            "/v1/admin/faqs",
            headers=headers,
            json={
                "tenant_id": "demo",
                "question": "如何联系客服",
                "answer": "请点击页面右下角的联系客服按钮。",
                "tags": ["contact"],
            },
        )
        assert resp.status_code == 200, resp.text
        faq_id = resp.json()["id"]

        # List
        resp = await client.get("/v1/admin/faqs?tenant_id=demo", headers=headers)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert any(it["id"] == faq_id for it in items)

        # Update
        resp = await client.put(
            f"/v1/admin/faqs/{faq_id}?tenant_id=demo",
            headers=headers,
            json={"answer": "更新后的答案"},
        )
        assert resp.status_code == 200
        assert resp.json()["answer"] == "更新后的答案"

        # Delete
        resp = await client.delete(
            f"/v1/admin/faqs/{faq_id}?tenant_id=demo",
            headers={
                **headers,
                "Idempotency-Key": f"integration-faq-delete-{faq_id}",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] == faq_id


@pytest.mark.asyncio
async def test_kb_document_crud(redis_client):
    from app.admin.kb_router import build_admin_router
    from app.common.config import get_settings
    from app.main import build_container

    get_settings.cache_clear()
    settings = get_settings()
    container = await build_container(settings)
    app = FastAPI()
    app.include_router(
        build_admin_router(
            container.faq_store,
            container.kb_service,
            settings,
        )
    )
    transport = httpx.ASGITransport(app=app)
    headers = _admin_headers(settings.admin_bearer_token)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/admin/kb/documents",
            headers=headers,
            json={
                "tenant_id": "demo",
                "title": "Return policy",
                "content": "退货政策: 收到商品后7天内可无理由退货。",
                "source": "manual",
            },
        )
        assert resp.status_code == 200, resp.text
        doc_id = resp.json()["doc_id"]

        resp = await client.get("/v1/admin/kb/documents?tenant_id=demo", headers=headers)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert any(d["id"] == doc_id for d in items)

        resp = await client.delete(
            f"/v1/admin/kb/documents/{doc_id}?tenant_id=demo",
            headers={
                **headers,
                "Idempotency-Key": f"integration-document-delete-{doc_id}",
            },
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_dlq_admin_list_replay_delete(redis_client):
    from app.admin.kb_router import build_admin_router
    from app.bus.base import BusMessage
    from app.bus.redis_streams import RedisStreamBus
    from app.common.config import get_settings
    from app.main import build_container

    get_settings.cache_clear()
    settings = get_settings()
    await redis_client.delete(settings.bus_dlq_stream, settings.bus_outbound_stream)

    bus = RedisStreamBus(redis_client, settings)
    await bus.move_to_dlq(
        BusMessage(
            id="orig-1",
            stream=settings.bus_outbound_stream,
            payload={"tenant_id": "demo", "message": "failed delivery"},
            headers={"tenant_id": "demo", "trace_id": "tr_dlq_1"},
            attempts=4,
        ),
        reason="client_error",
    )

    container = await build_container(settings)
    app = FastAPI()
    app.include_router(
        build_admin_router(
            container.faq_store,
            container.kb_service,
            settings,
            container.dlq_admin_service,
        )
    )
    transport = httpx.ASGITransport(app=app)
    headers = _admin_headers(settings.admin_bearer_token)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listing = await client.get("/v1/admin/dlq/messages?tenant_id=demo", headers=headers)
        assert listing.status_code == 200, listing.text
        items = listing.json()["items"]
        assert len(items) == 1
        entry_id = items[0]["id"]
        assert items[0]["reason"] == "client_error"
        assert items[0]["origin_stream"] == settings.bus_outbound_stream

        replay_headers = {
            **headers,
            "Idempotency-Key": "dlq-integration-replay-1",
        }
        replay = await client.post(
            f"/v1/admin/dlq/messages/{entry_id}/replay",
            headers=replay_headers,
            json={"delete_after_replay": False},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["deleted"] is False

        replay_again = await client.post(
            f"/v1/admin/dlq/messages/{entry_id}/replay",
            headers=replay_headers,
            json={"delete_after_replay": False},
        )
        assert replay_again.status_code == 200, replay_again.text
        assert replay_again.json() == replay.json()
        assert replay_again.headers["Idempotent-Replayed"] == "true"

        replay_conflict = await client.post(
            f"/v1/admin/dlq/messages/{entry_id}/replay",
            headers=replay_headers,
            json={"delete_after_replay": True},
        )
        assert replay_conflict.status_code == 409, replay_conflict.text
        assert replay_conflict.json()["detail"] == "dlq_replay_idempotency_conflict"

        outbound_len = await redis_client.xlen(settings.bus_outbound_stream)
        dlq_len = await redis_client.xlen(settings.bus_dlq_stream)
        assert outbound_len == 1
        assert dlq_len == 1

        delete = await client.delete(
            f"/v1/admin/dlq/messages/{entry_id}",
            headers={**headers, "Idempotency-Key": "dlq-integration-delete-1"},
        )
        assert delete.status_code == 200, delete.text

        delete_again = await client.delete(
            f"/v1/admin/dlq/messages/{entry_id}",
            headers={**headers, "Idempotency-Key": "dlq-integration-delete-1"},
        )
        assert delete_again.status_code == 200, delete_again.text
        assert delete_again.json() == delete.json()
        assert delete_again.headers["Idempotent-Replayed"] == "true"

        after = await client.get("/v1/admin/dlq/messages?tenant_id=demo", headers=headers)
        assert after.status_code == 200
        assert after.json()["items"] == []

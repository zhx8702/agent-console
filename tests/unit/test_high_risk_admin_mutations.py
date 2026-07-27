from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.admin.kb_router as kb_router_module
import app.admin.mutation_ledger as mutation_ledger_module
from app.admin.kb_router import build_admin_router
from app.admin.mutation_ledger import (
    MutationIdempotencyConflictError,
    MutationOutcome,
    fingerprint,
)
from app.common.config import Settings
from plugins.draw import router as draw_router_module
from plugins.group_activity import router as group_activity_router_module
from plugins.moderation import router as moderation_router_module
from plugins.tibo_reset import router as tibo_reset_router_module


class _MemoryMutationRunner:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str], tuple[str, MutationOutcome]] = {}

    async def __call__(self, *, identity, audit, mutate) -> MutationOutcome:
        mutation_ledger_module._audit_metadata(audit.scope, field_name="scope")
        key = (
            identity.tenant_id,
            identity.plugin_name,
            identity.operation,
            identity.idempotency_key,
        )
        request_hash = fingerprint(
            {
                "resource_key": identity.resource_key,
                "request_payload": identity.request_payload,
            }
        )
        existing = self.rows.get(key)
        if existing is not None:
            if existing[0] != request_hash:
                raise MutationIdempotencyConflictError("key reused")
            previous = existing[1]
            return MutationOutcome(
                response=previous.response,
                status_code=previous.status_code,
                replayed=True,
                mutation_id=previous.mutation_id,
            )
        change = await mutate()
        mutation_ledger_module._audit_metadata(
            change.before_state,
            field_name="before_state",
        )
        mutation_ledger_module._audit_metadata(
            change.after_state,
            field_name="after_state",
        )
        outcome = MutationOutcome(
            response=change.response,
            status_code=change.status_code,
            replayed=False,
            mutation_id=f"mutation-{len(self.rows) + 1}",
        )
        self.rows[key] = (request_hash, outcome)
        return outcome


class _FaqStore:
    def __init__(self) -> None:
        self.items = {
            1: SimpleNamespace(id=1, version=3, status="published"),
            2: SimpleNamespace(id=2, version=1, status="disabled"),
        }
        self.delete_calls = 0

    async def list(self, tenant_id, session_id=None, limit=100, offset=0):
        _ = (tenant_id, session_id, limit, offset)
        return list(self.items.values())

    async def delete(self, tenant_id, faq_id, session_id=None):
        _ = (tenant_id, session_id)
        self.delete_calls += 1
        return self.items.pop(faq_id, None) is not None


class _KnowledgeService:
    def __init__(self) -> None:
        self.items = {
            7: SimpleNamespace(id=7, source="manual", url=""),
            8: SimpleNamespace(id=8, source="import", url="https://example.test"),
        }
        self.delete_calls = 0

    async def get_document(self, tenant_id, doc_id, session_id=None):
        _ = (tenant_id, session_id)
        return self.items.get(doc_id)

    async def delete_document(self, tenant_id, doc_id, session_id=None):
        _ = (tenant_id, session_id)
        self.delete_calls += 1
        self.items.pop(doc_id, None)


def _client(faq_store: _FaqStore, kb_service: _KnowledgeService) -> TestClient:
    settings = Settings(admin_bearer_token="unit-admin-secret")
    app = FastAPI()
    app.include_router(
        build_admin_router(
            faq_store=faq_store,  # type: ignore[arg-type]
            kb_service=kb_service,  # type: ignore[arg-type]
            settings=settings,
        )
    )
    return TestClient(app)


def test_faq_and_document_delete_require_key_and_exactly_replay(monkeypatch) -> None:
    runner = _MemoryMutationRunner()
    monkeypatch.setattr(kb_router_module, "_run_admin_mutation", runner)
    faq_store = _FaqStore()
    kb_service = _KnowledgeService()
    auth = {"Authorization": "Bearer unit-admin-secret"}

    with _client(faq_store, kb_service) as client:
        missing = client.delete("/v1/admin/faqs/1?tenant_id=demo", headers=auth)

        faq_headers = {**auth, "Idempotency-Key": "faq-lost-response"}
        faq_first = client.delete(
            "/v1/admin/faqs/1?tenant_id=demo",
            headers=faq_headers,
        )
        faq_replay = client.delete(
            "/v1/admin/faqs/1?tenant_id=demo",
            headers=faq_headers,
        )
        faq_conflict = client.delete(
            "/v1/admin/faqs/2?tenant_id=demo",
            headers=faq_headers,
        )
        faq_absent_headers = {**auth, "Idempotency-Key": "faq-already-absent"}
        faq_absent = client.delete(
            "/v1/admin/faqs/999?tenant_id=demo",
            headers=faq_absent_headers,
        )
        faq_absent_replay = client.delete(
            "/v1/admin/faqs/999?tenant_id=demo",
            headers=faq_absent_headers,
        )

        doc_headers = {**auth, "Idempotency-Key": "document-lost-response"}
        doc_first = client.delete(
            "/v1/admin/kb/documents/7?tenant_id=demo",
            headers=doc_headers,
        )
        doc_replay = client.delete(
            "/v1/admin/kb/documents/7?tenant_id=demo",
            headers=doc_headers,
        )
        doc_conflict = client.delete(
            "/v1/admin/kb/documents/8?tenant_id=demo",
            headers=doc_headers,
        )
        doc_absent_headers = {
            **auth,
            "Idempotency-Key": "document-already-absent",
        }
        doc_absent = client.delete(
            "/v1/admin/kb/documents/999?tenant_id=demo",
            headers=doc_absent_headers,
        )
        doc_absent_replay = client.delete(
            "/v1/admin/kb/documents/999?tenant_id=demo",
            headers=doc_absent_headers,
        )

    assert missing.status_code == 428
    assert faq_first.status_code == faq_replay.status_code == 200
    assert faq_first.json() == faq_replay.json()
    assert faq_replay.headers["Idempotent-Replayed"] == "true"
    assert faq_store.delete_calls == 1
    assert faq_conflict.status_code == 409
    assert faq_conflict.json()["detail"]["code"] == "idempotency_key_conflict"
    assert faq_absent.status_code == faq_absent_replay.status_code == 200
    assert faq_absent.json()["deleted"] is False
    assert faq_absent.json() == faq_absent_replay.json()
    assert faq_absent_replay.headers["Idempotent-Replayed"] == "true"

    assert doc_first.status_code == doc_replay.status_code == 200
    assert doc_first.json() == doc_replay.json()
    assert doc_replay.headers["Idempotent-Replayed"] == "true"
    assert kb_service.delete_calls == 1
    assert doc_conflict.status_code == 409
    assert doc_conflict.json()["detail"]["code"] == "idempotency_key_conflict"
    assert doc_absent.status_code == doc_absent_replay.status_code == 200
    assert doc_absent.json()["deleted"] is False
    assert doc_absent.json() == doc_absent_replay.json()
    assert doc_absent_replay.headers["Idempotent-Replayed"] == "true"


def test_plugin_audit_summaries_satisfy_secret_free_ledger_contract() -> None:
    task = SimpleNamespace(
        status="failed",
        retry_count=2,
        callback_sent=False,
    )
    summaries = [
        draw_router_module._task_audit_state(task),
        draw_router_module._retry_audit_state(
            {"retry_queued": False, "retry_count": 2, "max_retries": 3}
        ),
        draw_router_module._callback_audit_state(
            {"sent": True, "skipped": False, "force": True, "status": "completed"}
        ),
        draw_router_module._recover_audit_state(
            {"recovered": 1, "callbacks_sent": 1, "callback_failed": 0}
        ),
        group_activity_router_module._decision_audit_state(
            {
                "status": "queued",
                "reason_code": "eligible_after_quiet_window",
                "event_id": 17,
                "reply_queue_id": 23,
            }
        ),
        tibo_reset_router_module._poll_audit_state(
            {
                "status": "completed",
                "fetched": 4,
                "groups": 2,
                "claimed": 2,
                "queued": 2,
                "failed": 0,
            }
        ),
        moderation_router_module._keyword_audit_summary(
            [{"keyword": "private phrase", "enabled": True}],
            5,
        ),
    ]

    for index, summary in enumerate(summaries):
        validated = mutation_ledger_module._audit_metadata(
            summary,
            field_name=f"summary_{index}",
        )
        assert validated == summary

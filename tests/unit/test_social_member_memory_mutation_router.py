from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request

from app.admin.authorization import AdminRole, Principal
from app.admin.mutation_ledger import MutationOutcome
from app.social.contracts import MemberPrivacyValues
from app.social.router import build_social_admin_router


async def _authorize(request: Request) -> Principal:
    principal = Principal(
        subject="operator-a",
        roles=(AdminRole.TENANT_ADMIN.value,),
        tenant_ids=("tenant-a",),
        group_ids=("room-a",),
        auth_kind="session",
    )
    request.state.admin_principal = principal
    return principal


class _PolicyStore:
    async def get_member_policy(self, tenant_id: str, session_id: str, user_id: str):
        _ = tenant_id, session_id, user_id
        return SimpleNamespace(
            policy=MemberPrivacyValues(
                memory_enabled=True,
                allow_group_recall=True,
                audience_scope="session",
            )
        )

    async def member_memory_mutation_replayed(self, **kwargs):  # pragma: no cover
        raise AssertionError(f"legacy split idempotency path used: {kwargs}")

    async def record_member_memory_mutation(self, **kwargs):  # pragma: no cover
        raise AssertionError(f"legacy split audit path used: {kwargs}")


class _MemoryStore:
    def __init__(self) -> None:
        self.correct_calls = 0
        self.delete_calls = 0
        self._outcomes: dict[str, dict] = {}

    async def correct_group_member_memory_item_idempotent(self, item_id: int, **kwargs):
        key = str(kwargs["idempotency_key"])
        if key in self._outcomes:
            return MutationOutcome(
                response=self._outcomes[key],
                status_code=200,
                replayed=True,
                mutation_id="correct",
            )
        self.correct_calls += 1
        item = {
            "id": item_id,
            "content": kwargs["content"],
            "memory_type": "note",
            "scope_type": "session",
            "audience_scope": "session",
            "status": "active",
            "sensitivity_category": "normal",
            "pinned": False,
            "expires_at": None,
            "updated_at": datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
            "etag": '"memory-updated"',
        }
        self._outcomes[key] = item
        return MutationOutcome(
            response=item,
            status_code=200,
            replayed=False,
            mutation_id="correct",
        )

    async def delete_group_member_memory_item_idempotent(self, item_id: int, **kwargs):
        key = str(kwargs["idempotency_key"])
        if key in self._outcomes:
            return MutationOutcome(
                response=self._outcomes[key],
                status_code=200,
                replayed=True,
                mutation_id="delete",
            )
        self.delete_calls += 1
        result = {"item_id": item_id, "status": "deleted", "idempotent_replayed": False}
        self._outcomes[key] = result
        return MutationOutcome(
            response=result,
            status_code=200,
            replayed=False,
            mutation_id="delete",
        )


@pytest.mark.asyncio
async def test_member_memory_routes_use_atomic_memory_ledger_and_exact_replay() -> None:
    memory = _MemoryStore()
    app = FastAPI()
    app.include_router(
        build_social_admin_router(
            _PolicyStore(),  # type: ignore[arg-type]
            memory_store=memory,  # type: ignore[arg-type]
            authorization_dependency=_authorize,
        )
    )
    transport = httpx.ASGITransport(app=app)
    base = "/v1/admin/tenants/tenant-a/groups/room-a/members/member-a/memory-items/7"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        correction_headers = {
            "If-Match": '"memory-original"',
            "Idempotency-Key": "correct-7",
        }
        first_correction = await client.patch(
            base,
            headers=correction_headers,
            json={"content": "corrected", "reason": "member requested correction"},
        )
        replayed_correction = await client.patch(
            base,
            headers=correction_headers,
            json={"content": "corrected", "reason": "member requested correction"},
        )
        deletion_headers = {
            "If-Match": '"memory-updated"',
            "Idempotency-Key": "delete-7",
        }
        first_delete = await client.delete(base, headers=deletion_headers)
        replayed_delete = await client.delete(base, headers=deletion_headers)

    assert first_correction.status_code == 200
    assert replayed_correction.json() == first_correction.json()
    assert replayed_correction.headers["Idempotent-Replayed"] == "true"
    assert first_correction.headers["etag"] == '"memory-updated"'
    assert memory.correct_calls == 1

    assert first_delete.status_code == 200
    assert replayed_delete.json() == first_delete.json()
    assert replayed_delete.json()["idempotent_replayed"] is False
    assert replayed_delete.headers["Idempotent-Replayed"] == "true"
    assert memory.delete_calls == 1

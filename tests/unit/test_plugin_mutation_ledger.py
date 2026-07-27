from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    UnsafeMutationAuditError,
    plugin_admin_mutation_audit,
    plugin_admin_mutation_idempotency,
    run_idempotent_mutation,
)


async def _engine_with_schema(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ledger.db'}")
    business_metadata = MetaData()
    side_effect = Table(
        "side_effect",
        business_metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("marker", String(64), nullable=False),
    )
    async with engine.begin() as conn:
        await conn.run_sync(plugin_admin_mutation_idempotency.metadata.create_all)
        await conn.run_sync(business_metadata.create_all)
    return engine, side_effect


def _identity(*, key: str = "stable-key", payload=None) -> MutationIdentity:
    return MutationIdentity(
        tenant_id="tenant-a",
        plugin_name="memory",
        operation="memory.item.delete",
        resource_key="item:7",
        idempotency_key=key,
        request_payload=payload or {"item_id": 7, "allow_pinned": False},
    )


def _audit(*, scope=None, reason: str = "contains private prose") -> MutationAudit:
    return MutationAudit(
        actor="admin-a",
        actor_kind="bearer",
        roles=("tenant_admin",),
        scope=scope or {"item_id": 7, "session_hash": "a" * 64},
        reason_code="memory_item_delete",
        reason=reason,
        trace_id="trace-a",
    )


@pytest.mark.asyncio
async def test_exact_replay_conflict_and_secret_free_audit(tmp_path) -> None:
    engine, side_effect = await _engine_with_schema(tmp_path)

    async def execute(payload):
        async with engine.begin() as conn:
            async def mutate() -> MutationChange:
                await conn.execute(insert(side_effect).values(marker="once"))
                return MutationChange(
                    response={"ok": True, "private_response": "needed for exact replay"},
                    before_state={"exists": True, "status": "active"},
                    after_state={"exists": False, "status": "deleted"},
                    resource_version="v2",
                )

            return await run_idempotent_mutation(
                conn,
                identity=_identity(payload=payload),
                audit=_audit(),
                mutate=mutate,
            )

    first = await execute({"item_id": 7, "allow_pinned": False})
    replay = await execute({"allow_pinned": False, "item_id": 7})
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response == first.response

    with pytest.raises(MutationIdempotencyConflictError):
        await execute({"item_id": 7, "allow_pinned": True})

    async with engine.connect() as conn:
        effects = (await conn.execute(select(side_effect))).mappings().all()
        idempotency_rows = (
            await conn.execute(select(plugin_admin_mutation_idempotency))
        ).mappings().all()
        audits = (
            await conn.execute(select(plugin_admin_mutation_audit))
        ).mappings().all()
    assert len(effects) == 1
    assert len(idempotency_rows) == 1
    assert idempotency_rows[0]["idempotency_key_hash"] != "stable-key"
    assert "allow_pinned" not in str(dict(idempotency_rows[0]))
    assert len(audits) == 1
    audit = audits[0]
    audit_blob = str(dict(audit))
    assert "private prose" not in audit_blob
    assert "private_response" not in audit_blob
    assert audit["actor"] == "admin-a"
    assert audit["actor_kind"] == "bearer"
    assert audit["roles_json"] == ["tenant_admin"]
    assert audit["scope_json"] == {"item_id": 7, "session_hash": "a" * 64}
    assert audit["reason_hash"]
    assert audit["trace_id"] == "trace-a"
    assert audit["resource_version"] == "v2"
    assert audit["before_state_json"] == {"exists": True, "status": "active"}
    assert audit["after_state_json"] == {"exists": False, "status": "deleted"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_transaction_releases_key_for_clean_retry(tmp_path) -> None:
    engine, side_effect = await _engine_with_schema(tmp_path)

    with pytest.raises(RuntimeError, match="injected failure"):
        async with engine.begin() as conn:
            async def fail_after_business_write() -> MutationChange:
                await conn.execute(insert(side_effect).values(marker="rolled-back"))
                raise RuntimeError("injected failure")

            await run_idempotent_mutation(
                conn,
                identity=_identity(),
                audit=_audit(),
                mutate=fail_after_business_write,
            )

    async with engine.begin() as conn:
        async def retry() -> MutationChange:
            await conn.execute(insert(side_effect).values(marker="committed"))
            return MutationChange(response={"ok": True})

        outcome = await run_idempotent_mutation(
            conn,
            identity=_identity(),
            audit=_audit(),
            mutate=retry,
        )
    assert outcome.replayed is False
    async with engine.connect() as conn:
        assert (await conn.execute(select(side_effect.c.marker))).scalars().all() == ["committed"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_key_commits_one_side_effect(tmp_path) -> None:
    engine, side_effect = await _engine_with_schema(tmp_path)

    async def invoke():
        async with engine.begin() as conn:
            async def mutate() -> MutationChange:
                await conn.execute(insert(side_effect).values(marker="once"))
                return MutationChange(response={"ok": True})

            return await run_idempotent_mutation(
                conn,
                identity=_identity(),
                audit=_audit(),
                mutate=mutate,
            )

    outcomes = await asyncio.gather(invoke(), invoke())
    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    async with engine.connect() as conn:
        assert len((await conn.execute(select(side_effect))).all()) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_audit_rejects_prose_fields_and_rolls_back(tmp_path) -> None:
    engine, side_effect = await _engine_with_schema(tmp_path)
    with pytest.raises(UnsafeMutationAuditError):
        async with engine.begin() as conn:
            async def mutate() -> MutationChange:
                await conn.execute(insert(side_effect).values(marker="rolled-back"))
                return MutationChange(
                    response={"ok": True},
                    before_state={"raw_content": "must never reach audit"},
                )

            await run_idempotent_mutation(
                conn,
                identity=_identity(),
                audit=_audit(),
                mutate=mutate,
            )
    async with engine.connect() as conn:
        assert (await conn.execute(select(side_effect))).all() == []
    await engine.dispose()

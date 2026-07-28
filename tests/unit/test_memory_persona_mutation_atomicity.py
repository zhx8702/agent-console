from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.admin.mutation_ledger import (
    MutationIdempotencyConflictError,
    plugin_admin_mutation_idempotency,
)
from plugins.memory import store as memory_store_module
from plugins.memory.store import MemoryStore
from plugins.persona_extract import store as persona_store_module
from plugins.persona_extract.store import PersonaExtractStore


async def _create_ledger(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(plugin_admin_mutation_idempotency.metadata.create_all)


class _AtomicForgetStore(MemoryStore):
    def __init__(self) -> None:
        self.fail_after_write = False

    async def forget_memory_items(self, **kwargs):
        rows = await memory_store_module._exec(
            "UPDATE mutation_counter SET value = value + 1 WHERE id = 1 RETURNING value"
        )
        if self.fail_after_write:
            raise RuntimeError("injected memory failure")
        return {"ids": [int(kwargs.get("item_id") or 7)], "count": int(rows[0]["value"] > 0)}


async def _forget(store: _AtomicForgetStore, *, key: str, query: str = ""):
    return await store.forget_memory_items_idempotent(
        tenant_id="tenant-a",
        channel="wechat",
        source_key="wxbot",
        user_id="member-a",
        item_id=7,
        query=query,
        session_id="room-a",
        scope_type="session",
        allow_pinned=True,
        limit=20,
        idempotency_key=key,
        actor="operator-a",
        actor_kind="session",
        roles=("tenant_admin",),
        trace_id="trace-a",
    )


@pytest.mark.asyncio
async def test_memory_wrapper_replays_once_and_retry_after_rollback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory-atomic.db'}")
    await _create_ledger(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE mutation_counter (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        )
        await conn.execute(text("INSERT INTO mutation_counter (id, value) VALUES (1, 0)"))
    monkeypatch.setattr(memory_store_module, "get_engine", lambda: engine)
    store = _AtomicForgetStore()

    first = await _forget(store, key="forget-7")
    replay = await _forget(store, key="forget-7")
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response == first.response
    with pytest.raises(MutationIdempotencyConflictError):
        await _forget(store, key="forget-7", query="different canonical request")

    store.fail_after_write = True
    with pytest.raises(RuntimeError, match="injected memory failure"):
        await _forget(store, key="retryable-forget")
    store.fail_after_write = False
    retried = await _forget(store, key="retryable-forget")
    assert retried.replayed is False

    async with engine.connect() as conn:
        value = await conn.scalar(text("SELECT value FROM mutation_counter WHERE id = 1"))
    assert value == 2
    await engine.dispose()


def _profile_fields(*, prompt_text: str = "concise style") -> dict:
    return {
        "tenant_id": "tenant-a",
        "session_id": "room-a",
        "session_name": "Room A",
        "channel": "wechat",
        "source_key": "wxbot",
        "source_label": "verified group",
        "profile_name": "default",
        "target_user_id": "member-a",
        "target_name": "Member A",
        "skill_slug": "member-a",
        "prompt_text": prompt_text,
        "artifact": {
            "slug": "member-a",
            "target": {"user_id": "member-a", "name": "Member A"},
            "files": {"skill_prompt": prompt_text},
        },
        "enabled": True,
        "job_id": None,
    }


async def _upsert_profile(store: PersonaExtractStore, *, key: str, prompt_text: str = "concise style"):
    return await store.upsert_profile_idempotent(
        **_profile_fields(prompt_text=prompt_text),
        idempotency_key=key,
        actor="operator-a",
        actor_kind="session",
        roles=("tenant_admin",),
        trace_id="trace-persona",
        reason="workspace save",
    )


@pytest.mark.asyncio
async def test_persona_upsert_and_hard_delete_have_exact_durable_replay(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'persona-atomic.db'}")
    await _create_ledger(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_persona_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    source_label TEXT NOT NULL DEFAULT '',
                    profile_name TEXT NOT NULL DEFAULT 'default',
                    target_user_id TEXT NOT NULL DEFAULT '',
                    target_name TEXT NOT NULL DEFAULT '',
                    skill_slug TEXT NOT NULL DEFAULT '',
                    prompt_text TEXT NOT NULL DEFAULT '',
                    artifact_json TEXT NOT NULL DEFAULT '',
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    job_id INTEGER NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX ux_persona_profiles_active_scope "
                "ON plugin_persona_profiles "
                "(tenant_id, session_id, channel, source_key) WHERE enabled = 1"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX ux_persona_profiles_scope_skill "
                "ON plugin_persona_profiles "
                "(tenant_id, session_id, channel, source_key, skill_slug) "
                "WHERE skill_slug <> ''"
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_persona_jobs (
                    id INTEGER PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    session_name TEXT NOT NULL DEFAULT '',
                    target_user_id TEXT NOT NULL DEFAULT '',
                    target_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    artifact_json TEXT NOT NULL DEFAULT '',
                    checkpoint_json TEXT NOT NULL DEFAULT '',
                    result_text TEXT NOT NULL DEFAULT '',
                    output_slug TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
    monkeypatch.setattr(persona_store_module, "get_engine", lambda: engine)
    store = PersonaExtractStore(SimpleNamespace())

    first = await _upsert_profile(store, key="profile-save")
    replay = await _upsert_profile(store, key="profile-save")
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response == first.response
    profile_id = int(first.response["id"])
    with pytest.raises(MutationIdempotencyConflictError):
        await _upsert_profile(store, key="profile-save", prompt_text="changed")

    deleted = await store.delete_profile_idempotent(
        profile_id=profile_id,
        tenant_id="tenant-a",
        session_id="room-a",
        idempotency_key="profile-delete",
        actor="operator-a",
        actor_kind="session",
        roles=("tenant_admin",),
        trace_id="trace-delete",
    )
    delete_replay = await store.delete_profile_idempotent(
        profile_id=profile_id,
        tenant_id="tenant-a",
        session_id="room-a",
        idempotency_key="profile-delete",
        actor="operator-a",
        actor_kind="session",
        roles=("tenant_admin",),
        trace_id="trace-delete",
    )
    assert deleted.response == {"deleted": profile_id}
    assert delete_replay.response == deleted.response
    assert delete_replay.replayed is True
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM plugin_persona_profiles")) == 0

    artifact_json = persona_store_module.serialize_artifact(
        {
            "slug": "member-a",
            "target": {"user_id": "member-a", "name": "Member A"},
            "files": {"skill_prompt": "applied style"},
        }
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO plugin_persona_jobs "
                "(id, tenant_id, session_id, session_name, target_user_id, target_name, "
                "status, artifact_json, result_text, output_slug) "
                "VALUES (41, 'tenant-a', 'room-a', 'Room A', 'member-a', 'Member A', "
                "'completed', :artifact, 'applied style', 'member-a')"
            ),
            {"artifact": artifact_json},
        )
        second_artifact_json = persona_store_module.serialize_artifact(
            {
                "slug": "member-b",
                "target": {"user_id": "member-b", "name": "Member B"},
                "files": {"skill_prompt": "second applied style"},
            }
        )
        await conn.execute(
            text(
                "INSERT INTO plugin_persona_jobs "
                "(id, tenant_id, session_id, session_name, target_user_id, target_name, "
                "status, artifact_json, result_text, output_slug) "
                "VALUES (42, 'tenant-a', 'room-a', 'Room A', 'member-b', 'Member B', "
                "'completed', :artifact, 'second applied style', 'member-b')"
            ),
            {"artifact": second_artifact_json},
        )
    apply_fields = {
        "tenant_id": "tenant-a",
        "session_id": "room-a",
        "session_name": "Room A",
        "job_id": 41,
        "channel": "wechat",
        "source_key": "wxbot",
        "source_label": "verified group",
        "profile_name": "default",
        "enabled": True,
        "idempotency_key": "profile-apply-job",
        "actor": "operator-a",
        "actor_kind": "session",
        "roles": ("tenant_admin",),
        "trace_id": "trace-apply",
    }
    applied = await store.apply_job_idempotent(**apply_fields)
    apply_replay = await store.apply_job_idempotent(**apply_fields)
    assert applied.replayed is False
    assert apply_replay.replayed is True
    assert apply_replay.response == applied.response
    with pytest.raises(MutationIdempotencyConflictError):
        await store.apply_job_idempotent(**{**apply_fields, "enabled": False})

    second = await store.apply_job_idempotent(
        **{
            **apply_fields,
            "job_id": 42,
            "profile_name": "Member B",
            "idempotency_key": "profile-apply-job-42",
        }
    )
    assert second.response["skill_slug"] == "member-b"
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM plugin_persona_profiles")) == 2
        active_slug = await conn.scalar(
            text("SELECT skill_slug FROM plugin_persona_profiles WHERE enabled = 1")
        )
        assert active_slug == "member-b"

    first_profile_id = int(applied.response["id"])
    activated = await store.activate_profile_idempotent(
        profile_id=first_profile_id,
        tenant_id="tenant-a",
        session_id="room-a",
        idempotency_key="profile-activate-41",
        actor="operator-a",
        actor_kind="session",
        roles=("tenant_admin",),
        trace_id="trace-activate",
    )
    assert bool(activated.response["enabled"]) is True
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM plugin_persona_profiles")) == 2
        active_slug = await conn.scalar(
            text("SELECT skill_slug FROM plugin_persona_profiles WHERE enabled = 1")
        )
        assert active_slug == "member-a"
    await engine.dispose()

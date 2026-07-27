"""Durable, secret-free runtime overrides for LLM-facing process roles.

The control plane stores only an allowlisted set of non-secret values. Saved
console values override environment and dotenv defaults so operators can edit
the model configuration from the UI. Mounted Secret Provider values remain
authoritative and secret fields are never persisted here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic_settings import EnvSettingsSource, SecretsSettingsSource
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.config import Settings
from app.infra.db import get_session_factory

RUNTIME_LLM_CONFIG_KEY = "global"
RUNTIME_LLM_CONFIG_ROLES = frozenset({"api", "inbound", "scheduler"})
RUNTIME_LLM_STRING_FIELDS = frozenset(
    {
        "llm_provider",
        "openai_base_url",
        "openai_api_mode",
        "openai_web_search_tool",
        "llm_embed_provider",
        "llm_model_tier1",
        "llm_model_tier2",
        "llm_model_tier3",
        "llm_embed_model",
    }
)
RUNTIME_LLM_BOOLEAN_FIELDS = frozenset(
    {
        "openai_web_search_enabled",
        "openai_web_search_live_enabled",
        "knowledge_features_enabled",
        "customer_service_prompt_enabled",
    }
)
RUNTIME_LLM_MUTABLE_FIELDS = RUNTIME_LLM_STRING_FIELDS | RUNTIME_LLM_BOOLEAN_FIELDS
RUNTIME_LLM_SECRET_FIELDS = frozenset({"openai_api_key"})

_metadata = MetaData()
runtime_llm_config_table = Table(
    "runtime_llm_config",
    _metadata,
    Column("config_key", String(32), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("overrides_json", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
runtime_llm_config_history_table = Table(
    "runtime_llm_config_history",
    _metadata,
    Column("version", Integer, primary_key=True),
    Column("overrides_json", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
runtime_llm_config_idempotency_table = Table(
    "runtime_llm_config_idempotency",
    _metadata,
    Column("key_hash", String(64), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column(
        "result_version",
        Integer,
        ForeignKey("runtime_llm_config_history.version", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True, slots=True)
class RuntimeLlmConfigSnapshot:
    version: int
    overrides: dict[str, str | bool]
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeLlmConfig:
    settings: Settings
    snapshot: RuntimeLlmConfigSnapshot
    field_sources: dict[str, str]


@dataclass(frozen=True, slots=True)
class RuntimeLlmConfigMutation:
    before: RuntimeLlmConfigSnapshot
    after: RuntimeLlmConfigSnapshot
    replayed: bool


class RuntimeLlmConfigVersionConflict(RuntimeError):
    def __init__(self, *, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(f"runtime LLM config version conflict: {expected} != {current}")


class RuntimeLlmConfigIdempotencyConflict(RuntimeError):
    """An idempotency key was reused for a different expected-version/payload."""


class RuntimeLlmConfigStore:
    """Cross-process compare-and-swap store backed by the migrated database."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    async def get(self) -> RuntimeLlmConfigSnapshot:
        async with self.session_factory() as session:
            result = await session.execute(
                select(runtime_llm_config_table).where(
                    runtime_llm_config_table.c.config_key == RUNTIME_LLM_CONFIG_KEY
                )
            )
            return _snapshot(result.mappings().first())

    async def replay_idempotent_result(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> RuntimeLlmConfigSnapshot | None:
        key_hash = _idempotency_key_hash(idempotency_key)
        async with self.session_factory() as session:
            return await _idempotent_snapshot(
                session,
                key_hash=key_hash,
                request_hash=request_hash,
            )

    async def compare_and_swap_idempotent(
        self,
        *,
        expected_version: int,
        overrides: Mapping[str, object],
        idempotency_key: str,
        request_hash: str,
    ) -> RuntimeLlmConfigMutation:
        """Atomically persist config, history, and the replay claim."""

        normalized = normalize_runtime_llm_overrides(overrides)
        key_hash = _idempotency_key_hash(idempotency_key)
        now = datetime.now(UTC)
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    current_result = await session.execute(
                        select(runtime_llm_config_table)
                        .where(
                            runtime_llm_config_table.c.config_key
                            == RUNTIME_LLM_CONFIG_KEY
                        )
                        .with_for_update()
                    )
                    current_row = current_result.mappings().first()
                    current = _snapshot(current_row)

                    replay = await _idempotent_snapshot(
                        session,
                        key_hash=key_hash,
                        request_hash=request_hash,
                    )
                    if replay is not None:
                        return RuntimeLlmConfigMutation(
                            before=replay,
                            after=replay,
                            replayed=True,
                        )
                    if current.version != expected_version:
                        raise RuntimeLlmConfigVersionConflict(
                            expected=expected_version,
                            current=current.version,
                        )

                    written = await _write_snapshot(
                        session,
                        current_row=current_row,
                        expected_version=expected_version,
                        overrides=normalized,
                        updated_at=now,
                    )
                    await session.execute(
                        insert(runtime_llm_config_history_table).values(
                            version=written.version,
                            overrides_json=written.overrides,
                            updated_at=now,
                        )
                    )
                    await session.execute(
                        insert(runtime_llm_config_idempotency_table).values(
                            key_hash=key_hash,
                            request_hash=request_hash,
                            result_version=written.version,
                            created_at=now,
                        )
                    )
                    return RuntimeLlmConfigMutation(
                        before=current,
                        after=written,
                        replayed=False,
                    )
        except IntegrityError as exc:
            # Initial version-zero writes cannot acquire a row lock.  The
            # config/idempotency primary keys arbitrate that race; after the
            # losing transaction rolls back, resolve an exact replay first.
            replay = await self.replay_idempotent_result(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return RuntimeLlmConfigMutation(
                    before=replay,
                    after=replay,
                    replayed=True,
                )
            latest = await self.get()
            raise RuntimeLlmConfigVersionConflict(
                expected=expected_version,
                current=latest.version,
            ) from exc

    async def compare_and_swap(
        self,
        *,
        expected_version: int,
        overrides: Mapping[str, object],
    ) -> RuntimeLlmConfigSnapshot:
        normalized = normalize_runtime_llm_overrides(overrides)
        now = datetime.now(UTC)
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    current_result = await session.execute(
                        select(runtime_llm_config_table)
                        .where(runtime_llm_config_table.c.config_key == RUNTIME_LLM_CONFIG_KEY)
                        .with_for_update()
                    )
                    current_row = current_result.mappings().first()
                    current = _snapshot(current_row)
                    if current.version != expected_version:
                        raise RuntimeLlmConfigVersionConflict(
                            expected=expected_version,
                            current=current.version,
                        )

                    return await _write_snapshot(
                        session,
                        current_row=current_row,
                        expected_version=expected_version,
                        overrides=normalized,
                        updated_at=now,
                    )
        except IntegrityError as exc:
            # Two replicas may both observe the implicit version-zero row.  The
            # primary key arbitrates that creation race; report the winner's
            # durable version as an ordinary optimistic-concurrency conflict.
            latest = await self.get()
            raise RuntimeLlmConfigVersionConflict(
                expected=expected_version,
                current=latest.version,
            ) from exc


def normalize_runtime_llm_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("idempotency_key_required")
    if len(normalized) > 128 or not normalized.isprintable():
        raise ValueError("invalid_idempotency_key")
    return normalized


def runtime_llm_request_hash(
    *,
    expected_version: int,
    updates: Mapping[str, object],
) -> str:
    normalized = normalize_runtime_llm_overrides(updates)
    canonical = json.dumps(
        {
            "expected_version": max(0, int(expected_version)),
            "updates": normalized,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def runtime_llm_overlay_enabled_for_role(role: str) -> bool:
    return str(role or "").strip().lower() in RUNTIME_LLM_CONFIG_ROLES


async def load_runtime_llm_config(
    base_settings: Settings,
    *,
    store: RuntimeLlmConfigStore | None = None,
) -> ResolvedRuntimeLlmConfig:
    snapshot = await (store or RuntimeLlmConfigStore()).get()
    return resolve_runtime_llm_config(base_settings, snapshot)


def resolve_runtime_llm_config(
    base_settings: Settings,
    snapshot: RuntimeLlmConfigSnapshot,
) -> ResolvedRuntimeLlmConfig:
    environment_fields, secret_provider_fields = _externally_managed_fields(base_settings)
    safe_overrides = normalize_runtime_llm_overrides(snapshot.overrides)
    applied = {
        key: value
        for key, value in safe_overrides.items()
        if key not in secret_provider_fields
    }
    effective = Settings.model_validate({**base_settings.model_dump(), **applied})
    field_sources: dict[str, str] = {}
    for field_name in sorted(RUNTIME_LLM_MUTABLE_FIELDS):
        if field_name in secret_provider_fields:
            field_sources[field_name] = "secret_provider"
        elif field_name in applied:
            field_sources[field_name] = "persisted_override"
        elif field_name in environment_fields:
            field_sources[field_name] = "environment"
        else:
            field_sources[field_name] = "dotenv_or_default"
    return ResolvedRuntimeLlmConfig(
        settings=effective,
        snapshot=snapshot,
        field_sources=field_sources,
    )


def externally_managed_runtime_llm_fields(settings: Settings) -> frozenset[str]:
    _, secret_provider_fields = _externally_managed_fields(settings)
    return frozenset(secret_provider_fields & RUNTIME_LLM_MUTABLE_FIELDS)


def runtime_llm_secret_status(settings: Settings) -> dict[str, object]:
    environment_fields, secret_provider_fields = _externally_managed_fields(settings)
    if "openai_api_key" in environment_fields:
        source = "environment"
    elif "openai_api_key" in secret_provider_fields:
        source = "secret_provider"
    elif settings.openai_api_key:
        source = "dotenv_or_explicit"
    else:
        source = "not_configured"
    return {
        "configured": bool(settings.openai_api_key),
        "source": source,
        "mutable": False,
    }


def normalize_runtime_llm_overrides(
    values: Mapping[str, object],
) -> dict[str, str | bool]:
    unexpected = set(values) - RUNTIME_LLM_MUTABLE_FIELDS
    if unexpected:
        raise ValueError(
            "unsupported runtime LLM override fields: " + ", ".join(sorted(unexpected))
        )
    normalized: dict[str, str | bool] = {}
    for field_name, value in values.items():
        if field_name in RUNTIME_LLM_STRING_FIELDS:
            cleaned = str(value or "").strip()
            if not cleaned:
                raise ValueError(f"{field_name} cannot be empty")
            normalized[field_name] = cleaned
        elif type(value) is bool:
            normalized[field_name] = value
        else:
            raise ValueError(f"{field_name} must be a boolean")
    return normalized


def _externally_managed_fields(settings: Settings) -> tuple[set[str], set[str]]:
    settings_cls = type(settings)
    try:
        environment_values = EnvSettingsSource(settings_cls)()
    except Exception:
        environment_values = {}
    try:
        secret_values = SecretsSettingsSource(settings_cls)()
    except Exception:
        secret_values = {}
    return set(environment_values), set(secret_values)


def _snapshot(row: Any | None) -> RuntimeLlmConfigSnapshot:
    if row is None:
        return RuntimeLlmConfigSnapshot(version=0, overrides={})
    raw_overrides = row.get("overrides_json") if hasattr(row, "get") else None
    overrides = normalize_runtime_llm_overrides(
        dict(raw_overrides) if isinstance(raw_overrides, dict) else {}
    )
    raw_updated_at = row.get("updated_at") if hasattr(row, "get") else None
    return RuntimeLlmConfigSnapshot(
        version=max(0, int(row.get("version") or 0)),
        overrides=overrides,
        updated_at=raw_updated_at if isinstance(raw_updated_at, datetime) else None,
    )


async def _write_snapshot(
    session: AsyncSession,
    *,
    current_row: Any | None,
    expected_version: int,
    overrides: Mapping[str, object],
    updated_at: datetime,
) -> RuntimeLlmConfigSnapshot:
    normalized = normalize_runtime_llm_overrides(overrides)
    next_version = expected_version + 1
    if current_row is None:
        written_result = await session.execute(
            insert(runtime_llm_config_table)
            .values(
                config_key=RUNTIME_LLM_CONFIG_KEY,
                version=next_version,
                overrides_json=normalized,
                updated_at=updated_at,
            )
            .returning(runtime_llm_config_table)
        )
    else:
        written_result = await session.execute(
            update(runtime_llm_config_table)
            .where(
                runtime_llm_config_table.c.config_key == RUNTIME_LLM_CONFIG_KEY,
                runtime_llm_config_table.c.version == expected_version,
            )
            .values(
                version=next_version,
                overrides_json=normalized,
                updated_at=updated_at,
            )
            .returning(runtime_llm_config_table)
        )
    written = written_result.mappings().first()
    if written is None:
        latest_result = await session.execute(
            select(runtime_llm_config_table).where(
                runtime_llm_config_table.c.config_key == RUNTIME_LLM_CONFIG_KEY
            )
        )
        latest = _snapshot(latest_result.mappings().first())
        raise RuntimeLlmConfigVersionConflict(
            expected=expected_version,
            current=latest.version,
        )
    return _snapshot(written)


async def _idempotent_snapshot(
    session: AsyncSession,
    *,
    key_hash: str,
    request_hash: str,
) -> RuntimeLlmConfigSnapshot | None:
    idempotency_result = await session.execute(
        select(runtime_llm_config_idempotency_table).where(
            runtime_llm_config_idempotency_table.c.key_hash == key_hash
        )
    )
    idempotency_row = idempotency_result.mappings().first()
    if idempotency_row is None:
        return None
    if str(idempotency_row.get("request_hash") or "") != request_hash:
        raise RuntimeLlmConfigIdempotencyConflict(
            "idempotency key was used for a different runtime LLM request"
        )
    history_result = await session.execute(
        select(runtime_llm_config_history_table).where(
            runtime_llm_config_history_table.c.version
            == int(idempotency_row.get("result_version") or 0)
        )
    )
    history_row = history_result.mappings().first()
    if history_row is None:
        raise RuntimeError("runtime LLM idempotency history is incomplete")
    return _snapshot(history_row)


def _idempotency_key_hash(value: str) -> str:
    normalized = normalize_runtime_llm_idempotency_key(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

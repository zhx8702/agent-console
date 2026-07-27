from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.admin.authorization import AdminRole, Principal
from app.admin.mutation_ledger import MutationAudit, plugin_admin_mutation_idempotency
from app.admin.route_permissions import RoutePermissionRegistry
from app.channel.adapters import (
    WECHAT_SDK_DESCRIPTOR,
    ChannelAdapterCatalog,
    ChannelAdapterDescriptor,
    ChannelAdapterRegistration,
    ChannelProbeResult,
    SecretFieldDescriptor,
    build_default_channel_adapter_catalog,
)
from app.channel.connections import (
    ChannelConnectionDocument,
    ChannelConnectionStore,
    connection_binding_fingerprint,
    legacy_wxbot_connection_from_settings,
    validate_connection_document,
)
from app.channel.router import _legacy_for_tenant, build_channel_admin_router
from app.models.channel_connection import ChannelConnectionRow


class _Outbound:
    async def get_session_policy(self, target):
        return {}

    async def send_text(self, target, text, options=None):
        return None

    async def send_image(self, target, media, options=None):
        return None


async def _authorize(_request: Request) -> Principal:
    return Principal(
        subject="channel-admin",
        roles=(AdminRole.PLATFORM_ADMIN.value,),
        tenant_ids=("*",),
        auth_kind="test",
    )


@pytest.fixture
async def channel_api(
    tmp_path,
) -> AsyncIterator[tuple[httpx.AsyncClient, AsyncEngine, ChannelConnectionStore]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'channel-connections.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(plugin_admin_mutation_idempotency.metadata.create_all)
        await connection.run_sync(ChannelConnectionRow.__table__.create)

    async def probe(_connection: ChannelConnectionDocument) -> ChannelProbeResult:
        return ChannelProbeResult(ok=True, status="online")

    catalog = build_default_channel_adapter_catalog(
        (
            ChannelAdapterRegistration(
                descriptor=WECHAT_SDK_DESCRIPTOR,
                provider_factory=lambda _connection: _Outbound(),
                probe=probe,
            ),
            ChannelAdapterRegistration(
                descriptor=ChannelAdapterDescriptor(
                    adapter_id="feixin",
                    display_name="Feixin",
                    channel="feixin",
                    capabilities=("inbound_text", "outbound_text"),
                    runtime_modes=("in_process",),
                    config_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"endpoint": {"type": "string"}},
                    },
                ),
                provider_factory=lambda _connection: _Outbound(),
            ),
        )
    )
    store = ChannelConnectionStore(
        async_sessionmaker(engine, expire_on_commit=False),
        catalog,
    )
    app = FastAPI()
    app.include_router(
        build_channel_admin_router(
            store,
            catalog=catalog,
            authorization_dependency=_authorize,
            legacy_settings=SimpleNamespace(
                wxbot_api_token="",
                wxbot_default_tenant_id="tenant-a",
            ),
        )
    )
    RoutePermissionRegistry(()).bind_and_validate(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client, engine, store
    await engine.dispose()


def _create_payload() -> dict:
    return {
        "connection_id": "wechat-prod",
        "adapter_id": "wechat-sdk",
        "display_name": "生产微信",
        "config": {
            "endpoint_url": "http://127.0.0.1:5080",
            "poll_interval_seconds": 3,
            "send_interval_seconds": 2,
        },
        "secret_ref": "",
        "required_for_launch": False,
        "desired_state": "draft",
    }


@pytest.mark.asyncio
async def test_catalog_is_extensible_and_factory_is_connection_scoped(channel_api) -> None:
    client, _engine, store = channel_api
    response = await client.get(
        "/v1/admin/channel-adapters",
        params={"tenant_id": "tenant-a"},
    )

    assert response.status_code == 200
    assert WECHAT_SDK_DESCRIPTOR.supports_multiple_connections is True
    assert {item["adapter_id"] for item in response.json()["items"]} == {
        "feixin",
        "wechat-sdk",
    }
    connections = await client.get(
        "/v1/admin/channel-connections",
        params={"tenant_id": "tenant-a"},
    )
    assert connections.status_code == 200
    assert connections.json()["items"] == []
    registration = store.catalog.require("feixin")
    provider = await registration.create_provider(
        ChannelConnectionDocument(
            tenant_id="tenant-a",
            connection_id="feixin-a",
            adapter_id="feixin",
            display_name="飞信 A",
            desired_state="draft",
            effective_state="unverified",
            secret_status="missing",
            version=1,
            priority=100,
            required_for_launch=False,
        )
    )
    assert isinstance(provider, _Outbound)


@pytest.mark.asyncio
async def test_connection_cas_state_operations_and_exact_replay(channel_api) -> None:
    client, _engine, _store = channel_api
    collection_path = "/v1/admin/channel-connections"
    item_path = f"{collection_path}/wechat-prod"
    params = {"tenant_id": "tenant-a"}

    created = await client.post(
        collection_path,
        params=params,
        headers={"Idempotency-Key": "create-wechat-prod"},
        json=_create_payload(),
    )
    replayed = await client.post(
        collection_path,
        params=params,
        headers={"Idempotency-Key": "create-wechat-prod"},
        json=_create_payload(),
    )
    missing_precondition = await client.patch(
        item_path,
        params=params,
        headers={"Idempotency-Key": "patch-without-etag"},
        json={"display_name": "新名字"},
    )
    patched = await client.patch(
        item_path,
        params=params,
        headers={
            "If-Match": created.headers["etag"],
            "Idempotency-Key": "patch-wechat-prod",
        },
        json={
            **{key: value for key, value in _create_payload().items() if key != "connection_id"},
            "display_name": "生产微信 2",
            "config": {
                "endpoint_url": "http://127.0.0.1:5081",
                "poll_interval_seconds": 4,
                "send_interval_seconds": 2,
            },
        },
    )
    stale = await client.patch(
        item_path,
        params=params,
        headers={
            "If-Match": created.headers["etag"],
            "Idempotency-Key": "stale-patch-wechat-prod",
        },
        json={"display_name": "stale"},
    )
    validated = await client.post(
        f"{item_path}/validate",
        params=params,
        headers={
            "If-Match": patched.headers["etag"],
            "Idempotency-Key": "validate-wechat-prod",
        },
    )
    enabled = await client.post(
        f"{item_path}/enable",
        params=params,
        headers={
            "If-Match": validated.headers["etag"],
            "Idempotency-Key": "enable-wechat-prod",
        },
    )
    invalid_enabled_patch = await client.patch(
        item_path,
        params=params,
        headers={
            "If-Match": enabled.headers["etag"],
            "Idempotency-Key": "reject-invalid-enabled-wechat",
        },
        json={"secret_ref": "env://WXBOT_API_TOKEN"},
    )
    probed = await client.post(
        f"{item_path}/probe",
        params=params,
        headers={
            "If-Match": enabled.headers["etag"],
            "Idempotency-Key": "probe-wechat-prod",
        },
    )
    probe_replayed = await client.post(
        f"{item_path}/probe",
        params=params,
        headers={
            "If-Match": enabled.headers["etag"],
            "Idempotency-Key": "probe-wechat-prod",
        },
    )
    premature_runtime_stop = await _store.mark_runtime_stopped(
        "tenant-a",
        "wechat-prod",
    )
    disabled = await client.post(
        f"{item_path}/disable",
        params=params,
        headers={
            "If-Match": probed.headers["etag"],
            "Idempotency-Key": "disable-wechat-prod",
        },
    )
    runtime_stopped = await _store.mark_runtime_stopped(
        "tenant-a",
        "wechat-prod",
    )
    duplicate_runtime_stop = await _store.mark_runtime_stopped(
        "tenant-a",
        "wechat-prod",
    )
    converged = await client.get(item_path, params=params)

    assert created.status_code == replayed.status_code == 201
    assert created.json() == replayed.json()
    assert replayed.headers["Idempotent-Replayed"] == "true"
    assert created.headers["etag"] == '"1"'
    assert created.json()["config_json"]["sdk_url"].endswith(":5080")
    assert created.json()["secret_ref"] == ""
    assert created.json()["secret_status"] == "not_required"
    assert missing_precondition.status_code == 428
    assert patched.status_code == 200
    assert patched.headers["etag"] == '"2"'
    assert patched.json()["config_json"]["sdk_url"].endswith(":5081")
    assert stale.status_code == 409
    assert stale.headers["etag"] == '"2"'
    assert validated.json()["ok"] is True
    assert validated.headers["etag"] == '"3"'
    assert validated.json()["connection"]["effective_state"] == "unverified"
    assert validated.json()["connection"]["last_probed_at"] is None
    assert validated.json()["connection"]["last_probe_status"] == ""
    assert enabled.json()["desired_state"] == "enabled"
    assert enabled.headers["etag"] == '"4"'
    assert invalid_enabled_patch.status_code == 400
    assert invalid_enabled_patch.json()["detail"]["code"] == (
        "secret_reference_not_supported_by_adapter"
    )
    assert probed.json()["ok"] is True
    assert probed.json()["connection"]["effective_state"] == "enabled"
    assert probed.headers["etag"] == '"5"'
    assert probe_replayed.status_code == 200
    assert probe_replayed.json() == probed.json()
    assert probe_replayed.headers["Idempotent-Replayed"] == "true"
    assert premature_runtime_stop is False
    assert disabled.json()["desired_state"] == "disabled"
    # Desired/effective are intentionally separate: the control plane does not
    # claim the connector stopped until runtime reconciliation confirms it.
    assert disabled.json()["effective_state"] == "enabled"
    assert disabled.headers["etag"] == '"6"'
    assert runtime_stopped is True
    assert duplicate_runtime_stop is False
    assert converged.json()["effective_state"] == "disabled"
    # Runtime telemetry must not invalidate the operator's configuration ETag.
    assert converged.headers["etag"] == '"6"'


@pytest.mark.asyncio
async def test_runtime_activity_requires_current_binding_and_final_delivery(channel_api) -> None:
    client, _engine, store = channel_api
    params = {"tenant_id": "tenant-a"}
    item_path = "/v1/admin/channel-connections/wechat-prod"
    created = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-runtime-activity"},
        json=_create_payload(),
    )
    enabled = await client.post(
        f"{item_path}/enable",
        params=params,
        headers={
            "If-Match": created.headers["etag"],
            "Idempotency-Key": "enable-runtime-activity",
        },
    )
    fingerprint = connection_binding_fingerprint(
        ChannelConnectionDocument.model_validate(enabled.json())
    )

    assert await store.record_runtime_activity(
        "tenant-a",
        "wechat-prod",
        direction="inbound",
        binding_fingerprint=fingerprint,
    )
    inbound_only = await store.get("tenant-a", "wechat-prod")
    assert inbound_only.last_inbound_at is not None
    assert inbound_only.last_outbound_delivered_at is None

    assert await store.record_runtime_activity(
        "tenant-a",
        "wechat-prod",
        direction="outbound_delivered",
        binding_fingerprint=fingerprint,
    )
    round_trip = await store.get("tenant-a", "wechat-prod")
    assert round_trip.last_outbound_delivered_at is not None
    assert round_trip.version == enabled.json()["version"]

    updated = await client.patch(
        item_path,
        params=params,
        headers={
            "If-Match": enabled.headers["etag"],
            "Idempotency-Key": "change-runtime-binding",
        },
        json={
            "config": {
                "endpoint_url": "http://127.0.0.1:5081",
                "poll_interval_seconds": 3,
                "send_interval_seconds": 2,
            }
        },
    )
    assert updated.status_code == 200
    assert updated.json()["last_inbound_at"] is None
    assert updated.json()["last_outbound_delivered_at"] is None
    assert not await store.record_runtime_activity(
        "tenant-a",
        "wechat-prod",
        direction="inbound",
        binding_fingerprint=fingerprint,
    )


@pytest.mark.asyncio
async def test_runtime_readiness_converges_without_configuration_version_change(
    channel_api,
) -> None:
    client, _engine, store = channel_api
    params = {"tenant_id": "tenant-a"}
    item_path = "/v1/admin/channel-connections/wechat-prod"
    created = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-runtime-ready"},
        json=_create_payload(),
    )
    enabled = await client.post(
        f"{item_path}/enable",
        params=params,
        headers={
            "If-Match": created.headers["etag"],
            "Idempotency-Key": "enable-runtime-ready",
        },
    )
    before = ChannelConnectionDocument.model_validate(enabled.json())
    fingerprint = connection_binding_fingerprint(before)

    assert before.effective_state == "unverified"
    assert await store.mark_runtime_ready(
        "tenant-a",
        "wechat-prod",
        binding_fingerprint=fingerprint,
    )
    ready = await store.get("tenant-a", "wechat-prod")
    assert ready.effective_state == "enabled"
    assert ready.last_probe_status == "ready"
    assert ready.last_probed_at is not None
    assert ready.last_error_code == ""
    assert ready.version == before.version

    updated = await client.patch(
        item_path,
        params=params,
        headers={
            "If-Match": enabled.headers["etag"],
            "Idempotency-Key": "change-runtime-ready-binding",
        },
        json={
            "config": {
                "endpoint_url": "http://127.0.0.1:5081",
                "poll_interval_seconds": 3,
                "send_interval_seconds": 2,
            }
        },
    )
    assert updated.status_code == 200
    assert not await store.mark_runtime_ready(
        "tenant-a",
        "wechat-prod",
        binding_fingerprint=fingerprint,
    )
    stale_worker_view = await store.get("tenant-a", "wechat-prod")
    assert stale_worker_view.effective_state == "unverified"
    assert stale_worker_view.last_probed_at is None


@pytest.mark.asyncio
async def test_plaintext_secrets_are_never_echoed_or_persisted(channel_api) -> None:
    client, engine, _store = channel_api
    raw_secret = "raw-connector-secret-value"
    params = {"tenant_id": "tenant-a"}

    top_level = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "reject-top-level-secret"},
        json={**_create_payload(), "token": raw_secret},
    )
    nested = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "reject-nested-secret"},
        json={
            **_create_payload(),
            "connection_id": "wechat-nested-secret",
            "config": {
                "endpoint_url": "http://127.0.0.1:5080",
                "api_token": raw_secret,
            },
        },
    )
    url_userinfo = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "reject-url-userinfo-secret"},
        json={
            **_create_payload(),
            "connection_id": "wechat-url-secret",
            "config": {
                "endpoint_url": f"http://service:{raw_secret}@127.0.0.1:5080",
            },
        },
    )
    unrelated_env_secret = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "reject-unrelated-env-secret"},
        json={
            **_create_payload(),
            "connection_id": "wechat-unrelated-env-secret",
            "secret_ref": "env://OPENAI_API_KEY",
        },
    )

    assert top_level.status_code == 422
    assert nested.status_code == 400
    assert url_userinfo.status_code == 400
    assert unrelated_env_secret.status_code == 400
    assert unrelated_env_secret.json()["detail"]["code"] == (
        "secret_reference_not_supported_by_adapter"
    )
    assert raw_secret not in top_level.text
    assert raw_secret not in nested.text
    assert raw_secret not in url_userinfo.text
    async with engine.connect() as connection:
        rows = (await connection.execute(select(ChannelConnectionRow.__table__))).mappings().all()
        ledger = (
            (await connection.execute(select(plugin_admin_mutation_idempotency))).mappings().all()
        )
    assert raw_secret not in str([dict(row) for row in rows])
    assert raw_secret not in str([dict(row) for row in ledger])
    assert all(row["connection_id"] != "wechat-unrelated-env-secret" for row in rows)


@pytest.mark.asyncio
async def test_tokenless_wechat_connection_is_valid_and_secret_update_is_rejected(
    channel_api,
) -> None:
    client, _engine, _store = channel_api
    params = {"tenant_id": "tenant-a"}
    missing_payload = {
        **_create_payload(),
        "connection_id": "wechat-missing-secret",
        "secret_ref": "",
    }
    missing = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-missing-secret-draft"},
        json=missing_payload,
    )
    validated = await client.post(
        "/v1/admin/channel-connections/wechat-missing-secret/validate",
        params=params,
        headers={
            "If-Match": missing.headers["etag"],
            "Idempotency-Key": "validate-missing-secret-draft",
        },
    )

    assert missing.status_code == 201
    assert missing.json()["secret_status"] == "not_required"
    assert validated.status_code == 200
    assert validated.json()["ok"] is True
    assert validated.json()["error_codes"] == []
    assert validated.json()["connection"]["last_probed_at"] is None
    assert validated.json()["connection"]["last_probe_status"] == ""

    valid_payload = {
        **_create_payload(),
        "connection_id": "wechat-secret-update",
    }
    valid = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-secret-update"},
        json=valid_payload,
    )
    rejected = await client.patch(
        "/v1/admin/channel-connections/wechat-secret-update",
        params=params,
        headers={
            "If-Match": valid.headers["etag"],
            "Idempotency-Key": "reject-secret-update",
        },
        json={"secret_ref": "env://OPENAI_API_KEY"},
    )
    unchanged = await client.get(
        "/v1/admin/channel-connections/wechat-secret-update",
        params=params,
    )

    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == (
        "secret_reference_not_supported_by_adapter"
    )
    assert unchanged.headers["etag"] == '"1"'
    assert unchanged.json()["secret_ref"] == ""


@pytest.mark.asyncio
async def test_uri_schema_is_enforced_on_create_update_validate_and_enable(
    channel_api,
) -> None:
    client, engine, _store = channel_api
    params = {"tenant_id": "tenant-a"}
    invalid_payload = {
        **_create_payload(),
        "connection_id": "wechat-invalid-uri-create",
        "config": {"endpoint_url": "not-a-url"},
    }
    invalid_create = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "reject-invalid-uri-create"},
        json=invalid_payload,
    )
    assert invalid_create.status_code == 400
    assert invalid_create.json()["detail"]["code"] == "config_format_sdk_url"

    valid_payload = {
        **_create_payload(),
        "connection_id": "wechat-uri-defensive-check",
    }
    created = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-uri-defensive-check"},
        json=valid_payload,
    )
    invalid_update = await client.patch(
        "/v1/admin/channel-connections/wechat-uri-defensive-check",
        params=params,
        headers={
            "If-Match": created.headers["etag"],
            "Idempotency-Key": "reject-invalid-uri-update",
        },
        json={"config": {"endpoint_url": "not-a-url"}},
    )
    assert invalid_update.status_code == 400
    assert invalid_update.json()["detail"]["code"] == "config_format_sdk_url"

    # Defensive validation still protects rows written by an older binary or
    # imported directly before strict create/update checks existed.
    async with engine.begin() as connection:
        await connection.execute(
            update(ChannelConnectionRow)
            .where(
                ChannelConnectionRow.tenant_id == "tenant-a",
                ChannelConnectionRow.connection_id == "wechat-uri-defensive-check",
            )
            .values(config_json={"sdk_url": "not-a-url"})
        )
    validated = await client.post(
        "/v1/admin/channel-connections/wechat-uri-defensive-check/validate",
        params=params,
        headers={
            "If-Match": created.headers["etag"],
            "Idempotency-Key": "validate-invalid-uri-row",
        },
    )
    enabled = await client.post(
        "/v1/admin/channel-connections/wechat-uri-defensive-check/enable",
        params=params,
        headers={
            "If-Match": validated.headers["etag"],
            "Idempotency-Key": "reject-enable-invalid-uri-row",
        },
    )

    assert validated.status_code == 200
    assert validated.json()["ok"] is False
    assert validated.json()["error_codes"] == ["config_format_sdk_url"]
    assert validated.json()["connection"]["effective_state"] == "unverified"
    assert validated.json()["connection"]["last_probed_at"] is None
    assert validated.json()["connection"]["last_probe_status"] == ""
    assert validated.json()["connection"]["last_error_code"] == ""
    assert enabled.status_code == 409
    assert enabled.json()["detail"]["code"] == "config_format_sdk_url"


@pytest.mark.asyncio
async def test_probe_timeout_is_bounded_and_persisted_as_a_safe_code(
    channel_api,
) -> None:
    client, engine, store = channel_api
    params = {"tenant_id": "tenant-a"}
    created = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-timeout-probe"},
        json={**_create_payload(), "connection_id": "wechat-timeout-probe"},
    )

    async def blocked_probe(
        _connection: ChannelConnectionDocument,
    ) -> ChannelProbeResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    store.catalog.register(
        ChannelAdapterRegistration(
            descriptor=WECHAT_SDK_DESCRIPTOR,
            provider_factory=lambda _connection: _Outbound(),
            probe=blocked_probe,
        ),
        replace=True,
    )
    short_timeout_store = ChannelConnectionStore(
        async_sessionmaker(engine, expire_on_commit=False),
        store.catalog,
        probe_timeout_seconds=0.01,
    )
    outcome = await short_timeout_store.probe(
        "tenant-a",
        "wechat-timeout-probe",
        expected_version=1,
        idempotency_key="timeout-probe",
        audit=MutationAudit(actor="test"),
    )

    assert created.status_code == 201
    assert outcome.value.ok is False
    assert outcome.value.status == "timeout"
    assert outcome.value.error_codes == ["adapter_probe_timeout"]
    assert outcome.value.connection.version == 2
    assert outcome.value.connection.last_error_code == "adapter_probe_timeout"


@pytest.mark.asyncio
async def test_probe_does_not_hold_database_lock_during_plugin_io(channel_api) -> None:
    client, _engine, store = channel_api
    params = {"tenant_id": "tenant-a"}
    created = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-concurrent-probe"},
        json={**_create_payload(), "connection_id": "wechat-concurrent-probe"},
    )
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def blocked_probe(
        _connection: ChannelConnectionDocument,
    ) -> ChannelProbeResult:
        probe_started.set()
        await release_probe.wait()
        return ChannelProbeResult(ok=True, status="online")

    store.catalog.register(
        ChannelAdapterRegistration(
            descriptor=WECHAT_SDK_DESCRIPTOR,
            provider_factory=lambda _connection: _Outbound(),
            probe=blocked_probe,
        ),
        replace=True,
    )
    probe_task = asyncio.create_task(
        client.post(
            "/v1/admin/channel-connections/wechat-concurrent-probe/probe",
            params=params,
            headers={
                "If-Match": created.headers["etag"],
                "Idempotency-Key": "concurrent-probe",
            },
        )
    )
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    try:
        patched = await asyncio.wait_for(
            client.patch(
                "/v1/admin/channel-connections/wechat-concurrent-probe",
                params=params,
                headers={
                    "If-Match": created.headers["etag"],
                    "Idempotency-Key": "patch-during-probe",
                },
                json={"display_name": "probe did not lock"},
            ),
            timeout=1,
        )
    finally:
        release_probe.set()
    probed = await asyncio.wait_for(probe_task, timeout=2)

    assert patched.status_code == 200
    assert patched.headers["etag"] == '"2"'
    assert probed.status_code == 409
    assert probed.headers["etag"] == '"2"'


@pytest.mark.asyncio
async def test_probe_fails_closed_if_adapter_deactivates_during_io(channel_api) -> None:
    client, _engine, store = channel_api
    params = {"tenant_id": "tenant-a"}
    created = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-deactivated-probe"},
        json={**_create_payload(), "connection_id": "wechat-deactivated-probe"},
    )
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def blocked_probe(
        _connection: ChannelConnectionDocument,
    ) -> ChannelProbeResult:
        probe_started.set()
        await release_probe.wait()
        return ChannelProbeResult(ok=True, status="online")

    active_registrations = [
        ChannelAdapterRegistration(
            descriptor=WECHAT_SDK_DESCRIPTOR,
            provider_factory=lambda _connection: _Outbound(),
            probe=blocked_probe,
        )
    ]
    store.catalog = ChannelAdapterCatalog(
        live_registrations_provider=lambda: tuple(active_registrations)
    )
    probe_task = asyncio.create_task(
        client.post(
            "/v1/admin/channel-connections/wechat-deactivated-probe/probe",
            params=params,
            headers={
                "If-Match": created.headers["etag"],
                "Idempotency-Key": "deactivated-probe",
            },
        )
    )
    await asyncio.wait_for(probe_started.wait(), timeout=1)
    active_registrations.clear()
    release_probe.set()
    probed = await asyncio.wait_for(probe_task, timeout=2)

    assert probed.status_code == 200
    assert probed.json()["ok"] is False
    assert probed.json()["error_codes"] == ["adapter_not_registered"]
    assert probed.json()["connection"]["effective_state"] == "error"


@pytest.mark.asyncio
async def test_live_catalog_removes_disabled_adapter_and_enable_fails_closed(
    channel_api,
) -> None:
    client, _engine, store = channel_api
    params = {"tenant_id": "tenant-a"}
    created = await client.post(
        "/v1/admin/channel-connections",
        params=params,
        headers={"Idempotency-Key": "create-live-adapter-check"},
        json={**_create_payload(), "connection_id": "wechat-live-adapter-check"},
    )
    active_registrations = [store.catalog.require("wechat-sdk")]
    live_catalog = ChannelAdapterCatalog(
        live_registrations_provider=lambda: tuple(active_registrations)
    )
    store.catalog = live_catalog
    assert live_catalog.get("wechat-sdk") is not None

    active_registrations.clear()
    enabled = await client.post(
        "/v1/admin/channel-connections/wechat-live-adapter-check/enable",
        params=params,
        headers={
            "If-Match": created.headers["etag"],
            "Idempotency-Key": "reject-disabled-adapter-enable",
        },
    )

    assert live_catalog.get("wechat-sdk") is None
    assert live_catalog.list_descriptors() == ()
    assert enabled.status_code == 409
    assert enabled.json()["detail"]["code"] == "adapter_not_registered"


def test_spi_v1_rejects_multiple_secrets_and_unsupported_schema_keywords() -> None:
    descriptor = ChannelAdapterDescriptor(
        adapter_id="multi-secret",
        display_name="Multiple secrets",
        channel="multi-secret",
        secret_fields=(
            SecretFieldDescriptor(name="one", label="One"),
            SecretFieldDescriptor(name="two", label="Two"),
        ),
    )
    with pytest.raises(ValueError, match="SPI v1 supports at most one secret field"):
        ChannelAdapterCatalog((ChannelAdapterRegistration(descriptor=descriptor),))

    unsupported_schema = ChannelAdapterDescriptor(
        adapter_id="unsupported-schema",
        display_name="Unsupported schema",
        channel="unsupported-schema",
        config_schema={
            "type": "object",
            "properties": {"endpoint": {"type": "string", "pattern": "^https://"}},
        },
    )
    with pytest.raises(ValueError, match="unsupported config schema keyword"):
        ChannelAdapterCatalog((ChannelAdapterRegistration(descriptor=unsupported_schema),))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_adapter_numbers_fail_closed(value: float) -> None:
    catalog = build_default_channel_adapter_catalog()
    document = ChannelConnectionDocument(
        tenant_id="tenant-a",
        connection_id="non-finite",
        adapter_id="wechat-sdk",
        display_name="Non-finite",
        desired_state="draft",
        effective_state="unverified",
        config_json={
            "sdk_url": "http://127.0.0.1:5080",
            "poll_interval_seconds": value,
        },
        secret_ref="",
        secret_status="not_required",
        version=1,
        priority=100,
        required_for_launch=False,
    )

    assert validate_connection_document(document, catalog) == (
        "config_number_non_finite_poll_interval_seconds",
    )


@pytest.mark.parametrize(
    ("field_schema", "message"),
    [
        ({"type": "string", "minLength": "x"}, "minLength"),
        ({"type": "number", "minimum": float("inf")}, "finite number"),
        ({"type": "number", "default": float("nan")}, "default"),
    ],
)
def test_catalog_rejects_malformed_schema_constraints(
    field_schema: dict,
    message: str,
) -> None:
    descriptor = ChannelAdapterDescriptor(
        adapter_id="malformed-schema",
        display_name="Malformed schema",
        channel="malformed-schema",
        config_schema={
            "type": "object",
            "properties": {"value": field_schema},
        },
    )
    with pytest.raises(ValueError, match=message):
        ChannelAdapterCatalog((ChannelAdapterRegistration(descriptor=descriptor),))


def test_legacy_projection_is_stable_optional_and_never_copies_token() -> None:
    token = "legacy-raw-token-must-not-escape"
    settings = SimpleNamespace(
        wxbot_default_tenant_id="tenant-a",
        wxbot_api_token=token,
        wxbot_sdk_url="http://127.0.0.1:5080",
        wxbot_bridge_poll_interval=3,
        wxbot_bridge_send_interval=2,
        wxbot_media_base_url="",
    )

    document = legacy_wxbot_connection_from_settings(settings)
    rendered = document.model_dump_json()

    assert document.connection_id == "legacy-wechat-default"
    assert document.secret_ref == ""
    assert document.secret_status == "not_required"
    assert document.required_for_launch is False
    assert document.managed_by == "environment"
    assert document.read_only is True
    assert token not in rendered

    no_token = SimpleNamespace(**{**settings.__dict__, "wxbot_api_token": ""})
    missing = legacy_wxbot_connection_from_settings(no_token)
    assert missing.secret_status == "not_required"
    assert _legacy_for_tenant(no_token, "tenant-a") is None
    assert _legacy_for_tenant(settings, "tenant-a") is not None

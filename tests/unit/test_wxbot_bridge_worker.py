from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.channel.connections import ChannelConnectionDocument
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from app.workers.wxbot_bridge_worker import (
    WxbotBridgeBinding,
    WxbotBridgeConfigurationError,
    managed_wxbot_binding_is_current,
    reconcile_managed_wxbot_ready,
    reconcile_managed_wxbot_stop,
    resolve_wxbot_bridge_binding,
)


class _ConnectionStore:
    def __init__(self, connection: ChannelConnectionDocument) -> None:
        self.connection = connection
        self.calls: list[tuple[str, str]] = []
        self.stopped_calls: list[tuple[str, str]] = []
        self.ready_calls: list[tuple[str, str, str]] = []

    async def get(self, tenant_id: str, connection_id: str) -> ChannelConnectionDocument:
        self.calls.append((tenant_id, connection_id))
        return self.connection

    async def mark_runtime_stopped(self, tenant_id: str, connection_id: str) -> bool:
        self.stopped_calls.append((tenant_id, connection_id))
        return self.connection.desired_state == "disabled"

    async def mark_runtime_ready(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        binding_fingerprint: str,
    ) -> bool:
        self.ready_calls.append((tenant_id, connection_id, binding_fingerprint))
        return self.connection.desired_state == "enabled"


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "wxbot_default_tenant_id": "tenant-a",
        "channel_connection_id": "",
        "channel_allowed_sdk_origins": "http://wxbot.internal:5080",
        "wxbot_api_token": "legacy-secret",
        "wxbot_sdk_url": "http://127.0.0.1:5080/",
        "wxbot_bridge_poll_interval": 3.0,
        "wxbot_bridge_send_interval": 2.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _connection(**overrides: object) -> ChannelConnectionDocument:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "connection_id": "wechat-main",
        "adapter_id": "wechat-sdk",
        "display_name": "微信主账号",
        "desired_state": "enabled",
        "effective_state": "ready",
        "config_json": {
            "sdk_url": "http://wxbot.internal:5080/",
            "poll_interval_seconds": 1.5,
            "send_interval_seconds": 0.5,
        },
        "secret_ref": "",
        "secret_status": "not_required",
        "version": 4,
        "priority": 100,
        "required_for_launch": False,
    }
    values.update(overrides)
    return ChannelConnectionDocument.model_validate(values)


@pytest.mark.asyncio
async def test_managed_bridge_binding_uses_selected_connection_without_a_token() -> None:
    store = _ConnectionStore(_connection())

    binding = await resolve_wxbot_bridge_binding(
        _settings(channel_connection_id="wechat-main", wxbot_api_token=""),
        connection_store=store,
        environ={},
    )

    assert store.calls == [("tenant-a", "wechat-main")]
    assert binding.connection_id == "wechat-main"
    assert binding.sdk_url == "http://wxbot.internal:5080"
    assert binding.poll_interval == 1.5
    assert binding.send_interval == 0.5
    assert binding.connection_version == 4
    assert binding.sdk_headers == {}


@pytest.mark.asyncio
async def test_managed_bridge_binding_rejects_disabled_or_wrong_adapter() -> None:
    with pytest.raises(
        WxbotBridgeConfigurationError,
        match="channel_connection_not_enabled",
    ):
        await resolve_wxbot_bridge_binding(
            _settings(channel_connection_id="wechat-main"),
            connection_store=_ConnectionStore(_connection(desired_state="disabled")),
            environ={"WXBOT_API_TOKEN": "managed-secret"},
        )

    with pytest.raises(
        WxbotBridgeConfigurationError,
        match="channel_adapter_not_wechat_sdk",
    ):
        await resolve_wxbot_bridge_binding(
            _settings(channel_connection_id="wechat-main"),
            connection_store=_ConnectionStore(_connection(adapter_id="feixin-http")),
            environ={"WXBOT_API_TOKEN": "managed-secret"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [float("nan"), float("inf"), float("-inf")])
async def test_managed_bridge_binding_rejects_non_finite_intervals(interval: float) -> None:
    with pytest.raises(
        WxbotBridgeConfigurationError,
        match="channel_poll_interval_invalid",
    ):
        await resolve_wxbot_bridge_binding(
            _settings(channel_connection_id="wechat-main"),
            connection_store=_ConnectionStore(
                _connection(
                    config_json={
                        "sdk_url": "http://wxbot.internal:5080",
                        "poll_interval_seconds": interval,
                        "send_interval_seconds": 0.5,
                    }
                )
            ),
            environ={"WXBOT_API_TOKEN": "managed-secret"},
        )

@pytest.mark.asyncio
async def test_managed_bridge_binding_fails_closed_when_secret_cannot_resolve() -> None:
    with pytest.raises(
        WxbotBridgeConfigurationError,
        match="channel_connection_secret_unavailable",
    ):
        await resolve_wxbot_bridge_binding(
            _settings(channel_connection_id="wechat-main"),
            connection_store=_ConnectionStore(
                _connection(
                    secret_ref="env://WXBOT_API_TOKEN",
                    secret_status="reference_configured",
                )
            ),
            environ={},
        )


@pytest.mark.asyncio
async def test_managed_bridge_binding_rejects_unapproved_origin_before_secret_resolution() -> None:
    with pytest.raises(
        WxbotBridgeConfigurationError,
        match="channel_sdk_origin_not_allowed",
    ):
        await resolve_wxbot_bridge_binding(
            _settings(channel_connection_id="wechat-main"),
            connection_store=_ConnectionStore(
                _connection(
                    config_json={
                        "sdk_url": "http://attacker.internal:5080",
                        "poll_interval_seconds": 1.5,
                        "send_interval_seconds": 0.5,
                    }
                )
            ),
            # The origin check must win even when the credential is absent.
            environ={},
        )

    with pytest.raises(
        WxbotBridgeConfigurationError,
        match="channel_connection_secret_unavailable",
    ):
        await resolve_wxbot_bridge_binding(
            _settings(channel_connection_id="wechat-main"),
            connection_store=_ConnectionStore(
                _connection(secret_ref="env://OPENAI_API_KEY")
            ),
            environ={"OPENAI_API_KEY": "must-not-leak"},
        )


@pytest.mark.asyncio
async def test_legacy_bridge_binding_preserves_environment_compatibility_scope() -> None:
    binding = await resolve_wxbot_bridge_binding(_settings())

    assert binding.connection_id == LEGACY_WXBOT_CONNECTION_ID
    assert binding.sdk_url == "http://127.0.0.1:5080"
    assert binding.sdk_headers == {"Authorization": "Bearer legacy-secret"}


@pytest.mark.asyncio
async def test_managed_binding_readiness_stops_on_disable_delete_or_edit() -> None:
    current = _connection()
    store = _ConnectionStore(current)
    binding = await resolve_wxbot_bridge_binding(
        _settings(channel_connection_id="wechat-main"),
        connection_store=store,
        environ={"WXBOT_API_TOKEN": "managed-secret"},
    )

    assert await managed_wxbot_binding_is_current(binding, store)

    store.connection = _connection(desired_state="disabled")
    assert not await managed_wxbot_binding_is_current(binding, store)

    # Probe/validation telemetry may advance the CAS version without changing
    # the connector binding and must not restart the worker.
    store.connection = _connection(version=5)
    assert await managed_wxbot_binding_is_current(binding, store)

    store.connection = _connection(
        version=6,
        config_json={
            "sdk_url": "http://wxbot-new.internal:5080",
            "poll_interval_seconds": 1.5,
            "send_interval_seconds": 0.5,
        },
    )
    assert not await managed_wxbot_binding_is_current(binding, store)

    class _DeletedStore:
        async def get(self, _tenant_id: str, _connection_id: str) -> ChannelConnectionDocument:
            raise LookupError("deleted")

    assert not await managed_wxbot_binding_is_current(binding, _DeletedStore())


@pytest.mark.asyncio
async def test_legacy_bridge_binding_allows_a_tokenless_sdk() -> None:
    binding = await resolve_wxbot_bridge_binding(_settings(wxbot_api_token=""))

    assert binding.sdk_headers == {}


@pytest.mark.asyncio
async def test_worker_confirms_effective_stop_only_for_managed_connection() -> None:
    store = _ConnectionStore(_connection(desired_state="disabled"))
    binding = WxbotBridgeBinding(
        tenant_id="tenant-a",
        connection_id="wechat-main",
        sdk_url="http://wxbot.internal:5080",
        poll_interval=1.0,
        send_interval=1.0,
        connection_version=4,
        binding_fingerprint="fingerprint",
        sdk_headers={},
    )

    assert await reconcile_managed_wxbot_stop(binding, store)
    assert store.stopped_calls == [("tenant-a", "wechat-main")]

    legacy = binding.__class__(
        tenant_id="tenant-a",
        connection_id=LEGACY_WXBOT_CONNECTION_ID,
        sdk_url=binding.sdk_url,
        poll_interval=binding.poll_interval,
        send_interval=binding.send_interval,
        connection_version=1,
        binding_fingerprint="legacy",
        sdk_headers={},
    )
    assert not await reconcile_managed_wxbot_stop(legacy, store)
    assert store.stopped_calls == [("tenant-a", "wechat-main")]


@pytest.mark.asyncio
async def test_worker_confirms_effective_ready_only_for_managed_connection() -> None:
    store = _ConnectionStore(_connection(effective_state="unverified"))
    binding = WxbotBridgeBinding(
        tenant_id="tenant-a",
        connection_id="wechat-main",
        sdk_url="http://wxbot.internal:5080",
        poll_interval=1.0,
        send_interval=1.0,
        connection_version=4,
        binding_fingerprint="fingerprint",
        sdk_headers={},
    )

    assert await reconcile_managed_wxbot_ready(binding, store)
    assert store.ready_calls == [
        ("tenant-a", "wechat-main", "fingerprint")
    ]

    legacy = binding.__class__(
        tenant_id="tenant-a",
        connection_id=LEGACY_WXBOT_CONNECTION_ID,
        sdk_url=binding.sdk_url,
        poll_interval=binding.poll_interval,
        send_interval=binding.send_interval,
        connection_version=1,
        binding_fingerprint="legacy",
        sdk_headers={},
    )
    assert not await reconcile_managed_wxbot_ready(legacy, store)
    assert store.ready_calls == [
        ("tenant-a", "wechat-main", "fingerprint")
    ]

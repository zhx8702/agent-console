from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.bus.redis_streams import RedisStreamBus
from app.channel.adapters import (
    WECHAT_SDK_ADAPTER_ID,
    build_default_channel_adapter_catalog,
)
from app.channel.connections import (
    ChannelConnectionDocument,
    ChannelConnectionStore,
    connection_binding_fingerprint,
)
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from app.channel.secrets import ChannelSecretReferenceError, resolve_channel_secret_ref
from app.common.logging import configure_logging, get_logger
from app.common.safe_url import normalize_origin
from app.container import WxbotBridgeContainer, set_container
from app.infra.db import get_session_factory
from app.infra.redis_client import get_redis
from app.plugin.state import PluginStateStore
from app.social.store import SocialPolicyStore
from app.workers.heartbeat import WorkerHeartbeat
from app.workers.readiness import (
    ensure_role_dependencies_ready,
    probe_role_dependencies,
    probe_wxbot_sdk_semantics,
)
from app.workers.runtime import run_worker_process, worker_process_settings
from plugins.wxbot.bridge import SdkBridge
from plugins.wxbot.store import WxbotStore

log = get_logger(__name__)


class _ChannelConnectionReader(Protocol):
    async def get(self, tenant_id: str, connection_id: str) -> ChannelConnectionDocument:
        ...


class WxbotBridgeConfigurationError(RuntimeError):
    """The selected connection cannot safely start a connector process."""


@dataclass(frozen=True, slots=True)
class WxbotBridgeBinding:
    tenant_id: str
    connection_id: str
    sdk_url: str
    poll_interval: float
    send_interval: float
    connection_version: int
    binding_fingerprint: str
    sdk_headers: dict[str, str] = field(repr=False)


async def resolve_wxbot_bridge_binding(
    settings: Any,
    *,
    connection_store: _ChannelConnectionReader | None = None,
    environ: Mapping[str, str] | None = None,
) -> WxbotBridgeBinding:
    """Bind one bridge process to one explicit, tenant-scoped connection.

    An empty ``CHANNEL_CONNECTION_ID`` keeps the one-release environment
    compatibility path. A deployment may still provide an optional SDK token
    through the connector process environment, but the WeChat connection itself
    does not require or expose a credential field.
    """

    tenant_id = str(
        getattr(settings, "wxbot_default_tenant_id", "") or "default"
    ).strip()
    connection_id = str(getattr(settings, "channel_connection_id", "") or "").strip()
    if not connection_id or connection_id == LEGACY_WXBOT_CONNECTION_ID:
        token = str(getattr(settings, "wxbot_api_token", "") or "").strip()
        return WxbotBridgeBinding(
            tenant_id=tenant_id,
            connection_id=LEGACY_WXBOT_CONNECTION_ID,
            sdk_url=_required_sdk_url(getattr(settings, "wxbot_sdk_url", "")),
            poll_interval=_positive_interval(
                getattr(settings, "wxbot_bridge_poll_interval", 3.0),
                "poll_interval",
            ),
            send_interval=_positive_interval(
                getattr(settings, "wxbot_bridge_send_interval", 2.0),
                "send_interval",
            ),
            connection_version=1,
            binding_fingerprint="legacy-environment",
            sdk_headers=_optional_sdk_headers(token),
        )

    store = connection_store
    if store is None:
        store = ChannelConnectionStore(
            get_session_factory(),
            build_default_channel_adapter_catalog(),
        )
    try:
        connection = await store.get(tenant_id, connection_id)
    except Exception as exc:
        raise WxbotBridgeConfigurationError("channel_connection_unavailable") from exc
    if connection.adapter_id != WECHAT_SDK_ADAPTER_ID:
        raise WxbotBridgeConfigurationError("channel_adapter_not_wechat_sdk")
    if connection.desired_state != "enabled":
        raise WxbotBridgeConfigurationError("channel_connection_not_enabled")
    config = dict(connection.config_json or {})
    sdk_url = _required_sdk_url(config.get("sdk_url"))
    _require_managed_sdk_origin_allowed(sdk_url, settings)
    token = str(getattr(settings, "wxbot_api_token", "") or "").strip()
    if connection.secret_ref:
        try:
            token = resolve_channel_secret_ref(
                connection.secret_ref,
                environ=environ,
                allowed_environment_variables={"WXBOT_API_TOKEN"},
            )
        except ChannelSecretReferenceError as exc:
            raise WxbotBridgeConfigurationError("channel_connection_secret_unavailable") from exc
    return WxbotBridgeBinding(
        tenant_id=connection.tenant_id,
        connection_id=connection.connection_id,
        sdk_url=sdk_url,
        poll_interval=_positive_interval(
            config.get("poll_interval_seconds", 3.0),
            "poll_interval",
        ),
        send_interval=_positive_interval(
            config.get("send_interval_seconds", 2.0),
            "send_interval",
        ),
        connection_version=connection.version,
        binding_fingerprint=connection_binding_fingerprint(connection),
        sdk_headers=_optional_sdk_headers(token),
    )


def _optional_sdk_headers(token: str) -> dict[str, str]:
    normalized = str(token or "").strip()
    return {"Authorization": f"Bearer {normalized}"} if normalized else {}


async def managed_wxbot_binding_is_current(
    binding: WxbotBridgeBinding,
    connection_store: _ChannelConnectionReader,
) -> bool:
    """Fail readiness when a managed connection is disabled or reconfigured.

    The worker process intentionally restarts to pick up a new credential or
    endpoint instead of continuing with a stale cached binding.
    """

    if binding.connection_id == LEGACY_WXBOT_CONNECTION_ID:
        return True
    try:
        connection = await connection_store.get(
            binding.tenant_id,
            binding.connection_id,
        )
    except Exception:
        return False
    return bool(
        connection.adapter_id == WECHAT_SDK_ADAPTER_ID
        and connection.desired_state == "enabled"
        and connection_binding_fingerprint(connection) == binding.binding_fingerprint
    )


async def reconcile_managed_wxbot_stop(
    binding: WxbotBridgeBinding,
    connection_store: _ChannelConnectionReader,
) -> bool:
    """Confirm effective=disabled only after this runner has actually stopped."""

    if binding.connection_id == LEGACY_WXBOT_CONNECTION_ID:
        return False
    marker = getattr(connection_store, "mark_runtime_stopped", None)
    if not callable(marker):
        return False
    try:
        return bool(await marker(binding.tenant_id, binding.connection_id))
    except Exception as exc:
        log.warning(
            "wxbot_bridge_worker.runtime_stop_reconcile_failed",
            tenant_id=binding.tenant_id,
            connection_id=binding.connection_id,
            error_class=exc.__class__.__name__,
        )
        return False


async def reconcile_managed_wxbot_ready(
    binding: WxbotBridgeBinding,
    connection_store: _ChannelConnectionReader,
) -> bool:
    """Persist successful live SDK readiness without requiring an admin probe."""

    if binding.connection_id == LEGACY_WXBOT_CONNECTION_ID:
        return False
    marker = getattr(connection_store, "mark_runtime_ready", None)
    if not callable(marker):
        return False
    try:
        return bool(
            await marker(
                binding.tenant_id,
                binding.connection_id,
                binding_fingerprint=binding.binding_fingerprint,
            )
        )
    except Exception as exc:
        log.warning(
            "wxbot_bridge_worker.runtime_ready_reconcile_failed",
            tenant_id=binding.tenant_id,
            connection_id=binding.connection_id,
            error_class=exc.__class__.__name__,
        )
        return False


def _required_sdk_url(value: Any) -> str:
    sdk_url = str(value or "").strip().rstrip("/")
    if not sdk_url:
        raise WxbotBridgeConfigurationError("channel_sdk_url_missing")
    return sdk_url


def _require_managed_sdk_origin_allowed(sdk_url: str, settings: Any) -> None:
    """Bind a managed credential to deployment-approved service origins."""

    requested_origin = normalize_origin(sdk_url)
    configured_values = [
        str(getattr(settings, "wxbot_sdk_url", "") or ""),
        *str(getattr(settings, "channel_allowed_sdk_origins", "") or "").split(","),
    ]
    allowed_origins = {
        origin
        for value in configured_values
        if (origin := normalize_origin(str(value or "").strip()))
    }
    if not requested_origin or requested_origin not in allowed_origins:
        raise WxbotBridgeConfigurationError("channel_sdk_origin_not_allowed")


def _positive_interval(value: Any, field_name: str) -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError) as exc:
        raise WxbotBridgeConfigurationError(f"channel_{field_name}_invalid") from exc
    if not math.isfinite(interval) or interval < 0.1:
        raise WxbotBridgeConfigurationError(f"channel_{field_name}_invalid")
    return interval


class WxbotBridgeWorker:
    def __init__(self, bridge: SdkBridge) -> None:
        self._bridge = bridge
        self._stop = asyncio.Event()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._bridge.start()
        self._initialized = True

    async def run(self) -> None:
        await self.initialize()
        log.info("wxbot_bridge_worker.starting")
        await self._stop.wait()
        log.info("wxbot_bridge_worker.stopped")

    async def stop(self) -> None:
        self._stop.set()
        await self._bridge.stop()


async def run_wxbot_bridge_worker() -> None:
    with worker_process_settings("wxbot_bridge") as settings:
        configure_logging()

        redis = get_redis()
        connection_store = ChannelConnectionStore(
            get_session_factory(),
            build_default_channel_adapter_catalog(),
        )
        binding = await resolve_wxbot_bridge_binding(
            settings,
            connection_store=connection_store,
        )

        async def sdk_readiness_probe() -> bool:
            if not await managed_wxbot_binding_is_current(binding, connection_store):
                return False
            ready = await probe_wxbot_sdk_semantics(
                settings,
                sdk_url=binding.sdk_url,
                sdk_headers=binding.sdk_headers,
                attempts=3,
                timeout_seconds=5.0,
                retry_delay_seconds=0.5,
            )
            if ready:
                await reconcile_managed_wxbot_ready(binding, connection_store)
            return ready

        await ensure_role_dependencies_ready(
            "wxbot_bridge",
            settings,
            redis=redis,
            wxbot_sdk_probe=sdk_readiness_probe,
        )
        bus = RedisStreamBus(redis, settings)
        store = WxbotStore(settings)
        await store.ensure_tables()
        plugin_state_store = PluginStateStore()

        async def owners_scope_execution_allowed(
            owner_versions: dict[str, str],
            tenant_id: str,
            session_id: str,
        ) -> bool:
            allowed = await plugin_state_store.execution_snapshot_allowed(
                owner_versions,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return allowed is True

        social_policy_store = SocialPolicyStore(get_session_factory())
        container = WxbotBridgeContainer(
            bus=bus,
            wxbot_store=store,
            social_policy_store=social_policy_store,
        )
        set_container(container)

        bridge = SdkBridge(
            sdk_url=binding.sdk_url,
            tenant_id=binding.tenant_id,
            container=container,
            settings=settings,
            store=store,
            redis=redis,
            social_policy_store=social_policy_store,
            poll_interval=binding.poll_interval,
            send_interval=binding.send_interval,
            connection_id=binding.connection_id,
            sdk_headers=binding.sdk_headers,
            owners_scope_execution_allowed=owners_scope_execution_allowed,
            connection_activity_recorder=(
                None
                if binding.connection_id == LEGACY_WXBOT_CONNECTION_ID
                else lambda direction: connection_store.record_runtime_activity(
                    binding.tenant_id,
                    binding.connection_id,
                    direction=direction,
                    binding_fingerprint=binding.binding_fingerprint,
                )
            ),
        )
        worker = WxbotBridgeWorker(bridge)
        try:
            await run_worker_process(
                "wxbot_bridge",
                initialize=worker.initialize,
                run=worker.run,
                stop=worker.stop,
                container=container,
                shutdown_timeout_seconds=settings.worker_shutdown_timeout_seconds,
                heartbeat=WorkerHeartbeat.from_settings(redis, settings),
                readiness_check=lambda: probe_role_dependencies(
                    "wxbot_bridge",
                    settings,
                    redis=redis,
                    wxbot_sdk_probe=sdk_readiness_probe,
                ),
                readiness_interval_seconds=settings.worker_heartbeat_interval_seconds,
                metrics_host=settings.worker_metrics_host,
                metrics_port=settings.worker_metrics_port,
            )
        finally:
            await reconcile_managed_wxbot_stop(binding, connection_store)


def main() -> None:
    asyncio.run(run_wxbot_bridge_worker())


if __name__ == "__main__":
    main()

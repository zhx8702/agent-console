from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from app.channel.models import ChannelMedia, ChannelSendOptions, ChannelSendResult, ChannelTarget


class ChannelOutbound(Protocol):
    async def get_session_policy(self, target: ChannelTarget) -> dict:
        ...

    async def send_text(
        self,
        target: ChannelTarget,
        text: str,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        ...

    async def send_image(
        self,
        target: ChannelTarget,
        media: ChannelMedia,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        ...


ChannelOwnerGate: TypeAlias = Callable[[str, ChannelTarget], Awaitable[bool]]


class ChannelOutboundExecutionDenied(RuntimeError):
    """An owned outbound provider is not executable for the target scope."""

    def __init__(
        self,
        owner: str,
        target: ChannelTarget,
        *,
        reason: str = "owner_execution_denied",
    ) -> None:
        self.owner = str(owner or "").strip()
        self.target = target
        self.reason = str(reason or "owner_execution_denied").strip()
        super().__init__(
            "channel outbound execution denied: "
            f"owner={self.owner or '<missing>'} "
            f"tenant={target.tenant_id or '<missing>'} "
            f"session={target.session_id or '<missing>'} "
            f"reason={self.reason}"
        )


@dataclass(frozen=True, slots=True)
class _GatedChannelOutbound:
    _provider: ChannelOutbound
    _owner: str
    _owner_gate: ChannelOwnerGate | None

    async def get_session_policy(self, target: ChannelTarget) -> dict:
        await self._require_execution(target)
        return await self._provider.get_session_policy(target)

    async def send_text(
        self,
        target: ChannelTarget,
        text: str,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        await self._require_execution(target)
        return await self._provider.send_text(target, text, options)

    async def send_image(
        self,
        target: ChannelTarget,
        media: ChannelMedia,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        await self._require_execution(target)
        return await self._provider.send_image(target, media, options)

    async def capture_group_delivery_contract(
        self,
        target: ChannelTarget,
        *,
        source_message_id: str,
        response_kind: str = "tool_result",
    ) -> dict[str, Any]:
        """Gate and preserve an adapter's optional async-delivery fence."""

        await self._require_execution(target)
        capture = getattr(self._provider, "capture_group_delivery_contract", None)
        if not callable(capture):
            return {}
        result = await capture(
            target,
            source_message_id=source_message_id,
            response_kind=response_kind,
        )
        return dict(result) if isinstance(result, dict) else {}

    async def _require_execution(self, target: ChannelTarget) -> None:
        gate = self._owner_gate
        if gate is None:
            raise ChannelOutboundExecutionDenied(
                self._owner,
                target,
                reason="owner_gate_not_configured",
            )
        try:
            allowed = await gate(self._owner, target)
        except Exception as exc:
            raise ChannelOutboundExecutionDenied(
                self._owner,
                target,
                reason="owner_gate_error",
            ) from exc
        if not isinstance(allowed, bool):
            raise ChannelOutboundExecutionDenied(
                self._owner,
                target,
                reason="owner_gate_invalid_result",
            )
        if not allowed:
            raise ChannelOutboundExecutionDenied(self._owner, target)


@dataclass(frozen=True, slots=True)
class _OutboundBinding:
    provider: ChannelOutbound
    owner: str = ""
    channel: str = ""
    adapter_id: str = ""


_RouteKey = tuple[str, str, str]


class ChannelRegistry:
    """Route outbound traffic by tenant+connection, with channel fallback.

    A connection route always wins when an event carries a connection ID.
    Legacy producers which do not yet carry one continue to use the channel
        fallback registration.

    Owned bindings are exposed through a facade which rechecks the target
    scope for every provider operation. Missing gates fail closed; an empty
    owner deliberately preserves kernel and legacy compatibility.
    """

    def __init__(self, *, owner_gate: ChannelOwnerGate | None = None) -> None:
        self._owner_gate = owner_gate
        self._outbound: dict[str, _OutboundBinding] = {}
        self._adapter_outbound: dict[str, _OutboundBinding] = {}
        self._connection_outbound: dict[tuple[str, str], _OutboundBinding] = {}
        self._owners: dict[str, set[_RouteKey]] = {}

    def register_outbound(
        self,
        channel: str,
        provider: ChannelOutbound,
        *,
        owner: str = "",
        tenant_id: str = "",
        connection_id: str = "",
        adapter_id: str = "",
    ) -> None:
        """Register a legacy channel fallback or a connection-specific provider."""

        if connection_id or tenant_id:
            self.register_connection_outbound(
                tenant_id,
                connection_id,
                provider,
                channel=channel,
                adapter_id=adapter_id,
                owner=owner,
            )
            return
        channel_key = self._normalize_channel(channel)
        if not channel_key:
            raise ValueError("channel cannot be empty")
        route: _RouteKey = ("channel", "", channel_key)
        self._replace_binding(
            route,
            _OutboundBinding(
                provider=provider,
                owner=str(owner or "").strip(),
                channel=channel_key,
                adapter_id=self._normalize_channel(adapter_id),
            ),
        )

    def register_connection_outbound(
        self,
        tenant_id: str,
        connection_id: str,
        provider: ChannelOutbound,
        *,
        channel: str = "",
        adapter_id: str = "",
        owner: str = "",
    ) -> None:
        tenant = str(tenant_id or "").strip()
        connection = str(connection_id or "").strip()
        if not tenant:
            raise ValueError("tenant_id cannot be empty for a connection route")
        if not connection:
            raise ValueError("connection_id cannot be empty for a connection route")
        route: _RouteKey = ("connection", tenant, connection)
        self._replace_binding(
            route,
            _OutboundBinding(
                provider=provider,
                owner=str(owner or "").strip(),
                channel=self._normalize_channel(channel),
                adapter_id=self._normalize_channel(adapter_id),
            ),
        )

    def register_adapter_outbound(
        self,
        adapter_id: str,
        provider: ChannelOutbound,
        *,
        channel: str = "",
        owner: str = "",
    ) -> None:
        """Register an adapter dispatcher for connection-scoped targets.

        Adapter dispatchers are deliberately separate from the legacy channel
        fallback.  They must validate the target connection before sending, so
        a scoped event can never silently fall through to another account.
        """

        adapter = self._normalize_channel(adapter_id)
        if not adapter:
            raise ValueError("adapter_id cannot be empty")
        route: _RouteKey = ("adapter", "", adapter)
        self._replace_binding(
            route,
            _OutboundBinding(
                provider=provider,
                owner=str(owner or "").strip(),
                channel=self._normalize_channel(channel),
                adapter_id=adapter,
            ),
        )

    def unregister_connection_outbound(
        self,
        tenant_id: str,
        connection_id: str,
    ) -> bool:
        route: _RouteKey = (
            "connection",
            str(tenant_id or "").strip(),
            str(connection_id or "").strip(),
        )
        binding = self._binding_for_route(route)
        if binding is None:
            return False
        self._delete_route(route)
        if binding.owner:
            routes = self._owners.get(binding.owner)
            if routes is not None:
                routes.discard(route)
                if not routes:
                    self._owners.pop(binding.owner, None)
        return True

    def unregister_owner(self, owner: str) -> int:
        normalized_owner = str(owner or "").strip()
        routes = self._owners.pop(normalized_owner, set())
        removed = 0
        for route in routes:
            binding = self._binding_for_route(route)
            # A later owner may have replaced this route. Never delete another
            # plugin's provider merely because stale ownership metadata exists.
            if binding is None or binding.owner != normalized_owner:
                continue
            self._delete_route(route)
            removed += 1
        return removed

    def outbound_for(
        self,
        channel: str,
        *,
        tenant_id: str = "",
        connection_id: str = "",
        adapter_id: str = "",
    ) -> ChannelOutbound | None:
        binding = self._resolve_binding(
            channel,
            tenant_id=tenant_id,
            connection_id=connection_id,
            adapter_id=adapter_id,
        )
        return self._outbound_facade(binding) if binding is not None else None

    def owner_for(
        self,
        channel: str,
        *,
        tenant_id: str = "",
        connection_id: str = "",
        adapter_id: str = "",
    ) -> str:
        """Return the executable owner of the selected outbound binding."""

        binding = self._resolve_binding(
            channel,
            tenant_id=tenant_id,
            connection_id=connection_id,
            adapter_id=adapter_id,
        )
        return binding.owner if binding is not None else ""

    def outbound_for_target(self, target: ChannelTarget) -> ChannelOutbound | None:
        return self.outbound_for(
            target.channel,
            tenant_id=target.tenant_id,
            connection_id=target.connection_id,
            adapter_id=target.adapter_id,
        )

    def owner_for_target(self, target: ChannelTarget) -> str:
        return self.owner_for(
            target.channel,
            tenant_id=target.tenant_id,
            connection_id=target.connection_id,
            adapter_id=target.adapter_id,
        )

    def require_outbound(
        self,
        channel: str,
        *,
        tenant_id: str = "",
        connection_id: str = "",
        adapter_id: str = "",
    ) -> ChannelOutbound:
        provider = self.outbound_for(
            channel,
            tenant_id=tenant_id,
            connection_id=connection_id,
            adapter_id=adapter_id,
        )
        if provider is None:
            scope = f" tenant={tenant_id} connection={connection_id}" if connection_id else ""
            raise RuntimeError(
                f"channel outbound provider not registered: {channel}{scope}"
            )
        return provider

    def require_outbound_for_target(self, target: ChannelTarget) -> ChannelOutbound:
        return self.require_outbound(
            target.channel,
            tenant_id=target.tenant_id,
            connection_id=target.connection_id,
            adapter_id=target.adapter_id,
        )

    def _resolve_binding(
        self,
        channel: str,
        *,
        tenant_id: str,
        connection_id: str,
        adapter_id: str,
    ) -> _OutboundBinding | None:
        tenant = str(tenant_id or "").strip()
        connection = str(connection_id or "").strip()
        if connection:
            if not tenant:
                return None
            binding = self._connection_outbound.get((tenant, connection))
            if binding is not None:
                return binding
            # A scoped event may use only its adapter dispatcher, never the
            # channel-wide legacy fallback. The dispatcher is responsible for
            # checking that tenant+connection is enabled before it sends.
            return self._adapter_outbound.get(self._normalize_channel(adapter_id))
        return self._outbound.get(self._normalize_channel(channel))

    def _replace_binding(self, route: _RouteKey, binding: _OutboundBinding) -> None:
        previous = self._binding_for_route(route)
        if previous is not None and previous.owner:
            previous_routes = self._owners.get(previous.owner)
            if previous_routes is not None:
                previous_routes.discard(route)
                if not previous_routes:
                    self._owners.pop(previous.owner, None)
        if route[0] == "channel":
            self._outbound[route[2]] = binding
        elif route[0] == "adapter":
            self._adapter_outbound[route[2]] = binding
        else:
            self._connection_outbound[(route[1], route[2])] = binding
        if binding.owner:
            self._owners.setdefault(binding.owner, set()).add(route)

    def _outbound_facade(self, binding: _OutboundBinding) -> ChannelOutbound:
        # Empty owners are the compatibility boundary for kernel and legacy
        # providers. Plugin registrations carry an owner and are always gated.
        if not binding.owner:
            return binding.provider
        return _GatedChannelOutbound(
            _provider=binding.provider,
            _owner=binding.owner,
            _owner_gate=self._owner_gate,
        )

    def _binding_for_route(self, route: _RouteKey) -> _OutboundBinding | None:
        if route[0] == "channel":
            return self._outbound.get(route[2])
        if route[0] == "adapter":
            return self._adapter_outbound.get(route[2])
        return self._connection_outbound.get((route[1], route[2]))

    def _delete_route(self, route: _RouteKey) -> None:
        if route[0] == "channel":
            self._outbound.pop(route[2], None)
        elif route[0] == "adapter":
            self._adapter_outbound.pop(route[2], None)
        else:
            self._connection_outbound.pop((route[1], route[2]), None)

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return str(channel or "").strip().lower()


__all__ = [
    "ChannelOutbound",
    "ChannelOutboundExecutionDenied",
    "ChannelOwnerGate",
    "ChannelRegistry",
]

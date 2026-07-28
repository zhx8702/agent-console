from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.billing import (
    BillingCapture,
    BillingCoordinator,
    BillingExecutionDenied,
    BillingQuote,
    BillingReservation,
    BillingResource,
    BillingSubject,
)
from app.plugin.base import PluginContext
from plugins.credits.billing import CreditsBillingProvider
from plugins.credits.plugin import CreditsPlugin

SUBJECT = BillingSubject(
    tenant_id="tenant-a",
    session_id="room-a",
    user_id="user-a",
    display_name="成员甲",
)
RESOURCE = BillingResource(
    kind="chat",
    operation="reply",
    reference="message-a",
)


class _FakeProvider:
    name = "credits"

    def __init__(self) -> None:
        self.quotes = 0
        self.reservations = 0
        self.captures = 0
        self.releases = 0

    async def quote(
        self,
        subject: BillingSubject,
        resource: BillingResource,
    ) -> BillingQuote:
        self.quotes += 1
        return BillingQuote(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=3,
            currency="credits",
        )

    async def reserve(
        self,
        subject: BillingSubject,
        resource: BillingResource,
    ) -> BillingReservation:
        self.reservations += 1
        return BillingReservation(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=3,
            currency="credits",
            reservation_id="reservation-a",
        )

    async def capture(
        self,
        reservation: BillingReservation,
        *,
        amount: int | None = None,
    ) -> BillingCapture:
        self.captures += 1
        return BillingCapture(
            provider=self.name,
            subject=reservation.subject,
            resource=reservation.resource,
            amount=reservation.amount if amount is None else amount,
            currency=reservation.currency,
        )

    async def release(self, reservation: BillingReservation) -> None:
        _ = reservation
        self.releases += 1


@pytest.mark.asyncio
async def test_owned_provider_rechecks_scope_and_keeps_release_available() -> None:
    provider = _FakeProvider()
    allowed = True
    gate_calls: list[tuple[str, str]] = []

    async def gate(tenant_id: str, session_id: str) -> bool:
        gate_calls.append((tenant_id, session_id))
        return allowed

    billing = BillingCoordinator()
    billing.register_provider(
        provider,
        owner="credits",
        scope_execution_allowed=gate,
    )

    await billing.quote(SUBJECT, RESOURCE)
    reservation = await billing.reserve(SUBJECT, RESOURCE)
    assert provider.quotes == provider.reservations == 1

    allowed = False
    with pytest.raises(BillingExecutionDenied, match="operation=quote"):
        await billing.quote(SUBJECT, RESOURCE)
    with pytest.raises(BillingExecutionDenied, match="operation=reserve"):
        await billing.reserve(SUBJECT, RESOURCE)
    with pytest.raises(BillingExecutionDenied, match="operation=capture"):
        await billing.capture(reservation)
    exposed = billing.provider("credits")
    assert exposed is not None
    with pytest.raises(BillingExecutionDenied, match="operation=reserve"):
        await exposed.reserve(SUBJECT, RESOURCE)

    await exposed.release(reservation)
    assert provider.captures == 0
    assert provider.releases == 1
    assert gate_calls == [("tenant-a", "room-a")] * 6


@pytest.mark.asyncio
async def test_owned_provider_missing_or_failing_gate_fails_closed() -> None:
    provider = _FakeProvider()
    missing_gate = BillingCoordinator()
    missing_gate.register_provider(provider, owner="credits")

    with pytest.raises(BillingExecutionDenied):
        await missing_gate.reserve(SUBJECT, RESOURCE)

    async def failing_gate(tenant_id: str, session_id: str) -> bool:
        _ = tenant_id, session_id
        raise RuntimeError("policy store unavailable")

    failing = BillingCoordinator()
    failing.register_provider(
        _FakeProvider(),
        owner="credits",
        scope_execution_allowed=failing_gate,
    )
    with pytest.raises(BillingExecutionDenied):
        await failing.reserve(SUBJECT, RESOURCE)
    assert provider.reservations == 0


@pytest.mark.asyncio
async def test_billing_scope_gate_propagates_cancellation() -> None:
    async def cancelled_gate(tenant_id: str, session_id: str) -> bool:
        _ = tenant_id, session_id
        raise asyncio.CancelledError

    billing = BillingCoordinator()
    billing.register_provider(
        _FakeProvider(),
        owner="credits",
        scope_execution_allowed=cancelled_gate,
    )
    with pytest.raises(asyncio.CancelledError):
        await billing.reserve(SUBJECT, RESOURCE)


def test_billing_provider_ownership_is_indexed_and_cannot_be_stolen() -> None:
    billing = BillingCoordinator()
    original = _FakeProvider()
    billing.register_provider(original, owner="credits")

    exposed = billing.provider("credits")
    assert exposed is not None
    assert exposed is not original
    assert exposed.name == "credits"
    assert billing.provider_owner("credits") == "credits"
    assert billing.providers_for_owner("credits") == ("credits",)
    with pytest.raises(ValueError, match="another owner"):
        billing.register_provider(_FakeProvider(), owner="other")

    assert billing.unregister_owner("credits") == 1
    assert billing.provider("credits") is None
    assert billing.providers_for_owner("credits") == ()


class _FakeCreditStore:
    def __init__(self, settings: object | None = None) -> None:
        self.settings = settings
        self.config_reads = 0
        self.balance_reads = 0
        self.reserve_calls = 0
        self.capture_calls = 0
        self.release_calls = 0

    async def ensure_tables(self) -> None:
        return None

    async def get_config(self, tenant_id: str, session_id: str) -> dict[str, object]:
        _ = tenant_id, session_id
        self.config_reads += 1
        return {
            "enabled": True,
            "cost_per_chat": 3,
            "credit_name": "积分",
        }

    async def peek_balance(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        display_name: str = "",
    ) -> int:
        _ = tenant_id, session_id, user_id, display_name
        self.balance_reads += 1
        return 20

    async def reserve_charge(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        amount: int,
        **kwargs: object,
    ) -> dict[str, object]:
        _ = tenant_id, session_id, user_id, kwargs
        self.reserve_calls += 1
        return {
            "reservation_id": "credit-reservation-a",
            "amount": amount,
            "balance": 17,
        }

    async def capture_reservation(
        self,
        reservation_id: str,
        **kwargs: object,
    ) -> dict[str, object]:
        _ = reservation_id, kwargs
        self.capture_calls += 1
        return {"amount": 3, "balance": 17}

    async def release_reservation(self, reservation_id: str) -> None:
        _ = reservation_id
        self.release_calls += 1


@pytest.mark.asyncio
async def test_credits_provider_rechecks_after_quote_before_reserve() -> None:
    store = _FakeCreditStore()
    decisions = iter((True, True, False))

    async def gate(tenant_id: str, session_id: str) -> bool:
        assert (tenant_id, session_id) == ("tenant-a", "room-a")
        return next(decisions)

    provider = CreditsBillingProvider(  # type: ignore[arg-type]
        store,
        scope_execution_allowed=gate,
    )
    with pytest.raises(BillingExecutionDenied, match="operation=reserve"):
        await provider.reserve(SUBJECT, RESOURCE)

    assert store.config_reads == 1
    assert store.balance_reads == 1
    assert store.reserve_calls == 0


@dataclass
class _FakePluginRegistry:
    allowed: bool = True

    def __post_init__(self) -> None:
        self.loaded_plugins: dict[str, object] = {}
        self.calls: list[tuple[str, str, str]] = []

    async def scope_execution_allowed(
        self,
        owner: str,
        *,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        self.calls.append((owner, tenant_id, session_id))
        return self.allowed


@pytest.mark.asyncio
async def test_credits_plugin_disable_blocks_new_settlement_but_shutdown_keeps_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeCreditStore()
    monkeypatch.setitem(
        CreditsPlugin.initialize.__globals__,
        "CreditStore",
        lambda settings: store,
    )
    registry = _FakePluginRegistry()
    billing = BillingCoordinator()
    plugin = CreditsPlugin()
    await plugin.initialize(
        PluginContext(
            container=SimpleNamespace(
                billing=billing,
                plugin_registry=registry,
            ),
            settings=SimpleNamespace(),
        )
    )

    reservation = await billing.reserve(SUBJECT, RESOURCE)
    assert billing.provider_owner("credits") == "credits"
    assert store.reserve_calls == 1

    # The registry closes the execution gate before invoking on_disable.
    registry.allowed = False
    await plugin.on_disable()
    with pytest.raises(BillingExecutionDenied, match="operation=capture"):
        await billing.capture(reservation)
    with pytest.raises(BillingExecutionDenied, match="operation=reserve"):
        await billing.reserve(SUBJECT, RESOURCE)

    await plugin.shutdown()
    await billing.release(reservation)
    assert store.capture_calls == 0
    assert store.release_calls == 1


@pytest.mark.asyncio
async def test_credits_plugin_without_registry_gate_cannot_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeCreditStore()
    monkeypatch.setitem(
        CreditsPlugin.initialize.__globals__,
        "CreditStore",
        lambda settings: store,
    )
    billing = BillingCoordinator()
    plugin = CreditsPlugin()
    await plugin.initialize(
        PluginContext(
            container=SimpleNamespace(
                billing=billing,
                plugin_registry=SimpleNamespace(loaded_plugins={}),
            ),
            settings=SimpleNamespace(),
        )
    )

    with pytest.raises(BillingExecutionDenied):
        await billing.reserve(SUBJECT, RESOURCE)
    assert store.reserve_calls == 0

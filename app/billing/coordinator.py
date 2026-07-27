from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import NoReturn

from app.billing.models import (
    BillingCapture,
    BillingQuote,
    BillingReservation,
    BillingResource,
    BillingSubject,
)
from app.billing.provider import (
    BillingProvider,
    BillingScopeExecutionGate,
    deny_billing_execution,
)


@dataclass(frozen=True, slots=True)
class _ProviderRegistration:
    provider: BillingProvider
    owner: str
    scope_execution_allowed: BillingScopeExecutionGate | None


class _BillingProviderFacade:
    """Public provider view that cannot bypass coordinator policy."""

    def __init__(self, coordinator: BillingCoordinator, name: str) -> None:
        self._coordinator = coordinator
        self.name = name

    async def quote(
        self,
        subject: BillingSubject,
        resource: BillingResource,
    ) -> BillingQuote:
        return await self._coordinator.quote(subject, resource, provider=self.name)

    async def reserve(
        self,
        subject: BillingSubject,
        resource: BillingResource,
    ) -> BillingReservation:
        return await self._coordinator.reserve(subject, resource, provider=self.name)

    async def capture(
        self,
        reservation: BillingReservation,
        *,
        amount: int | None = None,
    ) -> BillingCapture | None:
        if str(reservation.provider or "").strip() != self.name:
            raise ValueError(
                "billing reservation provider mismatch: "
                f"expected={self.name!r} actual={reservation.provider!r}"
            )
        return await self._coordinator.capture(reservation, amount=amount)

    async def release(self, reservation: BillingReservation) -> None:
        if str(reservation.provider or "").strip() != self.name:
            raise ValueError(
                "billing reservation provider mismatch: "
                f"expected={self.name!r} actual={reservation.provider!r}"
            )
        await self._coordinator.release(reservation)


class BillingCoordinator:
    def __init__(self) -> None:
        self._providers: dict[str, _ProviderRegistration] = {}
        self._providers_by_owner: dict[str, set[str]] = {}
        self._provider_facades: dict[str, BillingProvider] = {}

    def register_provider(
        self,
        provider: BillingProvider,
        *,
        owner: str = "",
        scope_execution_allowed: BillingScopeExecutionGate | None = None,
    ) -> None:
        name = str(provider.name or "").strip()
        if not name:
            raise ValueError("billing provider name cannot be empty")
        declared_owner = str(getattr(provider, "owner", "") or "").strip()
        requested_owner = str(owner or "").strip()
        if requested_owner and declared_owner and requested_owner != declared_owner:
            raise ValueError(
                "billing provider owner mismatch: "
                f"provider={name} expected={requested_owner!r} actual={declared_owner!r}"
            )
        # Owner-less registrations predate the plugin runtime.  Keep them as
        # explicit kernel-compatible providers so test/local integrations do
        # not silently become plugin-owned without an execution policy.
        provider_owner = requested_owner or declared_owner or "core"
        existing = self._providers.get(name)
        if existing is not None and existing.owner != provider_owner:
            raise ValueError(
                "billing provider already registered by another owner: "
                f"provider={name} existing_owner={existing.owner!r} "
                f"new_owner={provider_owner!r}"
            )
        if existing is not None:
            owner_names = self._providers_by_owner.get(existing.owner)
            if owner_names is not None:
                owner_names.discard(name)
                if not owner_names:
                    self._providers_by_owner.pop(existing.owner, None)
        self._providers[name] = _ProviderRegistration(
            provider=provider,
            owner=provider_owner,
            scope_execution_allowed=scope_execution_allowed,
        )
        self._providers_by_owner.setdefault(provider_owner, set()).add(name)
        self._provider_facades.setdefault(name, _BillingProviderFacade(self, name))

    def provider(self, name: str = "credits") -> BillingProvider | None:
        normalized_name = str(name or "").strip()
        if normalized_name not in self._providers:
            return None
        return self._provider_facades[normalized_name]

    def provider_owner(self, name: str = "credits") -> str:
        registration = self._providers.get(str(name or "").strip())
        return registration.owner if registration is not None else ""

    def providers_for_owner(self, owner: str) -> tuple[str, ...]:
        return tuple(sorted(self._providers_by_owner.get(str(owner or "").strip(), set())))

    def unregister_owner(self, owner: str) -> int:
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            return 0
        names = self._providers_by_owner.pop(normalized_owner, set())
        removed = 0
        for name in names:
            registration = self._providers.get(name)
            if registration is None or registration.owner != normalized_owner:
                continue
            self._providers.pop(name, None)
            self._provider_facades.pop(name, None)
            removed += 1
        return removed

    async def quote(
        self,
        subject: BillingSubject,
        resource: BillingResource,
        *,
        provider: str = "credits",
    ) -> BillingQuote:
        registration = self._require_provider(provider)
        await self._require_new_operation_allowed(registration, subject, operation="quote")
        return await registration.provider.quote(subject, resource)

    async def reserve(
        self,
        subject: BillingSubject,
        resource: BillingResource,
        *,
        provider: str = "credits",
    ) -> BillingReservation:
        registration = self._require_provider(provider)
        await self._require_new_operation_allowed(registration, subject, operation="reserve")
        return await registration.provider.reserve(subject, resource)

    async def capture(
        self,
        reservation: BillingReservation,
        *,
        amount: int | None = None,
    ) -> BillingCapture | None:
        registration = self._require_provider(reservation.provider)
        await self._require_new_operation_allowed(
            registration,
            reservation.subject,
            operation="capture",
        )
        return await registration.provider.capture(reservation, amount=amount)

    async def release(self, reservation: BillingReservation) -> None:
        # Release is a compensating operation.  It deliberately remains
        # available after an owner or scope is disabled so an already-debited
        # reservation cannot strand funds.
        registration = self._require_provider(reservation.provider)
        await registration.provider.release(reservation)

    def _require_provider(self, name: str) -> _ProviderRegistration:
        normalized_name = str(name or "").strip()
        registration = self._providers.get(normalized_name)
        if registration is None:
            raise ValueError(f"billing provider not registered: {name}")
        return registration

    async def _require_new_operation_allowed(
        self,
        registration: _ProviderRegistration,
        subject: BillingSubject,
        *,
        operation: str,
    ) -> None:
        if registration.owner == "core":
            return
        gate = registration.scope_execution_allowed
        if gate is None:
            self._raise_execution_denied(registration, operation)
        try:
            allowed = await gate(
                str(subject.tenant_id or "").strip(),
                str(subject.session_id or "").strip(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._raise_execution_denied(registration, operation)
        if allowed is not True:
            self._raise_execution_denied(registration, operation)

    @staticmethod
    def _raise_execution_denied(
        registration: _ProviderRegistration,
        operation: str,
    ) -> NoReturn:
        deny_billing_execution(
            provider=str(registration.provider.name or "").strip(),
            owner=registration.owner,
            operation=operation,
        )

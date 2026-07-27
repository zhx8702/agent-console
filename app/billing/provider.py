from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NoReturn, Protocol, TypeAlias

from app.billing.models import (
    BillingCapture,
    BillingQuote,
    BillingReservation,
    BillingResource,
    BillingSubject,
)

BillingScopeExecutionGate: TypeAlias = Callable[[str, str], Awaitable[bool]]


class BillingExecutionDenied(PermissionError):
    """Raised when an owned billing provider is not executable for a scope."""

    def __init__(self, *, provider: str, owner: str, operation: str) -> None:
        self.provider = str(provider or "").strip()
        self.owner = str(owner or "").strip()
        self.operation = str(operation or "").strip()
        super().__init__(
            "billing provider execution denied: "
            f"provider={self.provider or 'unknown'} "
            f"owner={self.owner or 'unknown'} "
            f"operation={self.operation or 'unknown'}"
        )


def deny_billing_execution(*, provider: str, owner: str, operation: str) -> NoReturn:
    raise BillingExecutionDenied(
        provider=provider,
        owner=owner,
        operation=operation,
    )


class BillingProvider(Protocol):
    name: str

    async def quote(self, subject: BillingSubject, resource: BillingResource) -> BillingQuote: ...

    async def reserve(self, subject: BillingSubject, resource: BillingResource) -> BillingReservation: ...

    async def capture(self, reservation: BillingReservation, *, amount: int | None = None) -> BillingCapture | None: ...

    async def release(self, reservation: BillingReservation) -> None: ...


__all__ = [
    "BillingExecutionDenied",
    "BillingProvider",
    "BillingScopeExecutionGate",
]

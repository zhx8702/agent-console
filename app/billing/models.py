from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BillingSubject:
    tenant_id: str
    session_id: str
    user_id: str
    display_name: str = ""


@dataclass(frozen=True)
class BillingResource:
    kind: str
    operation: str
    units: int = 1
    reference: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BillingQuote:
    provider: str
    subject: BillingSubject
    resource: BillingResource
    amount: int
    currency: str
    enabled: bool = True
    balance: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BillingReservation:
    provider: str
    subject: BillingSubject
    resource: BillingResource
    amount: int
    currency: str
    reservation_id: str = ""
    enabled: bool = True
    balance: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BillingCapture:
    provider: str
    subject: BillingSubject
    resource: BillingResource
    amount: int
    currency: str
    balance: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

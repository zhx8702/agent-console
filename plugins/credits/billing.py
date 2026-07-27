from __future__ import annotations

import asyncio
from typing import Any, NoReturn

from app.billing.models import (
    BillingCapture,
    BillingQuote,
    BillingReservation,
    BillingResource,
    BillingSubject,
)
from app.billing.provider import BillingScopeExecutionGate, deny_billing_execution
from plugins.credits.store import (
    CreditStore,
    command_cost_for_config,
    draw_quality_cost_for_config,
)


class CreditsBillingProvider:
    name = "credits"
    owner = "credits"

    def __init__(
        self,
        store: CreditStore,
        *,
        scope_execution_allowed: BillingScopeExecutionGate | None = None,
    ) -> None:
        self._store = store
        self._scope_execution_allowed = scope_execution_allowed

    async def quote(self, subject: BillingSubject, resource: BillingResource) -> BillingQuote:
        await self._require_scope(subject, operation="quote")
        cfg = await self._store.get_config(subject.tenant_id, subject.session_id)
        enabled = bool(cfg.get("enabled"))
        amount = self._amount_for_resource(cfg, resource) if enabled else 0
        # ``peek_balance`` may refresh the display name, so treat it as a
        # mutation boundary and revalidate after the config read.
        await self._require_scope(subject, operation="quote")
        balance = await self._store.peek_balance(
            subject.tenant_id,
            subject.session_id,
            subject.user_id,
            display_name=subject.display_name,
        )
        return BillingQuote(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=amount,
            currency=str(cfg.get("credit_name") or "积分"),
            enabled=enabled,
            balance=balance,
            metadata={"config": cfg},
        )

    async def reserve(
        self, subject: BillingSubject, resource: BillingResource
    ) -> BillingReservation:
        quote = await self.quote(subject, resource)
        await self._require_scope(subject, operation="reserve")
        if not quote.enabled or quote.amount <= 0:
            return BillingReservation(
                provider=self.name,
                subject=subject,
                resource=resource,
                amount=0,
                currency=quote.currency,
                enabled=quote.enabled,
                balance=quote.balance,
                metadata=quote.metadata,
            )
        reservation = await self._store.reserve_charge(
            subject.tenant_id,
            subject.session_id,
            subject.user_id,
            quote.amount,
            reason=self._reason_for_resource(resource),
            reference=resource.reference,
            display_name=subject.display_name,
            metadata={
                "resource_kind": resource.kind,
                "resource_operation": resource.operation,
                **dict(resource.metadata or {}),
            },
            idempotency_key=(
                f"billing:{resource.kind}:{resource.operation}:{resource.reference}"
                if resource.reference
                else ""
            ),
        )
        return BillingReservation(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=int(reservation.get("amount") or quote.amount),
            currency=quote.currency,
            reservation_id=str(reservation.get("reservation_id") or ""),
            enabled=True,
            balance=int(reservation.get("balance") or quote.balance or 0),
            metadata={"config": quote.metadata.get("config"), "reservation": reservation},
        )

    async def capture(
        self,
        reservation: BillingReservation,
        *,
        amount: int | None = None,
    ) -> BillingCapture | None:
        if reservation.amount <= 0 or not reservation.reservation_id:
            return None
        await self._require_scope(reservation.subject, operation="capture")
        captured = await self._store.capture_reservation(
            reservation.reservation_id,
            amount=amount,
            reference=reservation.resource.reference,
            display_name=reservation.subject.display_name,
        )
        if captured is None:
            return None
        captured_amount = captured.get("amount")
        return BillingCapture(
            provider=self.name,
            subject=reservation.subject,
            resource=reservation.resource,
            amount=int(
                captured_amount
                if captured_amount is not None
                else amount
                if amount is not None
                else reservation.amount
            ),
            currency=reservation.currency,
            balance=int(captured.get("balance") or 0),
            metadata={"reservation": captured},
        )

    async def release(self, reservation: BillingReservation) -> None:
        if reservation.amount <= 0 or not reservation.reservation_id:
            return
        await self._store.release_reservation(reservation.reservation_id)

    async def _require_scope(
        self,
        subject: BillingSubject,
        *,
        operation: str,
    ) -> None:
        gate = self._scope_execution_allowed
        if gate is None:
            self._raise_execution_denied(operation)
        try:
            allowed = await gate(
                str(subject.tenant_id or "").strip(),
                str(subject.session_id or "").strip(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._raise_execution_denied(operation)
        if allowed is not True:
            self._raise_execution_denied(operation)

    @classmethod
    def _raise_execution_denied(cls, operation: str) -> NoReturn:
        deny_billing_execution(
            provider=cls.name,
            owner=cls.owner,
            operation=operation,
        )

    @staticmethod
    def _amount_for_resource(cfg: dict[str, Any], resource: BillingResource) -> int:
        if resource.kind == "command":
            command = str(resource.metadata.get("command") or resource.operation or "")
            if command in {"/draw", "/redraw"}:
                quality = str(resource.metadata.get("quality") or "low")
                return draw_quality_cost_for_config(cfg, quality)
            return command_cost_for_config(cfg, command)
        if resource.kind == "chat":
            return max(0, int(cfg.get("cost_per_chat") or 0))
        if resource.kind == "agent_tool" and resource.operation.startswith("amap_"):
            if resource.operation == "amap_search":
                return max(0, int(cfg.get("amap_search_credit_cost") or 0))
            if resource.operation == "amap_map":
                return max(0, int(cfg.get("amap_map_credit_cost") or 0))
            if resource.operation == "amap_route_map":
                return max(0, int(cfg.get("amap_route_map_credit_cost") or 0))
        return max(0, int(resource.metadata.get("amount") or 0))

    @staticmethod
    def _reason_for_resource(resource: BillingResource) -> str:
        if resource.kind == "command":
            return "command_cost"
        if resource.kind == "chat":
            return "chat_cost"
        if resource.kind == "agent_tool":
            if resource.operation == "amap_search":
                return "amap_search_cost"
            if resource.operation == "amap_map":
                return "amap_map_cost"
            if resource.operation == "amap_route_map":
                return "amap_route_map_cost"
            return f"agent_tool_{resource.operation}_cost"[:64]
        return f"{resource.kind}_cost"[:64]

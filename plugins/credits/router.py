"""
REST API for the credits plugin.

Mounted at ``/plugins/credits/`` by the plugin framework.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from app.admin.audit import set_admin_audit_context
from app.common.request_models import StrictRequestModel
from plugins.credits.store import (
    CreditConfigVersionConflict,
    CreditIdempotencyConflict,
    CreditStore,
)


class AdjustRequest(StrictRequestModel):
    tenant_id: str
    session_id: str
    user_id: str
    mode: Literal["delta", "set"] = "delta"
    delta: int | None = None
    amount: int | None = None
    reason: str = "admin_adjust"
    display_name: str | None = None


class TransferRequest(StrictRequestModel):
    tenant_id: str
    session_id: str
    from_user_id: str
    to_user_id: str
    amount: int
    reason: str = "credit_transfer"


class ConfigUpdate(StrictRequestModel):
    enabled: bool | None = None
    credit_name: str | None = None
    cost_per_chat: int | None = None
    command_costs_text: str | None = None
    draw_quality_costs_text: str | None = None
    amap_search_credit_cost: int | None = None
    amap_map_credit_cost: int | None = None
    amap_route_map_credit_cost: int | None = None
    initial_credits: int | None = None
    daily_checkin: int | None = None
    streak_bonus: int | None = None
    streak_cap: int | None = None
    checkin_mode: int | None = None
    admin_user_ids_text: str | None = None
    user_commands_text: str | None = None
    admin_commands_text: str | None = None


def _required_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="idempotency_key_required",
        )
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_idempotency_key",
        )
    return normalized


def _required_if_match(value: str | None) -> str:
    normalized = str(value or "").strip()
    if value is None or not normalized:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="if_match_required",
        )
    if not normalized.startswith('"credits-config-') or not normalized.endswith('"'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_if_match",
        )
    return normalized


def _set_config_headers(response: Response, etag: str) -> None:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"


def _mutation_error(exc: ValueError) -> HTTPException:
    if isinstance(exc, CreditConfigVersionConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "version_conflict",
                "expected_etag": exc.expected,
                "current_etag": exc.current,
            },
            headers={"ETag": exc.current, "Cache-Control": "no-store"},
        )
    if isinstance(exc, CreditIdempotencyConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "idempotency_conflict"},
        )
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _config_audit_state(config: dict[str, object]) -> dict[str, object]:
    """Return useful configuration values without member IDs or command text."""

    safe_fields = (
        "enabled",
        "cost_per_chat",
        "amap_search_credit_cost",
        "amap_map_credit_cost",
        "amap_route_map_credit_cost",
        "initial_credits",
        "daily_checkin",
        "streak_bonus",
        "streak_cap",
        "checkin_mode",
    )
    return {key: config.get(key) for key in safe_fields if key in config}


def build_credits_router(store: CreditStore) -> APIRouter:
    router = APIRouter()

    @router.get("/balance/{tenant_id}/{session_id}/{user_id}")
    async def get_balance(tenant_id: str, session_id: str, user_id: str):
        try:
            balance = await store.get_balance(tenant_id, session_id, user_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_id": user_id,
            "credits": balance,
        }

    @router.get("/member/{tenant_id}/{session_id}/{user_id}")
    async def get_member_detail(
        tenant_id: str,
        session_id: str,
        user_id: str,
        ledger_limit: int = Query(default=20, ge=1, le=100),
    ):
        try:
            return await store.get_member_detail(
                tenant_id,
                session_id,
                user_id,
                ledger_limit=ledger_limit,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/members/{tenant_id}/{session_id}")
    async def get_members(
        tenant_id: str,
        session_id: str,
        limit: int = Query(default=200, ge=1, le=500),
        query: str = Query(default=""),
    ):
        return await store.list_members(tenant_id, session_id, limit=limit, query=query)

    @router.get("/checkin-status/{tenant_id}/{session_id}/{user_id}")
    async def get_checkin_status(tenant_id: str, session_id: str, user_id: str):
        try:
            return await store.get_checkin_status(tenant_id, session_id, user_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/top/{tenant_id}/{session_id}")
    async def get_top(
        tenant_id: str,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=100),
    ):
        rows = await store.get_top(tenant_id, session_id, limit)
        return {"items": rows}

    @router.get("/ledger/{tenant_id}/{session_id}")
    async def get_ledger(
        tenant_id: str,
        session_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        user_id: str = Query(default=""),
    ):
        return await store.get_ledger(tenant_id, session_id, limit=limit, user_id=user_id)

    @router.post("/checkin/{tenant_id}/{session_id}/{user_id}")
    async def checkin(tenant_id: str, session_id: str, user_id: str, request: Request):
        before = await store.get_checkin_status(tenant_id, session_id, user_id)
        try:
            result = await store.checkin(tenant_id, session_id, user_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        set_admin_audit_context(
            request,
            target_type="credits_checkin",
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            before_state={"checked_in_today": bool(before.get("checked_in_today"))},
            after_state={
                "checked_in": bool(result.get("checked_in")),
                "credits": int(result.get("balance") or 0),
            },
            reason="credits_checkin",
        )
        return result

    @router.post("/adjust")
    async def adjust(
        req: AdjustRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ):
        operation_key = _required_idempotency_key(idempotency_key)
        before_balance = await store.peek_balance(
            req.tenant_id,
            req.session_id,
            req.user_id,
            display_name=req.display_name or "",
        )
        try:
            if req.mode == "set":
                if req.amount is None:
                    raise ValueError("amount is required when mode=set")
                new_balance = await store.set_balance(
                    req.tenant_id,
                    req.session_id,
                    req.user_id,
                    req.amount,
                    req.reason,
                    actor="admin",
                    display_name=req.display_name or "",
                    idempotency_key=operation_key,
                )
            else:
                if req.delta is None:
                    raise ValueError("delta is required when mode=delta")
                new_balance = await store.adjust(
                    req.tenant_id,
                    req.session_id,
                    req.user_id,
                    req.delta,
                    req.reason,
                    actor="admin",
                    display_name=req.display_name or "",
                    idempotency_key=operation_key,
                )
        except ValueError as exc:
            raise _mutation_error(exc) from exc
        result = {
            "user_id": req.user_id,
            "credits": new_balance,
            "mode": req.mode,
        }
        set_admin_audit_context(
            request,
            target_type="credits_balance",
            tenant_id=req.tenant_id,
            session_id=req.session_id,
            user_id=req.user_id,
            before_state={"credits": before_balance},
            after_state={"credits": new_balance, "mode": req.mode},
            reason="credits_admin_adjust",
        )
        return result

    @router.post("/transfer")
    async def transfer(
        req: TransferRequest,
        request: Request,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ):
        operation_key = _required_idempotency_key(idempotency_key)
        before_from = await store.peek_balance(
            req.tenant_id,
            req.session_id,
            req.from_user_id,
        )
        before_to = await store.peek_balance(
            req.tenant_id,
            req.session_id,
            req.to_user_id,
        )
        try:
            balances = await store.transfer(
                req.tenant_id,
                req.session_id,
                req.from_user_id,
                req.to_user_id,
                req.amount,
                actor="admin",
                reference=req.reason,
                idempotency_key=operation_key,
            )
        except ValueError as exc:
            raise _mutation_error(exc) from exc
        result = {
            "from_user_id": req.from_user_id,
            "to_user_id": req.to_user_id,
            "amount": req.amount,
            **balances,
        }
        set_admin_audit_context(
            request,
            target_type="credits_transfer",
            tenant_id=req.tenant_id,
            session_id=req.session_id,
            before_state={
                "from_credits": before_from,
                "to_credits": before_to,
            },
            after_state={
                "from_credits": int(balances.get("from_balance") or 0),
                "to_credits": int(balances.get("to_balance") or 0),
                "amount": req.amount,
            },
            reason="credits_admin_transfer",
        )
        return result

    @router.get("/config/{tenant_id}/{session_id}")
    async def get_config(tenant_id: str, session_id: str, response: Response):
        config, etag = await store.get_config_versioned(tenant_id, session_id)
        _set_config_headers(response, etag)
        return config

    @router.post("/config/{tenant_id}/{session_id}")
    async def set_config(
        tenant_id: str,
        session_id: str,
        body: ConfigUpdate,
        request: Request,
        response: Response,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ):
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        before_config, _before_etag = await store.get_config_versioned(tenant_id, session_id)
        try:
            config, etag = await store.set_config_versioned(
                tenant_id,
                session_id,
                expected_etag=_required_if_match(if_match),
                **updates,
            )
        except ValueError as exc:
            raise _mutation_error(exc) from exc
        _set_config_headers(response, etag)
        set_admin_audit_context(
            request,
            target_type="credits_config",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state=_config_audit_state(before_config),
            after_state={
                **_config_audit_state(config),
                "changed_fields": sorted(updates),
            },
            reason="credits_config_updated",
        )
        return config

    return router
